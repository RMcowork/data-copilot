"""Turn a conversation into a file a person can keep or send: Markdown or a self-contained web page.

The page is built from escaped text: raw HTML inside a reply is shown as text, never run, so a
downloaded chat cannot carry a script (markdown-it with `html: False`). Nothing here is public:
the endpoint that calls this is behind the same sign-in as the rest of the app.
"""
from __future__ import annotations

import html
import re
from datetime import datetime, timezone
from urllib.parse import quote
from zoneinfo import ZoneInfo

from markdown_it import MarkdownIt

from .config import settings

FORMATS = {"md": "text/markdown; charset=utf-8", "html": "text/html; charset=utf-8"}

_md = MarkdownIt("commonmark", {"html": False, "breaks": True}).enable("table")


def _local(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(settings.timezone))


def _stamp(dt: datetime) -> str:
    return _local(dt).strftime("%Y-%m-%d %H:%M")


def file_name(title: str, fmt: str, now: datetime) -> tuple[str, str]:
    """(utf-8 name, ascii fallback) for Content-Disposition. Keeps Hebrew/Arabic letters in the first."""
    clean = re.sub(r"[^\w\- ]+", "", title, flags=re.UNICODE).strip().replace(" ", "-")[:60] or "chat"
    stamp = _local(now).strftime("%Y%m%d")
    ascii_part = re.sub(r"[^A-Za-z0-9\-]+", "", clean).strip("-")[:60] or "chat"
    return f"{clean}-{stamp}.{fmt}", f"{ascii_part}-{stamp}.{fmt}"


def content_disposition(title: str, fmt: str, now: datetime) -> str:
    utf8, fallback = file_name(title, fmt, now)
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(utf8)}"


def _author(role: str, user_name: str) -> str:
    return user_name if role == "user" else "Data Copilot"


def _body(m) -> str:
    text = m.content or ""
    if m.has_image:
        text = "(an image was attached)\n\n" + text if text.strip() else "(an image was attached)"
    return text


def to_markdown(title: str, dataset: str, user_name: str, messages: list, now: datetime) -> str:
    out = [f"# {title}", "", f"*{dataset} · {user_name} · exported {_stamp(now)} · {len(messages)} messages*", ""]
    for m in messages:
        out += ["---", "", f"**{_author(m.role, user_name)}** · {_stamp(m.created_at)}", "", _body(m).strip(), ""]
    return "\n".join(out).rstrip() + "\n"


_CSS = """
:root{color-scheme:light dark;--bg:#fff;--ink:#101828;--muted:#667085;--line:#e4e7ec;--soft:#f6f7f9;--accent:#4f46e5}
@media(prefers-color-scheme:dark){:root{--bg:#0c0e13;--ink:#eceef3;--muted:#9aa3b5;--line:#272c38;--soft:#14171e;--accent:#818cf8}}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.6 system-ui,-apple-system,"Segoe UI",Roboto,"Noto Sans","Noto Sans Hebrew",Arial,sans-serif}
main{max-width:760px;margin:0 auto;padding:32px 20px 64px}
h1{font-size:24px;letter-spacing:-.02em;margin:0 0 4px}
.sub{color:var(--muted);font-size:13px;margin:0 0 24px}
.msg{padding:16px 0;border-top:1px solid var(--line)}
.who{font-size:13px;font-weight:650;margin:0 0 4px}.who span{color:var(--muted);font-weight:450;margin-inline-start:8px}
.msg.assistant .who{color:var(--accent)}
.body>:first-child{margin-top:0}.body>:last-child{margin-bottom:0}
table{border-collapse:collapse;display:block;overflow-x:auto;margin:10px 0;font-size:14px}
th,td{border:1px solid var(--line);padding:6px 10px;text-align:start}th{background:var(--soft)}
code{background:var(--soft);padding:1px 5px;border-radius:5px;font-size:.9em}
pre{background:var(--soft);padding:12px;border-radius:8px;overflow-x:auto}pre code{background:none;padding:0}
@media print{body{background:#fff;color:#000}.msg{break-inside:avoid}}
"""


def to_html(title: str, dataset: str, user_name: str, messages: list, now: datetime) -> str:
    parts = []
    for m in messages:
        text = _body(m).strip()
        if m.role == "user":
            body = "<p>" + html.escape(text).replace("\n", "<br>") + "</p>"
        else:
            body = _md.render(text)
        parts.append(f'<section class="msg {m.role}"><p class="who">{html.escape(_author(m.role, user_name))}'
                     f'<span>{_stamp(m.created_at)}</span></p><div class="body" dir="auto">{body}</div></section>')
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f"<title>{html.escape(title)}</title><style>{_CSS}</style></head><body><main>"
            f'<h1 dir="auto">{html.escape(title)}</h1>'
            f'<p class="sub">{html.escape(dataset)} · {html.escape(user_name)} · exported {_stamp(now)} · {len(messages)} messages</p>'
            + "".join(parts) + "</main></body></html>")


def render(fmt: str, title: str, dataset: str, user_name: str, messages: list, now: datetime) -> str:
    return (to_html if fmt == "html" else to_markdown)(title, dataset, user_name, messages, now)
