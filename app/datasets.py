"""Datasets: any CSV or Excel file becomes a typed, read-only table the chat can query.

Three ways in, one loader:
  * a subfolder of data/ is one dataset (tab); every CSV/xlsx file in it is a table of that dataset,
  * a loose file in data/ is a dataset of its own,
  * a file uploaded in the UI, or fetched from a link (app/fetch.py), is a dataset too (at most
    `max_uploaded_datasets` of those).

Columns are typed by looking at the values (number / date / datetime / text), names are cleaned,
and the rows go into a real table per file (`ds<dataset>_<n>`). The model never sees SQL or table
names: `app/tools/data.py` builds queries from the schema stored here (architecture.md, "Datasets").
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

import yaml
from sqlalchemy import Column, Date, DateTime, Float, Integer, MetaData, Table, Text, delete, func, inspect, select
from sqlalchemy.orm import Session

from .config import ROOT, settings
from .db import Conversation, ConversationDataset, Dataset, DatasetRename, DatasetTable, Message, Run, utcnow

DATA_EXTENSIONS = {".csv", ".tsv", ".txt", ".xlsx", ".xlsm"}
MAX_COLUMNS = 100
INSERT_CHUNK = 2000
DISTINCT_LISTED = 25      # a text column with this many distinct values or fewer lists them all in the prompt
EXAMPLES_LISTED = 5
DEFAULT_SLUG = "bakery"   # the tab that is open first, and home of conversations that predate datasets


class DatasetError(ValueError):
    """A problem the user can fix (bad file, over a limit). The message is shown to them as is."""


# ---------------------------------------------------------------------------
# Reading files
# ---------------------------------------------------------------------------

@dataclass
class RawTable:
    name: str
    headers: list[str]
    rows: list[list[str | None]]
    source_file: str = ""


def slugify(text: str, default: str = "x", maxlen: int = 40) -> str:
    s = re.sub(r"[^\w]+", "_", str(text), flags=re.UNICODE).strip("_").lower()
    return (s or default)[:maxlen].strip("_") or default


def _decode(data: bytes) -> str:
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return data.decode("utf-16")
    for enc in ("utf-8-sig", "cp1255"):  # cp1255: Hebrew files saved by older Excel
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1")


def _clean_cell(v) -> str | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.strftime("%Y-%m-%d") if v.time() == datetime.min.time() else v.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(v, date):
        return v.strftime("%Y-%m-%d")
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    s = str(v).strip()
    return s or None


def _read_csv(data: bytes, stem: str, max_rows: int) -> RawTable:
    text = _decode(data)
    first = text.lstrip("\n\r")[:5000].split("\n", 1)[0]
    delimiter = max(",;\t|", key=first.count) if any(d in first for d in ",;\t|") else ","
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    headers: list[str] | None = None
    rows: list[list[str | None]] = []
    for raw in reader:
        cells = [_clean_cell(c) for c in raw]
        if not any(cells):
            continue
        if headers is None:
            headers = [c or "" for c in cells]
            continue
        rows.append(_fit(cells, len(headers)))
        if len(rows) > max_rows:
            raise DatasetError(f"The file has more than {max_rows:,} rows. Send a smaller file (filter or sample it first).")
    if headers is None:
        raise DatasetError("The file is empty.")
    return RawTable(name=stem, headers=headers, rows=rows)


def _read_xlsx(data: bytes, stem: str, max_rows: int) -> list[RawTable]:
    try:
        from openpyxl import load_workbook
        wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as exc:  # noqa: BLE001 - a corrupt or non-Excel file
        raise DatasetError(f"Could not read the Excel file ({type(exc).__name__}). Is it a real .xlsx file?") from exc
    tables: list[RawTable] = []
    try:
        for ws in wb.worksheets:
            headers: list[str] | None = None
            rows: list[list[str | None]] = []
            for raw in ws.iter_rows(values_only=True):
                cells = [_clean_cell(c) for c in raw]
                if not any(cells):
                    continue
                if headers is None:
                    headers = [c or "" for c in cells]
                    continue
                rows.append(_fit(cells, len(headers)))
                if len(rows) > max_rows:
                    raise DatasetError(f"Sheet '{ws.title}' has more than {max_rows:,} rows. Send a smaller file.")
            if headers is not None:
                tables.append(RawTable(name=ws.title, headers=headers, rows=rows))
    finally:
        wb.close()
    if not tables:
        raise DatasetError("The workbook has no data.")
    if len(tables) == 1:
        tables[0].name = stem
    return tables


def _fit(cells: list, width: int) -> list:
    return (cells + [None] * width)[:width]


def read_tables(filename: str, data: bytes, *, max_rows: int | None = None) -> list[RawTable]:
    """Parse one uploaded or on-disk file into one or more raw tables (one per sheet for Excel)."""
    max_rows = max_rows or settings.max_dataset_rows
    ext = Path(filename).suffix.lower()
    stem = slugify(Path(filename).stem, "data")
    if ext in (".xlsx", ".xlsm"):
        tables = _read_xlsx(data, stem, max_rows)
    elif ext in (".csv", ".tsv", ".txt"):
        tables = [_read_csv(data, stem, max_rows)]
    else:
        raise DatasetError("Unsupported file type. Use a .csv or .xlsx file (save older .xls files as .xlsx first).")
    for t in tables:
        if len(t.headers) > MAX_COLUMNS:
            raise DatasetError(f"'{t.name}' has {len(t.headers)} columns; the limit is {MAX_COLUMNS}.")
        if not t.rows:
            raise DatasetError(f"'{t.name}' has a header but no data rows.")
        t.name = slugify(t.name, "data")
    return tables


# ---------------------------------------------------------------------------
# Typing columns
# ---------------------------------------------------------------------------

_NULLISH = {"na", "n/a", "nan", "null", "none", "-", "--"}
_NUMBER = re.compile(r"^[-+]?\d+(\.\d+)?([eE][-+]?\d+)?$")
_THOUSANDS = re.compile(r"^[-+]?\d{1,3}(,\d{3})+(\.\d+)?$")
_DATE_FORMATS = ["%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d.%m.%Y", "%Y/%m/%d"]
_DATETIME_FORMATS = ["%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M",
                     "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M", "%d.%m.%Y %H:%M"]


def to_number(raw) -> float | None:
    """'1,240.50', '₪85', ' 12 ' -> float; anything else -> None. IDs with leading zeros are not numbers."""
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return float(raw)
    s = str(raw or "").strip().lstrip("₪$€£").strip().replace(" ", "")
    if _THOUSANDS.match(s):
        s = s.replace(",", "")
    if not _NUMBER.match(s):
        return None
    if re.match(r"^[-+]?0\d", s):
        return None
    return float(s)


def _parse_dt(s: str, fmt: str) -> datetime | None:
    try:
        return datetime.strptime(s, fmt)
    except ValueError:
        return None


def infer_type(values: list[str]) -> tuple[str, str | None]:
    """('number'|'date'|'datetime'|'text', datetime format or None) from the non-empty values of a column."""
    present = [v for v in values if v is not None and v.lower() not in _NULLISH]
    if not present:
        return "text", None
    if all(to_number(v) is not None for v in present):
        return "number", None
    for fmt in _DATETIME_FORMATS:
        if all(_parse_dt(v, fmt) for v in present):
            return "datetime", fmt
    for fmt in _DATE_FORMATS:
        if all(_parse_dt(v, fmt) for v in present):
            return "date", fmt
    return "text", None


def _column_names(headers: list[str]) -> list[str]:
    seen: Counter = Counter()
    names = []
    for i, h in enumerate(headers):
        base = slugify(h, f"col_{i + 1}", 48)
        seen[base] += 1
        names.append(base if seen[base] == 1 else f"{base}_{seen[base]}")
    return names


def _shorten(s: str, n: int = 40) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def analyse(raw: RawTable) -> tuple[list[dict], list[list]]:
    """The column specs (name, label, type, stats) and the rows converted to typed Python values."""
    names = _column_names(raw.headers)
    specs: list[dict] = []
    typed_cols: list[list] = []
    for i, name in enumerate(names):
        col = [r[i] for r in raw.rows]
        ctype, fmt = infer_type(col)
        spec: dict = {"name": name, "label": raw.headers[i] or name, "type": ctype}
        if ctype == "number":
            vals = [None if (v is None or v.lower() in _NULLISH) else to_number(v) for v in col]
            nums = [v for v in vals if v is not None]
            spec.update(min=min(nums), max=max(nums))
        elif ctype in ("date", "datetime"):
            vals = [None if (v is None or v.lower() in _NULLISH) else _parse_dt(v, fmt) for v in col]
            if ctype == "date":
                vals = [v.date() if v else None for v in vals]
            present = [v for v in vals if v is not None]
            spec.update(min=str(min(present)), max=str(max(present)))
        else:
            vals = col
            counts = Counter(v for v in col if v is not None)
            spec["distinct"] = len(counts)
            if len(counts) <= DISTINCT_LISTED:
                spec["values"] = [_shorten(k) for k, _ in counts.most_common()]
            else:
                spec["examples"] = [_shorten(k) for k, _ in counts.most_common(EXAMPLES_LISTED)]
        spec["empty"] = sum(1 for v in vals if v is None)
        specs.append(spec)
        typed_cols.append(vals)
    rows = [list(r) for r in zip(*typed_cols)] if typed_cols else []
    return specs, rows


_SQL_TYPES = {"number": Float, "date": Date, "datetime": DateTime, "text": Text}


def build_table(dt: DatasetTable) -> Table:
    """The SQLAlchemy Table for a stored dataset table, typed from the schema we saved at load time."""
    cols = [Column("_row_id", Integer, primary_key=True, autoincrement=True)]
    for c in json.loads(dt.columns_json):
        cols.append(Column(c["name"], _SQL_TYPES[c["type"]]))
    return Table(dt.physical_name, MetaData(), *cols)


# ---------------------------------------------------------------------------
# Loading into the database
# ---------------------------------------------------------------------------

def _drop_physical(db: Session, physical_name: str) -> None:
    Table(physical_name, MetaData(), Column("_row_id", Integer, primary_key=True)).drop(db.connection(), checkfirst=True)


def _drop_leftovers(db: Session, dataset_id: int) -> None:
    """After a failed load: drop the physical tables of this dataset that no committed DatasetTable row owns."""
    prefix = f"ds{dataset_id}_"
    keep = set(db.scalars(select(DatasetTable.physical_name).where(DatasetTable.dataset_id == dataset_id)))
    for name in inspect(db.connection()).get_table_names():
        if name.startswith(prefix) and name not in keep:
            _drop_physical(db, name)
    db.commit()


def _next_physical_name(db: Session, dataset: Dataset) -> str:
    used = set(db.scalars(select(DatasetTable.physical_name).where(DatasetTable.dataset_id == dataset.id)))
    n = 1
    while f"ds{dataset.id}_{n}" in used:
        n += 1
    return f"ds{dataset.id}_{n}"


def load_table(db: Session, dataset: Dataset, raw: RawTable, *, source_file: str, file_hash: str) -> DatasetTable:
    specs, rows = analyse(raw)
    name = raw.name
    taken = set(db.scalars(select(DatasetTable.name).where(DatasetTable.dataset_id == dataset.id)))
    base, k = name, 2
    while name in taken:
        name, k = f"{base}_{k}", k + 1
    dt = DatasetTable(dataset_id=dataset.id, name=name, physical_name=_next_physical_name(db, dataset),
                      source_file=source_file, file_hash=file_hash, row_count=len(rows), columns_json=json.dumps(specs, ensure_ascii=False))
    table = build_table(dt)
    _drop_physical(db, dt.physical_name)  # never reuse a leftover table from a failed earlier load
    table.create(db.connection())
    col_names = [c["name"] for c in specs]
    for start in range(0, len(rows), INSERT_CHUNK):
        db.execute(table.insert(), [dict(zip(col_names, r)) for r in rows[start:start + INSERT_CHUNK]])
    db.add(dt)
    db.flush()
    return dt


def _drop_dataset_table(db: Session, dt: DatasetTable) -> None:
    _drop_physical(db, dt.physical_name)
    db.delete(dt)
    db.flush()


def delete_dataset(db: Session, dataset: Dataset) -> None:
    """Remove a dataset, its tables, and the chats that were about it."""
    conv_ids = list(db.scalars(select(ConversationDataset.conversation_id).where(ConversationDataset.dataset_id == dataset.id)))
    if conv_ids:
        db.execute(delete(ConversationDataset).where(ConversationDataset.conversation_id.in_(conv_ids)))
        db.execute(Run.__table__.update().where(Run.conversation_id.in_(conv_ids)).values(conversation_id=None))
        db.execute(delete(Message).where(Message.conversation_id.in_(conv_ids)))
        db.execute(delete(Conversation).where(Conversation.id.in_(conv_ids)))
    for dt in list(db.scalars(select(DatasetTable).where(DatasetTable.dataset_id == dataset.id))):
        _drop_dataset_table(db, dt)
    db.execute(delete(DatasetRename).where(DatasetRename.dataset_id == dataset.id))
    db.delete(dataset)
    db.commit()


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# Folder datasets (data/)
# ---------------------------------------------------------------------------

@dataclass
class FolderDataset:
    slug: str
    origin: str
    files: list[Path]
    config: dict = field(default_factory=dict)


def _data_files(folder: Path) -> list[Path]:
    return sorted(p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in DATA_EXTENSIONS and not p.name.startswith((".", "~")))


def scan_data_dir(data_dir: Path) -> list[FolderDataset]:
    found: dict[str, FolderDataset] = {}
    if not data_dir.exists():
        return []
    for child in sorted(data_dir.iterdir()):
        if child.name.startswith("."):
            continue
        if child.is_dir():
            files = _data_files(child)
            if files:
                cfg = {}
                cfg_path = child / "dataset.yaml"
                if cfg_path.exists():
                    try:
                        cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
                    except yaml.YAMLError as exc:
                        print(f"dataset config skipped ({cfg_path.name} in {child.name}): {exc}", flush=True)
                found[slugify(child.name, "data")] = FolderDataset(slugify(child.name, "data"), child.name + "/", files, cfg if isinstance(cfg, dict) else {})
        elif child.suffix.lower() in DATA_EXTENSIONS:
            slug = slugify(child.stem, "data")
            if slug in found:
                slug += "_file"
            found[slug] = FolderDataset(slug, child.name, [child])
    return list(found.values())


def _apply_config(db: Session, ds: Dataset, fd: FolderDataset) -> None:
    cfg = fd.config
    renamed = db.get(DatasetRename, ds.id)  # a name someone chose beats the title in dataset.yaml
    ds.name = renamed.name if renamed else str(cfg.get("title") or fd.slug.replace("_", " ").title())[:80]
    ds.description = str(cfg.get("description") or "")
    ds.notes = str(cfg.get("notes") or "")
    ds.features = ",".join(str(f) for f in (cfg.get("features") or []))[:200]
    ds.starters = json.dumps([str(s) for s in (cfg.get("starters") or [])][:6], ensure_ascii=False)


def sync_folder_datasets(db: Session, data_dir: Path | None = None) -> dict:
    """Make the database match data/: load new or changed files, drop what is gone. Safe to run on every start."""
    data_dir = data_dir or (ROOT / "data")
    report = {"folder_datasets": 0, "tables_loaded": 0, "tables_dropped": 0, "skipped": []}
    found = scan_data_dir(data_dir)
    existing = {d.slug: d for d in db.scalars(select(Dataset).where(Dataset.source == "folder"))}

    for slug, ds in existing.items():
        if slug not in {f.slug for f in found}:
            report["tables_dropped"] += db.scalar(select(func.count()).select_from(DatasetTable).where(DatasetTable.dataset_id == ds.id)) or 0
            delete_dataset(db, ds)

    for fd in found:
        ds = existing.get(fd.slug)
        if ds is None:
            ds = Dataset(slug=fd.slug, name=fd.slug, source="folder", origin=fd.origin)
            db.add(ds)
            db.flush()
        _apply_config(db, ds, fd)
        ds.origin = fd.origin
        current: dict[str, list[DatasetTable]] = {}
        for dt in ds.tables:
            current.setdefault(dt.source_file, []).append(dt)
        wanted = {p.name for p in fd.files}
        for gone in set(current) - wanted:
            for dt in current[gone]:
                _drop_dataset_table(db, dt)
                report["tables_dropped"] += 1
        for path in fd.files:
            data = path.read_bytes()
            h = _hash(data)
            have = current.get(path.name, [])
            if have and all(dt.file_hash == h for dt in have):
                continue
            for dt in have:
                _drop_dataset_table(db, dt)
            try:
                for raw in read_tables(path.name, data):
                    load_table(db, ds, raw, source_file=path.name, file_hash=h)
                    report["tables_loaded"] += 1
            except DatasetError as exc:
                report["skipped"].append(f"{fd.slug}/{path.name}: {exc}")
        db.commit()
        db.expire(ds, ["tables"])
    report["folder_datasets"] = len(found)
    return report


# ---------------------------------------------------------------------------
# Uploaded and linked datasets
# ---------------------------------------------------------------------------

def uploaded_count(db: Session) -> int:
    return db.scalar(select(func.count()).select_from(Dataset).where(Dataset.source != "folder")) or 0


def files_in(ds: Dataset) -> int:
    """How many files the dataset was made from (a workbook with several sheets is one file)."""
    return len({t.file_hash for t in ds.tables})


def _load_file(db: Session, ds: Dataset, tables: list[RawTable], filename: str, file_hash: str) -> None:
    """Load the tables of one file into `ds`, or leave nothing of this file behind."""
    ds_id = ds.id
    try:
        for raw in tables:
            load_table(db, ds, raw, source_file=filename, file_hash=file_hash)
        db.commit()
    except Exception:
        db.rollback()
        # SQLite does not roll back CREATE TABLE: remove whatever this failed load left behind.
        _drop_leftovers(db, ds_id)
        raise
    db.expire(ds, ["tables"])


def add_dataset(db: Session, *, name: str, filename: str, data: bytes, source: str, origin: str, user_id: int | None) -> Dataset:
    """Create a dataset from one uploaded/fetched file. Parses first, so a bad file leaves nothing behind."""
    if uploaded_count(db) >= settings.max_uploaded_datasets:
        raise DatasetError(f"You already have {settings.max_uploaded_datasets} added datasets, the maximum. Delete one to add another.")
    if len(data) > settings.max_dataset_mb * 1024 * 1024:
        raise DatasetError(f"The file is larger than {settings.max_dataset_mb} MB.")
    tables = read_tables(filename, data)
    title = (name or "").strip() or Path(filename).stem
    slug = base = slugify(title, "dataset")
    n = 2
    while db.scalar(select(Dataset.id).where(Dataset.slug == slug)):
        slug, n = f"{base}_{n}", n + 1
    ds = Dataset(slug=slug, name=title[:80], source=source, origin=origin[:500], created_by=user_id,
                 description="", notes="", features="", starters="[]")
    db.add(ds)
    db.flush()
    _load_file(db, ds, tables, filename, _hash(data))
    return ds


def check_can_add_file(ds: Dataset) -> None:
    if ds.source == "folder":
        raise DatasetError("Built-in datasets come from the data/ folder: put the file in that folder instead.")
    if files_in(ds) >= settings.max_files_per_dataset:
        raise DatasetError(f"This tab already has {settings.max_files_per_dataset} files, the maximum. Add the file as a new dataset instead.")


def add_file(db: Session, ds: Dataset, *, filename: str, data: bytes) -> Dataset:
    """Add one more file to an added dataset (a tab holds up to `max_files_per_dataset` files)."""
    check_can_add_file(ds)
    if len(data) > settings.max_dataset_mb * 1024 * 1024:
        raise DatasetError(f"The file is larger than {settings.max_dataset_mb} MB.")
    h = _hash(data)
    if h in {t.file_hash for t in ds.tables}:
        raise DatasetError("That file is already in this tab.")
    tables = read_tables(filename, data)
    _load_file(db, ds, tables, filename, h)
    return ds


def rename_dataset(db: Session, ds: Dataset, name: str) -> Dataset:
    """Give a tab a new name. For a built-in dataset the name is remembered so a restart keeps it."""
    name = " ".join((name or "").split())[:80]
    if not name:
        raise DatasetError("The name cannot be empty.")
    ds.name = name
    if ds.source == "folder":
        row = db.get(DatasetRename, ds.id)
        if row is None:
            db.add(DatasetRename(dataset_id=ds.id, name=name))
        else:
            row.name = name
    db.commit()
    return ds


# ---------------------------------------------------------------------------
# Reading the registry
# ---------------------------------------------------------------------------

def list_datasets(db: Session) -> list[Dataset]:
    return list(db.scalars(select(Dataset).order_by(Dataset.source != "folder", Dataset.id)))


def default_dataset(db: Session) -> Dataset | None:
    ds = db.scalar(select(Dataset).where(Dataset.slug == DEFAULT_SLUG))
    return ds or db.scalar(select(Dataset).order_by(Dataset.source != "folder", Dataset.id).limit(1))


def dataset_for_conversation(db: Session, conversation: Conversation) -> Dataset | None:
    link = db.get(ConversationDataset, conversation.id)
    return (db.get(Dataset, link.dataset_id) if link else None) or default_dataset(db)


def features_of(ds: Dataset) -> set[str]:
    return {f for f in (ds.features or "").split(",") if f}


def table_by_name(db: Session, ds: Dataset, name: str | None) -> DatasetTable | None:
    tables = list(ds.tables)
    if not name:
        return tables[0] if len(tables) == 1 else None
    want = slugify(name)
    for t in tables:
        if t.name == want:
            return t
    return None


def dataset_summary(ds: Dataset) -> dict:
    return {
        "id": ds.id, "slug": ds.slug, "name": ds.name, "description": ds.description, "source": ds.source,
        "deletable": ds.source != "folder", "files": files_in(ds), "max_files": settings.max_files_per_dataset,
        "can_add_files": ds.source != "folder" and files_in(ds) < settings.max_files_per_dataset, "features": sorted(features_of(ds)), "starters": json.loads(ds.starters or "[]"),
        "tables": [{"name": t.name, "rows": t.row_count, "source_file": t.source_file,
                    "columns": [{"name": c["name"], "type": c["type"]} for c in json.loads(t.columns_json)]} for t in ds.tables],
    }


def schema_text(ds: Dataset) -> str:
    """The schema block for the system prompt: what is in each table, with value hints per column."""
    out = []
    for t in ds.tables:
        out.append(f"### Table `{t.name}` — {t.row_count:,} rows (from {t.source_file.split('|')[0]})")
        for c in json.loads(t.columns_json):
            line = f"- `{c['name']}` ({c['type']})"
            if c["label"] and c["label"] != c["name"]:
                line += f", header \"{c['label']}\""
            if c["type"] == "number":
                line += f" — from {c['min']:g} to {c['max']:g}"
            elif c["type"] in ("date", "datetime"):
                line += f" — from {c['min']} to {c['max']}"
            elif "values" in c:
                line += " — values: " + ", ".join(c["values"])
            else:
                line += f" — {c['distinct']:,} distinct, e.g. " + ", ".join(c["examples"])
            if c.get("empty"):
                line += f" ({c['empty']:,} empty)"
            out.append(line)
        out.append("")
    return "\n".join(out).strip()
