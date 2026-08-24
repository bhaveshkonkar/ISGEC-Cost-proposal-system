import os
import re
import asyncio
import difflib
import traceback
from datetime import datetime, timedelta, timezone

from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy import select, func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import EMAIL_AUTO_SEND, SENDER_NAME, PUBLIC_BASE_URL, UPLOAD_DIR  # noqa: F401 (UPLOAD_DIR reserved for future use)
from app.models import EmailMessage, Product, Proposal, ProposalItem, async_session
from app.services.email_client import fetch_unread, send_email, is_configured
from app.services.extractor import extract_requirements, classify_unmatched_items
from app.services.embedding import get_embedding
from app.services.search import ensure_collection, search_products
from app.services.document import parse_document_async
from app.services.payments import create_payment_for_quote
from app.services.policy import evaluate_quote_policy, auto_send_allowed
from app.services.quote_history import record_quote_version
from app.services.customer_link import link_proposal_to_customer, extract_email_address
from app.services.feedback import upsert_project_from_quote, close_project_outcome

MATCH_THRESHOLD = 0.65
MAX_ATTACHMENT_CHARS = 15000
PROPOSAL_NUMBER_RE = re.compile(r"ISGEC-\d{8}-[A-Z0-9]{6}", re.I)

TEMPLATES_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "templates")
_env = Environment(
    loader=FileSystemLoader(TEMPLATES_DIR),
    autoescape=select_autoescape(["html"]),
)


def _money(value) -> float:
    return round(float(value or 0), 2)


def _naive_utc(dt: datetime | None) -> datetime | None:
    """asyncpg requires naive datetimes for TIMESTAMP columns - strip tz after converting to UTC."""
    if dt is None:
        return None
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


async def _gather_email_text(body_text: str, attachments: list[dict]) -> str:
    parts = [body_text]
    for att in attachments:
        path = att.get("path", "")
        if not path or not os.path.exists(path):
            continue
        try:
            parsed = await parse_document_async(path)
            parsed = parsed.strip()
            if parsed.startswith("["):
                continue
            parts.append(f"\n--- Attachment: {att.get('filename', 'file')} ---\n{parsed[:MAX_ATTACHMENT_CHARS]}")
        except Exception:
            continue
    return "\n".join(p for p in parts if p.strip())


async def _match_product(description: str, db: AsyncSession) -> Product | None:
    result = await db.execute(select(Product).where(Product.sku.ilike(description.strip())))
    exact = result.scalar_one_or_none()
    if exact:
        return exact

    embedding = await get_embedding(description)
    results = search_products(embedding, limit=1)
    if not results:
        return None
    best = results[0]
    if best["score"] < MATCH_THRESHOLD:
        return None
    db_id = best["payload"].get("db_id")
    if not db_id:
        return None
    return await db.get(Product, db_id)


def _normalize_desc(desc: str) -> str:
    cleaned = re.sub(r"[^a-z0-9 ]", " ", desc.lower())
    return re.sub(r"\s+", " ", cleaned).strip()


def _is_duplicate(a: str, b: str) -> bool:
    na, nb = _normalize_desc(a), _normalize_desc(b)
    if na == nb:
        return True
    if na.endswith("s") and na[:-1] == nb:
        return True
    if nb.endswith("s") and nb[:-1] == na:
        return True
    return difflib.SequenceMatcher(None, na, nb).ratio() >= 0.85


def _build_line_items(requirements: dict) -> list[dict]:
    """Raw requirement entries awaiting catalog matching. Near-duplicate mentions are merged."""
    entries = []
    for item in requirements.get("items", []):
        desc = str(item.get("description", "")).strip()
        if not desc:
            continue
        try:
            qty = max(1, int(item.get("quantity", 1)))
        except (TypeError, ValueError):
            qty = 1
        duplicate = next((e for e in entries if _is_duplicate(e["description"], desc)), None)
        if duplicate:
            duplicate["quantity"] = max(duplicate["quantity"], qty)
            continue
        entry = {
            "sku": "",
            "description": desc,
            "quantity": qty,
            "unit": str(item.get("unit", "pcs") or "pcs"),
            "unit_price_net": 0.0,
            "unit_price_gross": 0.0,
            "subtotal_net": 0.0,
            "subtotal_gross": 0.0,
            "vat_rate": 18.0,
            "notes": "",
            "item_status": "quoted",
            "product_id": None,
        }
        entries.append(entry)
    return entries


