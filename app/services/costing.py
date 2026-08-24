import csv
import json
import re
import statistics
import uuid
from collections import Counter, defaultdict
from datetime import datetime

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import (
    QDRANT_URL, QDRANT_PROJECT_COLLECTION, EMBEDDING_DIM,
    DEFAULT_TARGET_MARGIN, MIN_MARGIN_FLOOR, MAX_MARGIN_CEILING,
    CONTINGENCY_LOW_PCT, CONTINGENCY_MEDIUM_PCT, CONTINGENCY_HIGH_PCT,
    HISTORY_STALE_YEARS,
)
from app.models import ProjectCost, ProposalCostEstimate, Proposal, ProposalItem
from app.services.item_pricing import analyze_rfq_items

RISK_MATERIALS = [
    "inconel", "hastelloy", "monel", "titanium", "duplex",
    "super duplex", "incoloy", "nickel alloy", "zirconium", "tantalum",
]
MEDIUM_MATERIALS = [
    "stainless", "ss316", "ss304", "cr-mo", "chrome moly", "low temp carbon",
]
DEFAULT_BREAKDOWN = {
    "material": 0.55, "labour": 0.18, "engineering": 0.10,
    "overhead": 0.12, "freight": 0.05,
}
RISK_CONTINGENCY_MAP = {
    "low": CONTINGENCY_LOW_PCT, "medium": CONTINGENCY_MEDIUM_PCT,
    "high": CONTINGENCY_HIGH_PCT,
}
MARGIN_BANDS = [
    (0, 5, "0-5%"), (5, 10, "5-10%"), (10, 15, "10-15%"),
    (15, 20, "15-20%"), (20, 100, "20%+"),
]
CSV_HEADER_MAP = {
    "projectname": "project_name", "name": "project_name", "project": "project_name",
    "project_name": "project_name",
    "customername": "customer_name", "customer": "customer_name", "client": "customer_name",
    "customer_name": "customer_name",
    "sector": "sector",
    "equipmentcategory": "equipment_category", "category": "equipment_category",
    "equipment": "equipment_category", "equipment_category": "equipment_category",
    "description": "description", "desc": "description", "scope": "description",
    "year": "year",
    "quantity": "quantity", "qty": "quantity",
    "weightkg": "weight_kg", "weight": "weight_kg", "weight_kg": "weight_kg",
    "complexity": "complexity",
    "materialcost": "material_cost", "material": "material_cost",
    "material_cost": "material_cost",
    "labourcost": "labour_cost", "laborcost": "labour_cost", "labor": "labour_cost",
    "labour": "labour_cost", "labour_cost": "labour_cost",
    "engineeringcost": "engineering_cost", "engineering": "engineering_cost",
    "engineering_cost": "engineering_cost",
    "overheadcost": "overhead_cost", "overhead": "overhead_cost",
    "overhead_cost": "overhead_cost",
    "freightcost": "freight_cost", "freight": "freight_cost", "logistics": "freight_cost",
    "freight_cost": "freight_cost",
    "quotedvalue": "quoted_value", "quotevalue": "quoted_value",
    "bidvalue": "quoted_value", "bid": "quoted_value", "quoted": "quoted_value",
    "quoted_value": "quoted_value",
    "finalvalue": "final_value", "ordervalue": "final_value", "order": "final_value",
    "final": "final_value", "final_value": "final_value",
    "currency": "currency",
    "outcome": "outcome", "status": "outcome", "result": "outcome",
    "lostreason": "lost_reason", "reason": "lost_reason", "lost_reason": "lost_reason",
    "marginpct": "margin_pct", "margin": "margin_pct", "markup": "margin_pct",
    "margin_pct": "margin_pct",
}


