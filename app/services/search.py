from qdrant_client import QdrantClient
from qdrant_client.models import VectorParams, Distance, PointStruct, Filter, FieldCondition, MatchValue
from app.config import QDRANT_URL, QDRANT_COLLECTION, EMBEDDING_DIM
import uuid

client = QdrantClient(url=QDRANT_URL)


def ensure_collection():
    collections = [c.name for c in client.get_collections().collections]
    if QDRANT_COLLECTION not in collections:
        client.create_collection(
            collection_name=QDRANT_COLLECTION,
            vectors_config=VectorParams(size=EMBEDDING_DIM, distance=Distance.COSINE),
        )


def upsert_product(point_id: str, embedding: list[float], payload: dict):
    client.upsert(
        collection_name=QDRANT_COLLECTION,
        points=[
            PointStruct(
                id=point_id if point_id else str(uuid.uuid4()),
                vector=embedding,
                payload=payload,
            )
        ],
    )


def search_products(embedding: list[float], limit: int = 5) -> list[dict]:
    results = client.query_points(
        collection_name=QDRANT_COLLECTION,
        query=embedding,
        limit=limit,
    )
    return [{"id": r.id, "score": r.score, "payload": r.payload} for r in results.points]


def delete_product(point_id: str):
    client.delete(
        collection_name=QDRANT_COLLECTION,
        points_selector=[point_id],
    )
