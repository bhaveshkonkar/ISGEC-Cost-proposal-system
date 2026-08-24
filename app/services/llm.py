import httpx
import json
from typing import AsyncGenerator
from fastapi import HTTPException
from app.config import GROQ_API_KEY, GROQ_MODEL


async def chat_completion(messages: list[dict], temperature: float = 0.3) -> str:
    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
            json={
                "model": GROQ_MODEL,
                "messages": messages,
                "temperature": temperature,
            },
        )
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            if resp.status_code == 401 or resp.status_code == 403:
                raise HTTPException(
                    status_code=502,
                    detail="Groq authentication failed. Update GROQ_API_KEY in .env.",
                ) from exc
            raise HTTPException(
                status_code=502,
                detail=f"Groq API request failed with status {resp.status_code}.",
            ) from exc
        return resp.json()["choices"][0]["message"]["content"]


async def chat_completion_stream(messages: list[dict], temperature: float = 0.4) -> AsyncGenerator[str, None]:
    """Yield content tokens one at a time from Groq streaming API."""
    async with httpx.AsyncClient(timeout=120) as client:
        async with client.stream(
            "POST",
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
            json={
                "model": GROQ_MODEL,
                "messages": messages,
                "temperature": temperature,
                "stream": True,
            },
        ) as resp:
            if resp.status_code in (401, 403):
                raise HTTPException(status_code=502, detail="Groq authentication failed.")
            if resp.status_code != 200:
                raise HTTPException(status_code=502, detail=f"Groq streaming failed ({resp.status_code}).")
            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                payload = line[6:]
                if payload.strip() == "[DONE]":
                    break
                try:
                    chunk = json.loads(payload)
                    delta = chunk["choices"][0].get("delta", {})
                    token = delta.get("content", "")
                    if token:
                        yield token
                except (json.JSONDecodeError, KeyError, IndexError):
                    continue


async def generate_proposal_from_rfq(rfq_text: str, matched_products: list[dict], kb_context: str = "") -> dict:
    products_desc = ""
    for i, p in enumerate(matched_products, 1):
        products_desc += f"""
Product {i}:
- SKU: {p.get('sku', 'N/A')}
- Name: {p.get('name', 'N/A')}
- Description: {p.get('description', '')[:300]}
- Price (Net): {p.get('currency', 'INR')} {p.get('price_net', 0)}
- Price (Gross): {p.get('currency', 'INR')} {p.get('price_gross', 0)}
- Category: {p.get('category', '')}
"""

    system_prompt = """You are an AI assistant for ISGEC Heavy Engineering Ltd, a leading manufacturer of process equipment.

Your task is to generate a price proposal based on the customer's RFQ (Request for Quotation) and the matched products from our catalog.

IMPORTANT: Return ONLY a valid JSON object, no markdown, no code blocks. The JSON must have this exact structure:
{
  "proposal_title": "Short title for the proposal",
  "customer_summary": "Brief description of what the customer needs",
  "line_items": [
    {
      "sku": "product SKU",
      "description": "detailed product description matching the RFQ",
      "quantity": 1,
      "unit_price_net": 0.00,
      "unit_price_gross": 0.00,
      "subtotal_net": 0.00,
      "subtotal_gross": 0.00,
      "notes": "any technical notes or specifications"
    }
  ],
  "total_net": 0.00,
  "total_gross": 0.00,
  "currency": "INR",
  "terms": "Payment: 30% advance, 70% on delivery. Delivery: 12-16 weeks from order. Prices valid for 30 days. GST applicable as per government norms.",
  "notes": "Additional notes about the proposal"
}

Use the matched product prices as a reference. If the RFQ specifies quantities, calculate subtotals. Always include all matched products that are relevant to the RFQ.
Use the knowledge base context for accurate pricing, material selection, and technical specifications."""

    user_prompt = f"""Customer RFQ:
{rfq_text}

Matched Products from our catalog:
{products_desc}"""

    if kb_context:
        user_prompt += f"\n\nKnowledge Base Context (use for pricing, materials, specifications, and similar project references):\n{kb_context}"

    user_prompt += "\n\nGenerate a structured price proposal. Return ONLY valid JSON."

    response = await chat_completion([
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ], temperature=0.2)

    try:
        cleaned = response.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[1]
            if cleaned.endswith("```"):
                cleaned = cleaned[:-3]
            cleaned = cleaned.strip()
        return json.loads(cleaned)
    except json.JSONDecodeError:
        return {
            "proposal_title": "Price Proposal",
            "customer_summary": rfq_text[:200],
            "line_items": [],
            "total_net": 0,
            "total_gross": 0,
            "currency": "INR",
            "terms": "Payment: 30% advance, 70% on delivery. Delivery: 12-16 weeks.",
            "notes": f"AI response parsing failed. Raw response: {response[:500]}",
        }


