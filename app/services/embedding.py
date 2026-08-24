import httpx
from app.config import OLLAMA_BASE_URL, EMBEDDING_MODEL


async def get_embedding(text: str) -> list[float]:
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            f"{OLLAMA_BASE_URL}/api/embeddings",
            json={"model": EMBEDDING_MODEL, "prompt": text},
        )
        resp.raise_for_status()
        return resp.json()["embedding"]


async def get_embeddings_batch(texts: list[str]) -> list[list[float]]:
    results = []
    for text in texts:
        emb = await get_embedding(text)
        results.append(emb)
    return results
