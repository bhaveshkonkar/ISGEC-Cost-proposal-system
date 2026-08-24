from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Form
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import EmailMessage, Proposal, ProposalItem, ProposalCostEstimate, get_session
from app.services.email_pipeline import run_poll_cycle, send_quote_for_email
from app.services.email_client import is_configured
from app.services.policy import evaluate_quote_policy
from app.services.costing import run_cost_estimate

router = APIRouter(prefix="/api/emails", tags=["email-automation"])


async def _latest_estimate(db: AsyncSession, proposal_id: int) -> dict | None:
    """Return a fresh estimate dict for the proposal, running one if needed."""
    est_result = await db.execute(
        select(ProposalCostEstimate)
        .where(ProposalCostEstimate.proposal_id == proposal_id)
        .order_by(ProposalCostEstimate.id.desc())
        .limit(1)
    )
    row = est_result.scalar_one_or_none()
    try:
        result = await run_cost_estimate(db, proposal_id=proposal_id, save=True)
        if "error" not in result:
            return result
    except Exception:
        pass
    if row:
        return {
            "estimate_id": row.id,
            "predicted_cost": float(row.predicted_cost),
            "recommended_price_net": float(row.recommended_price_net),
            "margin": {"recommended_margin_pct": float(row.recommended_margin_pct)},
            "risk": {"risk_level": row.risk_level, "risk_score": float(row.risk_score)},
            "confidence": float(row.confidence),
        }
    return None


async def _policy_check_or_raise(
    db: AsyncSession,
    proposal: Proposal,
    proposed_price_net: float,
    justification: str,
    force: bool,
) -> tuple[dict | None, dict]:
    """Run the pricing policy gate. Raises 409 unless allowed or force+justification given.

    Returns (estimate_dict, policy_result)."""
    estimate = await _latest_estimate(db, proposal.id)
    predicted_cost = float(estimate.get("predicted_cost") or 0) if estimate else 0.0
    discount_pct = 0.0
    if estimate and float(estimate.get("recommended_price_net") or 0) > 0 and proposed_price_net > 0:
        rec = float(estimate["recommended_price_net"])
        discount_pct = max(0.0, round((rec - proposed_price_net) / rec * 100, 2))
    policy = evaluate_quote_policy(
        estimate,
        proposed_price_net=proposed_price_net,
        predicted_cost=predicted_cost,
        discount_pct=discount_pct,
    )
    if not policy["allowed"]:
        reasons = "; ".join(policy.get("violations", []) or ["review required"])
        if force and justification.strip():
            return estimate, policy
        detail = f"Pricing policy blocks this quote: {reasons}."
        if not policy.get("violations"):
            detail = f"High-risk project requires manual review: {reasons}"
        raise HTTPException(
            409,
            detail + " Re-send with an explicit justification and force=true to override.",
        )
    if policy["requires_review"] and not (force and justification.strip()):
        # High-risk quotes can still be approved deliberately, but never silently.
        raise HTTPException(
            409,
            "High-risk project - approval requires a written justification "
            "(form field 'justification') to proceed.",
        )
    return estimate, policy


def _serialize_email(e: EmailMessage, proposal: Proposal | None = None) -> dict:
    data = {
        "id": e.id,
        "from_addr": e.from_addr,
        "subject": e.subject,
        "attachment_names": [a.get("filename", "") for a in (e.attachment_names or []) if isinstance(a, dict)],
        "status": e.status,
        "error_message": e.error_message,
        "proposal_id": e.proposal_id,
        "sent_by": e.sent_by,
        "received_at": e.received_at.isoformat() if e.received_at else None,
        "replied_at": e.replied_at.isoformat() if e.replied_at else None,
    }
    if proposal:
        data["proposal"] = {
            "id": proposal.id,
            "proposal_number": proposal.proposal_number,
            "total_net": float(proposal.total_net),
            "total_gross": float(proposal.total_gross),
            "currency": proposal.currency,
            "status": proposal.status,
        }
    return data


async def _load_items(db: AsyncSession, email_record: EmailMessage) -> list[dict]:
    if not email_record.proposal_id:
        return []
    proposal = await db.get(Proposal, email_record.proposal_id)
    if not proposal:
        return []
    result = await db.execute(
        select(ProposalItem)
        .where(ProposalItem.proposal_id == proposal.id)
        .order_by(ProposalItem.id)
    )
    return [
        {
            "sku": it.sku,
            "description": it.description,
            "quantity": it.quantity,
            "unit_price_net": float(it.unit_price_net),
            "subtotal_net": float(it.subtotal_net),
            "subtotal_gross": float(it.subtotal_gross),
            "currency": proposal.currency,
            "notes": it.notes,
            "item_status": it.item_status,
        }
        for it in result.scalars().all()
    ]


