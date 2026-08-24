import os
import uuid
from fastapi import APIRouter, UploadFile, File, Depends, HTTPException
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from app.models import Product, get_session
from app.services.proposal import import_products_from_csv
from app.services.search import ensure_collection, delete_product
from app.config import UPLOAD_DIR

router = APIRouter(prefix="/api/catalog", tags=["catalog"])


@router.get("/products")
async def list_products(
    search: str = "",
    category: str = "",
    limit: int = 100,
    offset: int = 0,
    db: AsyncSession = Depends(get_session),
):
    query = select(Product)
    if search:
        query = query.where(
            Product.name.ilike(f"%{search}%") | Product.sku.ilike(f"%{search}%") |
            Product.description.ilike(f"%{search}%")
        )
    if category:
        query = query.where(Product.category.ilike(f"%{category}%"))
    query = query.order_by(Product.id.desc()).offset(offset).limit(limit)
    result = await db.execute(query)
    products = result.scalars().all()
    count_result = await db.execute(select(func.count(Product.id)))
    total = count_result.scalar()
    return {
        "products": [
            {
                "id": p.id, "sku": p.sku, "name": p.name,
                "description": p.description[:200], "price_net": float(p.price_net),
                "price_gross": float(p.price_gross), "vat_rate": float(p.vat_rate),
                "currency": p.currency, "category": p.category,
                "has_embedding": bool(p.qdrant_point_id),
            }
            for p in products
        ],
        "total": total,
    }


@router.get("/products/{product_id}")
async def get_product(product_id: int, db: AsyncSession = Depends(get_session)):
    product = await db.get(Product, product_id)
    if not product:
        raise HTTPException(404, "Product not found")
    return {
        "id": product.id, "sku": product.sku, "name": product.name,
        "description": product.description, "price_net": float(product.price_net),
        "price_gross": float(product.price_gross), "vat_rate": float(product.vat_rate),
        "currency": product.currency, "category": product.category,
        "specs": product.specs, "has_embedding": bool(product.qdrant_point_id),
    }


@router.post("/upload")
async def upload_products(file: UploadFile = File(...), db: AsyncSession = Depends(get_session)):
    if not file.filename.endswith(".csv"):
        raise HTTPException(400, "Only CSV files are supported")
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    file_path = os.path.join(UPLOAD_DIR, f"{uuid.uuid4()}_{file.filename}")
    with open(file_path, "wb") as f:
        content = await file.read()
        f.write(content)
    result = await import_products_from_csv(file_path, db)
    return result


@router.delete("/products/{product_id}")
async def delete_product_by_id(product_id: int, db: AsyncSession = Depends(get_session)):
    product = await db.get(Product, product_id)
    if not product:
        raise HTTPException(404, "Product not found")
    if product.qdrant_point_id:
        try:
            delete_product(product.qdrant_point_id)
        except Exception:
            pass
    await db.delete(product)
    await db.commit()
    return {"deleted": product_id}