def _f(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _tokenize(text):
    return set(re.findall(r'\w{3,}', (text or "").lower()))


_qdrant_client = None


def _get_qdrant():
    global _qdrant_client
    if _qdrant_client is None:
        from qdrant_client import QdrantClient
        _qdrant_client = QdrantClient(url=QDRANT_URL)
    return _qdrant_client


def ensure_project_collection():
    try:
        client = _get_qdrant()
        names = [c.name for c in client.get_collections().collections]
        if QDRANT_PROJECT_COLLECTION not in names:
            from qdrant_client.models import VectorParams, Distance
            client.create_collection(
                collection_name=QDRANT_PROJECT_COLLECTION,
                vectors_config=VectorParams(size=EMBEDDING_DIM, distance=Distance.COSINE),
            )
    except Exception:
        pass


def upsert_project_embedding(point_id, embedding, payload):
    try:
        client = _get_qdrant()
        client.upsert(
            collection_name=QDRANT_PROJECT_COLLECTION,
            points=[{"id": point_id, "vector": embedding, "payload": payload}],
        )
    except Exception:
        pass


def search_project_embeddings(embedding, limit=8):
    try:
        client = _get_qdrant()
        results = client.query_points(
            collection_name=QDRANT_PROJECT_COLLECTION,
            query=embedding, limit=limit,
        )
        return [{"id": r.id, "score": r.score, "payload": r.payload} for r in results.points]
    except Exception:
        return []


def delete_project_embedding(point_id):
    try:
        client = _get_qdrant()
        client.delete(collection_name=QDRANT_PROJECT_COLLECTION, points_selector=[point_id])
    except Exception:
        pass


def project_to_dict(p):
    return {
        "id": p.id, "project_name": p.project_name,
        "customer_name": p.customer_name, "sector": p.sector,
        "equipment_category": p.equipment_category, "description": p.description,
        "year": p.year, "quantity": int(_f(p.quantity, 1)),
        "weight_kg": _f(p.weight_kg) if p.weight_kg else None,
        "complexity": p.complexity,
        "material_cost": _f(p.material_cost), "labour_cost": _f(p.labour_cost),
        "engineering_cost": _f(p.engineering_cost), "overhead_cost": _f(p.overhead_cost),
        "freight_cost": _f(p.freight_cost), "total_cost": _f(p.total_cost),
        "quoted_value": _f(p.quoted_value), "final_value": _f(p.final_value),
        "currency": p.currency, "margin_pct": _f(p.margin_pct),
        "outcome": p.outcome, "lost_reason": p.lost_reason,
        "qdrant_point_id": p.qdrant_point_id or "",
        "created_at": p.created_at.isoformat() if p.created_at else None,
    }


def _parse_projects_from_rows(rows):
    projects = []
    for row in rows:
        normalized = {}
        for k, v in row.items():
            nk = CSV_HEADER_MAP.get(k.strip().lower().replace(" ", "_").replace("-", "_"))
            if nk:
                normalized[nk] = v
        mc = _f(normalized.get("material_cost"))
        lc = _f(normalized.get("labour_cost"))
        ec = _f(normalized.get("engineering_cost"))
        oc = _f(normalized.get("overhead_cost"))
        fc = _f(normalized.get("freight_cost"))
        component_sum = mc + lc + ec + oc + fc
        final_val = _f(normalized.get("final_value"))
        margin_raw = _f(normalized.get("margin_pct"))
        if component_sum > 0:
            total_cost = component_sum
        elif final_val > 0 and margin_raw > 0:
            total_cost = final_val / (1 + margin_raw / 100)
        else:
            total_cost = 0
        if total_cost > 0 and final_val > 0 and normalized.get("margin_pct") is None:
            margin_pct = ((final_val - total_cost) / final_val) * 100
        else:
            margin_pct = margin_raw
        outcome = (normalized.get("outcome") or "won").lower().strip()
        if outcome in ("win", "won", "w", "success", "accepted", "a"):
            outcome = "won"
        elif outcome in ("loss", "lost", "l", "rejected", "r", "declined"):
            outcome = "lost"
        qty = max(1, int(_f(normalized.get("quantity"), 1)))
        year = normalized.get("year")
        try:
            year = int(float(year)) if year else None
        except (TypeError, ValueError):
            year = None
        projects.append({
            "project_name": normalized.get("project_name", "Untitled"),
            "customer_name": normalized.get("customer_name", ""),
            "sector": normalized.get("sector", ""),
            "equipment_category": normalized.get("equipment_category", ""),
            "description": normalized.get("description", ""),
            "year": year, "quantity": qty,
            "weight_kg": _f(normalized.get("weight_kg")) or None,
            "complexity": normalized.get("complexity", "medium"),
            "material_cost": mc, "labour_cost": lc, "engineering_cost": ec,
            "overhead_cost": oc, "freight_cost": fc, "total_cost": total_cost,
            "quoted_value": _f(normalized.get("quoted_value")),
            "final_value": final_val,
            "currency": normalized.get("currency", "INR"),
            "margin_pct": margin_pct, "outcome": outcome,
            "lost_reason": normalized.get("lost_reason", ""),
        })
    return projects


def parse_projects_csv(file_path):
    rows = []
    with open(file_path, "r", encoding="utf-8-sig", errors="replace") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)
    return _parse_projects_from_rows(rows)


def validate_history_rows(rows):
    """Data-quality gate (gap 2): drop duplicates, zero-cost entries, currency
    outliers, and stale records before they can skew an estimate.

    Returns (valid_rows, quality_report).
    """
    report = {"n_input": len(rows), "duplicates_removed": 0,
              "missing_cost_removed": 0, "currency_mismatch_removed": 0,
              "stale_removed": 0, "currency_used": None}
    current_year = datetime.now().year
    seen = set()
    candidates = []
    for r in rows:
        name = (r.project_name or "").strip().lower()
        cust = (r.customer_name or "").strip().lower()
        key = (name, cust)
        if key in seen:
            report["duplicates_removed"] += 1
            continue
        seen.add(key)
        if _f(r.total_cost) <= 0:
            report["missing_cost_removed"] += 1
            continue
        candidates.append(r)

    if not candidates:
        return [], report

    # Use only the dominant currency so mixed-currency data cannot distort costs.
    currencies = Counter(((r.currency or "INR").upper()) for r in candidates)
    dominant, _ = currencies.most_common(1)[0]
    report["currency_used"] = dominant

    valid = []
    for r in candidates:
        if (r.currency or "INR").upper() != dominant:
            report["currency_mismatch_removed"] += 1
            continue
        if r.year and r.year < current_year - HISTORY_STALE_YEARS:
            report["stale_removed"] += 1
            continue
        valid.append(r)
    report["n_valid"] = len(valid)
    return valid, report


async def import_projects_from_csv(file_path, db):
    ensure_project_collection()
    parsed = parse_projects_csv(file_path)
    imported = 0
    errors = []
    for p in parsed:
        if not p["project_name"] or p["project_name"] == "Untitled":
            errors.append("Row skipped: project_name missing")
            continue
        record = ProjectCost(**p)
        db.add(record)
        await db.flush()
        embed_text = f"{p['equipment_category']} {p['sector']} {p['project_name']} {p['description'][:500]}"
        try:
            from app.services.embedding import get_embedding
            emb = await get_embedding(embed_text)
            pt_id = str(uuid.uuid4())
            upsert_project_embedding(pt_id, emb, {
                "db_id": record.id, "project_name": p["project_name"],
                "category": p["equipment_category"], "sector": p["sector"],
                "outcome": p["outcome"],
            })
            record.qdrant_point_id = pt_id
        except Exception:
            pass
        imported += 1
    await db.commit()
    return {"imported": imported, "total": len(parsed), "errors": errors}


