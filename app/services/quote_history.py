"""Quote version history and estimate snapshots (recommendations 7.2 / 7.4).

Every time a quote is sent (auto or manual), a QuoteVersion row is written
capturing the estimate it was based on, the final price, the deviation from
the recommendation, who approved it, and why any override happened.
"""
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import QuoteVersion


def _f(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


async def record_quote_version(
    db: AsyncSession,
    proposal_id: int,
    final_price_net: float,
    email_id: int | None = None,
    estimate=None,
    change_type: str = "initial",
    approved_by: str = "",
    justification: str = "",
    discount_pct: float = 0.0,
    policy_result: dict | None = None,
) -> QuoteVersion:
    next_res = await db.execute(
        select(func.coalesce(func.max(QuoteVersion.version_number), 0))
        .where(QuoteVersion.proposal_id == proposal_id)
    )
    version_number = int(next_res.scalar() or 0) + 1

    est = estimate if isinstance(estimate, dict) else None
    predicted_cost = _f(est.get("predicted_cost")) if est else 0.0
    recommended_price_net = _f((est.get("price") or {}).get("price_net")) if est else 0.0
    margin_pct = _f((est.get("margin") or {}).get("recommended_margin_pct")) if est else 0.0
    risk_score = _f((est.get("risk") or {}).get("risk_score")) if est else 0.0
    risk_level = (est.get("risk") or {}).get("risk_level", "") if est else ""
    confidence = _f(est.get("confidence")) if est else 0.0
    currency = est.get("currency", "INR") if est else "INR"
    estimate_id = est.get("estimate_id") if est else None

    if not est:
        from app.models import ProposalCostEstimate
        res = await db.execute(
            select(ProposalCostEstimate)
            .where(ProposalCostEstimate.proposal_id == proposal_id)
            .order_by(ProposalCostEstimate.id.desc())
            .limit(1)
        )
        row = res.scalars().first()
        if row:
            estimate_id = row.id
            predicted_cost = _f(row.predicted_cost)
            recommended_price_net = _f(row.recommended_price_net)
            margin_pct = _f(row.recommended_margin_pct)
            risk_score = _f(row.risk_score)
            risk_level = row.risk_level or ""
            confidence = _f(row.confidence)
            currency = row.currency or "INR"

    deviation_pct = 0.0
    if recommended_price_net > 0:
        deviation_pct = round(
            (final_price_net - recommended_price_net) / recommended_price_net * 100, 2
        )

    record = QuoteVersion(
        proposal_id=proposal_id,
        email_id=email_id,
        version_number=version_number,
        estimate_id=estimate_id,
        change_type=change_type,
        predicted_cost=predicted_cost,
        recommended_price_net=recommended_price_net,
        final_price_net=round(_f(final_price_net), 2),
        deviation_pct=deviation_pct,
        margin_pct=margin_pct,
        discount_pct=_f(discount_pct),
        risk_score=risk_score,
        risk_level=risk_level,
        confidence=confidence,
        currency=currency,
        policy_result=policy_result or {},
        approved_by=approved_by,
        justification=justification or (
            f"Auto-sent by policy gate (confidence {confidence:.0%}, "
            f"margin {margin_pct:.1f}%, risk {risk_level or 'n/a'})"
            if change_type == "initial" else ""
        ),
    )
    db.add(record)
    await db.flush()
    return record


def serialize_quote_version(v: QuoteVersion) -> dict:
    return {
        "id": v.id,
        "proposal_id": v.proposal_id,
        "email_id": v.email_id,
        "version_number": v.version_number,
        "estimate_id": v.estimate_id,
        "change_type": v.change_type,
        "predicted_cost": float(v.predicted_cost),
        "recommended_price_net": float(v.recommended_price_net),
        "final_price_net": float(v.final_price_net),
        "deviation_pct": float(v.deviation_pct),
        "margin_pct": float(v.margin_pct),
        "discount_pct": float(v.discount_pct),
        "risk_score": float(v.risk_score),
        "risk_level": v.risk_level,
        "confidence": float(v.confidence),
        "currency": v.currency,
        "policy_result": v.policy_result,
        "approved_by": v.approved_by,
        "justification": v.justification,
        "created_at": v.created_at.isoformat() if v.created_at else None,
    }
