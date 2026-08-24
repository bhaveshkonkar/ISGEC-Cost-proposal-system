r"""
Generate pseudo-historical project records from the ISGEC product catalog.

Reads all Product rows from PostgreSQL, synthesizes realistic historical project
variations (different years, customers, outcomes), writes a CSV file, and imports
the records into the ProjectCost table + Qdrant project_costs collection.

Usage:
    .venv\Scripts\python.exe scripts\generate_history_from_catalog.py

Requirements: app must be importable, Postgres running, Ollama running (for embeddings).
If Ollama is down the import still succeeds but Qdrant embeddings are skipped (SQL fallback).
"""

import asyncio
import csv
import os
import random
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from app.models import Product, async_session
from sqlalchemy import select, func

# ─── Mapping tables ──────────────────────────────────────────────────────────

CATEGORY_MAP = {
    "PED-HX-": "Heat Exchanger",
    "PED-RV-": "Reactor",
    "PED-PV-": "Vessel",
    "PED-CT-": "Column",
    "PED-BD-": "Boiler Drum",
    "PED-WHB-": "Waste Heat Recovery Boiler",
    "PED-PGB-": "Waste Heat Recovery Boiler",
    "PED-CD-": "Coke Drum",
}

COMPLEXITY_KEYWORDS = {
    "high": ["inconel", "incoloy", "duplex", "clad", "cr-mo", "cr mo",
             "2.25cr", "1.25cr", "super duplex", "alloy steel", "hastelloy",
             "titanium", "q&t", "quench", "special grade", "high pressure"],
    "medium": ["stainless", "ss304", "ss316", "ss321", "ss316l", "304l",
               "316l", "structured packing", "fin-type", "helix"],
}

CUSTOMERS = [
    ("IOCL Haldia", "Oil & Gas"),
    ("IOCL Barauni", "Oil & Gas"),
    ("BPCL Kochi", "Oil & Gas"),
    ("BPCL Mumbai", "Oil & Gas"),
    ("HPCL Visakhapatnam", "Oil & Gas"),
    ("Reliance Industries", "Petrochemical"),
    ("Reliance Jamnagar", "Petrochemical"),
    ("Tata Chemicals", "Chemical"),
    ("Tata Steel", "Chemical"),
    ("NTPC", "Power"),
    ("NTPC Kudgi", "Power"),
    ("BHEL Bhopal", "Power"),
    ("Chambal Fertilisers", "Fertilizer"),
    ("Gujarat State Fertilizers", "Fertilizer"),
    ("NFL Bathinda", "Fertilizer"),
    ("Chambal Fertilisers", "Fertilizer"),
    ("UPL Gujarat", "Chemical"),
    ("Gujarat Alkalies", "Chemical"),
    ("Tata Chemicals", "Chemical"),
    ("MRPL Mangalore", "Refinery"),
    ("GAIL Pata", "Petrochemical"),
    ("GAIL Vijaipur", "Petrochemical"),
    ("Essar Oil", "Oil & Gas"),
    ("Reliance Industries", "Petrochemical"),
    ("Hindustan Zinc", "Chemical"),
    ("Dr Reddys Labs", "Pharmaceutical"),
    ("Cipla Ltd", "Pharmaceutical"),
    ("Sun Pharma", "Pharmaceutical"),
    ("BASF India", "Chemical"),
    ("Adani Hazira", "Petrochemical"),
    ("JSW Steel", "Chemical"),
    ("SAIL Bokaro", "Power"),
]