async def create_project_record(data, db):
    mc = _f(data.get("material_cost"))
    lc = _f(data.get("labour_cost"))
    ec = _f(data.get("engineering_cost"))
    oc = _f(data.get("overhead_cost"))
    fc = _f(data.get("freight_cost"))
    total = mc + lc + ec + oc + fc
    final = _f(data.get("final_value"))
    quoted = _f(data.get("quoted_value"))
    if total == 0 and final > 0:
        total = final
    margin = _f(data.get("margin_pct"))
    if total > 0 and final > 0 and margin == 0:
        margin = ((final - total) / final) * 100
    outcome = (data.get("outcome") or "won").lower()
    if outcome in ("loss", "lost", "l"):
        outcome = "lost"
    else:
        outcome = "won"
    qty = max(1, int(_f(data.get("quantity"), 1)))
    record = ProjectCost(
        project_name=data.get("project_name", "Untitled"),
        customer_name=data.get("customer_name", ""),
        sector=data.get("sector", ""),
        equipment_category=data.get("equipment_category", ""),
        description=data.get("description", ""),
        year=data.get("year"), quantity=qty,
        weight_kg=_f(data.get("weight_kg")) or None,
        complexity=data.get("complexity", "medium"),
        material_cost=mc, labour_cost=lc, engineering_cost=ec,
        overhead_cost=oc, freight_cost=fc, total_cost=total,
        quoted_value=quoted, final_value=final,
        currency=data.get("currency", "INR"),
        margin_pct=margin, outcome=outcome, lost_reason=data.get("lost_reason", ""),
    )
    db.add(record)
    await db.flush()
    embed_text = f"{record.equipment_category} {record.sector} {record.project_name} {record.description[:500]}"
    try:
        from app.services.embedding import get_embedding
        emb = await get_embedding(embed_text)
        pt_id = str(uuid.uuid4())
        upsert_project_embedding(pt_id, emb, {
            "db_id": record.id, "project_name": record.project_name,
            "category": record.equipment_category, "sector": record.sector,
            "outcome": record.outcome,
        })
        record.qdrant_point_id = pt_id
    except Exception:
        pass
    await db.commit()
    await db.refresh(record)
    return project_to_dict(record)


async def delete_project_record(project_id, db):
    record = await db.get(ProjectCost, project_id)
    if not record:
        return False
    if record.qdrant_point_id:
        delete_project_embedding(record.qdrant_point_id)
    await db.delete(record)
    await db.commit()
    return True


async def list_project_records(db, limit=200, offset=0, sector="", category="", outcome=""):
    q = select(ProjectCost)
    if sector:
        q = q.where(ProjectCost.sector == sector)
    if category:
        q = q.where(ProjectCost.equipment_category == category)
    if outcome:
        q = q.where(ProjectCost.outcome == outcome)
    q = q.order_by(ProjectCost.created_at.desc()).offset(offset).limit(limit)
    res = await db.execute(q)
    return [project_to_dict(r) for r in res.scalars().all()]


SQL_FALLBACK_WEIGHTS = {"equipment_category": 3, "sector": 2, "project_name": 1}


async def find_similar_projects(db, text, limit=6):
    results = []
    try:
        from app.services.embedding import get_embedding
        emb = await get_embedding(text)
        hits = search_project_embeddings(emb, limit)
        if hits:
            ids = [h["payload"]["db_id"] for h in hits if h["payload"].get("db_id")]
            if ids:
                res = await db.execute(select(ProjectCost).where(ProjectCost.id.in_(ids)))
                valid_rows, _q = validate_history_rows(res.scalars().all())
                db_rows = {r.id: r for r in valid_rows}
                seen = set()
                for h in hits:
                    db_id = h["payload"].get("db_id")
                    if db_id and db_id in db_rows and db_id not in seen:
                        d = project_to_dict(db_rows[db_id])
                        d["similarity_score"] = round(h["score"], 4)
                        results.append(d)
                        seen.add(db_id)
    except Exception:
        pass
    if not results:
        q = select(ProjectCost).order_by(ProjectCost.created_at.desc()).limit(200)
        res = await db.execute(q)
        all_rows, _q = validate_history_rows(res.scalars().all())
        if not all_rows:
            return []
        query_tokens = _tokenize(text)
        scored = []
        for r in all_rows:
            score = 0
            for field, weight in SQL_FALLBACK_WEIGHTS.items():
                score += len(query_tokens & _tokenize(getattr(r, field, ""))) * weight
            if score > 0:
                scored.append((score, r))
        if not scored:
            return []
        max_score = max(s for s, _ in scored)
        scored.sort(key=lambda x: -x[0])
        for score, r in scored[:limit]:
            d = project_to_dict(r)
            d["similarity_score"] = round(min(score / (max_score + 1) * 0.8, 0.85), 4)
            results.append(d)
    return results


async def _history_stats(db):
    res = await db.execute(select(ProjectCost))
    rows, quality = validate_history_rows(res.scalars().all())
    if not rows:
        return {
            "n": 0, "avg_cost_ratio": 0.62, "avg_breakdown": DEFAULT_BREAKDOWN.copy(),
            "won_margins": [], "lost_margins": [], "win_rate": 0.5,
        }
    cost_ratios = []
    breakdowns = {k: [] for k in DEFAULT_BREAKDOWN}
    won_margins = []
    lost_margins = []
    for r in rows:
        tc = _f(r.total_cost)
        fv = _f(r.final_value)
        if tc > 0 and fv > 0:
            cost_ratios.append(tc / fv)
        if tc > 0:
            for key in breakdowns:
                col = "labour_cost" if key == "labour" else f"{key}_cost"
                breakdowns[key].append(_f(getattr(r, col)) / tc)
        mp = _f(r.margin_pct)
        if r.outcome == "won":
            won_margins.append(mp)
        else:
            lost_margins.append(mp)
    avg_ratio = statistics.median(cost_ratios) if cost_ratios else 0.62
    avg_breakdown = {}
    for key, vals in breakdowns.items():
        avg_breakdown[key] = statistics.median(vals) if vals else DEFAULT_BREAKDOWN.get(key, 0.12)
    total_n = len(rows)
    decided = [r for r in rows if r.outcome in ("won", "lost")]
    won_n = sum(1 for r in decided if r.outcome == "won")
    return {
        "n": total_n, "avg_cost_ratio": avg_ratio, "avg_breakdown": avg_breakdown,
        "won_margins": won_margins, "lost_margins": lost_margins,
        "win_rate": won_n / len(decided) if decided else 0.5,
        "data_quality": quality,
    }


