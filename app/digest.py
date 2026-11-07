"""The morning digest: yesterday in six lines.

Runs as a Railway cron service (`python -m app.digest`, schedule in README.md)
or through POST /internal/digest. It only reads; the rows it reports were
written by the chat turns during the day.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from .config import settings
from .db import Invoice, Message, Run, SessionLocal, init_db


def _local_bounds(day: date) -> tuple[datetime, datetime]:
    tz = ZoneInfo(settings.timezone)
    start = datetime.combine(day, time.min, tzinfo=tz).astimezone(timezone.utc).replace(tzinfo=None)
    end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=tz).astimezone(timezone.utc).replace(tzinfo=None)
    return start, end


def build_digest(db: Session, day: date | None = None) -> str:
    tz = ZoneInfo(settings.timezone)
    day = day or (datetime.now(tz).date() - timedelta(days=1))
    start, end = _local_bounds(day)

    invoices = db.scalars(select(Invoice).where(Invoice.created_at >= start, Invoice.created_at < end)
                          .order_by(Invoice.created_at)).all()
    logged = [i for i in invoices if i.status == "logged"]
    needs = [i for i in invoices if i.status == "needs_a_look"]
    flagged = [i for i in invoices if i.flagged]
    total = sum(i.amount or 0 for i in logged)

    messages = db.scalar(select(func.count()).select_from(Message)
                         .where(Message.role == "user", Message.created_at >= start, Message.created_at < end)) or 0
    runs = db.execute(select(func.count(Run.id), func.coalesce(func.sum(Run.cost_usd), 0.0),
                             func.sum(case((Run.status != "ok", 1), else_=0)))
                      .where(Run.created_at >= start, Run.created_at < end)).one()
    run_count, cost, not_ok = int(runs[0] or 0), float(runs[1] or 0.0), int(runs[2] or 0)

    lines = [f"Data Copilot — {day.strftime('%A %d %b %Y')}",
             f"Invoices logged: {len(logged)} · total ₪{total:,.0f}",
             f"Needs a look: {len(needs)}" + (" — " + "; ".join(f"{i.supplier} ({i.invoice_number or 'no number'})" for i in needs[:5]) if needs else ""),
             f"Flagged over ₪5,000: {len(flagged)}" + (" — " + "; ".join(f"{i.supplier} ₪{i.amount:,.0f}" for i in flagged[:5]) if flagged else ""),
             f"Chat: {messages} messages · {run_count} model runs · ${cost:.2f} of ${settings.daily_cap_usd:.2f} cap",
             f"Runs not OK: {not_ok}" + (" — check the runs table and runbook.md" if not_ok else "")]
    return "\n".join(lines)


def send_digest(text: str) -> bool:
    """POST {"text": ...} to DIGEST_WEBHOOK_URL (Slack-compatible). Returns False when no URL is set."""
    if not settings.digest_webhook_url:
        return False
    import httpx

    r = httpx.post(settings.digest_webhook_url, json={"text": text}, timeout=15)
    r.raise_for_status()
    return True


def main() -> None:
    init_db()
    with SessionLocal() as db:
        body = build_digest(db)
    print(body)
    sent = send_digest(body)
    print(f"\n[sent to webhook: {sent}]")


if __name__ == "__main__":
    main()