async def _resolve_matches(unmatched: list[dict], db: AsyncSession):
    matched = []
    remaining = []
    for entry in unmatched:
        product = await _match_product(entry["description"], db)
        if product:
            vat = float(product.vat_rate or 18)
            net = float(product.price_net or 0)
            gross = round(net * (1 + vat / 100), 2)
            entry.update({
                "sku": product.sku,
                "description": f"{product.name}. {product.description}"[:500],
                "unit_price_net": _money(net),
                "unit_price_gross": _money(gross),
                "subtotal_net": _money(net * entry["quantity"]),
                "subtotal_gross": _money(gross * entry["quantity"]),
                "vat_rate": vat,
                "item_status": "quoted",
                "product_id": product.id,
                "currency": product.currency,
            })
            matched.append(entry)
        else:
            remaining.append(entry)
    return matched, remaining


def _build_quote_context(proposal: Proposal, items: list[ProposalItem], cost_breakdown: dict | None = None) -> dict:
    quoted = []
    on_request = []
    not_offered = []
    for it in items:
        row = {
            "sku": it.sku,
            "description": it.description,
            "quantity": it.quantity,
            "unit_price_net": float(it.unit_price_net),
            "unit_price_gross": float(it.unit_price_gross),
            "subtotal_net": float(it.subtotal_net),
            "subtotal_gross": float(it.subtotal_gross),
            "currency": proposal.currency,
            "notes": it.notes,
        }
        if it.item_status == "quoted":
            quoted.append(row)
        elif it.item_status == "on_request":
            on_request.append(row)
        else:
            not_offered.append(row)

    return {
        "proposal_number": proposal.proposal_number,
        "valid_until": proposal.valid_until.isoformat() if proposal.valid_until else "",
        "currency": proposal.currency,
        "quoted_items": quoted,
        "on_request_items": on_request,
        "not_offered_items": not_offered,
        "total_net": float(proposal.total_net),
        "total_gross": float(proposal.total_gross),
        "sender_name": SENDER_NAME,
        "cost_breakdown": cost_breakdown or {},
    }


def _render_quote_html(context: dict) -> str:
    template = _env.get_template("quote_email.html")
    return template.render(**context)


def _normalize_subject(subject: str) -> str:
    """Strip reply/forward prefixes and collapse whitespace for fuzzy matching."""
    s = str(subject or "")
    s = re.sub(r"^(?:\s*(?:re|fwd?|fw|aw)\s*(?:\[\d+\])?\s*:)+", "", s, flags=re.I)
    return re.sub(r"\s+", " ", s).strip().lower()


def _subject_tokens(s: str) -> set:
    return set(re.findall(r"[a-z0-9]{3,}", s.lower()))


async def _match_quoted_thread(email_data: dict, db: AsyncSession) -> tuple[EmailMessage | None, str]:
    """Find the sent-quote email record this customer reply belongs to.

    Match strategies, in order of trust (recommendation 7.6):
      1. header       - In-Reply-To / References vs stored quote_message_id
      2. proposal_no  - proposal number found in the subject
      3. sender_subj  - fallback: same sender + fuzzy normalized-subject match

    Returns (matched_record_or_None, method_used).
    """
    header_ids = []
    for raw in (email_data.get("in_reply_to"), email_data.get("references")):
        if not raw:
            continue
        raw = str(raw)
        header_ids.extend(m.strip() for m in re.findall(r"<[^>]+>", raw))
        stripped = raw.strip()
        if stripped and "<" not in stripped:
            header_ids.append(stripped)

    if header_ids:
        result = await db.execute(
            select(EmailMessage).where(EmailMessage.quote_message_id.in_(header_ids))
        )
        found = result.scalars().first()
        if found:
            return found, "header"

    subject_match = PROPOSAL_NUMBER_RE.search(str(email_data.get("subject") or ""))
    if subject_match:
        prop_result = await db.execute(
            select(Proposal).where(func.upper(Proposal.proposal_number) == subject_match.group(0).upper())
        )
        proposal = prop_result.scalars().first()
        if proposal:
            rec_result = await db.execute(
                select(EmailMessage)
                .where(EmailMessage.proposal_id == proposal.id, EmailMessage.status == "replied")
                .order_by(EmailMessage.id.desc())
            )
            found = rec_result.scalars().first()
            if found:
                return found, "proposal_no"

    # Fallback 7.6: same-sender + normalized subject similarity against all sent quotes.
    from_addr = extract_email_address(str(email_data.get("from_addr") or ""))
    norm_subject = _normalize_subject(email_data.get("subject") or "")
    subj_tokens = _subject_tokens(norm_subject)
    if from_addr and subj_tokens:
        result = await db.execute(
            select(EmailMessage).where(EmailMessage.status == "replied").order_by(EmailMessage.id.desc()).limit(200)
        )
        candidates = []
        for rec in result.scalars().all():
            rec_addr = extract_email_address(rec.from_addr or "")
            same_person = rec_addr == from_addr
            same_domain = (
                not same_person and "@" in rec_addr and "@" in from_addr
                and rec_addr.split("@")[1] == from_addr.split("@")[1]
            )
            if not (same_person or same_domain):
                continue
            quote_subject = _normalize_subject(re.sub(r"\s*—\s*Quotation\s+ISGEC-\d{8}-[A-Z0-9]{6}\s*$", "", rec.subject or "", flags=re.I))
            overlap = len(subj_tokens & _subject_tokens(quote_subject)) / max(1, len(subj_tokens | _subject_tokens(quote_subject)))
            if overlap >= 0.55:
                candidates.append((overlap, rec))
        if candidates:
            candidates.sort(key=lambda x: (-x[0], -x[1].id))
            best_score, best = candidates[0]
            distinct_proposals = {c[1].proposal_id for c in candidates}
            method = "sender_subj_ambiguous" if len(distinct_proposals) > 1 else "sender_subj"
            return best, method
    return None, ""