def predict_internal_cost(similar, stats, qty_total=1, est_weight=None, quoted_total=0):
    unit_costs = []
    cost_per_kgs = []
    breakdown_sources = []
    for p in similar:
        tc = _f(p.get("total_cost"))
        qty = max(1, int(_f(p.get("quantity"), 1)))
        if tc > 0:
            unit_costs.append(tc / qty)
            wt = _f(p.get("weight_kg")) if p.get("weight_kg") else 0
            if wt > 0:
                cost_per_kgs.append(tc / wt)
            breakdown_sources.append(p)
    predicted_unit = 0
    predicted_total = 0
    basis = "no_data"
    if est_weight and cost_per_kgs:
        median_cpk = statistics.median(cost_per_kgs)
        predicted_total = median_cpk * est_weight
        predicted_unit = predicted_total / qty_total if qty_total else 0
        basis = "weight_scaling"
    elif unit_costs:
        predicted_unit = statistics.median(unit_costs)
        predicted_total = predicted_unit * qty_total
        basis = "similar_project_scaling"
    elif stats["n"] > 0 and stats["avg_cost_ratio"] > 0 and quoted_total > 0:
        predicted_total = quoted_total * stats["avg_cost_ratio"]
        predicted_unit = predicted_total / qty_total if qty_total else 0
        basis = "global_cost_ratio"
    if breakdown_sources:
        bd = {k: [] for k in DEFAULT_BREAKDOWN}
        for p in breakdown_sources:
            tc = _f(p.get("total_cost"))
            if tc > 0:
                bd["material"].append(_f(p.get("material_cost")) / tc)
                bd["labour"].append(_f(p.get("labour_cost")) / tc)
                bd["engineering"].append(_f(p.get("engineering_cost")) / tc)
                bd["overhead"].append(_f(p.get("overhead_cost")) / tc)
                bd["freight"].append(_f(p.get("freight_cost")) / tc)
        breakdown = {k: statistics.median(v) if v else DEFAULT_BREAKDOWN[k] for k, v in bd.items()}
    else:
        breakdown = stats["avg_breakdown"].copy() if stats["n"] > 0 else DEFAULT_BREAKDOWN.copy()
    total_prop = sum(breakdown.values())
    if total_prop > 0 and abs(total_prop - 1) > 0.01:
        breakdown = {k: v / total_prop for k, v in breakdown.items()}
    cost_breakdown = {k: round(predicted_total * v, 2) for k, v in breakdown.items()}
    return {
        "predicted_total": round(predicted_total, 2),
        "predicted_unit": round(predicted_unit, 2),
        "basis": basis,
        "breakdown": cost_breakdown,
        "breakdown_pct": {k: round(v * 100, 1) for k, v in breakdown.items()},
        "n_similar_used": len(unit_costs) + len(cost_per_kgs),
    }


def calculate_risk_contingency(rfq_text="", sector="", n_similar=0, complexity="medium", equipment_category=""):
    factors = []
    score = 0
    all_terms = (rfq_text + " " + equipment_category).lower()
    for mat in RISK_MATERIALS:
        if mat in all_terms:
            score += 22
            factors.append({"factor": f"Exotic material: {mat.title()}", "points": 22})
            break
    if not factors:
        for mat in MEDIUM_MATERIALS:
            if mat in all_terms:
                score += 8
                factors.append({"factor": f"Special alloy: {mat.title()}", "points": 8})
                break
    custom_terms = ["bespoke", "custom design", "special design", "non-standard", "tailor"]
    for term in custom_terms:
        if term in all_terms:
            score += 12
            factors.append({"factor": f"Custom design: '{term}'", "points": 12})
            break
    urgency_terms = ["urgent", "expedited", "immediate", "rush", "asap", "emergency"]
    for term in urgency_terms:
        if term in all_terms:
            score += 14
            factors.append({"factor": f"Urgent delivery: '{term}'", "points": 14})
            break
    qty_match = re.search(r'(\d+)\s*(nos|units|pcs|pieces|each|set)', all_terms)
    qty_val = int(qty_match.group(1)) if qty_match else 1
    if qty_val > 50:
        score += 14
        factors.append({"factor": f"Very large quantity: {qty_val}", "points": 14})
    elif qty_val > 10:
        score += 7
        factors.append({"factor": f"Large quantity: {qty_val}", "points": 7})
    if n_similar < 2:
        score += 12
        factors.append({"factor": "Limited similar-project data", "points": 12})
    elif n_similar < 4:
        score += 5
        factors.append({"factor": "Moderate similar-project data", "points": 5})
    c_pts = {"high": 10, "medium": 3, "low": 0}.get(complexity.lower(), 3)
    if c_pts > 0:
        score += c_pts
        factors.append({"factor": f"Project complexity: {complexity.title()}", "points": c_pts})
    score = min(score, 100)
    level = "high" if score >= 65 else ("medium" if score >= 35 else "low")
    return {
        "risk_score": score, "risk_level": level,
        "contingency_pct": RISK_CONTINGENCY_MAP[level], "factors": factors,
    }


