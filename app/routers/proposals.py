import os
import uuid
from fastapi import APIRouter, UploadFile, File, Form, Depends, HTTPException
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from app.models import Proposal, ProposalItem, Product, Customer, QuoteVersion, get_session
from app.services.proposal import process_rfq, import_customers_from_csv
from app.services.payments import total_sales
from app.services.quote_history import serialize_quote_version
from app.services.document import parse_document
from app.config import UPLOAD_DIR

router = APIRouter(prefix="/api", tags=["proposals"])


@router.get("/dashboard/stats")
async def get_dashboard_stats(db: AsyncSession = Depends(get_session)):
    total_props_res = await db.execute(select(func.count(Proposal.id)))
    total_props = total_props_res.scalar() or 0

    total_val_res = await db.execute(select(func.coalesce(func.sum(Proposal.total_gross), 0)))
    total_val = float(total_val_res.scalar() or 0)

    status_query = select(
        Proposal.status,
        func.count(Proposal.id),
        func.coalesce(func.sum(Proposal.total_gross), 0)
    ).group_by(Proposal.status)
    status_res = await db.execute(status_query)
    
    status_counts = {"draft": 0, "sent": 0, "accepted": 0, "rejected": 0}
    status_values = {"draft": 0.0, "sent": 0.0, "accepted": 0.0, "rejected": 0.0}
    
    for row in status_res.all():
        st, cnt, val = row[0], row[1], float(row[2])
        if st in status_counts:
            status_counts[st] = cnt
            status_values[st] = val

    prods_res = await db.execute(select(func.count(Product.id)))
    total_prods = prods_res.scalar() or 0

    custs_res = await db.execute(select(func.count(Customer.id)))
    total_custs = custs_res.scalar() or 0

    sales_value, paid_count = await total_sales(db)

    return {
        "total_proposals": total_props,
        "total_quoted_value": total_val,
        "total_products": total_prods,
        "total_customers": total_custs,
        "status_counts": status_counts,
        "status_values": status_values,
        "total_sales": sales_value,
        "paid_count": paid_count,
    }


@router.post("/rfq")
async def submit_rfq(
    rfq_text: str = Form(None),
    rfq_file: UploadFile = File(None),
    customer_id: int = Form(None),
    db: AsyncSession = Depends(get_session),
):
    text = rfq_text or ""
    source = "text"

    if rfq_file:
        os.makedirs(UPLOAD_DIR, exist_ok=True)
        file_path = os.path.join(UPLOAD_DIR, f"{uuid.uuid4()}_{rfq_file.filename}")
        with open(file_path, "wb") as f:
            content = await rfq_file.read()
            f.write(content)
        try:
            text = parse_document(file_path)
            source = "file"
        except Exception as e:
            raise HTTPException(400, f"Failed to parse document: {str(e)}")

    if not text.strip():
        raise HTTPException(400, "RFQ text is required")

    result = await process_rfq(text, db)

    if customer_id:
        proposal = await db.get(Proposal, result["proposal_id"])
        if proposal:
            proposal.customer_id = customer_id
            await db.commit()

    return result


@router.post("/rfq/upload")
async def submit_rfq_file(
    file: UploadFile = File(...),
    customer_id: int = Form(None),
    db: AsyncSession = Depends(get_session),
):
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    file_path = os.path.join(UPLOAD_DIR, f"{uuid.uuid4()}_{file.filename}")
    with open(file_path, "wb") as f:
        content = await file.read()
        f.write(content)
    try:
        text = parse_document(file_path)
    except Exception as e:
        raise HTTPException(400, f"Failed to parse document: {str(e)}")

    result = await process_rfq(text, db)

    if customer_id:
        proposal = await db.get(Proposal, result["proposal_id"])
        if proposal:
            proposal.customer_id = customer_id
            await db.commit()

    return result


@router.get("/proposals")
async def list_proposals(
    status: str = "",
    limit: int = 50,
    offset: int = 0,
    db: AsyncSession = Depends(get_session),
):
    query = select(Proposal)
    if status:
        query = query.where(Proposal.status == status)
    query = query.order_by(Proposal.created_at.desc()).offset(offset).limit(limit)
    result = await db.execute(query)
    proposals = result.scalars().all()
    count_result = await db.execute(select(func.count(Proposal.id)))
    total = count_result.scalar()
    return {
        "proposals": [
            {
                "id": p.id, "proposal_number": p.proposal_number,
                "status": p.status, "total_net": float(p.total_net),
                "total_gross": float(p.total_gross), "currency": p.currency,
                "created_at": p.created_at.isoformat() if p.created_at else None,
                "valid_until": p.valid_until.isoformat() if p.valid_until else None,
            }
            for p in proposals
        ],
        "total": total,
    }


