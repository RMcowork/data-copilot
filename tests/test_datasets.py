"""Datasets: loading any CSV/Excel file, the folder scan, upload and link intake, tab isolation."""
import io
import json

import httpx
import pytest
from openpyxl import Workbook
from sqlalchemy import inspect

from app import datasets, fetch
from app.config import settings
from app.db import Conversation, ConversationDataset, Dataset, DatasetTable, SessionLocal, engine
from app.tools.data import data_query, data_rows
from app.tools.registry import execute, tools_for
from tests.conftest import read_sse

ORDERS = "id,when,amount,city,zip\nA1,2026-01-05,10.5,Haifa,01234\nA2,2026-01-06,\"1,200\",Haifa,05678\nA3,2026-02-01,7,Eilat,09999\n"


@pytest.fixture
def limits(monkeypatch):
    """Swap in a copy of the (frozen) settings with some limits changed, everywhere they are read."""
    import dataclasses

    import app.main as main

    def apply(**changes):
        changed = dataclasses.replace(settings, **changes)
        for module in (datasets, fetch, main):
            monkeypatch.setattr(module, "settings", changed)
    return apply


@pytest.fixture(autouse=True)
def _clean_added(client):
    """Each test starts with no uploaded datasets; the built-in bakery tab stays."""
    yield
    with SessionLocal() as db:
        for ds in db.query(Dataset).filter(Dataset.source != "folder").all():
            datasets.delete_dataset(db, ds)


def _upload(client, name, content, filename="orders.csv", title=""):
    return client.post(f"/api/datasets/upload?filename={filename}&name={title}", content=content,
                       headers={"Content-Type": "application/octet-stream"})


def _xlsx(sheets: dict[str, list[list]]) -> bytes:
    wb = Workbook()
    wb.remove(wb.active)
    for title, rows in sheets.items():
        ws = wb.create_sheet(title)
        for r in rows:
            ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ---------- typing ----------

def test_types_are_inferred_from_the_values():
    raw = datasets.read_tables("orders.csv", ORDERS.encode())[0]
    specs, rows = datasets.analyse(raw)
    assert {c["name"]: c["type"] for c in specs} == {"id": "text", "when": "date", "amount": "number", "city": "text", "zip": "text"}
    assert rows[1][2] == 1200.0          # "1,200" is a number
    assert rows[0][4] == "01234"         # leading zeros: an ID, not a number
    assert specs[3]["values"] == ["Haifa", "Eilat"]


@pytest.mark.parametrize("raw,expected", [("₪1,240.50", 1240.5), (" 12 ", 12.0), ("1e3", 1000.0), ("007", None), ("abc", None), ("", None)])
def test_to_number(raw, expected):
    assert datasets.to_number(raw) == expected


def test_semicolon_hebrew_cp1255_and_messy_headers():
    text = "שם;סכום;שם\nדנה;5;x\nרון;7;y\n"
    raw = datasets.read_tables("x.csv", text.encode("cp1255"))[0]
    assert raw.headers == ["שם", "סכום", "שם"] and raw.rows[0][:2] == ["דנה", "5"]
    specs, _ = datasets.analyse(raw)
    assert [c["name"] for c in specs] == ["שם", "סכום", "שם_2"]  # unique, readable names


def test_excel_sheets_become_tables_and_dates_stay_dates():
    data = _xlsx({"Orders": [["id", "day", "total"], [1, __import__("datetime").datetime(2026, 3, 1), 9.5], [2, __import__("datetime").datetime(2026, 3, 2), 4]],
                  "Notes": [["k", "v"], ["a", "b"]]})
    tables = datasets.read_tables("book.xlsx", data)
    assert [t.name for t in tables] == ["orders", "notes"]
    specs, _ = datasets.analyse(tables[0])
    assert [c["type"] for c in specs] == ["number", "date", "number"]


@pytest.mark.parametrize("filename,data,message", [
    ("a.pdf", b"x", "Unsupported file type"),
    ("a.csv", b"", "empty"),
    ("a.csv", b"only,a,header\n", "no data rows"),
    ("a.xlsx", b"not really excel", "Could not read the Excel"),
])
def test_bad_files_get_a_clear_message(filename, data, message):
    with pytest.raises(datasets.DatasetError, match=message):
        datasets.read_tables(filename, data)