DESCRIPTION_TEMPLATES = {
    "Heat Exchanger": [
        "Shell and tube heat exchanger for {sector} service. {mat} construction. Design per TEMA {tema}.",
        "High pressure heat exchanger for {sector}. {mat} tubes, CS shell. {pressure} bar design.",
        "Double pipe heat exchanger bundle for {sector}. {mat} construction.",
    ],
    "Reactor": [
        "{mat} process reactor for {sector}. Design pressure {pressure} bar. ASME VIII Div 1.",
        "Batch reactor with jacket cooling for {sector}. {mat} construction. {cap} capacity.",
        "Continuous stirred reactor for {sector}. {mat} with glass-lined internals.",
    ],
    "Vessel": [
        "{mat} pressure vessel for {sector}. Design pressure {pressure} bar. ASME VIII Div 1.",
        "Vertical storage vessel for {sector}. {mat} construction. Impact tested at -46C.",
        "High pressure separator vessel for {sector}. {mat} with demister pad. ASME VIII Div 2.",
    ],
    "Column": [
        "Distillation column for {sector}. {mat} with structured packing. Height {ht}m.",
        "Absorption column for {sector}. {mat} internals. {mat} shell.",
        "Stripping column for {sector}. {mat} construction. Sieve tray design.",
    ],
    "Boiler Drum": [
        "High pressure boiler drum for {sector}. {mat} construction. {pressure} bar MAWP.",
        "Steam drum package for {sector}. {mat} with internals. Hydrostatically tested.",
    ],
    "Waste Heat Recovery Boiler": [
        "Waste heat recovery boiler for {sector}. {mat} construction. Captures waste heat from process gas.",
        "Heat recovery steam generator for {sector}. {mat} tubes. Natural circulation design.",
    ],
    "Coke Drum": [
        "Coke drum for {sector}. {mat} with special thermal cycling design. Heavy wall construction.",
        "Delayed coker drum for {sector}. {mat} with clad internals. Impact tested.",
    ],
}

MATERIALS = {
    "Carbon Steel": ["carbon steel", "CS", "SA-516 Gr 70"],
    "Low Alloy Steel": ["1.25Cr-0.5Mo", "2.25Cr-1Mo", "Cr-Mo steel", "low alloy steel"],
    "SS 304/316": ["SS 304", "SS 316", "SS 316L", "SS 321", "stainless steel 316L", "SS 304L"],
    "Duplex SS": ["Duplex SS 2205", "super duplex 2507", "duplex stainless steel"],
    "Clad": ["Inconel 625 clad", "SS 321 clad", "Incoloy 825 clad"],
    "Inconel": ["Inconel 625", "Inconel 825", "Incoloy"],
}

TEMA_TYPES = ["BEM", "AES", "BEU", "BKU"]
PRESSURES = [15, 25, 40, 60, 85, 100, 120, 150, 200]
CAPACITIES = ["2KL", "3KL", "5KL", "8KL", "10KL", "15KL", "20KL"]
HEIGHTS = [12, 15, 18, 22, 25, 28, 32, 40]

CSV_HEADER = [
    "project_name", "customer_name", "sector", "equipment_category",
    "description", "year", "quantity", "weight_kg", "complexity",
    "material_cost", "labour_cost", "engineering_cost", "overhead_cost",
    "freight_cost", "total_cost", "quoted_value", "final_value",
    "currency", "margin_pct", "outcome", "lost_reason",
]


def classify_complexity(description):
    desc_lower = description.lower()
    for level in ("high", "medium"):
        for kw in COMPLEXITY_KEYWORDS[level]:
            if kw in desc_lower:
                return level
    return "low"


def classify_sector(product_name, description):
    text = (product_name + " " + description).lower()
    if any(k in text for k in ("ammonia", "fertilizer", "urea", "nitrogen")):
        return "Fertilizer"
    if any(k in text for k in ("power plant", "boiler", "steam drum", "feedwater")):
        return "Power"
    if any(k in text for k in ("coke", "refinery", "coker")):
        return "Oil & Gas"
    if any(k in text for k in ("petrochemical", "ethylene", "polymer")):
        return "Petrochemical"
    if any(k in text for k in ("pharma", "api")):
        return "Pharmaceutical"
    return "Chemical"


def generate_cost_breakdown(total_cost, complexity):
    spread = random.random
    if complexity == "high":
        mat_pct = 0.52 + spread() * 0.06
        lab_pct = 0.16 + spread() * 0.03
        eng_pct = 0.10 + spread() * 0.02
        oh_pct = 0.11 + spread() * 0.02
    elif complexity == "medium":
        mat_pct = 0.54 + spread() * 0.04
        lab_pct = 0.17 + spread() * 0.02
        eng_pct = 0.10 + spread() * 0.02
        oh_pct = 0.11 + spread() * 0.02
    else:
        mat_pct = 0.55 + spread() * 0.05
        lab_pct = 0.18 + spread() * 0.02
        eng_pct = 0.09 + spread() * 0.01
        oh_pct = 0.12 + spread() * 0.01
    fr_pct = max(0.04, 1.0 - mat_pct - lab_pct - eng_pct - oh_pct)
    total_pct = mat_pct + lab_pct + eng_pct + oh_pct + fr_pct
    mat_pct /= total_pct
    lab_pct /= total_pct
    eng_pct /= total_pct
    oh_pct /= total_pct
    fr_pct /= total_pct
    return {
        "material_cost": round(total_cost * mat_pct, 2),
        "labour_cost": round(total_cost * lab_pct, 2),
        "engineering_cost": round(total_cost * eng_pct, 2),
        "overhead_cost": round(total_cost * oh_pct, 2),
        "freight_cost": round(total_cost * fr_pct, 2),
    }