@router.get("/proposals/{proposal_id}")
async def get_proposal(proposal_id: int, db: AsyncSession = Depends(get_session)):
    proposal = await db.get(Proposal, proposal_id)
    if not proposal:
        raise HTTPException(404, "Proposal not found")

    items_result = await db.execute(
        select(ProposalItem).where(ProposalItem.proposal_id == proposal_id)
    )
    items = items_result.scalars().all()

    customer = None
    if proposal.customer_id:
        customer = await db.get(Customer, proposal.customer_id)

    return {
        "id": proposal.id,
        "proposal_number": proposal.proposal_number,
        "rfq_text": proposal.rfq_text,
        "status": proposal.status,
        "total_net": float(proposal.total_net),
        "total_gross": float(proposal.total_gross),
        "currency": proposal.currency,
        "notes": proposal.notes,
        "valid_until": proposal.valid_until.isoformat() if proposal.valid_until else None,
        "created_at": proposal.created_at.isoformat() if proposal.created_at else None,
        "customer": {
            "id": customer.id, "name": customer.name,
            "sector": customer.sector, "source": customer.source,
            "contact_info": customer.contact_info,
        } if customer else None,
        "items": [
            {
                "id": item.id, "sku": item.sku, "description": item.description,
                "quantity": item.quantity, "unit_price_net": float(item.unit_price_net),
                "unit_price_gross": float(item.unit_price_gross),
                "subtotal_net": float(item.subtotal_net),
                "subtotal_gross": float(item.subtotal_gross),
                "notes": item.notes,
            }
            for item in items
        ],
    }


@router.get("/proposals/{proposal_id}/quote-history")
async def get_quote_history(proposal_id: int, db: AsyncSession = Depends(get_session)):
    """Versioned pricing timeline (recommendation 7.4): original estimate,
    overrides, final approved quote, approver identity, deviation reasons."""
    proposal = await db.get(Proposal, proposal_id)
    if not proposal:
        raise HTTPException(404, "Proposal not found")
    result = await db.execute(
        select(QuoteVersion)
        .where(QuoteVersion.proposal_id == proposal_id)
        .order_by(QuoteVersion.version_number, QuoteVersion.id)
    )
    versions = [serialize_quote_version(v) for v in result.scalars().all()]
    return {
        "proposal_id": proposal_id,
        "proposal_number": proposal.proposal_number,
        "versions": versions,
        "total": len(versions),
    }


@router.put("/proposals/{proposal_id}/status")
async def update_proposal_status(
    proposal_id: int,
    status: str = Form(...),
    db: AsyncSession = Depends(get_session),
):
    proposal = await db.get(Proposal, proposal_id)
    if not proposal:
        raise HTTPException(404, "Proposal not found")
    if status not in ("draft", "sent", "accepted", "rejected"):
        raise HTTPException(400, "Invalid status")
    proposal.status = status
    if status in ("accepted", "rejected"):
        from app.services.feedback import close_project_outcome
        try:
            await close_project_outcome(db, proposal, status)
        except Exception:
            pass
    await db.commit()
    return {"id": proposal.id, "status": proposal.status}


@router.post("/customers")
async def create_customer(
    name: str = Form(...),
    sector: str = Form(""),
    source: str = Form(""),
    db: AsyncSession = Depends(get_session),
):
    customer = Customer(name=name, sector=sector, source=source)
    db.add(customer)
    await db.commit()
    await db.refresh(customer)
    return {"id": customer.id, "name": customer.name}


@router.get("/customers")
async def list_customers(db: AsyncSession = Depends(get_session)):
    result = await db.execute(select(Customer).order_by(Customer.id.desc()))
    customers = result.scalars().all()
    return {
        "customers": [
            {"id": c.id, "name": c.name, "sector": c.sector, "source": c.source}
            for c in customers
        ]
    }


@router.post("/customers/upload")
async def upload_customers(file: UploadFile = File(...), db: AsyncSession = Depends(get_session)):
    if not file.filename.endswith(".csv"):
        raise HTTPException(400, "Only CSV files are supported")
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    file_path = os.path.join(UPLOAD_DIR, f"{uuid.uuid4()}_{file.filename}")
    with open(file_path, "wb") as f:
        content = await file.read()
        f.write(content)
    result = await import_customers_from_csv(file_path, db)
    return result
