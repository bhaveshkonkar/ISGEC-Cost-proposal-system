import asyncio
import traceback
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from contextlib import asynccontextmanager
from app.models import init_db
from app.routers import catalog, proposals, chat, kb, email
from app.routers import payments as payments_router
from app.routers import costing as costing_router
from app.config import EMAIL_POLL_INTERVAL
from app.services.email_pipeline import run_poll_cycle


async def email_poller():
    while True:
        try:
            await run_poll_cycle()
        except Exception:
            traceback.print_exc()
        await asyncio.sleep(EMAIL_POLL_INTERVAL)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    poller_task = asyncio.create_task(email_poller())
    yield
    poller_task.cancel()

app = FastAPI(title="ISGEC Proposal System", lifespan=lifespan)

import os
os.makedirs("app/static/css", exist_ok=True)
os.makedirs("app/static/js", exist_ok=True)
app.mount("/static", StaticFiles(directory="app/static"), name="static")

templates = Jinja2Templates(directory="app/templates")

app.include_router(catalog.router)
app.include_router(proposals.router)
app.include_router(chat.router)
app.include_router(kb.router)
app.include_router(email.router)
app.include_router(payments_router.router)
app.include_router(payments_router.page_router)
app.include_router(costing_router.router)


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    return templates.TemplateResponse(request, "index.html", {"active": "dashboard"})


@app.get("/catalog", response_class=HTMLResponse)
async def catalog_page(request: Request):
    return templates.TemplateResponse(request, "catalog.html", {"active": "catalog"})


@app.get("/upload", response_class=HTMLResponse)
async def upload_page(request: Request):
    return templates.TemplateResponse(request, "upload.html", {"active": "upload"})


@app.get("/rfq", response_class=HTMLResponse)
async def rfq_page(request: Request):
    return templates.TemplateResponse(request, "rfq.html", {"active": "rfq"})


@app.get("/proposals", response_class=HTMLResponse)
async def proposals_page(request: Request):
    return templates.TemplateResponse(request, "proposals.html", {"active": "proposals"})


@app.get("/proposal/{proposal_id}", response_class=HTMLResponse)
async def proposal_detail_page(request: Request, proposal_id: int):
    return templates.TemplateResponse(request, "proposal.html", {"active": "proposals", "proposal_id": proposal_id})


@app.get("/chat", response_class=HTMLResponse)
async def chat_page(request: Request):
    return templates.TemplateResponse(request, "chat.html", {"active": "chat"})


@app.get("/email-automation", response_class=HTMLResponse)
async def email_automation_page(request: Request, tab: str = "pending"):
    if tab not in ("pending", "sent", "issues"):
        tab = "pending"
    return templates.TemplateResponse(request, "email.html", {"active": "email", "active_tab": tab})


@app.get("/knowledge-base", response_class=HTMLResponse)
async def kb_page(request: Request):
    return templates.TemplateResponse(request, "knowledge_base.html", {"active": "kb"})


@app.get("/costing", response_class=HTMLResponse)
async def costing_page(request: Request):
    return templates.TemplateResponse(request, "costing.html", {"active": "costing"})


@app.get("/api/health")
async def health():
    return {"status": "ok"}
