"""Data Copilot backend: the API, the static UI, the health check, the digest hook.

One service does everything (architecture.md, "Where things run"). The agent
loop itself lives in agent.py; this file is doors, locks and plumbing.
"""
from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from . import auth, limits
from . import export as export_lib
from . import datasets as ds_lib
from .agent import get_model_client, run_turn
from .config import ROOT, settings
from .db import Conversation, ConversationDataset, Dataset, Message, SessionLocal, engine, get_db, init_db
from .digest import build_digest, send_digest
from .fetch import fetch_url
from .tools.invoices import recent_invoices


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.init_report = init_db()
    # One line in the deploy log, so a first deploy can be checked (docs/deploy-railway.md, step 7).
    print("seed report: " + ", ".join(f"{k}: {v}" for k, v in app.state.init_report.items()), flush=True)
    app.state.model_client = get_model_client()
    yield


app = FastAPI(title="Data Copilot", version="0.1.0", lifespan=lifespan, docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=str(ROOT / "static")), name="static")


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class LoginIn(BaseModel):
    name: str = Field(min_length=2, max_length=40)
    password: str = Field(min_length=1, max_length=200)


class ImageIn(BaseModel):
    media_type: str = Field(pattern=r"^image/(png|jpeg|webp|gif)$")
    data: str  # base64, no data: prefix


class LinkIn(BaseModel):
    url: str = Field(min_length=8, max_length=2000)
    name: str = Field(default="", max_length=80)


class RenameIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)


class ChatIn(BaseModel):
    conversation_id: int | None = None
    dataset_id: int | None = None  # for a new conversation: which tab it starts in
    message: str = Field(default="", max_length=4000)
    image: ImageIn | None = None


# ---------------------------------------------------------------------------
# Pages and health
# ---------------------------------------------------------------------------

@app.get("/", include_in_schema=False)
def index():
    return FileResponse(ROOT / "static" / "index.html")


