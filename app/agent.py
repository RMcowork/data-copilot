"""The agent loop (Lesson 7, slide 9) and the model clients behind it.

    messages = [system, history, the new message]
    repeat:
        stream one model turn; forward text to the UI
        if it asked for tools: run them, append results, continue
        else: done
    stop early on: too many rounds, refusal, error

`run_turn` owns persistence: the user message is saved before the model is
called, the assistant message only once the turn is complete, and one `runs`
row records tokens, cost and status whatever happened. `ModelClient` is the
seam that lets tests replace the model with a scripted fake.
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterator, Protocol
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import ROOT, settings
from .costs import Usage, cost_usd
from . import datasets
from .db import Conversation, ConversationDataset, Dataset, Message, Run, User, utcnow
from .tools import registry

try:  # the SDK is optional at import time so tests run without it configured
    import anthropic
except ImportError:  # pragma: no cover
    anthropic = None  # type: ignore[assignment]

MAX_TOOL_ROUNDS = 6
SYSTEM_PATH = ROOT / "app" / "prompts" / "system.md"
FEATURE_PROMPTS = {"invoices": ROOT / "app" / "prompts" / "invoices.md"}
FALLBACK_MODELS = {"claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5", "claude-fable-5-1"}
REFUSAL_TEXT = ("I can't help with that request. If it is about this dataset, try rephrasing it; "
                "for anything else, ask the person who owns the data.")


# ---------------------------------------------------------------------------
# Model clients
# ---------------------------------------------------------------------------

@dataclass
class ModelEvent:
    type: str  # "text" | "stop"
    text: str = ""
    stop_reason: str = ""
    usage: Usage | None = None
    content: list[dict] | None = None  # assistant content blocks, replayable


class ModelClient(Protocol):
    name: str

    def stream(self, *, system: list[dict], messages: list[dict], tools: list[dict]) -> Iterator[ModelEvent]: ...


class FakeModelClient:
    """Scripted turns for tests and offline demos. No network, no cost."""

    name = "fake"

    def __init__(self, turns: list[list[ModelEvent]] | None = None):
        self.turns: list[list[ModelEvent]] = list(turns or [])
        self.last_messages: list[dict] = []
        self.last_system: list[dict] = []
        self.last_tools: list[dict] = []
        self.calls = 0

    def stream(self, *, system, messages, tools) -> Iterator[ModelEvent]:
        self.calls += 1
        self.last_messages, self.last_system, self.last_tools = messages, system, tools
        if self.turns:
            yield from self.turns.pop(0)
            return
        last = messages[-1]["content"]
        text = last if isinstance(last, str) else " ".join(b.get("text", "") for b in last if b.get("type") == "text")
        reply = f"(fake model) You said: {text.strip()[:200]}"
        yield ModelEvent(type="text", text=reply)
        yield ModelEvent(type="stop", stop_reason="end_turn", usage=Usage(10, 5), content=[{"type": "text", "text": reply}])


class AnthropicModelClient:
    """Streams one model turn through the Anthropic SDK and reports the final message."""

    name = "anthropic"

    def __init__(self):
        if anthropic is None:
            raise RuntimeError("the anthropic package is not installed")
        self.client = anthropic.Anthropic(api_key=settings.anthropic_api_key, max_retries=3, timeout=120)

    def stream(self, *, system, messages, tools) -> Iterator[ModelEvent]:
        kwargs: dict = {
            "model": settings.model,
            "max_tokens": settings.max_tokens,
            "system": system,
            "messages": messages,
            "tools": tools,
            "output_config": {"effort": settings.model_effort},
        }
        if settings.model in FALLBACK_MODELS:
            # Server-side fallback: a safety refusal on the primary model is retried on a substitute.
            kwargs["betas"] = ["server-side-fallback-2026-07-01"]
            kwargs["fallbacks"] = "default"
        with self.client.beta.messages.stream(**kwargs) as stream:
            for event in stream:
                if event.type == "text":
                    yield ModelEvent(type="text", text=event.text)
            final = stream.get_final_message()
        usage = Usage(final.usage.input_tokens, final.usage.output_tokens,
                      final.usage.cache_read_input_tokens or 0, final.usage.cache_creation_input_tokens or 0)
        content = []
        for block in final.content:
            d = block.model_dump(mode="json", exclude_none=True)
            d.pop("parsed", None)
            content.append(d)
        yield ModelEvent(type="stop", stop_reason=final.stop_reason or "end_turn", usage=usage, content=content)


def get_model_client() -> ModelClient:
    if settings.model_provider == "fake":
        return FakeModelClient()
    return AnthropicModelClient()


# ---------------------------------------------------------------------------
# The turn
# ---------------------------------------------------------------------------

def build_system(db: Session, ds: Dataset) -> list[dict]:
    """The system prompt for one dataset: the schema, the owner's notes, and the prompts of its features."""
    today = datetime.now(ZoneInfo(settings.timezone)).date()
    features = "".join("\n" + FEATURE_PROMPTS[f].read_text(encoding="utf-8") for f in sorted(datasets.features_of(ds)) if f in FEATURE_PROMPTS)
    notes = f"\n### Notes about this dataset\n{ds.notes.strip()}\n" if (ds.notes or "").strip() else ""
    text = (SYSTEM_PATH.read_text(encoding="utf-8")
            .replace("{{DATASET_NAME}}", ds.name).replace("{{DESCRIPTION}}", ds.description or "")
            .replace("{{SCHEMA}}", datasets.schema_text(ds) or "(this dataset has no tables)")
            .replace("{{NOTES}}", notes).replace("{{FEATURES}}", features)
            .replace("{{TODAY}}", today.isoformat()))
    # One cached block: tools + system form the stable prefix every turn reuses.
    return [{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}]