async def generate_chat_response(user_message: str, context: str = "", kb_context: str = "") -> str:
    system_prompt = """You are an AI assistant for ISGEC Heavy Engineering Ltd.
You help sales teams with:
- Product recommendations for customer RFQs
- Technical specifications for process equipment (heat exchangers, reactors, vessels, columns, boilers)
- Pricing guidance based on material costs and complexity
- Understanding customer requirements from RFQ text

Be concise and professional. Use engineering terminology accurately.
ISGEC certifications: ISO 9001:2015, ASME U/U-2/S/R Stamp, CE Marking.
Design codes: ASME Section I, VIII Div 1/2/3, TEMA, PD-5500.
Materials: Carbon Steel, Cr-Mo alloys, Stainless Steel, Duplex, Inconel, Hastelloy.

Formatting rules - use markdown in your responses:
- **Bold** key terms like material grades, specs, and part numbers
- Use tables for comparisons (specs, pricing, alternatives)
- Use bullet lists for features or requirements
- Use headers (##, ###) to organize longer responses
- Use `inline code` for SKUs and part numbers
- Use blockquotes > for important notes or warnings
"""

    if context:
        system_prompt += f"\n\nRelevant catalog data:\n{context}"

    if kb_context:
        system_prompt += f"\n\nKnowledge Base references:\n{kb_context}"

    messages = [{"role": "system", "content": system_prompt}]
    messages.append({"role": "user", "content": user_message})

    return await chat_completion(messages, temperature=0.4)


async def build_chat_stream(user_message: str, context: str = "", kb_context: str = "") -> AsyncGenerator[str, None]:
    """Stream chat response tokens via async generator."""
    system_prompt = """You are an AI assistant for ISGEC Heavy Engineering Ltd.
You help sales teams with:
- Product recommendations for customer RFQs
- Technical specifications for process equipment (heat exchangers, reactors, vessels, columns, boilers)
- Pricing guidance based on material costs and complexity
- Understanding customer requirements from RFQ text

Be concise and professional. Use engineering terminology accurately.
ISGEC certifications: ISO 9001:2015, ASME U/U-2/S/R Stamp, CE Marking.
Design codes: ASME Section I, VIII Div 1/2/3, TEMA, PD-5500.
Materials: Carbon Steel, Cr-Mo alloys, Stainless Steel, Duplex, Inconel, Hastelloy.

Formatting rules - use markdown in your responses:
- **Bold** key terms like material grades, specs, and part numbers
- Use tables for comparisons (specs, pricing, alternatives)
- Use bullet lists for features or requirements
- Use headers (##, ###) to organize longer responses
- Use `inline code` for SKUs and part numbers
- Use blockquotes > for important notes or warnings"""

    if context:
        system_prompt += f"\n\nRelevant catalog data:\n{context}"
    if kb_context:
        system_prompt += f"\n\nKnowledge Base references:\n{kb_context}"

    messages = [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_message}]
    async for token in chat_completion_stream(messages, temperature=0.4):
        yield token
