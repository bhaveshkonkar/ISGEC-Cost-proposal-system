import os
import json
import httpx
import pandas as pd
from pathlib import Path


DOCLING_URL = "http://localhost:5001"


def parse_csv_upload(file_path: str) -> list[dict]:
    df = pd.read_csv(file_path)
    df.columns = [c.strip().lower().replace(" ", "_") for c in df.columns]
    records = []
    for _, row in df.iterrows():
        record = {}
        for col in df.columns:
            val = row[col]
            if pd.isna(val):
                val = ""
            record[col] = val
        records.append(record)
    return records


def parse_product_csv(file_path: str) -> list[dict]:
    df = pd.read_csv(file_path)
    df.columns = [c.strip().lower().replace(" ", "_") for c in df.columns]
    col_map = {}
    for col in df.columns:
        if col in ("sku", "id", "kod", "index", "symbol"):
            col_map["sku"] = col
        elif col in ("name", "product", "produkt"):
            col_map["name"] = col
        elif col in ("description", "opis", "summary"):
            col_map["description"] = col
        elif col in ("price_net", "cena_netto", "net_price", "netto"):
            col_map["price_net"] = col
        elif col in ("price_gross", "cena_brutto", "gross_price", "brutto"):
            col_map["price_gross"] = col
        elif col in ("vat_rate", "vat", "stawka_vat", "tax_rate"):
            col_map["vat_rate"] = col
        elif col in ("currency", "waluta", "ccy"):
            col_map["currency"] = col
        elif col in ("category", "kategoria", "type"):
            col_map["category"] = col

    products = []
    for _, row in df.iterrows():
        product = {
            "sku": str(row.get(col_map.get("sku", ""), "")).strip(),
            "name": str(row.get(col_map.get("name", ""), "")).strip(),
            "description": str(row.get(col_map.get("description", ""), "")).strip(),
            "price_net": float(row.get(col_map.get("price_net", ""), 0) or 0),
            "price_gross": float(row.get(col_map.get("price_gross", ""), 0) or 0),
            "vat_rate": float(row.get(col_map.get("vat_rate", ""), 18) or 18),
            "currency": str(row.get(col_map.get("currency", ""), "INR")).strip() or "INR",
            "category": str(row.get(col_map.get("category", ""), "")).strip(),
        }
        if product["sku"] and product["name"]:
            products.append(product)
    return products


def parse_customer_csv(file_path: str) -> list[dict]:
    df = pd.read_csv(file_path)
    df.columns = [c.strip().lower().replace(" ", "_") for c in df.columns]
    customers = []
    for _, row in df.iterrows():
        name = ""
        for col in df.columns:
            if col in ("name", "customer", "customer_name", "client", "company", "company_name"):
                name = str(row[col]).strip()
                break
        if not name:
            name = str(row.iloc[0]).strip() if len(row) > 0 else ""
        source = ""
        for col in df.columns:
            if col in ("source", "origin", "sector"):
                source = str(row[col]).strip()
                break
        if name:
            customers.append({"name": name, "source": source})
    return customers


async def parse_document_async(file_path: str) -> str:
    ext = Path(file_path).suffix.lower()

    if ext == ".csv":
        df = pd.read_csv(file_path)
        return df.to_string(index=False)
    elif ext in (".xlsx", ".xls"):
        df = pd.read_excel(file_path)
        return df.to_string(index=False)
    elif ext == ".txt":
        with open(file_path, "r", encoding="utf-8") as f:
            return f.read()
    elif ext == ".json":
        with open(file_path, "r", encoding="utf-8") as f:
            return json.dumps(json.load(f), indent=2)
    elif ext in (".pdf", ".docx", ".doc", ".png", ".jpg", ".jpeg", ".tiff", ".bmp", ".pptx", ".html"):
        return await _parse_with_docling_api(file_path)
    else:
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                return f.read()
        except Exception:
            return f"[Unsupported file format: {ext}]"


async def _parse_with_docling_api(file_path: str) -> str:
    async with httpx.AsyncClient(timeout=300) as client:
        with open(file_path, "rb") as f:
            files = {"file": (os.path.basename(file_path), f)}
            try:
                resp = await client.post(
                    f"{DOCLING_URL}/v1/convert/file",
                    files=files,
                    data={"output_format": "md"},
                )
                resp.raise_for_status()
                result = resp.json()
                if "document" in result and "markdown" in result["document"]:
                    return result["document"]["markdown"]
                elif "markdown" in result:
                    return result["markdown"]
                else:
                    return json.dumps(result, indent=2)
            except httpx.ConnectError:
                return f"[Docling API not available at {DOCLING_URL} - container may not be running]"
            except Exception as e:
                return f"[Error parsing with Docling: {str(e)}]"


def parse_document(file_path: str) -> str:
    ext = Path(file_path).suffix.lower()
    if ext == ".csv":
        df = pd.read_csv(file_path)
        return df.to_string(index=False)
    elif ext in (".xlsx", ".xls"):
        df = pd.read_excel(file_path)
        return df.to_string(index=False)
    elif ext == ".txt":
        with open(file_path, "r", encoding="utf-8") as f:
            return f.read()
    elif ext == ".json":
        with open(file_path, "r", encoding="utf-8") as f:
            return json.dumps(json.load(f), indent=2)
    else:
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                return f.read()
        except Exception:
            return f"[Unsupported file: {os.path.basename(file_path)}]"