async def win_loss_analysis(db):
    res = await db.execute(select(ProjectCost))
    rows = res.scalars().all()
    if not rows:
        return {
            "overall": {"total": 0, "won": 0, "lost": 0, "win_rate": 0,
                        "total_value": 0, "won_value": 0, "lost_value": 0},
            "avg_margin_won": 0, "avg_margin_lost": 0,
            "by_margin_band": {}, "by_sector": {}, "lost_reasons": {},
            "lost_on_price_ratio": 0,
        }
    won = [r for r in rows if r.outcome == "won"]
    lost = [r for r in rows if r.outcome == "lost"]
    rows = won + lost  # exclude open live cases from win/loss analytics
    total_n = len(rows)
    total_val = sum(_f(r.quoted_value) for r in rows)
    won_val = sum(_f(r.quoted_value) for r in won)
    lost_val = sum(_f(r.quoted_value) for r in lost)
    avg_m_won = statistics.mean([_f(r.margin_pct) for r in won]) if won else 0
    avg_m_lost = statistics.mean([_f(r.margin_pct) for r in lost]) if lost else 0
    by_band = {label: {"won": 0, "lost": 0, "win_rate": 0} for _, _, label in MARGIN_BANDS}
    for r in rows:
        mp = _f(r.margin_pct)
        for lo, hi, label in MARGIN_BANDS:
            if lo <= mp < hi:
                by_band[label]["won" if r.outcome == "won" else "lost"] += 1
                break
    for label, d in by_band.items():
        t = d["won"] + d["lost"]
        d["win_rate"] = d["won"] / t if t else 0
    sector_stats = defaultdict(lambda: {"won": 0, "lost": 0})
    for r in rows:
        sector_stats[r.sector or "Unknown"][r.outcome] += 1
    by_sector = {}
    for s, d in sector_stats.items():
        t = d["won"] + d["lost"]
        by_sector[s] = {"won": d["won"], "lost": d["lost"], "win_rate": d["won"] / t if t else 0}
    lost_reasons = Counter(r.lost_reason for r in lost if r.lost_reason)
    lost_on_price = len([r for r in lost if r.lost_reason and "price" in r.lost_reason.lower()])
    return {
        "overall": {
            "total": total_n, "won": len(won), "lost": len(lost),
            "win_rate": len(won) / total_n if total_n else 0,
            "total_value": round(total_val, 2),
            "won_value": round(won_val, 2), "lost_value": round(lost_val, 2),
        },
        "avg_margin_won": round(avg_m_won, 2),
        "avg_margin_lost": round(avg_m_lost, 2),
        "by_margin_band": by_band, "by_sector": by_sector,
        "lost_reasons": dict(lost_reasons),
        "lost_on_price_ratio": lost_on_price / len(lost) if lost else 0,
    }


async def recommend_margin(db, risk_score, n_similar, sector=""):
    stats = await _history_stats(db)
    wl = await win_loss_analysis(db)
    base = statistics.mean(stats["won_margins"]) if stats["won_margins"] else DEFAULT_TARGET_MARGIN * 100
    adjustment = 0
    rationale = []
    wr = wl["overall"]["win_rate"]
    if wr >= 0.65:
        adjustment += 1.5
        rationale.append(f"Strong win rate ({wr:.0%}) - can push margin up")
    elif wr <= 0.35:
        adjustment -= 2
        rationale.append(f"Low win rate ({wr:.0%}) - sharpen pricing to compete")
    else:
        rationale.append(f"Moderate win rate ({wr:.0%})")
    lpr = wl.get("lost_on_price_ratio", 0)
    if lpr > 0.5:
        adjustment -= 1.5
        rationale.append(f"High lost-on-price ratio ({lpr:.0%}) - price sensitivity detected")
    if risk_score >= 65:
        adjustment += 3
        rationale.append("High-risk project demands higher risk premium")
    elif risk_score >= 35:
        adjustment += 1
        rationale.append("Medium risk - slight margin uplift applied")
    else:
        rationale.append("Low risk - competitive margin maintained")
    rec = base + adjustment
    floor = max(MIN_MARGIN_FLOOR * 100, 4)
    ceiling = MAX_MARGIN_CEILING * 100
    rec = max(floor, min(ceiling, rec))
    return {
        "base_margin_pct": round(base, 2),
        "recommended_margin_pct": round(rec, 2),
        "margin_floor_pct": round(floor, 2),
        "margin_ceiling_pct": round(ceiling, 2),
        "rationale": rationale,
    }


def recommend_price(cost, contingency_pct, margin_pct, discount_pct=0):
    cost_with_contingency = cost * (1 + contingency_pct / 100)
    net = cost_with_contingency / (1 - margin_pct / 100) if margin_pct < 100 else cost * 10
    net_after_discount = net * (1 - discount_pct / 100)
    band = 0.06
    return {
        "cost": round(cost, 2),
        "contingency_amount": round(cost * contingency_pct / 100, 2),
        "price_net": round(net_after_discount, 2),
        "price_min": round(net_after_discount * (1 - band), 2),
        "price_max": round(net_after_discount * (1 + band), 2),
        "margin_pct": round(margin_pct, 2),
        "contingency_pct": round(contingency_pct, 2),
        "discount_pct": round(discount_pct, 2),
    }


def calculate_confidence(similar_count, avg_similarity, cost_stdev, cost_mean, basis):
    confidence = 0.15
    drivers = []
    if similar_count >= 5:
        confidence += 0.32
        drivers.append(f"{similar_count} similar projects found (strong evidence base)")
    elif similar_count >= 3:
        confidence += 0.22
        drivers.append(f"{similar_count} similar projects found (adequate evidence)")
    elif similar_count >= 1:
        confidence += 0.10
        drivers.append(f"{similar_count} similar project found (limited evidence)")
    else:
        drivers.append("No similar projects found - using global estimates")
    if avg_similarity > 0.7:
        confidence += 0.15
        drivers.append(f"High similarity scores (avg {avg_similarity:.0%})")
    elif avg_similarity > 0.4:
        confidence += 0.08
        drivers.append(f"Moderate similarity scores (avg {avg_similarity:.0%})")
    elif avg_similarity > 0:
        drivers.append(f"Low similarity scores (avg {avg_similarity:.0%}) - weak match")
    if cost_stdev is not None and cost_mean > 0:
        cv = cost_stdev / cost_mean
        if cv > 0.5:
            confidence -= 0.12
            drivers.append("High cost variability among similar projects")
        elif cv > 0.25:
            confidence -= 0.06
            drivers.append("Moderate cost variability among similar projects")
        else:
            confidence += 0.03
            drivers.append("Low cost variability - consistent estimates")
    if basis == "weight_scaling":
        confidence += 0.06
        drivers.append("Weight-based scaling applied (higher precision)")
    elif basis == "similar_project_scaling":
        confidence += 0.03
        drivers.append("Unit-cost scaling from similar projects")
    elif basis == "global_cost_ratio":
        confidence -= 0.05
        drivers.append("Global cost-ratio estimate (lower precision)")
    confidence = max(0.05, min(0.92, confidence))
    return round(confidence, 3), drivers