def _history(db: Session, conversation_id: int, exclude_id: int) -> list[dict]:
    rows = db.scalars(select(Message).where(Message.conversation_id == conversation_id, Message.id != exclude_id)
                      .order_by(Message.id.desc()).limit(settings.history_limit)).all()
    msgs: list[dict] = []
    for m in reversed(rows):
        if not m.content:
            continue
        if not msgs and m.role != "user":
            continue  # history must start with the user
        if msgs and msgs[-1]["role"] == m.role:
            msgs[-1]["content"] += "\n\n" + m.content  # the API wants alternating roles
        else:
            msgs.append({"role": m.role, "content": m.content})
    if msgs and msgs[-1]["role"] == "user":
        msgs[-1]["content"] += "\n\n(no reply was recorded)"
        msgs.append({"role": "assistant", "content": "(no reply was recorded)"})
    return msgs


def _summarise_input(tool_input: dict) -> str:
    s = json.dumps(tool_input, ensure_ascii=False)
    return s if len(s) <= 90 else s[:87] + "..."


def _summarise_result(result_text: str) -> str:
    try:
        r = json.loads(result_text)
    except Exception:
        return result_text[:90]
    if not isinstance(r, dict):
        return result_text[:90]
    if "error" in r:
        return f"error: {str(r['error'])[:80]}"
    if "status" in r and "created" in r:  # log_invoice
        return f"{'created' if r['created'] else r.get('reason', 'existing')} · {r['status']}" + (" · flagged" if r.get("flagged") else "")
    if "matches" in r:  # lookup_supplier
        return f"{len(r['matches'])} match" + ("" if len(r["matches"]) == 1 else "es")
    if "row_count" in r:  # data_query grouped
        return f"{r['row_count']} rows · {r['metric']} by {r['group_by']}"
    if "total_matching" in r:  # data_rows
        return f"{r['returned']} of {r['total_matching']} rows"
    if "value" in r:  # data_query total
        return f"{r['metric']} = {r['value']}"
    return result_text[:90]


def _friendly(exc: Exception) -> str:
    if anthropic is not None:
        if isinstance(exc, (anthropic.RateLimitError, getattr(anthropic, "OverloadedError", anthropic.RateLimitError))):
            return "The model is busy right now. Try again in a minute."
        if isinstance(exc, anthropic.AuthenticationError):
            return "The model key is not valid. Check ANTHROPIC_API_KEY."
        if isinstance(exc, anthropic.APIConnectionError):
            return "Could not reach the model. Check the connection and try again."
        if isinstance(exc, anthropic.BadRequestError):
            return f"The request was rejected: {exc.message if hasattr(exc, 'message') else exc}"
    return "Something went wrong on our side. The error was recorded; try again."


