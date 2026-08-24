import uuid
from datetime import datetime, timedelta
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.models import Product, Proposal, ProposalItem, Customer
from app.services.embedding import get_embedding
from app.services.search import upsert_product, search_products, ensure_collection
from app.services.kb import search_kb
from app.services.llm import generate_proposal_from_rfq
from app.services.document import parse_product_csv, parse_customer_csv, parse_document


async def import_products_from_csv(file_path: str, db: AsyncSession) -> dict:
    ensure_collection()
    products = parse_product_csv(file_path)
    imported = 0
    errors = []
    for p in products:
        existing = await db.execute(select(Product).where(Product.sku == p["sku"]))
        if existing.scalar_one_or_none():
            errors.append(f"SKU {p['sku']} already exists, skipped")
            continue
        product = Product(**p)
        db.add(product)
        await db.flush()
        embedding_text = f"{p['sku']} {p['name']} {p['description']} {p.get('category', '')}"
        try:
            embedding = await get_embedding(embedding_text)
            point_id = str(uuid.uuid4())
            upsert_product(point_id, embedding, {
                "db_id": product.id,
                "sku": p["sku"],
                "name": p["name"],
                "description": p["description"][:500],
                "category": p.get("category", ""),
                "price_net": p["price_net"],
                "currency": p.get("currency", "INR"),
            })
            product.qdrant_point_id = point_id
            imported += 1
        except Exception as e:
            errors.append(f"Embedding failed for {p['sku']}: {str(e)}")
    await db.commit()
    return {"imported": imported, "errors": errors, "total": len(products)}


async def import_customers_from_csv(file_path: str, db: AsyncSession) -> dict:
    customers = parse_customer_csv(file_path)
    imported = 0
    for c in customers:
        customer = Customer(name=c["name"], source=c.get("source", ""))
        db.add(customer)
        imported += 1
    await db.commit()
    return {"imported": imported, "total": len(customers)}


async def process_rfq(rfq_text: str, db: AsyncSession) -> dict:
    ensure_collection()
    embedding = await get_embedding(rfq_text)
    results = search_products(embedding, limit=5)

    matched_products = []
    for r in results:
        payload = r["payload"]
        db_id = payload.get("db_id")
        if db_id:
            product = await db.get(Product, db_id)
            if product:
                matched_products.append({
                    "sku": product.sku,
                    "name": product.name,
                    "description": product.description,
                    "price_net": float(product.price_net),
                    "price_gross": float(product.price_gross),
                    "currency": product.currency,
                    "category": product.category,
                    "score": r["score"],
                })
        else:
            matched_products.append(payload)

    kb_context = ""
    try:
        kb_results = await search_kb(rfq_text, limit=5)
        if kb_results:
            kb_parts = []
            for kr in kb_results:
                kb_parts.append(f"[{kr['title']} - {kr['space']}]\n{kr['body_chunk']}")
            kb_context = "\n\n".join(kb_parts)
    except Exception:
        pass

    proposal_data = await generate_proposal_from_rfq(rfq_text, matched_products, kb_context=kb_context)

    proposal = Proposal(
        proposal_number=f"ISGEC-{datetime.now().strftime('%Y%m%d')}-{str(uuid.uuid4())[:6].upper()}",
        rfq_text=rfq_text,
        rfq_source="text",
        status="draft",
        currency=proposal_data.get("currency", "INR"),
        total_net=proposal_data.get("total_net", 0),
        total_gross=proposal_data.get("total_gross", 0),
        notes=proposal_data.get("notes", ""),
        valid_until=datetime.now().date() + timedelta(days=30),
    )
    db.add(proposal)
    await db.flush()

    for item in proposal_data.get("line_items", []):
        product_id = None
        if item.get("sku"):
            result = await db.execute(select(Product).where(Product.sku == item["sku"]))
            prod = result.scalar_one_or_none()
            if prod:
                product_id = prod.id

        proposal_item = ProposalItem(
            proposal_id=proposal.id,
            product_id=product_id,
            sku=item.get("sku", ""),
            description=item.get("description", ""),
            quantity=item.get("quantity", 1),
            unit_price_net=item.get("unit_price_net", 0),
            unit_price_gross=item.get("unit_price_gross", 0),
            subtotal_net=item.get("subtotal_net", 0),
            subtotal_gross=item.get("subtotal_gross", 0),
            notes=item.get("notes", ""),
        )
        db.add(proposal_item)

    await db.commit()
    await db.refresh(proposal)

    return {
        "proposal_id": proposal.id,
        "proposal_number": proposal.proposal_number,
        "proposal_data": proposal_data,
        "matched_products": matched_products,
    }