def apply_what_if(baseline, overrides):
    breakdown = baseline["breakdown"].copy()
    total_cost = baseline["predicted_cost"]
    contingency_pct = baseline["risk"]["contingency_pct"]
    margin_pct = baseline["margin"]["recommended_margin_pct"]
    discount_pct = overrides.get("discount_pct", 0)
    for key in ["material", "labour", "engineering", "overhead", "freight"]:
        delta_key = f"{key}_delta_pct"
        if delta_key in overrides:
            delta = _f(overrides[delta_key])
            breakdown[key] = round(breakdown.get(key, 0) * (1 + delta / 100), 2)
    total_cost = sum(breakdown.values())
    breakdown_pct = {k: round(v / total_cost * 100, 1) if total_cost > 0 else 0 for k, v in breakdown.items()}
    if "contingency_pct" in overrides:
        contingency_pct = _f(overrides["contingency_pct"])
    if "margin_pct" in overrides:
        margin_pct = _f(overrides["margin_pct"])
    if "discount_pct" in overrides:
        discount_pct = _f(overrides["discount_pct"])
    price = recommend_price(total_cost, contingency_pct, margin_pct, discount_pct)
    baseline_price = baseline.get("price", {}).get("price_net", 0)
    return {
        "total_cost": price["cost"], "breakdown": breakdown, "breakdown_pct": breakdown_pct,
        "contingency_pct": contingency_pct, "contingency_amount": price["contingency_amount"],
        "margin_pct": margin_pct, "discount_pct": discount_pct,
        "price_net": price["price_net"], "price_min": price["price_min"], "price_max": price["price_max"],
        "delta_vs_baseline": round(price["price_net"] - baseline_price, 2),
        "overrides_applied": overrides,
    }


async def generate_estimate_narrative(payload):
    try:
        from app.services.llm import chat_completion
        compact = json.dumps({
            "rfq_summary": str(payload.get("input_snapshot", {}).get("rfq_text", ""))[:300],
            "n_similar": payload.get("similar_project_count", 0),
            "cost": payload.get("predicted_cost", 0),
            "price": payload.get("recommended_price_net", 0),
            "margin": payload.get("recommended_margin_pct", 0),
            "risk": payload.get("risk_level", "medium"),
            "confidence": payload.get("confidence", 0),
        })
        resp = await chat_completion([
            {"role": "system", "content": (
                "You are an expert estimator at ISGEC Heavy Engineering. "
                "Write a concise professional narrative (120 words max) summarizing this cost "
                "and pricing estimate: basis, key cost drivers, risks, margin rationale, and "
                "confidence level. Use Indian Rupees. Be direct and factual."
            )},
            {"role": "user", "content": compact},
        ], temperature=0.3)
        return resp.strip()[:1000]
    except Exception:
        return ""


