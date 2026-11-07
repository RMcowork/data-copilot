"""data_query and data_rows: structured, read-only views of any dataset table.

The model does not write SQL. It names a table, a metric, a grouping and filters; this module
checks every name against the schema stored at load time and builds the query. The tools can
reach only the tables of the dataset the conversation belongs to, and cannot write (Lesson 5's
least-access rule, applied to an agent).
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta

from sqlalchemy import Table, and_, func, or_, select
from sqlalchemy.orm import Session

from ..datasets import build_table, slugify, to_number
from ..db import Dataset, DatasetTable

METRICS = ["count", "sum", "avg", "min", "max", "count_distinct"]
TIME_BUCKETS = ["day", "week", "month", "year"]
OPS = ["eq", "neq", "gt", "gte", "lt", "lte", "contains", "in", "between", "is_empty", "not_empty"]
MAX_GROUPS = 50
MAX_SERIES = 400
MAX_ROWS = 50


class QueryError(ValueError):
    pass


def _resolve_table(ds: Dataset, name: str | None) -> DatasetTable:
    tables = list(ds.tables)
    if not tables:
        raise QueryError("This dataset has no tables.")
    if not name:
        if len(tables) == 1:
            return tables[0]
        raise QueryError(f"Say which table: {[t.name for t in tables]}")
    want = slugify(name)
    for t in tables:
        if t.name == want:
            return t
    raise QueryError(f"unknown table '{name}'", )


def _columns(dt: DatasetTable) -> dict[str, dict]:
    return {c["name"]: c for c in json.loads(dt.columns_json)}


def _col(cols: dict[str, dict], name: str, what: str = "column") -> dict:
    if name in cols:
        return cols[name]
    lowered = {k.lower(): v for k, v in cols.items()}
    labels = {c["label"].lower(): c for c in cols.values()}
    if str(name).lower() in lowered:
        return lowered[str(name).lower()]
    if str(name).lower() in labels:
        return labels[str(name).lower()]
    raise QueryError(f"unknown {what} '{name}'. Columns: {list(cols)}")


def _parse_when(value) -> tuple[datetime, bool]:
    """(moment, date_only) from 'YYYY-MM-DD' or 'YYYY-MM-DD HH:MM[:SS]'."""
    s = str(value).strip()
    for fmt, date_only in (("%Y-%m-%d", True), ("%Y-%m-%d %H:%M:%S", False), ("%Y-%m-%d %H:%M", False),
                           ("%Y-%m-%dT%H:%M:%S", False), ("%Y-%m-%dT%H:%M", False)):
        try:
            return datetime.strptime(s, fmt), date_only
        except ValueError:
            continue
    raise QueryError(f"'{value}' is not a date; use YYYY-MM-DD")


def _like_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _filter_clause(table: Table, cols: dict[str, dict], f: dict):
    spec = _col(cols, f.get("column", ""))
    col = table.c[spec["name"]]
    ctype = spec["type"]
    op = f.get("op", "eq")
    if op not in OPS:
        raise QueryError(f"unknown op '{op}'. Allowed: {OPS}")
    value = f.get("value")

    if op == "is_empty":
        return or_(col.is_(None), col == "") if ctype == "text" else col.is_(None)
    if op == "not_empty":
        return and_(col.is_not(None), col != "") if ctype == "text" else col.is_not(None)
    if value is None:
        raise QueryError(f"filter on '{spec['name']}' needs a value")

    if ctype == "number":
        def num(v):
            n = to_number(v)
            if n is None:
                raise QueryError(f"'{v}' is not a number (column '{spec['name']}')")
            return n
        if op in ("in",):
            return col.in_([num(v) for v in _as_list(value)])
        if op == "between":
            lo, hi = _pair(value)
            return col.between(num(lo), num(hi))
        if op == "contains":
            raise QueryError(f"'contains' works on text columns; '{spec['name']}' is a number")
        return _compare(col, op, num(value))

    if ctype in ("date", "datetime"):
        def when(v):
            return _parse_when(v)
        if op == "contains":
            raise QueryError(f"'contains' works on text columns; '{spec['name']}' is a {ctype}")
        if op == "in":
            return or_(*[_date_clause(col, ctype, "eq", *when(v)) for v in _as_list(value)])
        if op == "between":
            lo, hi = _pair(value)
            return and_(_date_clause(col, ctype, "gte", *when(lo)), _date_clause(col, ctype, "lte", *when(hi)))
        return _date_clause(col, ctype, op, *when(value))

    text = func.lower(col)
    if op == "in":
        return text.in_([str(v).lower() for v in _as_list(value)])
    if op == "contains":
        return text.like(f"%{_like_escape(str(value).lower())}%", escape="\\")
    if op == "between":
        lo, hi = _pair(value)
        return col.between(str(lo), str(hi))
    return _compare(text, op, str(value).lower())


def _compare(expr, op: str, v):
    return {"eq": expr == v, "neq": expr != v, "gt": expr > v, "gte": expr >= v, "lt": expr < v, "lte": expr <= v}[op]


def _date_clause(col, ctype: str, op: str, moment: datetime, date_only: bool):
    """Compare a date/datetime column. A date-only value on a datetime column means the whole day."""
    if ctype == "date":
        return _compare(col, op, moment.date())
    start = moment
    nxt = moment + timedelta(days=1)
    if not date_only:
        return _compare(col, op, moment)
    return {"eq": and_(col >= start, col < nxt), "neq": or_(col < start, col >= nxt), "gt": col >= nxt, "gte": col >= start,
            "lt": col < start, "lte": col < nxt}[op]


def _as_list(v) -> list:
    return list(v) if isinstance(v, (list, tuple)) else [v]


def _pair(v) -> tuple:
    if not isinstance(v, (list, tuple)) or len(v) != 2:
        raise QueryError("'between' needs a value like [low, high]")
    return v[0], v[1]


def _bucket(db: Session, col, bucket: str):
    sqlite = db.get_bind().dialect.name == "sqlite"
    if sqlite:
        fmt = {"day": "%Y-%m-%d", "week": "%Y-W%W", "month": "%Y-%m", "year": "%Y"}[bucket]
        return func.strftime(fmt, col)
    fmt = {"day": "YYYY-MM-DD", "week": 'IYYY-"W"IW', "month": "YYYY-MM", "year": "YYYY"}[bucket]
    return func.to_char(col, fmt)


def _jsonable(v):
    if isinstance(v, (date, datetime)):
        return str(v)
    if isinstance(v, float):
        return round(v, 2)
    return v


def data_query(db: Session, ds: Dataset, *, table: str | None = None, metric: str = "count", column: str | None = None,
               group_by: str | None = None, time_bucket: str | None = None, filters: list | None = None,
               order: str = "desc", top_n: int = 10) -> dict:
    try:
        dt = _resolve_table(ds, table)
        cols = _columns(dt)
        tbl = build_table(dt)
        if metric not in METRICS:
            raise QueryError(f"unknown metric '{metric}'. Allowed: {METRICS}")

        mcol = None
        if metric != "count" or column:
            if not column:
                raise QueryError(f"metric '{metric}' needs a column")
            mcol = _col(cols, column)
            if metric in ("sum", "avg") and mcol["type"] != "number":
                raise QueryError(f"'{metric}' needs a number column; '{mcol['name']}' is {mcol['type']}")
        c = tbl.c[mcol["name"]] if mcol else None
        value_expr = {"count": func.count(c) if c is not None else func.count(), "sum": func.sum(c) if c is not None else None,
                      "avg": func.avg(c) if c is not None else None, "min": func.min(c) if c is not None else None,
                      "max": func.max(c) if c is not None else None,
                      "count_distinct": func.count(func.distinct(c)) if c is not None else None}[metric]

        where = [_filter_clause(tbl, cols, f) for f in (filters or [])]
        applied = [{"column": _col(cols, f["column"])["name"], "op": f.get("op", "eq"), "value": f.get("value")} for f in (filters or [])]
        head = {"table": dt.name, "metric": metric, "column": mcol["name"] if mcol else None, "filters": applied}

        if not group_by:
            row = db.execute(select(value_expr.label("value"), func.count().label("n")).select_from(tbl).where(*where)).one()
            return {**head, "group_by": None, "value": _jsonable(row.value if row.value is not None else 0), "rows_matched": int(row.n)}

        gspec = _col(cols, group_by, "group_by column")
        gcol = tbl.c[gspec["name"]]
        is_time = gspec["type"] in ("date", "datetime")
        bucket = None
        if is_time:
            bucket = time_bucket or "day"
            if bucket not in TIME_BUCKETS:
                raise QueryError(f"unknown time_bucket '{bucket}'. Allowed: {TIME_BUCKETS}")
            gexpr = _bucket(db, gcol, bucket)
        else:
            if time_bucket:
                raise QueryError(f"time_bucket needs a date column; '{gspec['name']}' is {gspec['type']}")
            gexpr = gcol
        stmt = (select(gexpr.label("key"), value_expr.label("value"), func.count().label("n")).select_from(tbl)
                .where(*where).group_by(gexpr))
        if is_time:
            stmt = stmt.order_by(gexpr).limit(MAX_SERIES)
        else:
            ordered = value_expr.asc() if order == "asc" else value_expr.desc()
            stmt = stmt.order_by(ordered.nulls_last() if hasattr(ordered, "nulls_last") else ordered).limit(max(1, min(int(top_n or 10), MAX_GROUPS)))
        rows = [{"key": "(empty)" if r.key in (None, "") else _jsonable(r.key) if not isinstance(r.key, str) else r.key,
                 "value": _jsonable(r.value if r.value is not None else 0), "count": int(r.n)} for r in db.execute(stmt)]
        return {**head, "group_by": gspec["name"], "time_bucket": bucket, "rows": rows, "row_count": len(rows)}
    except QueryError as exc:
        return {"error": str(exc), **_help(ds)}


def data_rows(db: Session, ds: Dataset, *, table: str | None = None, columns: list | None = None, filters: list | None = None,
              order_by: str | None = None, order: str = "asc", limit: int = 10) -> dict:
    try:
        dt = _resolve_table(ds, table)
        cols = _columns(dt)
        tbl = build_table(dt)
        names = [_col(cols, c)["name"] for c in columns] if columns else list(cols)
        where = [_filter_clause(tbl, cols, f) for f in (filters or [])]
        limit = max(1, min(int(limit or 10), MAX_ROWS))
        stmt = select(*[tbl.c[n] for n in names]).where(*where)
        if order_by:
            oc = tbl.c[_col(cols, order_by, "order_by column")["name"]]
            stmt = stmt.order_by(oc.desc() if order == "desc" else oc.asc())
        else:
            stmt = stmt.order_by(tbl.c._row_id)
        total = db.scalar(select(func.count()).select_from(tbl).where(*where)) or 0
        rows = [{n: _jsonable(v) for n, v in zip(names, r)} for r in db.execute(stmt.limit(limit))]
        return {"table": dt.name, "columns": names, "rows": rows, "returned": len(rows), "total_matching": int(total)}
    except QueryError as exc:
        return {"error": str(exc), **_help(ds)}


def _help(ds: Dataset) -> dict:
    return {"tables": {t.name: [c["name"] for c in json.loads(t.columns_json)] for t in ds.tables}}