async def send_quote_for_email(
    email_record: EmailMessage,
    db: AsyncSession,
    sent_by: str,
    cost_breakdown: dict | None = None,
    estimate: dict | None = None,
    justification: str = "",
    discount_pct: float = 0.0,
    change_type: str = "initial",
    policy_result: dict | None = None,
) -> bool:
    """Renders + sends the quote reply for an email that has a proposal, then
    records an immutable QuoteVersion audit row (recommendations 7.2 / 7.4).
    Returns True on success."""
    proposal = await db.get(Proposal, email_record.proposal_id)
    if not proposal:
        raise RuntimeError("Linked proposal not found")

    items_result = await db.execute(
        select(ProposalItem).where(ProposalItem.proposal_id == proposal.id).order_by(ProposalItem.id)
    )
    items = items_result.scalars().all()

    context = _build_quote_context(proposal, items, cost_breakdown=cost_breakdown)
    payment = await create_payment_for_quote(proposal, email_record.id, db)
    if payment:
        context["pay_url"] = f"{PUBLIC_BASE_URL}/pay/{payment.id}"
        context["pay_amount"] = float(payment.amount)

    html = _render_quote_html(context)
    quote_message_id = await send_email(
        to_addr=email_record.from_addr,
        subject=f"Re: {email_record.subject} — Quotation {proposal.proposal_number}",
        html_body=html,
        in_reply_to=email_record.message_id,
    )

    email_record.status = "replied"
    email_record.sent_by = sent_by
    email_record.replied_at = datetime.utcnow()
    email_record.quote_message_id = quote_message_id or ""
    proposal.status = "sent"

    await record_quote_version(
        db,
        proposal_id=proposal.id,
        final_price_net=float(proposal.total_net or 0),
        email_id=email_record.id,
        estimate=estimate,
        change_type=change_type,
        approved_by=sent_by,
        justification=justification,
        discount_pct=discount_pct,
        policy_result=policy_result,
    )

    # Closed feedback loop: push this quote into cost intelligence as a live
    # historical case (outcome stays "open" until the customer decides).
    try:
        await upsert_project_from_quote(db, proposal, estimate=estimate)
    except Exception:
        traceback.print_exc()

    await db.commit()
    return True


