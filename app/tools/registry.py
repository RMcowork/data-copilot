"""Tool definitions the model sees, and the dispatcher that runs them.

Every dataset gets the two data tools (`data_query`, `data_rows`), limited to its own tables.
A dataset whose dataset.yaml lists the `invoices` feature also gets the supplier and invoice tools.
"""
from __future__ import annotations

import json
from typing import Any

from sqlalchemy.orm import Session

from ..datasets import features_of
from ..db import Dataset
from . import data, invoices

FILTER_SCHEMA = {
    "type": "array",
    "description": "Conditions that ALL must hold. Text matching is case-insensitive.",
    "items": {
        "type": "object",
        "properties": {
            "column": {"type": "string"},
            "op": {"type": "string", "enum": data.OPS,
                   "description": "eq/neq/gt/gte/lt/lte compare; contains is a text substring; in takes a list; between takes [low, high] (inclusive); is_empty/not_empty take no value."},
            "value": {"description": "A string, number, list (for in) or [low, high] (for between). Dates as YYYY-MM-DD."},
        },
        "required": ["column", "op"],
        "additionalProperties": False,
    },
}


def data_tools(ds: Dataset) -> list[dict[str, Any]]:
    table_names = [t.name for t in ds.tables]
    table_prop = {"type": "string", "enum": table_names, "description": "Which table to read. Optional when the dataset has one table."}
    return [
        {
            "name": "data_query",
            "description": (
                "Read-only aggregate query over one table of this dataset. Use it for any question about totals, counts, averages, "
                "rankings, breakdowns or trends. Pick a metric (count rows, or sum/avg/min/max/count_distinct of a column), "
                "optionally group by a column (a date column is grouped by day/week/month/year), and filter. "
                "Never guess numbers: if the answer needs data, call this tool."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "table": table_prop,
                    "metric": {"type": "string", "enum": data.METRICS, "description": "count = number of rows; sum and avg need a number column; min/max work on numbers and dates; count_distinct counts different values."},
                    "column": {"type": "string", "description": "The column the metric is computed over. Not needed for count."},
                    "group_by": {"type": "string", "description": "Column to break the result down by. Leave out for a single total."},
                    "time_bucket": {"type": "string", "enum": data.TIME_BUCKETS, "description": "Only when group_by is a date column. Default day."},
                    "filters": FILTER_SCHEMA,
                    "order": {"type": "string", "enum": ["desc", "asc"], "default": "desc", "description": "Ranking direction for non-date groupings."},
                    "top_n": {"type": "integer", "minimum": 1, "maximum": data.MAX_GROUPS, "default": 10},
                },
                "required": ["metric"],
                "additionalProperties": False,
            },
        },
        {
            "name": "data_rows",
            "description": (
                "Read-only look at actual rows of one table: to see what the data looks like, find a specific record, or list the "
                "top rows by a column. Returns at most 50 rows and the total number of matching rows. "
                "For totals and rankings over many rows use data_query instead."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "table": table_prop,
                    "columns": {"type": "array", "items": {"type": "string"}, "description": "Columns to return; all if left out."},
                    "filters": FILTER_SCHEMA,
                    "order_by": {"type": "string"},
                    "order": {"type": "string", "enum": ["asc", "desc"], "default": "asc"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": data.MAX_ROWS, "default": 10},
                },
                "required": [],
                "additionalProperties": False,
            },
        },
    ]


INVOICE_TOOLS: list[dict[str, Any]] = [
    {
        "name": "lookup_supplier",
        "description": "Look up a supplier by (partial) name in the supplier list. Returns the canonical name and the items and usual prices we buy from them. Call it before logging an invoice to normalise the supplier name.",
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "supplier name as written on the invoice"}},
            "required": ["name"],
            "additionalProperties": False,
        },
    },
    {
        "name": "log_invoice",
        "description": (
            "Record one supplier invoice as one row. Pass the fields exactly as read from the document; "
            "pass amount as null if you cannot read it with confidence, and the row will be marked needs_a_look. "
            "Always pass source_ref (the image reference given in the message) so the same invoice is never logged twice. "
            "Never call this to modify or delete an existing row."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "supplier": {"type": "string"},
                "invoice_number": {"type": ["string", "null"]},
                "amount": {"type": ["number", "null"], "description": "total amount, numbers only; null if unreadable"},
                "currency": {"type": "string", "default": "ILS"},
                "invoice_date": {"type": ["string", "null"], "description": "YYYY-MM-DD"},
                "source_ref": {"type": ["string", "null"], "description": "the image reference from the message, e.g. img_8f3a2c"},
                "notes": {"type": ["string", "null"]},
            },
            "required": ["supplier", "amount"],
            "additionalProperties": False,
        },
    },
]

MAX_RESULT_CHARS = 12000


def tools_for(ds: Dataset) -> list[dict[str, Any]]:
    """Stable order, so the tool block stays cacheable for a given dataset."""
    tools = data_tools(ds)
    if "invoices" in features_of(ds):
        tools += INVOICE_TOOLS
    return tools


def execute(name: str, tool_input: dict[str, Any], db: Session, *, ds: Dataset, user_id: int | None = None) -> str:
    """Run one tool for a dataset and return its result as a JSON string for the model."""
    allowed = {t["name"] for t in tools_for(ds)}
    try:
        if name not in allowed:
            result = {"error": f"tool '{name}' is not available in this dataset"}
        elif name == "data_query":
            result = data.data_query(db, ds, **_clean(tool_input))
        elif name == "data_rows":
            result = data.data_rows(db, ds, **_clean(tool_input))
        elif name == "lookup_supplier":
            result = invoices.lookup_supplier(db, name=str(tool_input.get("name", "")))
        elif name == "log_invoice":
            result = invoices.log_invoice(db, logged_by=user_id, **_clean(tool_input))
        else:
            result = {"error": f"unknown tool '{name}'"}
    except Exception as exc:  # the loop must survive a bad input; the model sees the error
        result = {"error": f"{type(exc).__name__}: {exc}"}
    text = json.dumps(result, ensure_ascii=False, default=str)
    if len(text) > MAX_RESULT_CHARS:
        text = text[:MAX_RESULT_CHARS] + '... (truncated)"}'
    return text


def _clean(d: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in d.items() if v is not None}
