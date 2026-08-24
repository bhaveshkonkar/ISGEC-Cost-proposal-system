"""Closed feedback loop from live quotes into cost intelligence.

When a quote is sent, a ProjectCost case is created (or updated) from the
estimate snapshot so the very next estimate can learn from it. When the
customer accepts or rejects, the outcome is written back so win-rate and
margin recommendations stay current. Live cases with outcome="open" are used
for cost similarity but excluded from win/loss statistics until decided.
"""
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ProjectCost, Proposal, ProposalItem, ProposalCostEstimate


def _f(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


async def _embed_and_attach(record: ProjectCost):
    embed_text = f"{record.equipment_category} {record.sector} {record.project_name} {(record.description or '')[:500]}"
    try:
        from app.services.embedding import get_embedding
        from app.services.costing import upsert_project_embedding
        emb = await get_embedding(embed_text)
        if record.qdrant_point_id:
            point_id = record.qdrant_point_id
        else:
            import uuid
            point_id = str(uuid.uuid4())
        upsert_project_embedding(point_id, emb, {
            "db_id": record.id,
            "project_name": record.project_name,
            "category": record.equipment_category,
            "sector": record.sector,
            "outcome": record.outcome,
        })
        record.qdrant_point_id = point_id
    except Exception:
        pass


async def upsert_project_from_quote(
    db: AsyncSession,
    proposal: Proposal,
    estimate: dict | None = None,
) -> ProjectCost | None:
    """Create/update the historical case for this proposal at quote-send time."""
    result = await db.execute(
        select(ProjectCost).where(ProjectCost.proposal_id == proposal.id)
    )
    record = result.scalars().first()

    est = estimate if isinstance(estimate, dict) else None
    predicted_cost = 0.0
    breakdown = {}
    category = ""
    sector = ""
    complexity_band = "medium"
    confidence = 0.0
    if not est:
        res = await db.execute(
            select(ProposalCostEstimate)
            .where(ProposalCostEstimate.proposal_id == proposal.id)
            .order_by(ProposalCostEstimate.id.desc())
            .limit(1)
        )
        row = res.scalars().first()
        if row:
            predicted_cost = _f(row.predicted_cost)
            breakdown = row.breakdown or {}
            snapshot = row.input_snapshot or {}
            ia = snapshot.get("item_analysis", {})
            complexity_band = (ia.get("aggregate") or {}).get("complexity_band") or row.risk_level or "medium"
            similar = row.similar_projects or []
            if similar:
                top = similar[0]
                category = top.get("category", "") if isinstance(top, dict) else ""
            confidence = _f(row.confidence)
    else:
        predicted_cost = _f(est.get("predicted_cost"))
        breakdown = est.get("breakdown") or {}
        ia = (est.get("input_snapshot") or {}).get("item_analysis", {})
        complexity_band = (ia.get("aggregate") or {}).get("complexity_band") or \
            ((est.get("risk") or {}).get("risk_level") or "medium")
        similar = est.get("similar_projects") or []
        if similar and isinstance(similar[0], dict):
            category = similar[0].get("equipment_category", "")
            sector = similar[0].get("sector", "")
        confidence = _f(est.get("confidence"))

    quoted_price = _f(proposal.total_net)

    if record is None:
        items_res = await db.execute(
            select(ProposalItem).where(ProposalItem.proposal_id == proposal.id)
        )
        qty_total = sum(max(1, int(_f(i.quantity, 1))) for i in items_res.scalars().all()) or 1

        # Scale the estimated component split to the predicted total so the
        # case contributes realistic cost ratios to future estimates.
        comp_sum = sum(_f(v) for v in breakdown.values())
        scale = predicted_cost / comp_sum if comp_sum > 0 else 0.0
        record = ProjectCost(
            project_name=f"{proposal.proposal_number} — {category or 'Quoted RFQ'}",
            customer_name="",
            sector=sector,
            equipment_category=category,
            description=(proposal.rfq_text or "")[:1000],
            year=datetime.now().year,
            quantity=max(1, int(qty_total)),
            complexity=complexity_band if complexity_band in ("low", "medium", "high") else "medium",
            material_cost=round(_f(breakdown.get("material")) * scale, 2),
            labour_cost=round(_f(breakdown.get("labour")) * scale, 2),
            engineering_cost=round(_f(breakdown.get("engineering")) * scale, 2),
            overhead_cost=round(_f(breakdown.get("overhead")) * scale, 2),
            freight_cost=round(_f(breakdown.get("freight")) * scale, 2),
            total_cost=predicted_cost,
            quoted_value=quoted_price,
            final_value=0,
            currency=proposal.currency or "INR",
            margin_pct=0,
            outcome="open",
            lost_reason="",
            proposal_id=proposal.id,
        )
        db.add(record)
        await db.flush()
    else:
        record.quoted_value = quoted_price
        if predicted_cost > 0:
            record.total_cost = predicted_cost

    await _embed_and_attach(record)
    await db.flush()
    return record


async def close_project_outcome(db: AsyncSession, proposal: Proposal, outcome: str) -> ProjectCost | None:
    """Write won/lost back onto the historical case created at quote time."""
    outcome = "won" if outcome == "accepted" else ("lost" if outcome == "rejected" else outcome)
    result = await db.execute(
        select(ProjectCost).where(ProjectCost.proposal_id == proposal.id)
    )
    record = result.scalars().first()
    if record is None:
        return None

    final_val = _f(proposal.total_net)
    record.outcome = outcome
    if final_val > 0:
        record.final_value = final_val
        if _f(record.total_cost) > 0:
            record.margin_pct = round((final_val - _f(record.total_cost)) / final_val * 100, 2)
    await db.flush()

    # Refresh the embedding payload so outcome-aware filtering stays accurate.
    await _embed_and_attach(record)
    await db.flush()
    return record
