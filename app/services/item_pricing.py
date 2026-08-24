"""Item-level engineering-aware pricing signals (recommendation 7.5 / gap 1).

Parses RFQ text for per-item engineering parameters - material class, design
pressure/temperature, weight, thickness, custom vs standard fabrication -
and derives a complexity score plus a cost adjustment multiplier that is
applied on top of the historical-similarity estimate.
"""
import re

EXOTIC_MATERIALS = [
    "inconel", "incoloy", "hastelloy", "monel", "titanium", "zirconium",
    "tantalum", "nickel alloy", "super duplex", "alloy 20", "cupro nickel",
]
ALLOY_MATERIALS = [
    "stainless", "ss304", "ss316", "ss321", "ss347", "duplex", "2205",
    "cr-mo", "chrome moly", "1.25cr", "2.25cr", "low temp carbon", "ltcs",
    "clad", "lined", "glass lined", "nace",
]
CUSTOM_TERMS = [
    "custom", "bespoke", "special design", "non-standard", "tailor-made",
    "tailored", "as per drawing", "per our specification", "client spec",
    "specific design", "engineered to order",
]
STANDARD_TERMS = [
    "standard", "catalog", " catalogue", "tema bem", "regular", "off-the-shelf",
]
CODE_STANDARDS = ["asme", "api", "tema", "en 13445", "is 2825", "ped"]

_PRESSURE_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(?:bar\b|barg\b|kg/cm2|kg/cm²|mpa|psi)",
    re.I,
)
_TEMP_RE = re.compile(r"(-?\d+(?:\.\d+)?)\s*(?:deg\s*c|°c|degrees?\s*celsius|c\b)", re.I)
_WEIGHT_RE = re.compile(r"(\d+(?:,\d{3})*(?:\.\d+)?)\s*(kg|kgs|tonne|tonnes|tons?\b|mt\b|t\b)", re.I)
_THICKNESS_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:mm)\s*(?:thk|thick|thickness)?", re.I)
_QTY_RE = re.compile(r"(\d+)\s*(nos|units|pcs|pieces|each|set|sets)", re.I)


def _num(s: str) -> float:
    return float(s.replace(",", ""))


def _classify_material(text: str) -> tuple[str, str | None]:
    low = text.lower()
    for mat in EXOTIC_MATERIALS:
        if mat in low:
            return "exotic", mat
    for mat in ALLOY_MATERIALS:
        if mat in low:
            return "alloy", mat
    if re.search(r"\b(?:cs|carbon steel|mild steel|ms)\b", low):
        return "carbon_steel", "carbon steel"
    return "unknown", None


def _pressure_to_bar(value: float, unit: str) -> float:
    u = unit.lower()
    if "mpa" in u:
        return value * 10.0
    if "psi" in u:
        return value * 0.0689476
    return value  # bar / kg/cm2 ~ bar


