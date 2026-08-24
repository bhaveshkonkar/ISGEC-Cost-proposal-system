import os

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import RAZORPAY_KEY_ID, razorpay_configured
from app.models import EmailMessage, Payment, Proposal, get_session
from app.services.payments import confirm_payment, get_payment_status_for_proposal, mark_failed_from_page

router = APIRouter(prefix="/api/payments", tags=["payments"])
page_router = APIRouter(tags=["payments"])

templates = Jinja2Templates(directory="app/templates")


class VerifyPayload(BaseModel):
    razorpay_order_id: str
    razorpay_payment_id: str
    razorpay_signature: str


class FailedPayload(BaseModel):
    message: str = "Payment failed or dismissed"


@page_router.get("/pay/{payment_id}", response_class=HTMLResponse)
async def pay_page(payment_id: int, request: Request, db: AsyncSession = Depends(get_session)):
    payment = await db.get(Payment, payment_id)
    if not payment:
        return templates.TemplateResponse(request, "payment_error.html", {"message": "Payment link not found."}, status_code=404)

    proposal = await db.get(Proposal, payment.proposal_id)
    customer_email = ""
    if payment.email_id:
        email_record = await db.get(EmailMessage, payment.email_id)
        if email_record:
            customer_email = email_record.from_addr

    already_paid = payment.status == "paid"
    can_pay = (
        razorpay_configured()
        and not already_paid
        and payment.status != "failed"
        and bool(RAZORPAY_KEY_ID)
        and bool(proposal)
    )

    return templates.TemplateResponse(request, "payment.html", {
        "payment": payment,
        "proposal": proposal,
        "customer_email": customer_email,
        "razorpay_key_id": RAZORPAY_KEY_ID,
        "amount_paise": int(float(payment.amount) * 100),
        "already_paid": already_paid,
        "can_pay": can_pay,
    })


@router.post("/verify")
async def verify_payment(payload: VerifyPayload, db: AsyncSession = Depends(get_session)):
    if not razorpay_configured():
        raise HTTPException(400, "Razorpay is not configured")
    result = await confirm_payment(
        payload.razorpay_order_id, payload.razorpay_payment_id, payload.razorpay_signature, db
    )
    if not result.get("ok"):
        raise HTTPException(400, result.get("error", "Verification failed"))
    return result


@router.post("/{payment_id}/failed")
async def report_failed(payment_id: int, payload: FailedPayload, db: AsyncSession = Depends(get_session)):
    payment = await db.get(Payment, payment_id)
    if not payment:
        raise HTTPException(404, "Payment not found")
    if payment.status == "paid":
        return {"ok": True, "note": "Already paid — failure report ignored"}
    await mark_failed_from_page(payment.razorpay_order_id, payload.message, db)
    return {"ok": True}


@router.get("/proposal/{proposal_id}")
async def payment_status(proposal_id: int, db: AsyncSession = Depends(get_session)):
    status = await get_payment_status_for_proposal(db, proposal_id)
    return {"proposal_id": proposal_id, "payment": status}
