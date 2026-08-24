import hashlib
import hmac
import math
import traceback

import httpx
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET, RAZORPAY_MAX_AMOUNT, razorpay_configured
from app.models import EmailMessage, Payment, Proposal

RAZORPAY_API = "https://api.razorpay.com/v1"


def _auth() -> tuple[str, str]:
    return (RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET)


def _to_paise(amount) -> int:
    return int(math.ceil(float(amount or 0) * 100))


async def create_payment_for_quote(proposal: Proposal, email_id: int | None, db: AsyncSession) -> Payment | None:
    """Create a Razorpay order for the full gross amount and store a Payment row.

    Returns None when Razorpay is not configured or order creation fails
    (the quote email is still sent, just without the pay button).
    """
    if not razorpay_configured():
        return None

    quote_gross = float(proposal.total_gross or 0)
    if quote_gross <= 0:
        return None

    amount_rupees = quote_gross
    if amount_rupees > RAZORPAY_MAX_AMOUNT:
        amount_rupees = float(RAZORPAY_MAX_AMOUNT)

    payload = {
        "amount": _to_paise(amount_rupees),
        "currency": proposal.currency or "INR",
        "receipt": proposal.proposal_number,
        "notes": {
            "proposal_id": str(proposal.id),
            "proposal_number": proposal.proposal_number,
            "quote_gross": f"{quote_gross:.2f}",
            "capped": "true" if amount_rupees < quote_gross else "false",
        },
    }

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(f"{RAZORPAY_API}/orders", json=payload, auth=_auth())
            resp.raise_for_status()
            order = resp.json()
    except httpx.HTTPStatusError as exc:
        body = ""
        try:
            body = exc.response.text[:300]
        except Exception:
            pass
        print(f"[payments] Razorpay order creation failed for proposal {proposal.id}: {exc} {body}")
        return None
    except Exception as exc:
        print(f"[payments] Razorpay order creation failed for proposal {proposal.id}: {exc}")
        return None

    payment = Payment(
        proposal_id=proposal.id,
        email_id=email_id,
        razorpay_order_id=order["id"],
        amount=amount_rupees,
        currency=order.get("currency", proposal.currency or "INR"),
        status="created",
        error_message=(
            f"Quote value {quote_gross:.2f} capped to test-mode limit" if amount_rupees < quote_gross else ""
        ),
    )
    db.add(payment)
    await db.flush()
    return payment


def verify_signature(order_id: str, payment_id: str, signature: str) -> bool:
    expected = hmac.new(
        RAZORPAY_KEY_SECRET.encode(),
        f"{order_id}|{payment_id}".encode(),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature or "")


async def confirm_payment(order_id: str, payment_id: str, signature: str, db: AsyncSession) -> dict:
    """Verify the checkout signature and mark the payment paid + proposal accepted."""
    result = await db.execute(select(Payment).where(Payment.razorpay_order_id == order_id))
    payment = result.scalar_one_or_none()
    if not payment:
        return {"ok": False, "error": "Unknown order"}

    if not verify_signature(order_id, payment_id, signature):
        payment.status = "failed"
        payment.error_message = "Signature verification failed"
        await db.commit()
        return {"ok": False, "error": "Signature verification failed"}

    if payment.status == "paid":
        return {"ok": True, "already_paid": True, "amount": float(payment.amount)}

    payment.status = "paid"
    payment.razorpay_payment_id = payment_id
    payment.razorpay_signature = signature
    from datetime import datetime

    payment.paid_at = datetime.utcnow()

    proposal = await db.get(Proposal, payment.proposal_id)
    if proposal:
        proposal.status = "accepted"
        from app.services.feedback import close_project_outcome
        try:
            await close_project_outcome(db, proposal, "accepted")
        except Exception:
            pass

    await db.commit()
    print(f"[payments] Payment {payment_id} confirmed for order {order_id}, proposal #{payment.proposal_id} accepted")
    return {"ok": True, "already_paid": False, "amount": float(payment.amount), "proposal_id": payment.proposal_id}


async def get_payment_status_for_proposal(db: AsyncSession, proposal_id: int) -> dict | None:
    result = await db.execute(
        select(Payment).where(Payment.proposal_id == proposal_id).order_by(Payment.id.desc()).limit(1)
    )
    payment = result.scalar_one_or_none()
    if not payment:
        return None
    return {
        "id": payment.id,
        "status": payment.status,
        "amount": float(payment.amount),
        "currency": payment.currency,
        "razorpay_payment_id": payment.razorpay_payment_id,
        "paid_at": payment.paid_at.isoformat() if payment.paid_at else None,
        "error_message": payment.error_message,
    }


async def total_sales(db: AsyncSession) -> tuple[float, int]:
    res = await db.execute(
        select(func.coalesce(func.sum(Payment.amount), 0), func.count(Payment.id)).where(Payment.status == "paid")
    )
    row = res.one()
    return float(row[0] or 0), int(row[1] or 0)


async def mark_failed_from_page(order_id: str, message: str, db: AsyncSession) -> None:
    try:
        result = await db.execute(select(Payment).where(Payment.razorpay_order_id == order_id))
        payment = result.scalar_one_or_none()
        if payment and payment.status == "created":
            payment.status = "failed"
            payment.error_message = f"Checkout failed/dismissed: {message}"[:2000]
            await db.commit()
    except Exception:
        traceback.print_exc()