def estimate_weight(total_cost, complexity):
    cost_per_kg = {"high": 750, "medium": 650, "low": 580}
    base = cost_per_kg.get(complexity, 650)
    jitter = base * (0.9 + random.random() * 0.2)
    return round(total_cost / jitter, 2) if jitter > 0 else 0


def generate_variants(product):
    rows = []
    num_variants = random.choice([2, 2, 3])
    likely_sectors = [classify_sector(product.name, product.description or "")]
    for _ in range(num_variants - 1):
        likely_sectors.append(random.choice(["Chemical", "Oil & Gas", "Petrochemical", "Power"]))

    for i in range(num_variants):
        sector = likely_sectors[i]
        customer_name = random.choice(CUSTOMERS)[0]
        year = random.randint(2021, 2025)
        quantity = random.choice([1, 1, 1, 2, 2, 3])
        is_won = random.random() < 0.82
        outcome = "won" if is_won else "lost"
        lost_reason = "" if is_won else random.choice(["price", "price", "technical"])

        cost_ratio = 0.60 + random.random() * 0.12
        total_cost = round(float(product.price_net) * cost_ratio, 2)
        breakdown = generate_cost_breakdown(total_cost, classify_complexity(product.description or ""))
        material = breakdown["material_cost"]
        labour = breakdown["labour_cost"]
        engineering = breakdown["engineering_cost"]
        overhead = breakdown["overhead_cost"]
        freight = breakdown["freight_cost"]

        margin_pct = round(10.5 + random.random() * 10, 2)
        if not is_won:
            margin_pct = round(12 + random.random() * 8, 2)
        quoted_value = round(total_cost / (1 - margin_pct / 100), 2)
        final_value = quoted_value if is_won else quoted_value

        weight = estimate_weight(total_cost, classify_complexity(product.description or ""))

        mat_name = random.choice(list(MATERIALS.keys()))
        mat_sample = random.choice(MATERIALS[mat_name])
        tema = random.choice(TEMA_TYPES)
        pressure = random.choice(PRESSURES)
        capacity = random.choice(CAPACITIES)
        height = random.choice(HEIGHTS)

        cat = "Vessel"
        for prefix, mapped in CATEGORY_MAP.items():
            if product.sku.startswith(prefix):
                cat = mapped
                break

        templates = DESCRIPTION_TEMPLATES.get(cat, DESCRIPTION_TEMPLATES["Vessel"])
        template = random.choice(templates)
        description = template.format(
            sector=sector.lower(),
            mat=mat_sample,
            tema=tema,
            pressure=pressure,
            cap=capacity,
            ht=height,
        )

        proj_name = f"{product.name} - {customer_name} {year}"
        if quantity > 1:
            proj_name += f" ({quantity} nos)"

        rows.append({
            "project_name": proj_name,
            "customer_name": customer_name,
            "sector": sector,
            "equipment_category": cat,
            "description": description,
            "year": year,
            "quantity": quantity,
            "weight_kg": weight,
            "complexity": classify_complexity(product.description or ""),
            "material_cost": material,
            "labour_cost": labour,
            "engineering_cost": engineering,
            "overhead_cost": overhead,
            "freight_cost": freight,
            "total_cost": total_cost,
            "quoted_value": quoted_value,
            "final_value": final_value,
            "currency": product.currency,
            "margin_pct": margin_pct,
            "outcome": outcome,
            "lost_reason": lost_reason,
        })

    return rows


