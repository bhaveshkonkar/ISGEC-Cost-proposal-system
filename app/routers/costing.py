import os
import uuid
from fastapi import APIRouter, UploadFile, File, Form, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from app.models import get_session, ProjectCost, ProposalCostEstimate
from app.config import UPLOAD_DIR
from app.services.costing import (
    import_projects_from_csv, create_project_record, delete_project_record,
    list_project_records, find_similar_projects, win_loss_analysis,
    run_cost_estimate, apply_what_if, seed_demo_projects,
)

router = APIRouter(prefix="/api/costing", tags=["costing"])


@router.get("/projects")
async def list_projects(
    limit: int = Query(200, ge=1, le=500),
    offset: int = Query(0, ge=0),
    sector: str = Query(""),
    category: str = Query(""),
    outcome: str = Query(""),
    db: AsyncSession = Depends(get_session),
):
    projects = await list_project_records(db, limit=limit, offset=offset, sector=sector, category=category, outcome=outcome)
    return {"projects": projects, "total": len(projects)}


@router.post("/projects")
async def create_project(
    project_name: str = Form(...),
    customer_name: str = Form(""),
    sector: str = Form(""),
    equipment_category: str = Form(""),
    description: str = Form(""),
    year: int = Form(None),
    quantity: int = Form(1),
    weight_kg: float = Form(0),
    complexity: str = Form("medium"),
    material_cost: float = Form(0),
    labour_cost: float = Form(0),
    engineering_cost: float = Form(0),
    overhead_cost: float = Form(0),
    freight_cost: float = Form(0),
    quoted_value: float = Form(0),
    final_value: float = Form(0),
    currency: str = Form("INR"),
    outcome: str = Form("won"),
    lost_reason: str = Form(""),
    db: AsyncSession = Depends(get_session),
):
    data = {
        "project_name": project_name, "customer_name": customer_name,
        "sector": sector, "equipment_category": equipment_category,
        "description": description, "year": year, "quantity": quantity,
        "weight_kg": weight_kg, "complexity": complexity,
        "material_cost": material_cost, "labour_cost": labour_cost,
        "engineering_cost": engineering_cost, "overhead_cost": overhead_cost,
        "freight_cost": freight_cost, "quoted_value": quoted_value,
        "final_value": final_value, "currency": currency,
        "outcome": outcome, "lost_reason": lost_reason,
    }
    return await create_project_record(data, db)


@router.delete("/projects/{project_id}")
async def delete_project(project_id: int, db: AsyncSession = Depends(get_session)):
    ok = await delete_project_record(project_id, db)
    if not ok:
        raise HTTPException(404, "Project not found")
    return {"deleted": True}


@router.post("/projects/upload")
async def upload_projects(
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_session),
):
    if not file.filename.endswith(".csv"):
        raise HTTPException(400, "Only CSV files are supported")
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    file_path = os.path.join(UPLOAD_DIR, f"costing_{uuid.uuid4()}_{file.filename}")
    with open(file_path, "wb") as f:
        content = await file.read()
        f.write(content)
    result = await import_projects_from_csv(file_path, db)
    return result


@router.post("/projects/seed-demo")
async def seed_demo(db: AsyncSession = Depends(get_session)):
    return await seed_demo_projects(db)


@router.post("/estimate")
async def estimate_from_rfq(
    rfq_text: str = Form(...),
    db: AsyncSession = Depends(get_session),
):
    result = await run_cost_estimate(db, rfq_text=rfq_text)
    if "error" in result:
        raise HTTPException(400, result["error"])
    return result


@router.post("/estimate/proposal/{proposal_id}")
async def estimate_for_proposal(
    proposal_id: int,
    db: AsyncSession = Depends(get_session),
):
    result = await run_cost_estimate(db, proposal_id=proposal_id)
    if "error" in result:
        raise HTTPException(400, result["error"])
    return result


@router.get("/similar")
async def get_similar_projects(
    text: str = Query(...),
    limit: int = Query(6, ge=1, le=20),
    db: AsyncSession = Depends(get_session),
):
    similar = await find_similar_projects(db, text, limit=limit)
    return {"similar_projects": similar, "total": len(similar)}


@router.get("/win-loss")
async def get_win_loss(db: AsyncSession = Depends(get_session)):
    return await win_loss_analysis(db)


@router.post("/what-if")
async def what_if(
    rfq_text: str = Form(""),
    proposal_id: int = Form(None),
    material_delta_pct: float = Form(0),
    labour_delta_pct: float = Form(0),
    engineering_delta_pct: float = Form(0),
    overhead_delta_pct: float = Form(0),
    freight_delta_pct: float = Form(0),
    contingency_pct: float = Form(None),
    margin_pct: float = Form(None),
    discount_pct: float = Form(0),
    db: AsyncSession = Depends(get_session),
):
    baseline = await run_cost_estimate(db, rfq_text=rfq_text, proposal_id=proposal_id, save=False)
    if "error" in baseline:
        raise HTTPException(400, baseline["error"])
    overrides = {}
    if material_delta_pct:
        overrides["material_delta_pct"] = material_delta_pct
    if labour_delta_pct:
        overrides["labour_delta_pct"] = labour_delta_pct
    if engineering_delta_pct:
        overrides["engineering_delta_pct"] = engineering_delta_pct
    if overhead_delta_pct:
        overrides["overhead_delta_pct"] = overhead_delta_pct
    if freight_delta_pct:
        overrides["freight_delta_pct"] = freight_delta_pct
    if contingency_pct is not None:
        overrides["contingency_pct"] = contingency_pct
    if margin_pct is not None:
        overrides["margin_pct"] = margin_pct
    if discount_pct:
        overrides["discount_pct"] = discount_pct
    return apply_what_if(baseline, overrides)


@router.get("/estimates/{proposal_id}")
async def get_estimates(proposal_id: int, db: AsyncSession = Depends(get_session)):
    from sqlalchemy import select
    res = await db.execute(
        select(ProposalCostEstimate)
        .where(ProposalCostEstimate.proposal_id == proposal_id)
        .order_by(ProposalCostEstimate.created_at.desc())
        .limit(10)
    )
    estimates = res.scalars().all()
    return {
        "estimates": [
            {
                "id": e.id, "proposal_id": e.proposal_id,
                "predicted_cost": float(e.predicted_cost),
                "recommended_price_net": float(e.recommended_price_net),
                "recommended_margin_pct": float(e.recommended_margin_pct),
                "risk_level": e.risk_level, "confidence": float(e.confidence),
                "breakdown": e.breakdown, "drivers": e.drivers,
                "llm_narrative": e.llm_narrative,
                "created_at": e.created_at.isoformat() if e.created_at else None,
            }
            for e in estimates
        ],
        "total": len(estimates),
    }