async def process_email(email_data: dict, db: AsyncSession) -> EmailMessage:
    existing = await db.execute(
        select(EmailMessage).where(EmailMessage.message_id == email_data["message_id"])
    )
    if existing.scalar_one_or_none():
        return None

    try:
        uid = int(email_data.get("uid", 0))
    except (TypeError, ValueError):
        uid = 0

    record = EmailMessage(
        message_id=email_data["message_id"],
        uid=uid,
        from_addr=email_data.get("from_addr", ""),
        subject=email_data.get("subject", ""),
        body_text=email_data.get("body_text", ""),
        attachment_names=[{"filename": a.get("filename", ""), "path": a.get("path", "")} for a in email_data.get("attachments", [])],
        status="processing",
        received_at=_naive_utc(email_data.get("received_at")),
    )
    db.add(record)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        return None
    await db.refresh(record)
    record_id = record.id

    try:
        full_text = await _gather_email_text(record.body_text, email_data.get("attachments", []))
        requirements = await extract_requirements(full_text)

        email_type = requirements.get("email_type", "other")
        summary = requirements.get("summary", "")
        record.email_type = email_type

        if email_type == "other":
            record.status = "ignored"
            record.triage_note = (summary or "Not an RFQ / acceptance / rejection — auto-neglected.")[:2000]
            await db.commit()
            return record

        if email_type in ("acceptance", "rejection"):
            linked, match_method = await _match_quoted_thread(email_data, db)
            record.reply_match_method = match_method
            if linked and linked.proposal_id:
                proposal = await db.get(Proposal, linked.proposal_id)
                if proposal:
                    proposal.status = "accepted" if email_type == "acceptance" else "rejected"
                    # Feedback loop: write the outcome onto the historical case.
                    try:
                        await close_project_outcome(db, proposal, email_type)
                    except Exception:
                        traceback.print_exc()
                record.status = "replied"
                record.replied_at = datetime.utcnow()
                record.linked_quote_id = linked.id
                note = f"Customer {email_type}: {summary}"
                if match_method == "sender_subj_ambiguous":
                    note += " [WARN: matched via sender+subject fallback; multiple quotes possible]"
                elif match_method == "sender_subj":
                    note += f" [matched via sender+subject fallback (headers missing)]"
                record.triage_note = note[:2000]
                await db.commit()
                print(f"[triage] customer reply '{email_type}' -> quote #{linked.id} ({match_method}), proposal #{linked.proposal_id} now {proposal.status if proposal else 'n/a'}")
                return record
            record.status = "ignored"
            record.triage_note = f"{summary} (no matching quotation found)"[:2000]
            await db.commit()
            return record

        if not requirements["is_rfq"] or not requirements["items"]:
            record.status = "needs_review"
            record.error_message = "No product requirements detected in this email."
            record.triage_note = summary
            await db.commit()
            return record

        unmatched = _build_line_items(requirements)
        ensure_collection()
        matched, remaining = await _resolve_matches(unmatched, db)

        classifications = {}
        if remaining:
            for c in await classify_unmatched_items([{"description": e["description"]} for e in remaining]):
                classifications[c["description"]] = c["classification"]

        all_items = []
        total_net = 0.0
        total_gross = 0.0
        currency = "INR"

        for entry in matched:
            all_items.append(entry)
            total_net += entry["subtotal_net"]
            total_gross += entry["subtotal_gross"]
            if entry.get("currency"):
                currency = entry["currency"]

        for entry in remaining:
            cls = classifications.get(entry["description"], "on_request")
            entry["item_status"] = cls
            if cls == "on_request":
                entry["notes"] = (
                    "Not part of our standard catalog. We can arrange this item for you - "
                    "confirm the rest of the pricing and we will proceed with sourcing."
                )
            else:
                entry["notes"] = "This item is not manufactured/supplied by ISGEC."
            all_items.append(entry)

        proposal = Proposal(
            proposal_number=f"ISGEC-{datetime.now().strftime('%Y%m%d')}-{os.urandom(3).hex().upper()}",
            rfq_text=full_text[:20000],
            rfq_source="email",
            status="draft",
            currency=currency,
            total_net=_money(total_net),
            total_gross=_money(total_gross),
            notes=requirements.get("notes", ""),
            valid_until=(datetime.now() + timedelta(days=30)).date(),
        )
        db.add(proposal)
        await db.flush()

        # Customer master linkage (recommendation 7.3): map the email sender to
        # a Customer record so account-level history accumulates.
        try:
            await link_proposal_to_customer(db, proposal, record.from_addr)
        except Exception:
            traceback.print_exc()

        for entry in all_items:
            db.add(ProposalItem(
                proposal_id=proposal.id,
                product_id=entry.get("product_id"),
                sku=entry.get("sku", ""),
                description=entry["description"],
                quantity=entry["quantity"],
                unit_price_net=_money(entry["unit_price_net"]),
                unit_price_gross=_money(entry["unit_price_gross"]),
                subtotal_net=_money(entry["subtotal_net"]),
                subtotal_gross=_money(entry["subtotal_gross"]),
                notes=entry.get("notes", ""),
                item_status=entry["item_status"],
            ))

        record.proposal_id = proposal.id
        await db.commit()
        await db.refresh(proposal)

        all_quoted = all(e["item_status"] == "quoted" for e in all_items)
        if all_quoted and EMAIL_AUTO_SEND and is_configured():
            # Pricing policy gate (gap 3 / gap 10 / recommendation 7.1):
            # never auto-send a quote that has no estimate or fails policy.
            estimate = None
            policy = {"allowed": False, "violations": ["estimate unavailable"], "checks": []}
            try:
                from app.services.costing import run_cost_estimate
                estimate = await run_cost_estimate(db, proposal_id=proposal.id, save=True)
                if "error" in estimate:
                    estimate = None
            except Exception as est_err:
                traceback.print_exc()
            if estimate:
                predicted_cost = float(estimate.get("predicted_cost") or 0)
                policy = evaluate_quote_policy(
                    estimate,
                    proposed_price_net=float(proposal.total_net or 0),
                    predicted_cost=predicted_cost,
                )
            if not auto_send_allowed(policy):
                reasons = "; ".join(policy.get("violations", []))
                record.status = "quoted"
                record.error_message = (
                    "Held by pricing policy - manual approval required: "
                    + (reasons or "review required")
                )[:2000]
                await db.commit()
                print(f"[policy] auto-send blocked for email #{record.id}: {reasons}")
            else:
                try:
                    await send_quote_for_email(
                        record, db, sent_by="auto",
                        cost_breakdown=estimate.get("breakdown_pct") if estimate else None,
                        estimate=estimate, policy_result=policy,
                    )
                except Exception as send_err:
                    record.status = "failed"
                    record.error_message = f"Quote generated but auto-send failed: {send_err}"
                    await db.commit()
        else:
            reasons = []
            if not all_quoted:
                special = [e for e in all_items if e["item_status"] != "quoted"]
                n_req = sum(1 for e in special if e["item_status"] == "on_request")
                n_no = sum(1 for e in special if e["item_status"] == "not_offered")
                bits = []
                if n_req:
                    bits.append(f"{n_req} item(s) available on request")
                if n_no:
                    bits.append(f"{n_no} item(s) not offered")
                reasons.append("; ".join(bits))
            if not EMAIL_AUTO_SEND:
                reasons.append("auto-send disabled")
            record.status = "quoted"
            record.error_message = "Pending approval: " + ". ".join(reasons) if reasons else ""
            await db.commit()

        return record

    except Exception as exc:
        traceback.print_exc()
        try:
            await db.rollback()
            fresh = await db.get(EmailMessage, record_id)
            if fresh:
                fresh.status = "failed"
                fresh.error_message = str(exc)[:2000]
                await db.commit()
                await db.refresh(fresh)
                return fresh
        except Exception:
            traceback.print_exc()
        return record