def test_row_limit_is_enforced_while_reading(limits):
    limits(max_dataset_rows=2)
    with pytest.raises(datasets.DatasetError, match="more than 2 rows"):
        datasets.read_tables("a.csv", b"x\n1\n2\n3\n")


# ---------- upload, limits, delete ----------

def test_upload_makes_a_queryable_tab(client, db, logged_in):
    r = _upload(client, "orders", ORDERS.encode(), title="Orders 2026")
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["name"] == "Orders 2026" and body["deletable"] is True and body["features"] == []
    assert body["tables"][0]["rows"] == 3
    ds = db.get(Dataset, body["id"])
    res = data_query(db, ds, metric="sum", column="amount", group_by="city")
    assert {r["key"]: r["value"] for r in res["rows"]} == {"Haifa": 1210.5, "Eilat": 7.0}
    assert data_query(db, ds, metric="count", filters=[{"column": "when", "op": "gte", "value": "2026-01-06"}])["value"] == 2


def test_at_most_three_added_datasets(client, logged_in):
    for i in range(3):
        assert _upload(client, "o", ORDERS.encode(), title=f"d{i}").status_code == 201
    fourth = _upload(client, "o", ORDERS.encode(), title="d3")
    assert fourth.status_code == 400 and "maximum" in fourth.json()["detail"]
    listing = client.get("/api/datasets").json()
    assert listing["added"] == 3 and listing["max_added"] == 3
    # Deleting one frees a slot.
    first_added = next(d for d in listing["datasets"] if d["deletable"])
    assert client.delete(f"/api/datasets/{first_added['id']}").status_code == 204
    assert _upload(client, "o", ORDERS.encode(), title="d4").status_code == 201


def test_a_bad_upload_leaves_nothing_behind(client, db, logged_in):
    before = set(inspect(engine).get_table_names())
    assert _upload(client, "x", b"", filename="empty.csv").status_code == 400
    assert _upload(client, "x", b"junk", filename="x.pdf").status_code == 400
    assert set(inspect(engine).get_table_names()) == before
    assert db.query(Dataset).filter(Dataset.source != "folder").count() == 0


def test_oversized_upload_is_refused(client, logged_in, limits):
    limits(max_dataset_mb=0)
    r = _upload(client, "x", ORDERS.encode())
    assert r.status_code == 413


def test_built_in_datasets_cannot_be_deleted_and_login_is_required(client, db, logged_in):
    bakery = datasets.default_dataset(db)
    assert client.delete(f"/api/datasets/{bakery.id}").status_code == 400
    client.cookies.clear()
    assert client.get("/api/datasets").status_code == 401
    assert _upload(client, "o", ORDERS.encode()).status_code == 401


def test_delete_drops_the_table_and_its_chats(client, db, logged_in):
    created = _upload(client, "o", ORDERS.encode(), title="temp").json()
    physical = db.query(DatasetTable).filter_by(dataset_id=created["id"]).one().physical_name
    assert physical in inspect(engine).get_table_names()
    with SessionLocal() as s:
        user = s.query(__import__("app.db", fromlist=["User"]).User).filter_by(name="Dana").one()
        conv = Conversation(user_id=user.id, title="about temp")
        s.add(conv)
        s.commit()
        s.add(ConversationDataset(conversation_id=conv.id, dataset_id=created["id"]))
        s.commit()
        conv_id = conv.id
    assert client.delete(f"/api/datasets/{created['id']}").status_code == 204
    assert physical not in inspect(engine).get_table_names()
    db.expire_all()
    assert db.get(Conversation, conv_id) is None


# ---------- links ----------

def _fake_check(url):  # stands in for the DNS check so tests need no network
    pass


def test_link_downloads_and_loads(client, db, logged_in, monkeypatch):
    transport = httpx.MockTransport(lambda req: httpx.Response(200, content=ORDERS.encode()))
    import app.main as main
    monkeypatch.setattr(main, "fetch_url", lambda url: fetch.fetch_url(url, client=httpx.Client(transport=transport), check=_fake_check))
    r = client.post("/api/datasets/link", json={"url": "https://example.com/data/orders.csv", "name": "Linked"})
    assert r.status_code == 201, r.text
    assert r.json()["tables"][0]["name"] == "orders" and r.json()["source"] == "link"


def test_html_page_is_not_a_data_file():
    transport = httpx.MockTransport(lambda req: httpx.Response(200, content=b"<!DOCTYPE html><html>sign in</html>"))
    with pytest.raises(datasets.DatasetError, match="web page"):
        fetch.fetch_url("https://example.com/x", client=httpx.Client(transport=transport), check=_fake_check)