@app.get("/health")
def health(db: Session = Depends(get_db)):
    """Railway's health check. 200 only when the database answers and a model is configured."""
    problems = []
    try:
        db.execute(text("SELECT 1"))
    except Exception as exc:  # pragma: no cover - exercised on a broken deploy, not in tests
        problems.append(f"db: {type(exc).__name__}")
    if settings.model_provider == "anthropic" and not settings.anthropic_api_key:
        problems.append("ANTHROPIC_API_KEY missing")
    body = {"status": "ok" if not problems else "degraded", "env": settings.environment, "model": settings.model,
            "provider": settings.model_provider, "db": "sqlite" if settings.is_sqlite else "postgres", "problems": problems,
            "time": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    if problems:
        raise HTTPException(503, body)
    return body


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

@app.post("/api/login")
def login(payload: LoginIn, response: Response, db: Session = Depends(get_db)):
    if not auth.password_ok(payload.password):
        raise HTTPException(401, "Wrong password.")
    user = auth.get_or_create_user(db, auth.normalise_name(payload.name))
    response.set_cookie(auth.COOKIE_NAME, auth.make_token(user.id, user.name), httponly=True, samesite="lax",
                        secure=settings.is_production, max_age=settings.session_hours * 3600, path="/")
    return {"id": user.id, "name": user.name}


@app.post("/api/logout", status_code=204)
def logout(response: Response):
    response.delete_cookie(auth.COOKIE_NAME, path="/")
    return Response(status_code=204)


@app.get("/api/me")
def me(request: Request, db: Session = Depends(get_db)):
    user = auth.current_user(request, db)
    return {"id": user.id, "name": user.name}


# ---------------------------------------------------------------------------
# Conversations and messages
# ---------------------------------------------------------------------------

@app.get("/api/conversations")
def list_conversations(request: Request, dataset_id: int | None = None, db: Session = Depends(get_db)):
    user = auth.current_user(request, db)
    q = select(Conversation).where(Conversation.user_id == user.id)
    if dataset_id is not None:
        linked = select(ConversationDataset.conversation_id).where(ConversationDataset.dataset_id == dataset_id)
        any_link = select(ConversationDataset.conversation_id)
        default = ds_lib.default_dataset(db)
        if default is not None and default.id == dataset_id:  # chats without a link belong to the default tab
            q = q.where(Conversation.id.in_(linked) | Conversation.id.not_in(any_link))
        else:
            q = q.where(Conversation.id.in_(linked))
    rows = db.scalars(q.order_by(Conversation.updated_at.desc()).limit(100)).all()
    return [{"id": c.id, "title": c.title, "updated_at": c.updated_at.isoformat(timespec="seconds")} for c in rows]


@app.get("/api/conversations/{conversation_id}/messages")
def list_messages(conversation_id: int, request: Request, db: Session = Depends(get_db)):
    user = auth.current_user(request, db)
    conv = db.get(Conversation, conversation_id)
    if conv is None or conv.user_id != user.id:
        raise HTTPException(404, "Conversation not found.")
    rows = db.scalars(select(Message).where(Message.conversation_id == conv.id).order_by(Message.id)).all()
    return [{"id": m.id, "role": m.role, "content": m.content, "has_image": m.has_image,
             "created_at": m.created_at.isoformat(timespec="seconds")} for m in rows]


@app.get("/api/conversations/{conversation_id}/export")
def export_conversation(conversation_id: int, request: Request, format: str = "md", db: Session = Depends(get_db)):
    """Download a chat as Markdown (`md`) or as a self-contained web page (`html`, prints well to PDF)."""
    user = auth.current_user(request, db)
    conv = db.get(Conversation, conversation_id)
    if conv is None or conv.user_id != user.id:
        raise HTTPException(404, "Conversation not found.")
    if format not in export_lib.FORMATS:
        raise HTTPException(400, "Format must be md or html.")
    messages = db.scalars(select(Message).where(Message.conversation_id == conv.id).order_by(Message.id)).all()
    ds = ds_lib.dataset_for_conversation(db, conv)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    body = export_lib.render(format, conv.title, ds.name if ds else "", user.name, list(messages), now)
    headers = {"Content-Disposition": export_lib.content_disposition(conv.title, format, now),
               "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
    return Response(content=body, media_type=export_lib.FORMATS[format], headers=headers)


@app.post("/api/chat")
def chat(payload: ChatIn, request: Request, db: Session = Depends(get_db)):
    """One turn. Checks the three limits, then streams the agent's events as SSE."""
    user = auth.current_user(request, db)
    if not payload.message.strip() and payload.image is None:
        raise HTTPException(400, "Say something or attach a file.")
    limits.check_rate_limit(db, user.id)
    limits.check_daily_cap(db)
    limits.check_upload_size(payload.image.data if payload.image else None)

    conv = None
    if payload.conversation_id is not None:
        conv = db.get(Conversation, payload.conversation_id)
        if conv is None or conv.user_id != user.id:
            raise HTTPException(404, "Conversation not found.")
    conv_id = conv.id if conv else None
    chat_ds = ds_lib.dataset_for_conversation(db, conv) if conv else (
        db.get(Dataset, payload.dataset_id) if payload.dataset_id is not None else ds_lib.default_dataset(db))
    if chat_ds is None:
        raise HTTPException(404, "Dataset not found.")
    if payload.image is not None and "invoices" not in ds_lib.features_of(chat_ds):
        raise HTTPException(400, f"The {chat_ds.name} tab does not take images.")
    dataset_id = chat_ds.id
    user_id = user.id
    image = payload.image.model_dump() if payload.image else None
    client = request.app.state.model_client

    def sse():
        # A dedicated session: the request-scoped one may close before the stream ends.
        with SessionLocal() as sdb:
            suser = sdb.get(type(user), user_id)
            sconv = sdb.get(Conversation, conv_id) if conv_id else None
            sds = sdb.get(Dataset, dataset_id)
            for event in run_turn(sdb, user=suser, conversation=sconv, text=payload.message, image=image, client=client, dataset=sds):
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

    headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"}
    return StreamingResponse(sse(), media_type="text/event-stream", headers=headers)


# ---------------------------------------------------------------------------
# Datasets (the tabs)
# ---------------------------------------------------------------------------

def _dataset_list(db: Session) -> dict:
    items = [ds_lib.dataset_summary(d) for d in ds_lib.list_datasets(db)]
    return {"datasets": items, "default_id": (ds_lib.default_dataset(db).id if items else None),
            "added": ds_lib.uploaded_count(db), "max_added": settings.max_uploaded_datasets, "max_files": settings.max_files_per_dataset,
            "max_mb": settings.max_dataset_mb, "max_rows": settings.max_dataset_rows}


@app.get("/api/datasets")
def list_datasets(request: Request, db: Session = Depends(get_db)):
    auth.current_user(request, db)
    return _dataset_list(db)


async def _read_body(request: Request) -> bytes:
    limit = settings.max_dataset_mb * 1024 * 1024
    if int(request.headers.get("content-length") or 0) > limit:
        raise HTTPException(413, f"The file is larger than {settings.max_dataset_mb} MB.")
    buf = bytearray()
    async for chunk in request.stream():
        buf += chunk
        if len(buf) > limit:
            raise HTTPException(413, f"The file is larger than {settings.max_dataset_mb} MB.")
    return bytes(buf)


def _dataset_or_404(db: Session, dataset_id: int) -> Dataset:
    ds = db.get(Dataset, dataset_id)
    if ds is None:
        raise HTTPException(404, "Dataset not found.")
    return ds


@app.post("/api/datasets/upload", status_code=201)
async def upload_dataset(request: Request, filename: str, name: str = "", db: Session = Depends(get_db)):
    """The file is the raw request body (no multipart); `filename` says what kind it is."""
    user = auth.current_user(request, db)
    data = await _read_body(request)
    try:
        ds = await run_in_threadpool(ds_lib.add_dataset, db, name=name, filename=filename, data=data,
                                     source="upload", origin=filename, user_id=user.id)
    except ds_lib.DatasetError as exc:
        raise HTTPException(400, str(exc)) from None
    return ds_lib.dataset_summary(ds)


@app.post("/api/datasets/link", status_code=201)
def add_dataset_from_link(payload: LinkIn, request: Request, db: Session = Depends(get_db)):
    user = auth.current_user(request, db)
    try:
        if ds_lib.uploaded_count(db) >= settings.max_uploaded_datasets:  # fail before downloading anything
            raise ds_lib.DatasetError(f"You already have {settings.max_uploaded_datasets} added datasets, the maximum. Delete one to add another.")
        filename, data = fetch_url(payload.url)
        ds = ds_lib.add_dataset(db, name=payload.name, filename=filename, data=data, source="link",
                                origin=payload.url.strip(), user_id=user.id)
    except ds_lib.DatasetError as exc:
        raise HTTPException(400, str(exc)) from None
    return ds_lib.dataset_summary(ds)


@app.post("/api/datasets/{dataset_id}/upload", status_code=201)
async def add_file_to_dataset(dataset_id: int, request: Request, filename: str, db: Session = Depends(get_db)):
    """One more file for an added dataset's tab (up to MAX_FILES_PER_DATASET in all)."""
    auth.current_user(request, db)
    ds = _dataset_or_404(db, dataset_id)
    data = await _read_body(request)
    try:
        await run_in_threadpool(ds_lib.add_file, db, ds, filename=filename, data=data)
    except ds_lib.DatasetError as exc:
        raise HTTPException(400, str(exc)) from None
    return ds_lib.dataset_summary(ds)


@app.post("/api/datasets/{dataset_id}/link", status_code=201)
def add_link_to_dataset(dataset_id: int, payload: LinkIn, request: Request, db: Session = Depends(get_db)):
    auth.current_user(request, db)
    ds = _dataset_or_404(db, dataset_id)
    try:
        ds_lib.check_can_add_file(ds)  # fail before downloading anything
        filename, data = fetch_url(payload.url)
        ds_lib.add_file(db, ds, filename=filename, data=data)
    except ds_lib.DatasetError as exc:
        raise HTTPException(400, str(exc)) from None
    return ds_lib.dataset_summary(ds)


@app.patch("/api/datasets/{dataset_id}")
def rename_dataset(dataset_id: int, payload: RenameIn, request: Request, db: Session = Depends(get_db)):
    auth.current_user(request, db)
    ds = _dataset_or_404(db, dataset_id)
    try:
        ds_lib.rename_dataset(db, ds, payload.name)
    except ds_lib.DatasetError as exc:
        raise HTTPException(400, str(exc)) from None
    return ds_lib.dataset_summary(ds)


@app.delete("/api/datasets/{dataset_id}", status_code=204)
def remove_dataset(dataset_id: int, request: Request, db: Session = Depends(get_db)):
    auth.current_user(request, db)
    ds = db.get(Dataset, dataset_id)
    if ds is None:
        raise HTTPException(404, "Dataset not found.")
    if ds.source == "folder":
        raise HTTPException(400, "Built-in datasets come from the data/ folder and cannot be deleted here.")
    ds_lib.delete_dataset(db, ds)
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# Invoices, stats, digest
# ---------------------------------------------------------------------------

@app.get("/api/invoices")
def invoices(request: Request, db: Session = Depends(get_db)):
    auth.current_user(request, db)
    return recent_invoices(db, limit=50)


@app.get("/api/stats")
def stats(request: Request, db: Session = Depends(get_db)):
    user = auth.current_user(request, db)
    return {"spend_today_usd": round(limits.spend_today_usd(db), 4), "daily_cap_usd": settings.daily_cap_usd,
            "messages_last_hour": limits.messages_last_hour(db, user.id), "rate_limit_per_hour": settings.rate_limit_per_hour}


@app.post("/internal/digest")
def digest(db: Session = Depends(get_db), x_cron_secret: str | None = Header(default=None)):
    """Manual or cron trigger for the morning digest. Protected by CRON_SECRET."""
    if not settings.cron_secret or x_cron_secret != settings.cron_secret:
        raise HTTPException(401, "Bad or missing X-Cron-Secret.")
    body = build_digest(db)
    sent = send_digest(body)
    return {"text": body, "sent": sent}