_poll_lock = asyncio.Lock()


async def run_poll_cycle(limit: int = 10) -> dict:
    if not is_configured():
        return {"checked": 0, "processed": 0, "skipped": 0, "error": "Email not configured in .env"}

    if _poll_lock.locked():
        return {"checked": 0, "processed": 0, "skipped": 0, "error": "A poll cycle is already running"}
    async with _poll_lock:
        return await _run_poll_cycle_locked(limit)


async def _recover_stale_records(db: AsyncSession, max_age_minutes: int = 15) -> int:
    """Mark long-stuck processing/new records as failed so they can be retried from the dashboard."""
    cutoff = datetime.utcnow() - timedelta(minutes=max_age_minutes)
    result = await db.execute(
        select(EmailMessage).where(
            EmailMessage.status.in_(["processing", "new"]),
            EmailMessage.received_at < cutoff,
        )
    )
    stale = result.scalars().all()
    for rec in stale:
        rec.status = "failed"
        rec.error_message = "Processing stalled — marked failed automatically. Use Retry."
    if stale:
        await db.commit()
    return len(stale)


async def _run_poll_cycle_locked(limit: int) -> dict:
    try:
        emails = fetch_unread(limit=limit)
    except Exception as exc:
        return {"checked": 0, "processed": 0, "skipped": 0, "error": str(exc)}

    processed = 0
    skipped = 0
    failed = 0
    async with async_session() as db:
        await _recover_stale_records(db)
        for email_data in emails:
            try:
                record = await process_email(email_data, db)
                if record is None:
                    skipped += 1
                else:
                    processed += 1
            except Exception:
                traceback.print_exc()
                await db.rollback()
                failed += 1

    return {"checked": len(emails), "processed": processed, "skipped": skipped, "failed": failed}
