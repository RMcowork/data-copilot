"""Configuration: everything comes from environment variables.

Locally they are loaded from `.env`; on Railway they live in the service's
Variables tab. The code reads the same names in both places (architecture.md,
"Secrets").
"""
from __future__ import annotations

import os
import re
import secrets
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


_INLINE_COMMENT = re.compile(r"(?:^|\s)#")


def _dotenv_value(raw: str) -> str:
    """The value part of KEY=VALUE. A quoted value is taken as written, '#' included;
    an unquoted one ends where an inline comment starts (a '#' at the start or after a space)."""
    value = raw.strip()
    if value[:1] in ('"', "'"):
        end = value.find(value[0], 1)
        if end != -1:
            return value[1:end]
    return _INLINE_COMMENT.split(value, maxsplit=1)[0].strip().strip('"').strip("'")


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader: KEY=VALUE lines, inline comments dropped, never overrides real env."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), _dotenv_value(value))


_load_dotenv(ROOT / ".env")


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


# Prices per million tokens, USD, as of writing (October 2026). Check the
# pricing page before quoting a client; the numbers move.
PRICES_PER_MTOK: dict[str, dict[str, float]] = {
    "claude-opus-5-5": {"input": 4.00, "output": 20.00, "cache_read": 0.20, "cache_write": 5.00},
    "claude-sonnet-5-5": {"input": 2.00, "output": 10.00, "cache_read": 0.20, "cache_write": 2.50},
    "claude-haiku-5-5": {"input": 0.10, "output": 0.50, "cache_read": 0.01, "cache_write": 0.125},
}


@dataclass(frozen=True)
class Settings:
    app_password: str
    app_secret: str
    database_url: str
    anthropic_api_key: str | None
    model: str
    model_effort: str
    max_tokens: int
    history_limit: int
    rate_limit_per_hour: int
    daily_cap_usd: float
    max_upload_mb: int
    max_dataset_mb: int
    max_dataset_rows: int
    max_uploaded_datasets: int
    max_files_per_dataset: int
    cron_secret: str | None
    digest_webhook_url: str | None
    timezone: str
    model_provider: str  # "anthropic" | "fake"
    session_hours: int
    environment: str  # "local" | "staging" | "production"

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @property
    def is_production(self) -> bool:
        return self.environment == "production"


def load_settings() -> Settings:
    provider = _env("MODEL_PROVIDER", "anthropic")
    api_key = _env("ANTHROPIC_API_KEY")
    environment = _env("APP_ENV", "local")
    app_password = _env("APP_PASSWORD")
    app_secret = _env("APP_SECRET")

    if environment == "production":
        missing = [n for n, v in (("APP_PASSWORD", app_password), ("APP_SECRET", app_secret)) if not v]
        if provider == "anthropic" and not api_key:
            missing.append("ANTHROPIC_API_KEY")
        if missing:
            raise RuntimeError(f"Missing required variables in production: {', '.join(missing)}")

    database_url = _env("DATABASE_URL", "sqlite:///./local.db")
    # Railway's Postgres plugin hands out postgresql:// URLs; SQLAlchemy needs the driver name.
    if database_url.startswith("postgres://"):
        database_url = database_url.replace("postgres://", "postgresql+psycopg://", 1)
    elif database_url.startswith("postgresql://"):
        database_url = database_url.replace("postgresql://", "postgresql+psycopg://", 1)

    return Settings(
        app_password=app_password or "bakery",
        app_secret=app_secret or secrets.token_hex(32),
        database_url=database_url,
        anthropic_api_key=api_key,
        model=_env("MODEL", "claude-opus-5-5"),
        model_effort=_env("MODEL_EFFORT", "medium"),
        max_tokens=int(_env("MAX_TOKENS", "2048")),
        history_limit=int(_env("HISTORY_LIMIT", "20")),
        rate_limit_per_hour=int(_env("RATE_LIMIT_PER_HOUR", "30")),
        daily_cap_usd=float(_env("DAILY_CAP_USD", "3.00")),
        max_upload_mb=int(_env("MAX_UPLOAD_MB", "5")),
        max_dataset_mb=int(_env("MAX_DATASET_MB", "25")),
        max_dataset_rows=int(_env("MAX_DATASET_ROWS", "200000")),
        max_uploaded_datasets=int(_env("MAX_UPLOADED_DATASETS", "3")),
        max_files_per_dataset=int(_env("MAX_FILES_PER_DATASET", "3")),
        cron_secret=_env("CRON_SECRET"),
        digest_webhook_url=_env("DIGEST_WEBHOOK_URL"),
        timezone=_env("TIMEZONE", "Asia/Jerusalem"),
        model_provider=provider,
        session_hours=int(_env("SESSION_HOURS", "72")),
        environment=environment,
    )


settings = load_settings()