@router.get("")
async def list_emails(
    tab: str = "pending",
    limit: int = 100,
    db: AsyncSession = Depends(get_session),
):
    status_map = {
        "pending": ["quoted", "needs_review", "processing", "new"],
        "sent": ["replied"],
        "issues": ["failed", "rejected"],
    }
    statuses = status_map.get(tab)
    if not statuses:
        raise HTTPException(400, "Invalid tab. Use: pending, sent, issues")

    query = select(EmailMessage).where(EmailMessage.status.in_(statuses)).order_by(EmailMessage.id.desc()).limit(limit)
    result = await db.execute(query)
    emails = result.scalars().all()

    counts_result = await db.execute(select(EmailMessage.status, func.count(EmailMessage.id)).group_by(EmailMessage.status))
    counts = {row[0]: row[1] for row in counts_result.all()}

    items = []
    for e in emails:
        proposal = await db.get(Proposal, e.proposal_id) if e.proposal_id else None
        items.append({**_serialize_email(e, proposal), "items": await _load_items(db, e)})
    return {"emails": items, "tab": tab, "counts": counts, "configured": is_configured()}


@router.post("/check-now")
async def check_now():
    try:
        result = await run_poll_cycle()
    except Exception as exc:
        raise HTTPException(500, f"Poll cycle crashed: {exc}")
    if result.get("error"):
        raise HTTPException(502, result["error"])
    return result


@router.post("/{email_id}/approve")
async def approve_email(
    email_id: int,
    justification: str = Form(""),
    force: bool = Form(False),
    db: AsyncSession = Depends(get_session),
):
    record = await db.get(EmailMessage, email_id)
    if not record:
        raise HTTPException(404, "Email not found")
    if record.status != "quoted":
        raise HTTPException(400, f"Only quotes pending approval can be sent (current: {record.status})")
    if not record.from_addr:
        raise HTTPException(400, "Email has no sender address")
    if not record.proposal_id:
        raise HTTPException(400, "No linked proposal")

    proposal = await db.get(Proposal, record.proposal_id)
    if not proposal:
        raise HTTPException(404, "Proposal not found")

    estimate, policy = await _policy_check_or_raise(
        db, proposal, float(proposal.total_net or 0), justification, force
    )

    try:
        await send_quote_for_email(
            record, db, sent_by="admin",
            cost_breakdown=(estimate or {}).get("breakdown_pct"),
            estimate=estimate,
            justification=justification,
            change_type="initial",
            policy_result=policy,
        )
    except Exception as exc:
        record.status = "failed"
        record.error_message = f"Send failed: {exc}"[:2000]
        await db.commit()
        raise HTTPException(502, f"Failed to send quote: {str(exc)}")

    return {"id": record.id, "status": record.status, "sent_by": record.sent_by,
            "policy": policy}


@router.post("/{email_id}/reject")
async def reject_email(
    email_id: int,
    reason: str = Form(""),
    db: AsyncSession = Depends(get_session),
):
    record = await db.get(EmailMessage, email_id)
    if not record:
        raise HTTPException(404, "Email not found")
    if record.status in ("replied",):
        raise HTTPException(400, "Already sent — cannot reject")
    record.status = "rejected"
    stamp = datetime.now(timezone.utc).isoformat()
    record.error_message = f"Rejected by admin at {stamp}" + (f" — {reason}" if reason else "")
    await db.commit()
    return {"id": record.id, "status": record.status}