async def main():
    random.seed(42)

    print("=" * 70)
    print("  ISGEC Catalog -> Historical Project Generator")
    print("=" * 70)

    async with async_session() as db:
        total_products = await db.scalar(select(func.count(Product.id)))
        print(f"\n  Products in catalog: {total_products}")
        if total_products == 0:
            print("  No products found. Upload a product catalog first.")
            return

        result = await db.execute(select(Product).order_by(Product.id))
        products = result.scalars().all()

        all_rows = []
        for p in products:
            variants = generate_variants(p)
            all_rows.extend(variants)

        print(f"  Generated {len(all_rows)} pseudo-historical project records")

        csv_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "historical_projects_catalog.csv")
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_HEADER)
            writer.writeheader()
            writer.writerows(all_rows)
        print(f"  CSV written to: {csv_path}")

        won_count = sum(1 for r in all_rows if r["outcome"] == "won")
        lost_count = len(all_rows) - won_count
        avg_margin = statistics.mean([r["margin_pct"] for r in all_rows])
        total_value = sum(r["quoted_value"] for r in all_rows)
        print(f"\n  Summary:")
        print(f"    Won:   {won_count}")
        print(f"    Lost:  {lost_count}")
        print(f"    Avg margin: {avg_margin:.1f}%")
        print(f"    Total quoted value: INR {total_value:,.2f}")

        from app.services.costing import import_projects_from_csv
        print(f"\n  Importing into ProjectCost + Qdrant project_costs...")
        import_result = await import_projects_from_csv(csv_path, db)
        created = import_result.get("imported", 0)
        errors = import_result.get("errors", [])
        print(f"    Created: {created} records")
        if errors:
            print(f"    Errors: {len(errors)}")
            for err in errors[:5]:
                print(f"      - {err}")

        from app.services.costing import _history_stats
        stats = await _history_stats(db)
        print(f"\n  History stats after import:")
        print(f"    n_projects: {stats['n']}")
        print(f"    avg_cost_ratio: {stats['avg_cost_ratio']:.3f}")
        print(f"    win_rate: {stats['win_rate']:.1%}")
        won_m = stats['won_margins']
        lost_m = stats['lost_margins']
        print(f"    avg_won_margin: {statistics.mean(won_m):.1f}%" if won_m else "    avg_won_margin: n/a")
        print(f"    avg_lost_margin: {statistics.mean(lost_m):.1f}%" if lost_m else "    avg_lost_margin: n/a")

        print(f"\n  Running verification estimates...")
        from app.services.costing import predict_internal_cost, calculate_risk_contingency, recommend_margin, recommend_price, calculate_confidence, find_similar_projects, _history_stats

        test_rfqs = [
            ("Heat Exchanger RFQ",
             "Supply 2 nos shell and tube heat exchanger TEMA BEM type, carbon steel shell, "
             "SS 304 tubes. Design pressure 25 bar, temperature 280C. For refinery service."),
            ("Reactor RFQ",
             "Supply 1 nos duplex stainless steel 2205 reactor vessel, 5KL capacity, "
             "jacketed design. For corrosive chemical service. ASME VIII Div 1."),
        ]

        for name, rfq in test_rfqs:
            print(f"\n  --- {name} ---")
            similar = await find_similar_projects(rfq, limit=5)
            print(f"    Similar projects found: {len(similar)}")
            for s in similar[:3]:
                print(f"      - {s.get('project_name', '?')} (score: {s.get('similarity_score', '?')})")

            est_stats = await _history_stats(db)
            prediction = predict_internal_cost(similar, est_stats, qty_total=2)
            risk = calculate_risk_contingency(rfq_text=rfq, n_similar=len(similar))
            margin = recommend_margin(all_rows)
            price = recommend_price(prediction["predicted_total"], risk["contingency_pct"], margin["recommended_margin_pct"])
            conf, drivers = calculate_confidence(len(similar), similar[0].get("similarity_score", 0) if similar else 0,
                                                  prediction["predicted_total"], prediction["predicted_total"] * 1.1, prediction["basis"])

            print(f"    Predicted cost: INR {prediction['predicted_total']:,.2f}")
            print(f"    Basis: {prediction['basis']}")
            print(f"    Risk: {risk['risk_level']} ({risk['risk_score']} pts) contingency={risk['contingency_pct']}%")
            print(f"    Margin: {margin['recommended_margin_pct']:.1f}%")
            print(f"    Recommended price: INR {price['price_net']:,.2f}")
            print(f"    Confidence: {conf:.0%}")
            print(f"    Drivers: {' | '.join(drivers[:3])}")

            if prediction["predicted_total"] == 0:
                print(f"    WARNING: predicted cost is ZERO - check data import!")
            else:
                print(f"    OK: non-zero prediction verified")

        print(f"\n  Done. Restart the app to see new data.")
        print(f"  POST /api/costing/estimate with RFQ text to test via API.")
        print("=" * 70)


if __name__ == "__main__":
    asyncio.run(main())