def test_redirects_are_rechecked_and_capped():
    seen = []

    def check(url):
        seen.append(url)
        if "internal" in url:
            raise datasets.DatasetError("not public")

    transport = httpx.MockTransport(lambda req: httpx.Response(302, headers={"location": "http://internal.example/secret"}))
    with pytest.raises(datasets.DatasetError, match="not public"):
        fetch.fetch_url("https://example.com/x", client=httpx.Client(transport=transport), check=check)
    assert seen == ["https://example.com/x", "http://internal.example/secret"]


def test_download_over_the_size_limit_is_stopped(limits):
    limits(max_dataset_mb=1)
    transport = httpx.MockTransport(lambda req: httpx.Response(200, content=b"a,b\n" + b"1,2\n" * 400_000))
    with pytest.raises(datasets.DatasetError, match="larger than 1 MB"):
        fetch.fetch_url("https://example.com/big.csv", client=httpx.Client(transport=transport), check=_fake_check)


@pytest.mark.parametrize("url", ["http://127.0.0.1/x.csv", "http://localhost:8000/x.csv", "http://10.0.0.5/x.csv",
                                 "http://169.254.169.254/latest/meta-data", "http://[::1]/x.csv", "file:///etc/passwd",
                                 "ftp://example.com/x.csv", "http://user:pw@example.com/x.csv"])
def test_private_and_odd_addresses_are_refused(url):
    with pytest.raises(datasets.DatasetError):
        fetch.check_public_url(url)


def test_google_sheets_links_become_csv_exports():
    n = fetch.normalise_url
    assert n("https://docs.google.com/spreadsheets/d/abc_123-X/edit#gid=77") == "https://docs.google.com/spreadsheets/d/abc_123-X/export?format=csv&gid=77"
    assert n("https://docs.google.com/spreadsheets/d/abc/edit?usp=sharing") == "https://docs.google.com/spreadsheets/d/abc/export?format=csv&gid=0"
    assert n("https://example.com/a.csv") == "https://example.com/a.csv"


# ---------- folders ----------

def test_folder_scan_one_subfolder_is_one_dataset(tmp_path, db):
    (tmp_path / "retail").mkdir()
    (tmp_path / "retail" / "orders.csv").write_text(ORDERS)
    (tmp_path / "retail" / "customers.csv").write_text("id,name\n1,Dana\n2,Ron\n")
    (tmp_path / "retail" / "dataset.yaml").write_text("title: Retail\nnotes: amounts are USD\nstarters: [\"How many orders?\"]\n")
    (tmp_path / "loose.csv").write_text("a,b\n1,2\n")
    (tmp_path / ".hidden").mkdir()
    (tmp_path / "empty").mkdir()
    found = {f.slug: f for f in datasets.scan_data_dir(tmp_path)}
    assert set(found) == {"retail", "loose"} and len(found["retail"].files) == 2


def test_sync_loads_reloads_on_change_and_drops_removed(tmp_path):
    from app.db import Base
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    eng = create_engine(f"sqlite:///{tmp_path / 't.db'}")
    Base.metadata.create_all(eng)
    data = tmp_path / "data"
    (data / "retail").mkdir(parents=True)
    (data / "retail" / "orders.csv").write_text(ORDERS)
    (data / "retail" / "dataset.yaml").write_text("title: Retail\nnotes: amounts are USD\nfeatures: [invoices]\nstarters: [\"How many orders?\"]\n")
    with Session(eng) as s:
        r1 = datasets.sync_folder_datasets(s, data)
        assert r1["tables_loaded"] == 1 and r1["folder_datasets"] == 1
        ds = s.query(Dataset).filter_by(slug="retail").one()
        assert ds.name == "Retail" and ds.notes.strip() == "amounts are USD" and datasets.features_of(ds) == {"invoices"}
        assert json.loads(ds.starters) == ["How many orders?"]

        assert datasets.sync_folder_datasets(s, data)["tables_loaded"] == 0           # unchanged: nothing reloaded
        (data / "retail" / "orders.csv").write_text(ORDERS + "A4,2026-03-01,5,Haifa,00001\n")
        assert datasets.sync_folder_datasets(s, data)["tables_loaded"] == 1           # changed: reloaded
        s.expire_all()
        assert s.query(DatasetTable).one().row_count == 4
        (data / "retail" / "extra.csv").write_text("k,v\na,1\n")                       # a new file joins the same tab
        datasets.sync_folder_datasets(s, data)
        s.expire_all()
        assert sorted(t.name for t in s.query(Dataset).filter_by(slug="retail").one().tables) == ["extra", "orders"]
        (data / "retail" / "dataset.yaml").unlink()
        (data / "retail" / "orders.csv").unlink()
        (data / "retail" / "extra.csv").unlink()
        (data / "retail").rmdir()
        datasets.sync_folder_datasets(s, data)                                         # folder gone: tab gone
        assert s.query(Dataset).count() == 0 and s.query(DatasetTable).count() == 0
        assert not [t for t in inspect(eng).get_table_names() if t.startswith("ds")]