async def run_cost_estimate(db, rfq_text="", proposal_id=None, save=True):
    proposal = None
    items = []
    qty_total = 1
    quoted_total = 0
    if proposal_id:
        proposal = await db.get(Proposal, proposal_id)
        if proposal:
            rfq_text = rfq_text or proposal.rfq_text or ""
            quoted_total = _f(proposal.total_net)
            res = await db.execute(select(ProposalItem).where(ProposalItem.proposal_id == proposal_id))
            items = res.scalars().all()
            qty_total = max(1, sum(max(1, int(_f(item.quantity, 1))) for item in items)) if items else 1
    if not rfq_text.strip():
        return {"error": "RFQ text is required", "rfq_text": ""}
    similar = await find_similar_projects(db, rfq_text)
    stats = await _history_stats(db)
    est = predict_internal_cost(similar, stats, qty_total=qty_total, quoted_total=quoted_total)

    # Item-level engineering analysis (gap 1 / recommendation 7.5)
    item_analysis = analyze_rfq_items(rfq_text)
    cost_multiplier = item_analysis["aggregate"]["cost_multiplier"]
    if est["predicted_total"] > 0 and abs(cost_multiplier - 1.0) > 0.001:
        adj = round(est["predicted_total"] * cost_multiplier, 2)
        est["adjustment"] = {
            "base_predicted_total": est["predicted_total"],
            "engineering_multiplier": cost_multiplier,
            "drivers": item_analysis["aggregate"]["adjustment_drivers"],
        }
        est["predicted_total"] = adj
        est["predicted_unit"] = round(adj / qty_total, 2) if qty_total else 0
        est["breakdown"] = {k: round(v * cost_multiplier, 2) for k, v in est["breakdown"].items()}
        if "no_data" not in est["basis"]:
            est["basis"] += "+item_engineering"

    avg_sim_score = statistics.mean([s["similarity_score"] for s in similar]) if similar else 0
    unit_costs_spread = [_f(s.get("total_cost")) / max(1, _f(s.get("quantity"), 1)) for s in similar if _f(s.get("total_cost")) > 0]
    cost_stdev = statistics.stdev(unit_costs_spread) if len(unit_costs_spread) > 1 else None
    cost_mean = statistics.mean(unit_costs_spread) if unit_costs_spread else 0
    confidence, drivers = calculate_confidence(
        similar_count=len(similar), avg_similarity=avg_sim_score,
        cost_stdev=cost_stdev, cost_mean=cost_mean, basis=est["basis"],
    )
    risk = calculate_risk_contingency(
        rfq_text=rfq_text, n_similar=len(similar),
        equipment_category=similar[0]["equipment_category"] if similar else "",
        sector=similar[0]["sector"] if similar else "",
    )
    margin = await recommend_margin(db, risk_score=risk["risk_score"], n_similar=len(similar))
    price = recommend_price(est["predicted_total"], risk["contingency_pct"], margin["recommended_margin_pct"])
    llm_narrative = await generate_estimate_narrative({
        "input_snapshot": {"rfq_text": rfq_text[:500]},
        "similar_project_count": len(similar),
        "predicted_cost": est["predicted_total"],
        "recommended_price_net": price["price_net"],
        "recommended_margin_pct": margin["recommended_margin_pct"],
        "risk_level": risk["risk_level"],
        "confidence": confidence,
    })
    proposal_cost = None
    if save:
        proposal_cost = ProposalCostEstimate(
            proposal_id=proposal_id,
            input_snapshot={
                "rfq_text": rfq_text[:500],
                "item_analysis": item_analysis,
            },
            breakdown=est["breakdown"],
            predicted_cost=est["predicted_total"],
            similar_projects=[{"id": s["id"], "name": s["project_name"], "score": s["similarity_score"]} for s in similar],
            recommended_margin_pct=margin["recommended_margin_pct"],
            margin_floor_pct=margin["margin_floor_pct"],
            margin_ceiling_pct=margin["margin_ceiling_pct"],
            risk_level=risk["risk_level"], risk_score=risk["risk_score"],
            risk_factors=risk["factors"],
            contingency_pct=risk["contingency_pct"],
            contingency_amount=price["contingency_amount"],
            recommended_price_net=price["price_net"],
            price_min=price["price_min"], price_max=price["price_max"],
            confidence=confidence, drivers=drivers,
            llm_narrative=llm_narrative,
        )
        db.add(proposal_cost)
        await db.commit()
        await db.refresh(proposal_cost)
    return {
        "estimate_id": proposal_cost.id if proposal_cost else None,
        "proposal_id": proposal_id,
        "input_snapshot": {"rfq_text": rfq_text[:500], "item_analysis": item_analysis},
        "predicted_cost": est["predicted_total"],
        "predicted_unit_cost": est["predicted_unit"],
        "cost_basis": est["basis"],
        "breakdown": est["breakdown"],
        "breakdown_pct": est["breakdown_pct"],
        "item_analysis": item_analysis,
        "similar_projects": similar,
        "similar_project_count": len(similar),
        "risk": risk, "margin": margin, "price": price,
        "confidence": confidence, "drivers": drivers,
        "llm_narrative": llm_narrative, "currency": "INR",
    }