@router.post("/{email_id}/approve-with-price")
async def approve_with_price(
    email_id: int,
    total_price: float = Form(...),
    justification: str = Form(""),
    force: bool = Form(False),
    db: AsyncSession = Depends(get_session),
):
    """Approve an emailed quote, rescale line items to the chosen price, and send with cost breakdown."""
    record = await db.get(EmailMessage, email_id)
    if not record:
        raise HTTPException(404, "Email not found")
    if record.status != "quoted":
        raise HTTPException(400, f"Only pending quotes can be approved (current: {record.status})")
    if not record.from_addr:
        raise HTTPException(400, "Email has no sender address")
    if not record.proposal_id:
        raise HTTPException(400, "No linked proposal")

    proposal = await db.get(Proposal, record.proposal_id)
    if not proposal:
        raise HTTPException(404, "Proposal not found")

    # Pricing policy gate (recommendation 7.1) - price overrides need a
    # justified, forced approval whenever policy is violated.
    estimate, policy = await _policy_check_or_raise(
        db, proposal, float(total_price), justification, force
    )
    rec_price = float(estimate.get("recommended_price_net") or 0) if estimate else 0.0
    discount_pct = max(0.0, round((rec_price - float(total_price)) / rec_price * 100, 2)) if rec_price > 0 else 0.0

    items_result = await db.execute(
        select(ProposalItem).where(ProposalItem.proposal_id == proposal.id).order_by(ProposalItem.id)
    )
    items = items_result.scalars().all()

    quoted_items = [it for it in items if it.item_status == "quoted"]
    current_quoted_total = sum(float(it.subtotal_net) for it in quoted_items)

    if current_quoted_total > 0 and total_price > 0:
        scale = total_price / current_quoted_total
        for it in quoted_items:
            old_unit_net = float(it.unit_price_net)
            old_unit_gross = float(it.unit_price_gross)
            vat_ratio = old_unit_gross / old_unit_net if old_unit_net > 0 else 1.18
            new_unit_net = round(old_unit_net * scale, 2)
            new_unit_gross = round(new_unit_net * vat_ratio, 2)
            new_subtotal_net = round(new_unit_net * it.quantity, 2)
            new_subtotal_gross = round(new_unit_gross * it.quantity, 2)
            it.unit_price_net = new_unit_net
            it.unit_price_gross = new_unit_gross
            it.subtotal_net = new_subtotal_net
            it.subtotal_gross = new_subtotal_gross

    new_total_net = sum(float(it.subtotal_net) for it in items)
    new_total_gross = sum(float(it.subtotal_gross) for it in items)
    proposal.total_net = round(new_total_net, 2)
    proposal.total_gross = round(new_total_gross, 2)
    await db.flush()

    breakdown_pct = {}
    if estimate and estimate.get("breakdown"):
        tc = sum(float(v) for v in estimate["breakdown"].values())
        if tc > 0:
            breakdown_pct = {k: round(float(v) / tc * 100, 1) for k, v in estimate["breakdown"].items()}
    if not breakdown_pct:
        breakdown_pct = {"material": 55.0, "labour": 18.0, "engineering": 10.0, "overhead": 12.0, "freight": 5.0}

    try:
        await send_quote_for_email(
            record, db, sent_by="admin", cost_breakdown=breakdown_pct,
            estimate=estimate,
            justification=justification or f"Price override to {total_price} (policy: {'passed' if policy['allowed'] else 'forced'})",
            discount_pct=discount_pct,
            change_type="price_override",
            policy_result=policy,
        )
    except Exception as exc:
        record.status = "failed"
        record.error_message = f"Send failed: {exc}"[:2000]
        await db.commit()
        raise HTTPException(502, f"Failed to send quote: {str(exc)}")

    return {"id": record.id, "status": record.status, "sent_by": "admin",
            "price_applied": total_price,
            "recommended_price_net": rec_price,
            "discount_pct_vs_recommendation": discount_pct,
            "policy": policy}


@router.post("/{email_id}/retry")
async def retry_email(email_id: int, db: AsyncSession = Depends(get_session)):
    from app.services.email_pipeline import process_email

    record = await db.get(EmailMessage, email_id)
    if not record:
        raise HTTPException(404, "Email not found")
    if record.status in ("replied", "rejected"):
        raise HTTPException(400, f"Email already {record.status} — cannot retry")

    stored = {
        "message_id": record.message_id,
        "uid": record.uid,
        "from_addr": record.from_addr,
        "subject": record.subject,
        "body_text": record.body_text,
        "attachments": [
            {"filename": a.get("filename", ""), "path": a.get("path", "")}
            for a in (record.attachment_names or []) if isinstance(a, dict)
        ],
        "received_at": record.received_at,
    }

    await db.delete(record)
    await db.commit()

    reprocessed = await process_email(stored, db)
    if reprocessed is None:
        raise HTTPException(409, "Email could not be reprocessed (duplicate)")

    return {
        "id": reprocessed.id,
        "status": reprocessed.status,
        "error_message": reprocessed.error_message,
    }


