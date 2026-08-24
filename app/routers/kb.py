import os
import uuid
from fastapi import APIRouter, UploadFile, File, HTTPException
from app.services.kb import (
    import_kb_from_file,
    search_kb,
    list_kb_spaces,
    list_kb_articles,
    delete_kb_article,
)
from app.config import UPLOAD_DIR

router = APIRouter(prefix="/api/kb", tags=["knowledge-base"])


@router.post("/upload")
async def upload_knowledge_base(file: UploadFile = File(...)):
    if not file.filename:
        raise HTTPException(400, "No file provided")
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    file_path = os.path.join(UPLOAD_DIR, f"kb_{uuid.uuid4()}_{file.filename}")
    with open(file_path, "wb") as f:
        content = await file.read()
        f.write(content)
    try:
        result = await import_kb_from_file(file_path)
    except Exception as e:
        raise HTTPException(400, f"Failed to process file: {str(e)}")
    return {"message": f"Imported {result['articles']} articles ({result['chunks']} chunks)", **result}


@router.post("/search")
async def search_knowledge_base(query: str, limit: int = 5):
    results = await search_kb(query, limit=limit)
    return {"results": results, "total": len(results)}


@router.get("/spaces")
async def get_spaces():
    spaces = list_kb_spaces()
    return {"spaces": spaces}


@router.get("/articles")
async def get_articles(space: str = ""):
    articles = list_kb_articles(space=space)
    return {"articles": articles, "total": len(articles)}


@router.delete("/articles/{article_id}")
async def delete_article(article_id: str):
    success = delete_kb_article(article_id)
    if not success:
        raise HTTPException(404, "Article not found")
    return {"deleted": True}
