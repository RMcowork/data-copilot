"""Test setup: fake model, throwaway SQLite, a logged-in client.

Environment must be set before `app` is imported, because settings load once.
"""
import os
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="data-copilot-tests-"))
os.environ.update({
    "MODEL_PROVIDER": "fake",
    "DATABASE_URL": f"sqlite:///{_TMP / 'test.db'}",
    "APP_PASSWORD": "test-pass",
    "APP_SECRET": "test-secret-0123456789abcdef",
    "APP_ENV": "local",
    "DAILY_CAP_USD": "1.00",
    "RATE_LIMIT_PER_HOUR": "30",
    "CRON_SECRET": "cron-test",
    "ANTHROPIC_API_KEY": "",
})

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import agent as agent_module  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture(scope="session")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def db():
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture
def logged_in(client):
    r = client.post("/api/login", json={"name": "Dana", "password": "test-pass"})
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture
def fake(client):
    """The fake model behind the app; tests push scripted turns onto it."""
    fake_client = agent_module.FakeModelClient()
    app.state.model_client = fake_client
    yield fake_client
    fake_client.turns.clear()


def read_sse(response) -> list[dict]:
    import json

    events = []
    for chunk in response.text.split("\n\n"):
        for line in chunk.split("\n"):
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
    return events
