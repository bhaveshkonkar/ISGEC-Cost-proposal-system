import json
from app.services.llm import chat_completion

EXTRACTION_PROMPT = """You are an AI assistant for ISGEC Heavy Engineering Ltd, a manufacturer of process equipment
(heat exchangers, pressure vessels, reactors, columns, boilers) and heavy engineering products.

Analyze this incoming email and classify it into EXACTLY ONE type:

- "rfq": a genuine request for quotation — the customer asks for pricing/quotes for products or equipment.
- "acceptance": the customer clearly ACCEPTS a quotation previously sent to them
  (e.g., "we accept your quotation", "please proceed with the order", "quotation approved", "confirming the order").
- "rejection": the customer clearly REJECTS/DECLINES a quotation previously sent to them
  (e.g., "we are not interested", "declining the offer", "we will pass on this").
- "other": ANYTHING else — greetings ("hi"), hiring/job queries, newsletters, spam, general questions,
  complaints, negotiations ("can you reduce price"), purchase orders without clear acceptance wording,
  thank-you notes, unrelated topics. When in doubt, use "other".

IMPORTANT: Return ONLY a valid JSON object, no markdown, no code blocks:
{
  "email_type": "rfq" | "acceptance" | "rejection" | "other",
  "summary": "one short sentence describing what this email says",
  "customer_name": "company or person name if mentioned, else empty string",
  "items": [
    {
      "description": "clear product description with specs mentioned",
      "quantity": 1,
      "unit": "pcs / tons / sets etc."
    }
  ],
  "deadline": "delivery deadline date if mentioned, else empty string",
  "notes": "any special requirements, terms or notes"
}

Rules:
- For non-"rfq" types, items must be an empty array.
- Extract EVERY distinct product/item requested. If quantity is not stated, use 1.
- Merge duplicate mentions of the same item into one entry.
- Do not invent items that are not requested in the email."""


async def extract_requirements(email_text: str) -> dict:
    response = await chat_completion([
        {"role": "system", "content": EXTRACTION_PROMPT},
        {"role": "user", "content": email_text[:12000]},
    ], temperature=0.1)

    cleaned = response.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[1]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
        cleaned = cleaned.strip()

    data = json.loads(cleaned)
    email_type = str(data.get("email_type", "other") or "other").strip().lower()
    if email_type not in ("rfq", "acceptance", "rejection", "other"):
        email_type = "other"
    return {
        "email_type": email_type,
        "summary": str(data.get("summary", "") or ""),
        "is_rfq": email_type == "rfq",
        "customer_name": str(data.get("customer_name", "") or ""),
        "items": [i for i in data.get("items", []) if isinstance(i, dict) and i.get("description")],
        "deadline": str(data.get("deadline", "") or ""),
        "notes": str(data.get("notes", "") or ""),
    }


CLASSIFICATION_PROMPT = """You are an AI assistant for ISGEC Heavy Engineering Ltd.

ISGEC manufactures and supplies: heat exchangers (shell & tube, finned, plate), pressure vessels,
reactors, distillation/fractionation columns, boilers and power plant equipment, sugar mill machinery,
cement plant equipment, cranes and material handling, cryogenic equipment, and related heavy process equipment.
Design codes: ASME, TEMA, PD-5500. Materials: Carbon Steel, Cr-Mo alloys, Stainless Steel, Duplex,
Inconel, Hastelloy, Titanium.

The following items were requested by a customer but were NOT found in our standard product catalog.
For each item classify:

- "on_request"  : it is the type of product ISGEC could manufacture or arrange/supply as a special order
- "not_offered" : it is completely outside what ISGEC sells (e.g., office supplies, electronics, vehicles)

IMPORTANT: Return ONLY a valid JSON array, no markdown, no code blocks:
[
  {"description": "the item description", "classification": "on_request" | "not_offered"}
]"""


async def classify_unmatched_items(items: list[dict]) -> list[dict]:
    if not items:
        return []
    items_desc = "\n".join(f"- {i.get('description', '')}" for i in items)
    response = await chat_completion([
        {"role": "system", "content": CLASSIFICATION_PROMPT},
        {"role": "user", "content": f"Items to classify:\n{items_desc}"},
    ], temperature=0.1)

    cleaned = response.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[1]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
        cleaned = cleaned.strip()

    try:
        classified = json.loads(cleaned)
    except json.JSONDecodeError:
        return [{"description": i.get("description", ""), "classification": "on_request"} for i in items]

    by_desc = {str(c.get("description", "")).strip().lower(): c for c in classified if isinstance(c, dict)}
    results = []
    for item in items:
        desc = str(item.get("description", ""))
        match = by_desc.get(desc.strip().lower())
        classification = "not_offered"
        if match and match.get("classification") in ("on_request", "not_offered"):
            classification = match["classification"]
        elif match is None:
            classification = "on_request"
        results.append({"description": desc, "classification": classification})
    return results