def run_turn(db: Session, *, user: User, conversation: Conversation | None, text: str, image: dict | None,
             client: ModelClient, dataset: Dataset | None = None) -> Iterator[dict]:
    """Yield SSE-ready events for one user turn. See the module docstring for the contract.

    The turn works on one dataset: the conversation's own if it has one, else `dataset` (for a new
    conversation), else the default."""
    started = time.monotonic()
    text = (text or "").strip()
    image_ref = f"img_{hashlib.sha256(image['data'].encode()).hexdigest()[:10]}" if image else None

    if conversation is None:
        dataset = dataset or datasets.default_dataset(db)
        conversation = Conversation(user_id=user.id, title=(text or "Invoice photo")[:60])
        db.add(conversation)
        db.commit()
        db.refresh(conversation)
        if dataset is not None:
            db.add(ConversationDataset(conversation_id=conversation.id, dataset_id=dataset.id))
            db.commit()
    else:
        dataset = datasets.dataset_for_conversation(db, conversation)
    if dataset is None:
        yield {"type": "error", "message": "No dataset is loaded yet. Add files under data/ or upload one."}
        return
    yield {"type": "meta", "conversation_id": conversation.id, "title": conversation.title}

    # 1. The user's message is saved first: it counts for the rate limit even if the stream dies.
    stored = text + (f"\n[image {image_ref}]" if image_ref else "")
    user_msg = Message(conversation_id=conversation.id, user_id=user.id, role="user",
                       content=stored or "(invoice photo)", has_image=bool(image))
    db.add(user_msg)
    conversation.updated_at = utcnow()
    db.commit()

    # 2. Context: cached system block, capped history, the new message (with the image, if any).
    messages = _history(db, conversation.id, user_msg.id)
    content: list[dict] = []
    if image:
        content.append({"type": "image", "source": {"type": "base64", "media_type": image["media_type"], "data": image["data"]}})
    prompt_text = text or "Please read this invoice and log it."
    if image_ref:
        prompt_text += f"\n\n(image reference: {image_ref})"
    content.append({"type": "text", "text": prompt_text})
    messages.append({"role": "user", "content": content})
    system = build_system(db, dataset)
    tools = registry.tools_for(dataset)

    run = Run(conversation_id=conversation.id, user_id=user.id,
              model=settings.model if client.name == "anthropic" else client.name, status="ok")
    total = Usage()
    pieces: list[str] = []
    tool_calls = 0
    tool_names: list[str] = []

    def finish_run(status: str, error: str | None = None) -> None:
        run.status = status
        run.error = error
        run.input_tokens, run.output_tokens = total.input_tokens, total.output_tokens
        run.cache_read_tokens, run.cache_write_tokens = total.cache_read_tokens, total.cache_write_tokens
        run.cost_usd = cost_usd(run.model, total)
        run.tool_calls, run.tool_names = tool_calls, ",".join(tool_names)[:200]
        run.duration_ms = int((time.monotonic() - started) * 1000)
        db.add(run)
        db.commit()

    try:
        # 3. The loop.
        for round_no in range(MAX_TOOL_ROUNDS + 1):
            stop: ModelEvent | None = None
            for ev in client.stream(system=system, messages=messages, tools=tools):
                if ev.type == "text" and ev.text:
                    pieces.append(ev.text)
                    yield {"type": "text", "text": ev.text}
                elif ev.type == "stop":
                    stop = ev
            if stop is None:
                raise RuntimeError("model stream ended without a final message")
            if stop.usage:
                total.add(stop.usage)
            blocks = stop.content or []

            if stop.stop_reason == "tool_use":
                tool_uses = [b for b in blocks if b.get("type") == "tool_use"]
                if not tool_uses:
                    break
                if round_no >= MAX_TOOL_ROUNDS:
                    note = "\n\nI stopped after too many tool steps. Please ask again, more specifically."
                    pieces.append(note)
                    yield {"type": "text", "text": note}
                    break
                results = []
                for tu in tool_uses:
                    tool_calls += 1
                    tool_names.append(tu["name"])
                    tool_input = tu.get("input") or {}
                    yield {"type": "tool", "name": tu["name"], "status": "start", "summary": _summarise_input(tool_input)}
                    result_text = registry.execute(tu["name"], tool_input, db, ds=dataset, user_id=user.id)
                    yield {"type": "tool", "name": tu["name"], "status": "done", "summary": _summarise_result(result_text)}
                    results.append({"type": "tool_result", "tool_use_id": tu["id"], "content": result_text})
                messages.append({"role": "assistant", "content": blocks})
                messages.append({"role": "user", "content": results})
                continue

            if stop.stop_reason == "refusal":
                run.status = "refusal"
                pieces.append(REFUSAL_TEXT)
                yield {"type": "text", "text": REFUSAL_TEXT}
            elif stop.stop_reason == "max_tokens":
                note = "\n\n(Reply cut short at the length limit.)"
                pieces.append(note)
                yield {"type": "text", "text": note}
            break

        # 4. Persist once, at the end.
        reply = "".join(pieces).strip()
        if reply:
            db.add(Message(conversation_id=conversation.id, user_id=user.id, role="assistant", content=reply))
        conversation.updated_at = utcnow()
        finish_run(run.status)
        yield {"type": "done", "usage": total.as_dict(), "cost_usd": run.cost_usd, "run_id": run.id, "status": run.status}

    except GeneratorExit:
        # The browser went away mid-stream: keep what it cost, write no half reply.
        db.rollback()
        finish_run("aborted")
        raise
    except Exception as exc:  # noqa: BLE001 - every failure becomes a run row and a visible message
        db.rollback()
        finish_run("error", f"{type(exc).__name__}: {exc}"[:2000])
        yield {"type": "error", "message": _friendly(exc)}
