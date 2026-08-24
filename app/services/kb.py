import json
import uuid
from qdrant_client import QdrantClient
from qdrant_client.models import VectorParams, Distance, PointStruct
from app.config import QDRANT_URL, QDRANT_KB_COLLECTION, EMBEDDING_DIM
from app.services.embedding import get_embedding

client = QdrantClient(url=QDRANT_URL)

CHUNK_SIZE = 1000
CHUNK_OVERLAP = 200


def ensure_kb_collection():
    collections = [c.name for c in client.get_collections().collections]
    if QDRANT_KB_COLLECTION not in collections:
        client.create_collection(
            collection_name=QDRANT_KB_COLLECTION,
            vectors_config=VectorParams(size=EMBEDDING_DIM, distance=Distance.COSINE),
        )


def chunk_text(text: str, chunk_size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    if len(text) <= chunk_size:
        return [text]
    chunks = []
    start = 0
    while start < len(text):
        end = start + chunk_size
        chunk = text[start:end]
        if chunk.strip():
            chunks.append(chunk.strip())
        start = end - overlap
    return chunks


def parse_kb_json(file_path: str) -> list[dict]:
    with open(file_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    articles = []
    for space in data.get("spaces", []):
        space_name = space.get("name", "")
        for article in space.get("articles", []):
            articles.append({
                "title": article.get("title", ""),
                "space": space_name,
                "tags": article.get("tags", []),
                "body": article.get("body", ""),
                "type": article.get("type", ""),
            })
    return articles


def parse_kb_text(file_path: str) -> list[dict]:
    with open(file_path, "r", encoding="utf-8") as f:
        text = f.read()
    return [{
        "title": file_path.split("/")[-1].split("\\")[-1],
        "space": "Uploaded Documents",
        "tags": [],
        "body": text,
        "type": "document",
    }]


def parse_kb_csv(file_path: str) -> list[dict]:
    import pandas as pd
    df = pd.read_csv(file_path)
    articles = []
    for _, row in df.iterrows():
        title = str(row.get("title", row.iloc[0] if len(row) > 0 else "Untitled"))
        body = str(row.get("body", row.get("content", row.get("text", ""))))
        tags = []
        if "tags" in df.columns and row.get("tags"):
            tags = [t.strip() for t in str(row["tags"]).split(",")]
        articles.append({
            "title": title,
            "space": "Uploaded Documents",
            "tags": tags,
            "body": body,
            "type": "document",
        })
    return articles


def parse_kb_file(file_path: str) -> list[dict]:
    if file_path.endswith(".json"):
        return parse_kb_json(file_path)
    elif file_path.endswith(".csv"):
        return parse_kb_csv(file_path)
    else:
        return parse_kb_text(file_path)


async def import_kb_from_file(file_path: str) -> dict:
    ensure_kb_collection()
    articles = parse_kb_file(file_path)
    total_chunks = 0
    article_ids = []

    for article in articles:
        chunks = chunk_text(article["body"])
        article_id = str(uuid.uuid4())
        article_ids.append(article_id)

        for i, chunk in enumerate(chunks):
            embedding_text = f"{article['title']} {article['space']} {chunk}"
            embedding = await get_embedding(embedding_text)
            point_id = str(uuid.uuid4())
            client.upsert(
                collection_name=QDRANT_KB_COLLECTION,
                points=[
                    PointStruct(
                        id=point_id,
                        vector=embedding,
                        payload={
                            "article_id": article_id,
                            "title": article["title"],
                            "space": article["space"],
                            "tags": article["tags"],
                            "type": article["type"],
                            "chunk_index": i,
                            "total_chunks": len(chunks),
                            "body_chunk": chunk,
                        },
                    )
                ],
            )
            total_chunks += 1

    return {"articles": len(articles), "chunks": total_chunks}


async def search_kb(query: str, limit: int = 5) -> list[dict]:
    ensure_kb_collection()
    try:
        embedding = await get_embedding(query)
        results = client.query_points(
            collection_name=QDRANT_KB_COLLECTION,
            query=embedding,
            limit=limit,
        )
        seen_articles = set()
        deduped = []
        for r in results.points:
            payload = r.payload
            article_id = payload.get("article_id", "")
            if article_id in seen_articles:
                continue
            seen_articles.add(article_id)
            deduped.append({
                "score": r.score,
                "title": payload.get("title", ""),
                "space": payload.get("space", ""),
                "tags": payload.get("tags", []),
                "body_chunk": payload.get("body_chunk", ""),
                "chunk_index": payload.get("chunk_index", 0),
                "total_chunks": payload.get("total_chunks", 1),
            })
        return deduped
    except Exception:
        return []


def list_kb_spaces() -> list[dict]:
    ensure_kb_collection()
    try:
        from qdrant_client.models import Filter, FieldCondition, MatchValue
        all_points = []
        offset = None
        while True:
            result = client.query_points(
                collection_name=QDRANT_KB_COLLECTION,
                query=None if not all_points else None,
                offset=offset,
                limit=100,
            )
            if not result.points:
                break
            all_points.extend(result.points)
            offset = len(all_points)
            if len(result.points) < 100:
                break

        spaces = {}
        for p in all_points:
            payload = p.payload
            space = payload.get("space", "Unknown")
            if space not in spaces:
                spaces[space] = {"name": space, "articles": set(), "chunks": 0}
            spaces[space]["articles"].add(payload.get("article_id", ""))
            spaces[space]["chunks"] += 1

        return [
            {"name": s["name"], "article_count": len(s["articles"]), "chunk_count": s["chunks"]}
            for s in spaces.values()
        ]
    except Exception:
        return []


def list_kb_articles(space: str = "") -> list[dict]:
    ensure_kb_collection()
    try:
        all_points = []
        offset = 0
        while True:
            result = client.query_points(
                collection_name=QDRANT_KB_COLLECTION,
                offset=offset,
                limit=100,
            )
            if not result.points:
                break
            all_points.extend(result.points)
            offset += len(result.points)
            if len(result.points) < 100:
                break

        articles = {}
        for p in all_points:
            payload = p.payload
            aid = payload.get("article_id", "")
            if aid in articles:
                continue
            if space and payload.get("space") != space:
                continue
            articles[aid] = {
                "id": aid,
                "title": payload.get("title", ""),
                "space": payload.get("space", ""),
                "tags": payload.get("tags", []),
                "type": payload.get("type", ""),
                "chunk_count": payload.get("total_chunks", 1),
            }
        return list(articles.values())
    except Exception:
        return []


def delete_kb_article(article_id: str) -> bool:
    ensure_kb_collection()
    try:
        from qdrant_client.models import Filter, FieldCondition, MatchValue
        points = client.query_points(
            collection_name=QDRANT_KB_COLLECTION,
            query=None,
        )
        to_delete = [
            p.id for p in points.points
            if p.payload.get("article_id") == article_id
        ]
        if to_delete:
            client.delete(
                collection_name=QDRANT_KB_COLLECTION,
                points_selector=to_delete,
            )
            return True
        return False
    except Exception:
        return False