def test_a_broken_file_is_skipped_not_fatal(tmp_path):
    from app.db import Base
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    eng = create_engine(f"sqlite:///{tmp_path / 't.db'}")
    Base.metadata.create_all(eng)
    (tmp_path / "d" / "good").mkdir(parents=True)
    (tmp_path / "d" / "good" / "ok.csv").write_text("a\n1\n")
    (tmp_path / "d" / "good" / "bad.csv").write_text("")
    with Session(eng) as s:
        report = datasets.sync_folder_datasets(s, tmp_path / "d")
    assert report["tables_loaded"] == 1 and "bad.csv" in report["skipped"][0]


# ---------- tabs stay separate ----------

def test_a_tab_can_only_reach_its_own_tables(client, db, logged_in):
    created = _upload(client, "o", ORDERS.encode(), title="Mine").json()
    mine, bakery = db.get(Dataset, created["id"]), datasets.default_dataset(db)
    assert "unknown table" in data_query(db, mine, table="bakery_sales", metric="count")["error"]
    assert "unknown table" in data_rows(db, bakery, table="orders")["error"]
    assert {t["name"] for t in tools_for(mine)} == {"data_query", "data_rows"}            # no invoice tools off the bakery tab
    assert "not available" in json.loads(execute("log_invoice", {"supplier": "x", "amount": 1}, db, ds=mine))["error"]
    assert {"lookup_supplier", "log_invoice"} <= {t["name"] for t in tools_for(bakery)}


def test_system_prompt_comes_from_the_dataset_schema(client, db, logged_in):
    from app import agent
    created = _upload(client, "o", ORDERS.encode(), title="Mine").json()
    text = agent.build_system(db, db.get(Dataset, created["id"]))[0]["text"]
    assert "**Mine**" in text and "`amount` (number)" in text and "Haifa, Eilat" in text
    assert "Bakery" not in text and "Challah" not in text and "## Invoices" not in text and "{{" not in text


def test_chats_and_images_follow_the_tab(client, db, logged_in, fake):
    from app.agent import ModelEvent
    from app.costs import Usage
    created = _upload(client, "o", ORDERS.encode(), title="Mine").json()
    fake.turns.append([ModelEvent(type="text", text="Three rows."),
                       ModelEvent(type="stop", stop_reason="end_turn", usage=Usage(5, 2), content=[{"type": "text", "text": "Three rows."}])])
    events = read_sse(client.post("/api/chat", json={"message": "how many rows?", "dataset_id": created["id"]}))
    conv_id = events[0]["conversation_id"]
    assert "**Mine**" in fake.last_system[0]["text"]

    mine = [c["id"] for c in client.get(f"/api/conversations?dataset_id={created['id']}").json()]
    bakery_id = datasets.default_dataset(db).id
    bakery = [c["id"] for c in client.get(f"/api/conversations?dataset_id={bakery_id}").json()]
    assert conv_id in mine and conv_id not in bakery

    # The same conversation keeps using its own dataset, whatever the client sends.
    fake.turns.append([ModelEvent(type="stop", stop_reason="end_turn", usage=Usage(5, 2), content=[{"type": "text", "text": "ok"}])])
    client.post("/api/chat", json={"message": "again", "conversation_id": conv_id, "dataset_id": bakery_id})
    assert "**Mine**" in fake.last_system[0]["text"]

    png = "iVBORw0KGgo="
    r = client.post("/api/chat", json={"message": "", "dataset_id": created["id"], "image": {"media_type": "image/png", "data": png}})
    assert r.status_code == 400 and "does not take images" in r.json()["detail"]