def analyze_item(text: str) -> dict:
    """Extract engineering parameters and a complexity score for one item."""
    low = (text or "").lower()
    material_class, material_found = _classify_material(low)

    pressure_bar = 0.0
    pm = _PRESSURE_RE.search(text)
    if pm:
        pressure_bar = round(_pressure_to_bar(_num(pm.group(1)), pm.group(0).split()[-1]), 1)

    temp_c = None
    tm = _TEMP_RE.search(text)
    if tm:
        try:
            temp_c = float(tm.group(1))
        except ValueError:
            temp_c = None

    weight_kg = None
    wm = _WEIGHT_RE.search(text or "")
    if wm:
        val = _num(wm.group(1))
        unit = wm.group(2).lower()
        if unit.startswith("ton") or unit == "mt" or unit == "t":
            val *= 1000
        weight_kg = val

    thickness_mm = None
    thm = _THICKNESS_RE.search(text)
    if thm:
        t = float(thm.group(1))
        if 1 <= t <= 300:
            thickness_mm = t

    qty = 1
    qm = _QTY_RE.search(low)
    if qm:
        qty = max(1, int(qm.group(1)))

    fabrication = "standard"
    if any(t in low for t in CUSTOM_TERMS):
        fabrication = "custom"
    elif any(t in low for t in STANDARD_TERMS):
        fabrication = "standard"

    codes = [c.upper() for c in CODE_STANDARDS if c in low]

    # Complexity score 0-100 from weighted engineering signals
    score = 0
    drivers = []
    if material_class == "exotic":
        score += 35; drivers.append(f"exotic material ({material_found})")
    elif material_class == "alloy":
        score += 18; drivers.append(f"alloy material ({material_found})")
    elif material_class == "carbon_steel":
        score += 4; drivers.append("carbon steel")
    else:
        score += 8; drivers.append("material unspecified")

    if pressure_bar >= 100:
        score += 25; drivers.append(f"very high design pressure {pressure_bar} bar")
    elif pressure_bar >= 40:
        score += 15; drivers.append(f"high design pressure {pressure_bar} bar")
    elif pressure_bar >= 16:
        score += 7; drivers.append(f"medium design pressure {pressure_bar} bar")

    if temp_c is not None:
        at = abs(temp_c)
        if at >= 450 or at <= -30:
            score += 18; drivers.append(f"extreme design temperature {temp_c}C")
        elif at >= 250:
            score += 9; drivers.append(f"elevated design temperature {temp_c}C")

    if thickness_mm is not None:
        if thickness_mm >= 60:
            score += 14; drivers.append(f"heavy wall {thickness_mm} mm")
        elif thickness_mm >= 25:
            score += 7; drivers.append(f"thick wall {thickness_mm} mm")

    if fabrication == "custom":
        score += 15; drivers.append("custom fabrication")
    if codes:
        score += min(3 * len(codes), 9); drivers.append(f"code design ({', '.join(codes)})")
    if weight_kg and weight_kg > 20000:
        score += 6; drivers.append(f"heavy equipment {weight_kg:.0f} kg")

    complexity_score = min(score, 100)
    band = "high" if complexity_score >= 55 else ("medium" if complexity_score >= 25 else "low")

    return {
        "description_snippet": (text or "").strip()[:200],
        "material_class": material_class,
        "material": material_found,
        "design_pressure_bar": pressure_bar or None,
        "design_temp_c": temp_c,
        "weight_kg": weight_kg,
        "thickness_mm": thickness_mm,
        "quantity": qty,
        "fabrication": fabrication,
        "code_standards": codes,
        "complexity_score": complexity_score,
        "complexity_band": band,
        "complexity_drivers": drivers,
    }


def split_rfq_items(rfq_text: str) -> list[str]:
    """Best-effort split of an RFQ into per-item chunks (numbered/bulleted lines)."""
    lines = [ln.strip() for ln in (rfq_text or "").splitlines()]
    chunks = []
    current = []
    for ln in lines:
        if re.match(r"^(?:\d+[.)\-]|[-*•])\s+\S+", ln) and current:
            chunks.append(" ".join(current))
            current = [re.sub(r"^(?:\d+[.)\-]|[-*•])\s+", "", ln)]
        elif ln.strip():
            current.append(ln)
    if current:
        chunks.append(" ".join(current))
    # Fall back to whole text when no list structure was found
    if len(chunks) <= 1:
        return [rfq_text] if rfq_text and rfq_text.strip() else []
    return [c for c in chunks if c.strip()]


def analyze_rfq_items(rfq_text: str) -> dict:
    """Analyze all items in an RFQ and produce an aggregate cost adjustment.

    Returns {"items": [...], "aggregate": {...}} where aggregate contains the
    cost_multiplier to apply on top of the similarity-based prediction.
    """
    texts = split_rfq_items(rfq_text)
    items = [analyze_item(t) for t in texts]
    if not items:
        items = []

    multiplier = 1.0
    adj_drivers = []
    for it in items:
        m = 1.0
        if it["material_class"] == "exotic":
            m += 0.20
        elif it["material_class"] == "alloy":
            m += 0.08
        if it["design_pressure_bar"] and it["design_pressure_bar"] >= 40:
            m += 0.06
        if it["design_temp_c"] is not None and (abs(it["design_temp_c"]) >= 400):
            m += 0.05
        if it["fabrication"] == "custom":
            m += 0.07
        m += (it["complexity_score"] / 100) * 0.10
        it["item_multiplier"] = round(m, 3)
        multiplier = max(multiplier, m) if len(items) > 1 else multiplier + (m - 1)

    if len(items) > 1:
        multiplier = 1.0 + sum(m - 1 for m in (it["item_multiplier"] for it in items)) / len(items)
    multiplier = round(min(max(multiplier, 0.85), 2.0), 3)

    if multiplier > 1.01:
        adj_drivers.append(f"engineering parameters raise cost estimate by {(multiplier - 1) * 100:.0f}%")
    elif multiplier < 0.99:
        adj_drivers.append(f"standard/simple build lowers cost estimate by {(1 - multiplier) * 100:.0f}%")

    bands = [it["complexity_band"] for it in items]
    agg_band = "high" if "high" in bands else ("medium" if "medium" in bands else ("low" if bands else ""))
    return {
        "items": items,
        "aggregate": {
            "n_items_analyzed": len(items),
            "cost_multiplier": multiplier,
            "complexity_band": agg_band,
            "adjustment_drivers": adj_drivers,
        },
    }
