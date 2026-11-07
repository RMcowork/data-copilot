"""Integration tests: the API with the model replaced by a scripted fake."""
from app import agent as agent_module
from app.agent import ModelEvent, run_turn
from app.costs import Usage
from app.db import Conversation, Invoice, Message, Run, SessionLocal, User
from tests.conftest import read_sse


def test_health_ok(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_login_required_and_wrong_password(client):
    client.cookies.clear()
    assert client.get("/api/conversations").status_code == 401
    bad = client.post("/api/login", json={"name": "Eve", "password": "nope"})
    assert bad.status_code == 401
    assert client.post("/api/login", json={"name": "E", "password": "test-pass"}).status_code in (400, 422)


def test_chat_streams_text_and_saves_once(client, db, logged_in, fake):
    fake.turns.append([
        ModelEvent(type="text", text="Challah sold best "),
        ModelEvent(type="text", text="last week: 312 units."),
        ModelEvent(type="stop", stop_reason="end_turn", usage=Usage(120, 18),
                   content=[{"type": "text", "text": "Challah sold best last week: 312 units."}]),
    ])
    r = client.post("/api/chat", json={"message": "What sold best last week?"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    events = read_sse(r)
    kinds = [e["type"] for e in events]
    assert kinds[0] == "meta" and kinds[-1] == "done"
    assert "".join(e["text"] for e in events if e["type"] == "text") == "Challah sold best last week: 312 units."
    conv_id = events[0]["conversation_id"]
    msgs = db.query(Message).filter_by(conversation_id=conv_id).order_by(Message.id).all()
    assert [m.role for m in msgs] == ["user", "assistant"]
    assert msgs[1].content == "Challah sold best last week: 312 units."
    run = db.query(Run).filter_by(conversation_id=conv_id).one()
    assert run.status == "ok" and run.input_tokens == 120 and run.output_tokens == 18
    assert events[-1]["cost_usd"] == 0.0  # fake model has no price table


def test_tool_loop_logs_invoice_once_even_when_sent_twice(client, db, logged_in, fake):
    tool_turn = [
        ModelEvent(type="text", text="Reading the invoice. "),
        ModelEvent(type="stop", stop_reason="tool_use", usage=Usage(300, 40), content=[
            {"type": "text", "text": "Reading the invoice. "},
            {"type": "tool_use", "id": "tu_1", "name": "log_invoice",
             "input": {"supplier": "Levi Flour", "invoice_number": "L-2201", "amount": None, "source_ref": "img_fixed"}},
        ]),
    ]
    final_turn = [
        ModelEvent(type="text", text="Logged Levi Flour L-2201 — amount unreadable, marked needs a look."),
        ModelEvent(type="stop", stop_reason="end_turn", usage=Usage(420, 30),
                   content=[{"type": "text", "text": "Logged Levi Flour L-2201 — amount unreadable, marked needs a look."}]),
    ]
    fake.turns.extend([tool_turn, final_turn, tool_turn, final_turn])

    r1 = client.post("/api/chat", json={"message": "log this invoice please"})
    e1 = read_sse(r1)
    tools = [e for e in e1 if e["type"] == "tool"]
    assert [t["status"] for t in tools] == ["start", "done"] and tools[0]["name"] == "log_invoice"
    assert "needs_a_look" in tools[1]["summary"]
    rows = db.query(Invoice).filter_by(source_ref="img_fixed").all()
    assert len(rows) == 1 and rows[0].status == "needs_a_look"

    r2 = client.post("/api/chat", json={"message": "log this invoice please", "conversation_id": e1[0]["conversation_id"]})
    e2 = read_sse(r2)
    assert any(e["type"] == "tool" and e["status"] == "done" and "duplicate" in e["summary"] for e in e2)
    assert db.query(Invoice).filter_by(source_ref="img_fixed").count() == 1
    run = db.query(Run).filter_by(conversation_id=e1[0]["conversation_id"]).order_by(Run.id.desc()).first()
    assert run.tool_calls == 1 and run.input_tokens == 720  # both model calls of the turn are summed


def test_refusal_is_reported_not_hidden(client, db, logged_in, fake):
    fake.turns.append([ModelEvent(type="stop", stop_reason="refusal", usage=Usage(50, 0), content=[])])
    events = read_sse(client.post("/api/chat", json={"message": "do something odd"}))
    assert any(e["type"] == "text" and "can't help" in e["text"] for e in events)
    conv_id = events[0]["conversation_id"]
    assert db.query(Run).filter_by(conversation_id=conv_id).one().status == "refusal"


def test_dropped_connection_leaves_no_half_row(client, db, logged_in, fake):
    fake.turns.append([
        ModelEvent(type="text", text="This reply will be cut "),
        ModelEvent(type="text", text="in the middle."),
        ModelEvent(type="stop", stop_reason="end_turn", usage=Usage(10, 5), content=[{"type": "text", "text": "x"}]),
    ])
    with SessionLocal() as s:
        user = s.query(User).filter_by(name="Dana").one()
        gen = run_turn(s, user=user, conversation=None, text="cut me off", image=None, client=fake)
        meta = next(gen)
        first = next(gen)
        assert meta["type"] == "meta" and first["type"] == "text"
        gen.close()  # the browser went away
        conv_id = meta["conversation_id"]
    roles = [m.role for m in db.query(Message).filter_by(conversation_id=conv_id).all()]
    assert roles == ["user"]  # the user's message is kept, no partial assistant row
    run = db.query(Run).filter_by(conversation_id=conv_id).one()
    assert run.status == "aborted"


def test_image_message_carries_a_reference(client, db, logged_in, fake):
    import base64

    fake.turns.append([ModelEvent(type="stop", stop_reason="end_turn", usage=Usage(10, 2),
                                  content=[{"type": "text", "text": "Got the image."}])])
    png = base64.b64encode(b"\x89PNG\r\n\x1a\nfake").decode()
    events = read_sse(client.post("/api/chat", json={"message": "", "image": {"media_type": "image/png", "data": png}}))
    assert events[-1]["type"] == "done"
    sent = fake.last_messages[-1]["content"]
    assert sent[0]["type"] == "image" and sent[0]["source"]["media_type"] == "image/png"
    assert "image reference: img_" in sent[1]["text"]
    conv_id = events[0]["conversation_id"]
    user_msg = db.query(Message).filter_by(conversation_id=conv_id, role="user").one()
    assert user_msg.has_image is True and "[image img_" in user_msg.content


def test_system_prompt_names_the_real_branches(client, db):
    # Issue #3: unnamed, the model made one up ("Haifa") for its examples. Now the schema lists every value.
    from app import datasets

    text = agent_module.build_system(db, datasets.default_dataset(db))[0]["text"]
    for branch in ("Tel Aviv - Florentin", "Tel Aviv - Dizengoff", "Ramat Gan", "Herzliya"):
        assert branch in text
    assert "{{" not in text
    assert "## Invoices" in text  # the bakery tab has the invoices feature


def test_startup_prints_the_seed_report(client, capsys):
    # Issue #4: the deploy guide checks this line in the logs. The session's first start
    # already seeded, so this second start reports zeros, as a redeploy does.
    from fastapi.testclient import TestClient

    from app.main import app

    capsys.readouterr()
    with TestClient(app):
        pass
    assert "seed report: schema_version: 1, suppliers_seeded: 0, folder_datasets: 1, tables_loaded: 0" in capsys.readouterr().out


def test_digest_endpoint_needs_secret(client, logged_in):
    assert client.post("/internal/digest").status_code == 401
    r = client.post("/internal/digest", headers={"X-Cron-Secret": "cron-test"})
    assert r.status_code == 200
    assert r.json()["text"].startswith("Data Copilot —")
    assert r.json()["sent"] is False