DEMO_PROJECTS = [
    {"project_name": "Shell & Tube Heat Exchanger - HP Alkylation", "customer_name": "BPCL Refinery", "sector": "Oil & Gas", "equipment_category": "Heat Exchanger", "description": "High-pressure shell and tube heat exchanger for HP alkylation unit. Inconel 625 tubes, CS shell. Design pressure 45 bar, temp 280C.", "year": 2023, "quantity": 2, "weight_kg": 18500, "complexity": "high", "material_cost": 4200000, "labour_cost": 1350000, "engineering_cost": 890000, "overhead_cost": 620000, "freight_cost": 280000, "total_cost": 7340000, "quoted_value": 8800000, "final_value": 8800000, "margin_pct": 16.6, "outcome": "won"},
    {"project_name": "SS316 Reactor Vessel - Pharma API", "customer_name": "Dr Reddys Labs", "sector": "Pharmaceutical", "equipment_category": "Reactor", "description": "SS316L reactor vessel for API manufacturing. Electropolished internal finish Ra 0.4um. Jacketed design. 5KL capacity.", "year": 2024, "quantity": 3, "weight_kg": 8200, "complexity": "medium", "material_cost": 3100000, "labour_cost": 1100000, "engineering_cost": 650000, "overhead_cost": 480000, "freight_cost": 180000, "total_cost": 5510000, "quoted_value": 6950000, "final_value": 6950000, "margin_pct": 20.7, "outcome": "won"},
    {"project_name": "Sour Water Stripper Column", "customer_name": "IOCL Haldia", "sector": "Oil & Gas", "equipment_category": "Column", "description": "Sour water stripper column with structured packing. Cr-Mo steel. Design per ASME VIII Div 1. Height 22m, diameter 1.8m.", "year": 2023, "quantity": 1, "weight_kg": 42000, "complexity": "high", "material_cost": 5800000, "labour_cost": 1900000, "engineering_cost": 1100000, "overhead_cost": 850000, "freight_cost": 450000, "total_cost": 10100000, "quoted_value": 11800000, "final_value": 11800000, "margin_pct": 14.4, "outcome": "won"},
    {"project_name": "Ammonia Converter Internals", "customer_name": "Chambal Fertilisers", "sector": "Fertilizer", "equipment_category": "Internals", "description": "Catalyst bed support and distribution internals for ammonia converter. SS321 construction. High temp service 450C.", "year": 2022, "quantity": 1, "weight_kg": 6500, "complexity": "high", "material_cost": 2400000, "labour_cost": 850000, "engineering_cost": 720000, "overhead_cost": 380000, "freight_cost": 150000, "total_cost": 4500000, "quoted_value": 5200000, "final_value": 5200000, "margin_pct": 13.5, "outcome": "lost", "lost_reason": "technical"},
    {"project_name": "Distillation Column - SS304", "customer_name": "Tata Chemicals", "sector": "Chemical", "equipment_category": "Column", "description": "SS304 distillation column for soda ash process. Sieve tray design. Height 18m, diameter 1.2m.", "year": 2024, "quantity": 1, "weight_kg": 28000, "complexity": "medium", "material_cost": 3200000, "labour_cost": 1100000, "engineering_cost": 680000, "overhead_cost": 520000, "freight_cost": 300000, "total_cost": 5800000, "quoted_value": 6800000, "final_value": 6800000, "margin_pct": 14.7, "outcome": "won"},
    {"project_name": "HP Separator Vessel - CrMo", "customer_name": "Reliance Industries", "sector": "Petrochemical", "equipment_category": "Vessel", "description": "High pressure separator vessel. 1.25Cr-0.5Mo steel. Design pressure 120 bar. ASME VIII Div 2.", "year": 2023, "quantity": 1, "weight_kg": 35000, "complexity": "high", "material_cost": 5200000, "labour_cost": 1800000, "engineering_cost": 950000, "overhead_cost": 720000, "freight_cost": 380000, "total_cost": 9050000, "quoted_value": 10500000, "final_value": 10500000, "margin_pct": 13.8, "outcome": "won"},
    {"project_name": "CS Utility Heat Exchanger", "customer_name": "NTPC", "sector": "Power", "equipment_category": "Heat Exchanger", "description": "Carbon steel utility heat exchanger for cooling water service. TEMA BEM. Low complexity standard design.", "year": 2024, "quantity": 4, "weight_kg": 5200, "complexity": "low", "material_cost": 980000, "labour_cost": 380000, "engineering_cost": 180000, "overhead_cost": 220000, "freight_cost": 90000, "total_cost": 1850000, "quoted_value": 2050000, "final_value": 2050000, "margin_pct": 9.8, "outcome": "won"},
    {"project_name": "Hastelloy C276 Reactor", "customer_name": "Gujarat Alkalies", "sector": "Chemical", "equipment_category": "Reactor", "description": "Hastelloy C276 lined reactor for corrosive chemical service. Glass lined internals. 3KL capacity.", "year": 2022, "quantity": 1, "weight_kg": 9800, "complexity": "high", "material_cost": 6800000, "labour_cost": 2100000, "engineering_cost": 1200000, "overhead_cost": 900000, "freight_cost": 350000, "total_cost": 11350000, "quoted_value": 13200000, "final_value": 13200000, "margin_pct": 14.0, "outcome": "lost", "lost_reason": "price"},
    {"project_name": "Duplex Storage Vessel", "customer_name": "Essar Oil", "sector": "Oil & Gas", "equipment_category": "Vessel", "description": "Duplex SS 2205 storage vessel for sour service. Impact tested at -46C. NACE MR0175 compliant.", "year": 2023, "quantity": 2, "weight_kg": 22000, "complexity": "medium", "material_cost": 4100000, "labour_cost": 1400000, "engineering_cost": 780000, "overhead_cost": 580000, "freight_cost": 250000, "total_cost": 7110000, "quoted_value": 8400000, "final_value": 8400000, "margin_pct": 15.4, "outcome": "won"},
    {"project_name": "Fin Fan Air Cooler", "customer_name": "MRPL Mangalore", "sector": "Oil & Gas", "equipment_category": "Heat Exchanger", "description": "Fin fan air cooler for overhead condenser service. CS tubes with aluminum fins. 3-bay design.", "year": 2024, "quantity": 2, "weight_kg": 14000, "complexity": "medium", "material_cost": 2800000, "labour_cost": 920000, "engineering_cost": 520000, "overhead_cost": 410000, "freight_cost": 200000, "total_cost": 4850000, "quoted_value": 5600000, "final_value": 5600000, "margin_pct": 13.4, "outcome": "won"},
    {"project_name": "Reboiler - Kettle Type", "customer_name": "UPL Gujarat", "sector": "Chemical", "equipment_category": "Heat Exchanger", "description": "Kettle type reboiler for distillation column. SS304 tube bundle with CS shell. U-tube design.", "year": 2023, "quantity": 1, "weight_kg": 11500, "complexity": "medium", "material_cost": 2200000, "labour_cost": 780000, "engineering_cost": 450000, "overhead_cost": 350000, "freight_cost": 160000, "total_cost": 3940000, "quoted_value": 4600000, "final_value": 4600000, "margin_pct": 14.3, "outcome": "won"},
    {"project_name": "Deaerator Vessel", "customer_name": "BHEL", "sector": "Power", "equipment_category": "Vessel", "description": "Spray type deaerator vessel for power plant feedwater system. CS construction with SS internals. 30T/hr capacity.", "year": 2022, "quantity": 1, "weight_kg": 16000, "complexity": "low", "material_cost": 1800000, "labour_cost": 650000, "engineering_cost": 320000, "overhead_cost": 280000, "freight_cost": 120000, "total_cost": 3170000, "quoted_value": 3600000, "final_value": 3600000, "margin_pct": 12.0, "outcome": "won"},
]


async def seed_demo_projects(db):
    existing = await db.execute(select(func.count(ProjectCost.id)))
    if (existing.scalar() or 0) > 0:
        return {"message": "Historical data already exists", "seeded": 0}
    ensure_project_collection()
    seeded = 0
    for p in DEMO_PROJECTS:
        record = ProjectCost(**p)
        db.add(record)
        await db.flush()
        embed_text = f"{p['equipment_category']} {p['sector']} {p['project_name']} {p['description'][:500]}"
        try:
            from app.services.embedding import get_embedding
            emb = await get_embedding(embed_text)
            pt_id = str(uuid.uuid4())
            upsert_project_embedding(pt_id, emb, {
                "db_id": record.id, "project_name": p["project_name"],
                "category": p["equipment_category"], "sector": p["sector"],
                "outcome": p["outcome"],
            })
            record.qdrant_point_id = pt_id
        except Exception:
            pass
        seeded += 1
    await db.commit()
    return {"message": f"Seeded {seeded} demo projects", "seeded": seeded}
