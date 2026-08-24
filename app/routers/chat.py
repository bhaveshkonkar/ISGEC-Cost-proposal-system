import uuid
from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.models import ChatMessage, Product, get_session
from app.services.llm import generate_chat_response, build_chat_stream
from app.services.embedding import get_embedding
from app.services.search import search_products, ensure_collection
from app.services.kb import search_kb

router = APIRouter(prefix="/api", tags=["chat"])


@router.post("/chat")
async def chat(
    message: str,
    session_id: str = "default",
    db: AsyncSession = Depends(get_session),
):
    user_msg = ChatMessage(session_id=session_id, role="user", message=message)
    db.add(user_msg)

    context = ""
    kb_context = ""
    try:
        ensure_collection()
        embedding = await get_embedding(message)
        results = search_products(embedding, limit=3)
        if results:
            context_lines = []
            for r in results:
                p = r["payload"]
                context_lines.append(
                    f"- {p.get('sku', '')} {p.get('name', '')}: {p.get('description', '')[:200]} "
                    f"(Price: {p.get('currency', 'INR')} {p.get('price_net', 0)})"
                )
            context = "Relevant products:\n" + "\n".join(context_lines)
    except Exception:
        pass

    try:
        kb_results = await search_kb(message, limit=3)
        if kb_results:
            kb_parts = []
            for kr in kb_results:
                kb_parts.append(f"[{kr['title']} - {kr['space']}]\n{kr['body_chunk']}")
            kb_context = "\n\n".join(kb_parts)
    except Exception:
        pass

    response = await generate_chat_response(message, context, kb_context=kb_context)

    assistant_msg = ChatMessage(session_id=session_id, role="assistant", message=response)
    db.add(assistant_msg)
    await db.commit()

    return {"response": response, "session_id": session_id}


@router.post("/chat/stream")
async def chat_stream(
    message: str,
    session_id: str = "default",
    db: AsyncSession = Depends(get_session),
):
    """Stream chat response token-by-token via Server-Sent Events."""
    user_msg = ChatMessage(session_id=session_id, role="user", message=message)
    db.add(user_msg)
    await db.flush()

    context = ""
    kb_context = ""
    try:
        ensure_collection()
        embedding = await get_embedding(message)
        results = search_products(embedding, limit=3)
        if results:
            context_lines = []
            for r in results:
                p = r["payload"]
                context_lines.append(
                    f"- {p.get('sku', '')} {p.get('name', '')}: {p.get('description', '')[:200]} "
                    f"(Price: {p.get('currency', 'INR')} {p.get('price_net', 0)})"
                )
            context = "Relevant products:\n" + "\n".join(context_lines)
    except Exception:
        pass

    try:
        kb_results = await search_kb(message, limit=3)
        if kb_results:
            kb_parts = []
            for kr in kb_results:
                kb_parts.append(f"[{kr['title']} - {kr['space']}]\n{kr['body_chunk']}")
            kb_context = "\n\n".join(kb_parts)
    except Exception:
        pass

    async def event_generator():
        full_response = []
        try:
            async for token in build_chat_stream(message, context, kb_context=kb_context):
                full_response.append(token)
                yield f"data: {token}\n\n"
        except Exception as exc:
            yield f"data: [ERROR] {exc}\n\n"
        finally:
            complete = "".join(full_response)
            if complete:
                assistant_msg = ChatMessage(session_id=session_id, role="assistant", message=complete)
                db.add(assistant_msg)
                await db.commit()
            yield "data: [DONE]\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@router.get("/chat/history")
async def get_chat_history(
    session_id: str = "default",
    limit: int = 50,
    db: AsyncSession = Depends(get_session),
):
    result = await db.execute(
        select(ChatMessage)
        .where(ChatMessage.session_id == session_id)
        .order_by(ChatMessage.created_at.desc())
        .limit(limit)
    )
    messages = result.scalars().all()
    messages.reverse()
    return {
        "messages": [
            {"role": m.role, "message": m.message, "created_at": m.created_at.isoformat() if m.created_at else None}
            for m in messages
        ]
    }
