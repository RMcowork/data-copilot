"""Unit tests: the deterministic parts. Same input, same output, every time."""
import pytest

from app import datasets
from app.tools.data import data_query, data_rows
from app.tools.invoices import log_invoice, lookup_supplier, parse_amount


@pytest.mark.parametrize("raw,expected", [
    ("₪1,240.00", 1240.0), ("1 240", 1240.0), ("NIS 85", 85.0), (1240, 1240.0), (99.5, 99.5),
    ("TBD", None), ("", None), (None, None), ("-20", None), ("call us", None),
])
def test_parse_amount(raw, expected):
    assert parse_amount(raw) == expected


def test_log_invoice_same_source_ref_is_one_row(client, db):
    first = log_invoice(db, supplier="Levi Flour", invoice_number="A-1", amount=1240, source_ref="img_t1")
    second = log_invoice(db, supplier="Levi Flour", invoice_number="A-1", amount=1240, source_ref="img_t1")
    assert first["created"] is True
    assert second["created"] is False and second["reason"] == "duplicate"
    assert second["id"] == first["id"]


def test_log_invoice_same_supplier_and_number_is_one_row(client, db):
    a = log_invoice(db, supplier="Golan Dairy", invoice_number="7781", amount=500, source_ref="img_t2a")
    b = log_invoice(db, supplier="golan dairy", invoice_number="7781", amount=500, source_ref="img_t2b")
    assert b["created"] is False and b["id"] == a["id"]


def test_unreadable_amount_goes_to_needs_a_look(client, db):
    row = log_invoice(db, supplier="Negev Eggs", invoice_number="9", amount=None, source_ref="img_t3")
    assert row["status"] == "needs_a_look"
    assert row["amount"] is None
    assert "amount unreadable" in (row["notes"] or "")


def test_large_amount_is_flagged_not_blocked(client, db):
    row = log_invoice(db, supplier="Big Oven Ltd", invoice_number="X1", amount="₪6,200", source_ref="img_t4")
    assert row["status"] == "logged" and row["flagged"] is True and row["amount"] == 6200.0


def test_lookup_supplier_partial_match(client, db):
    res = lookup_supplier(db, name="vanilla")
    assert res["known_supplier"] is True
    assert any("Vanilla" in m["supplier"] for m in res["matches"])
    assert lookup_supplier(db, name="nobody-here")["known_supplier"] is False


@pytest.fixture
def bakery(db):
    return datasets.default_dataset(db)


COMPLETED = [{"column": "status", "op": "eq", "value": "completed"}]


def test_data_query_totals_match_the_dataset(client, db, bakery):
    # The Lesson 4/5 dataset: completed revenue ₪647,065 · 40,194 items · 14,196 completed orders
    # (14,421 orders in total once refunded lines are included).
    rev = data_query(db, bakery, table="bakery_sales", metric="sum", column="line_total", filters=COMPLETED)
    assert round(rev["value"]) == 647065
    items = data_query(db, bakery, table="bakery_sales", metric="sum", column="quantity", filters=COMPLETED)
    assert items["value"] == 40194
    orders = data_query(db, bakery, table="bakery_sales", metric="count_distinct", column="order_id", filters=COMPLETED)
    assert orders["value"] == 14196
    everything = data_query(db, bakery, table="bakery_sales", metric="count_distinct", column="order_id")
    assert everything["value"] == 14421


def test_branch_names_are_cleaned_at_the_door(client, db, bakery):
    # The CSV has "Herzliya " with a trailing space; the loader strips it, so there are exactly four branches.
    res = data_query(db, bakery, table="bakery_sales", metric="count", group_by="branch")
    assert res["row_count"] == 4
    assert sorted(r["key"] for r in res["rows"]) == ["Herzliya", "Ramat Gan", "Tel Aviv - Dizengoff", "Tel Aviv - Florentin"]


def test_data_query_ranking_trend_and_filters(client, db, bakery):
    top = data_query(db, bakery, table="bakery_sales", metric="sum", column="quantity", group_by="product", top_n=3)
    assert top["row_count"] == 3
    assert top["rows"][0]["value"] >= top["rows"][1]["value"] >= top["rows"][2]["value"]
    august = [{"column": "order_datetime", "op": "between", "value": ["2026-08-01", "2026-08-31"]}]
    month = data_query(db, bakery, table="bakery_sales", metric="sum", column="line_total", group_by="branch", filters=august)
    assert 0 < month["row_count"] <= 4
    trend = data_query(db, bakery, table="bakery_sales", metric="sum", column="line_total", group_by="order_datetime",
                       time_bucket="month", filters=august)
    assert [r["key"] for r in trend["rows"]] == ["2026-08"]  # a date-only bound includes the whole last day
    assert abs(trend["rows"][0]["value"] - sum(r["value"] for r in month["rows"])) < 0.01


def test_data_query_rejects_bad_requests_with_a_hint(client, db, bakery):
    assert data_query(db, bakery, table="bakery_sales", metric="nope")["error"].startswith("unknown metric")
    assert "needs a number column" in data_query(db, bakery, table="bakery_sales", metric="sum", column="branch")["error"]
    bad_col = data_query(db, bakery, table="bakery_sales", metric="count", group_by="color")
    assert "unknown group_by column" in bad_col["error"] and "branch" in bad_col["tables"]["bakery_sales"]
    assert "not a date" in data_query(db, bakery, table="bakery_sales", metric="count",
                                      filters=[{"column": "order_datetime", "op": "gte", "value": "31/08/2026"}])["error"]
    assert "unknown table" in data_query(db, bakery, table="payroll", metric="count")["error"]


def test_data_rows_lists_rows_and_counts_matches(client, db, bakery):
    res = data_rows(db, bakery, table="suppliers", filters=[{"column": "currency", "op": "eq", "value": "eur"}], limit=2)
    assert res["returned"] == 2 and res["total_matching"] >= 2
    assert set(res["rows"][0]) == {"item", "supplier", "unit", "price", "currency"}
    assert data_rows(db, bakery, table="suppliers", limit=500)["returned"] == 10  # capped at the table size; never above 50
