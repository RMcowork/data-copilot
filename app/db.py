"""Database layer: models, engine, session, startup migration and seeding.

Postgres on Railway (DATABASE_URL from the plugin) or SQLite locally. The
schema is created at start with `create_all`; `schema_version` records which
version is live so a later real migration tool has somewhere to start.
"""
from __future__ import annotations

import csv
from datetime import date, datetime, timezone
from pathlib import Path

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    create_engine,
    func,
    select,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship, sessionmaker

from .config import ROOT, settings

SCHEMA_VERSION = 1


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class SchemaVersion(Base):
    __tablename__ = "schema_version"
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    applied_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Conversation(Base):
    __tablename__ = "conversations"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    title: Mapped[str] = mapped_column(String(120), default="New chat")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    messages: Mapped[list["Message"]] = relationship(back_populates="conversation", order_by="Message.id")


class Message(Base):
    __tablename__ = "messages"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    role: Mapped[str] = mapped_column(String(10))  # "user" | "assistant"
    content: Mapped[str] = mapped_column(Text)  # plain text shown in the UI
    has_image: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    conversation: Mapped[Conversation] = relationship(back_populates="messages")


class Run(Base):
    """One model turn: the record behind the cost cap, the digest and the runbook."""

    __tablename__ = "runs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    conversation_id: Mapped[int | None] = mapped_column(ForeignKey("conversations.id"), nullable=True, index=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    model: Mapped[str] = mapped_column(String(60))
    status: Mapped[str] = mapped_column(String(20), default="ok")  # ok | refusal | error | aborted | capped
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_read_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_write_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    tool_calls: Mapped[int] = mapped_column(Integer, default=0)
    tool_names: Mapped[str] = mapped_column(String(200), default="")
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class Supplier(Base):
    __tablename__ = "suppliers"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), index=True)
    item: Mapped[str] = mapped_column(String(120))
    unit: Mapped[str] = mapped_column(String(20))
    price: Mapped[float] = mapped_column(Float)
    currency: Mapped[str] = mapped_column(String(3))


class Invoice(Base):
    __tablename__ = "invoices"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    supplier: Mapped[str] = mapped_column(String(120), index=True)
    invoice_number: Mapped[str | None] = mapped_column(String(60), nullable=True, index=True)
    amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    currency: Mapped[str] = mapped_column(String(3), default="ILS")
    invoice_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="logged")  # logged | needs_a_look
    flagged: Mapped[bool] = mapped_column(Boolean, default=False)  # over the spec threshold
    source_ref: Mapped[str | None] = mapped_column(String(120), nullable=True, unique=True)  # the dedup key
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    logged_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class Dataset(Base):
    """One tab: a set of related tables the chat can query. Source: folder (data/), upload, or link."""

    __tablename__ = "datasets"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(String(60), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(80))
    description: Mapped[str] = mapped_column(Text, default="")
    notes: Mapped[str] = mapped_column(Text, default="")  # extra instructions for the model, from dataset.yaml
    features: Mapped[str] = mapped_column(String(200), default="")  # comma list, e.g. "invoices"
    starters: Mapped[str] = mapped_column(Text, default="[]")  # JSON list of suggested first questions
    source: Mapped[str] = mapped_column(String(10), default="folder")  # folder | upload | link
    origin: Mapped[str] = mapped_column(String(500), default="")  # folder name or URL
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    tables: Mapped[list["DatasetTable"]] = relationship(order_by="DatasetTable.id")


class DatasetTable(Base):
    """One loaded file (or sheet): its real table is `physical_name`; `columns_json` is the typed schema."""

    __tablename__ = "dataset_tables"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    dataset_id: Mapped[int] = mapped_column(ForeignKey("datasets.id"), index=True)
    name: Mapped[str] = mapped_column(String(80))
    physical_name: Mapped[str] = mapped_column(String(60), unique=True)
    source_file: Mapped[str] = mapped_column(String(200), default="")
    file_hash: Mapped[str] = mapped_column(String(64), default="")
    row_count: Mapped[int] = mapped_column(Integer, default=0)
    columns_json: Mapped[str] = mapped_column(Text, default="[]")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class DatasetRename(Base):
    """A name a person gave a tab. Folder datasets take their title from dataset.yaml on every start;
    a row here stops that, so a rename survives a restart. A separate table for the same reason as
    ConversationDataset: no migration on an existing database."""

    __tablename__ = "dataset_renames"
    dataset_id: Mapped[int] = mapped_column(ForeignKey("datasets.id"), primary_key=True)
    name: Mapped[str] = mapped_column(String(80))


class ConversationDataset(Base):
    """Which tab a conversation belongs to. No row = the default dataset (chats that predate datasets).

    A separate table, not a column on conversations, so existing databases need no migration (architecture.md)."""

    __tablename__ = "conversation_datasets"
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id"), primary_key=True)
    dataset_id: Mapped[int] = mapped_column(ForeignKey("datasets.id"), index=True)


# ---------------------------------------------------------------------------
# Engine and session
# ---------------------------------------------------------------------------

def make_engine(url: str | None = None):
    url = url or settings.database_url
    kwargs: dict = {"pool_pre_ping": True}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
    return create_engine(url, **kwargs)


engine = make_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Startup: schema, version, seed data
# ---------------------------------------------------------------------------

def init_db(bind=None, data_dir: Path | None = None) -> dict:
    """Create tables, record the schema version, seed the suppliers list, load the data/ datasets."""
    bind = bind or engine
    data_dir = data_dir or (ROOT / "data")
    Base.metadata.create_all(bind)
    report = {"schema_version": SCHEMA_VERSION, "suppliers_seeded": 0}
    with Session(bind) as db:
        if db.scalar(select(func.count()).select_from(SchemaVersion)) == 0:
            db.add(SchemaVersion(version=SCHEMA_VERSION))
            db.commit()
        if db.scalar(select(func.count()).select_from(Supplier)) == 0:
            report["suppliers_seeded"] = _seed_suppliers(db, data_dir / "bakery" / "suppliers.csv")
        from .datasets import sync_folder_datasets  # imported here: datasets.py imports the models above

        report.update(sync_folder_datasets(db, data_dir))
    return report


def _seed_suppliers(db: Session, path: Path) -> int:
    if not path.exists():
        return 0
    rows = []
    with path.open(encoding="utf-8") as f:
        for r in csv.DictReader(f):
            rows.append(Supplier(name=r["supplier"].strip(), item=r["item"].strip(), unit=r["unit"].strip(),
                                 price=float(r["price"]), currency=r["currency"].strip().upper()))
    db.add_all(rows)
    db.commit()
    return len(rows)
