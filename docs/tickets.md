# Tickets — Data Copilot

The spec broken into tickets a stranger could test, grouped by milestone. Each becomes a GitHub Issue on the project board (To do / In progress / Done). `scripts/create_issues.sh` creates them with `gh` once the repo exists.

Format: **title** · label · done when.

## Milestone v1 — chat

1. **Project skeleton: FastAPI app, config from env, health endpoint** · chore · `GET /health` returns 200 locally; settings load from `.env`.
2. **Database models and startup seed** · feature · tables created at start; `sales` has 29,115 rows; `suppliers` has 10.
3. **Login with team password and typed name** · feature · right password sets a cookie; wrong password 401; name 2–40 chars.
4. **Conversations list and messages API** · feature · a user sees only their conversations; messages return in order.
5. **Chat turn with streaming (SSE)** · feature · first token under 2 s on a 3-turn chat; full reply saved once; dropped connection leaves no half-row.
6. **Static chat UI: sidebar, bubbles, composer** · feature · works on a phone width; Enter sends, Shift+Enter newline.
7. **Markdown rendering of replies** · feature · lists and tables render; plain text falls back when the CDN is blocked.
8. **System prompt with today's date and data range** · feature · the prompt names the dataset's first and last dates.

## Milestone v2 — tools

9. **`sales_query` tool: metric, grouping, filters** · feature · totals match the dataset (₪647,065 · 14,421 orders · 40,194 items); unknown metric returns an error, not an exception. (Replaced by `data_query` / `data_rows` in ticket 33.)
10. **`lookup_supplier` tool** · feature · partial, case-insensitive match; unknown name returns `known_supplier: false`.
11. **`log_invoice` tool with dedup, needs-a-look and flag rules** · feature · same `source_ref` twice is one row; null amount → `needs_a_look`; > ₪5,000 → `flagged`.
12. **Tool loop in the agent (max 6 rounds), tool events to the UI** · feature · the UI shows a chip per tool call; the run row counts tool calls.
13. **Image upload: base64 in the turn, reference string, not stored** · feature · a PNG under 5 MB reaches the model; the saved message has `has_image`.
14. **Invoices panel in the UI** · feature · last 50 invoices with status and flag badges.
15. **Eval cases and runner** · chore · `python -m evals.run` grades 10 cases and appends to `history.csv`.

## Milestone v3 — limits and deploy

16. **Per-user hourly rate limit** · feature · the 31st message in an hour returns 429 with a clear message.
17. **Daily spend cap from the runs table** · feature · at the cap the chat returns 402 "back tomorrow".
18. **Cost per run from a price table** · feature · `cost_usd` on every run; unknown model costs 0 and logs a warning.
19. **Dockerfile, railway.json, health check** · chore · the service builds on Railway and passes `/health`.
20. **Morning digest: CLI + protected endpoint** · feature · six lines; wrong `X-Cron-Secret` is 401; webhook POST when configured.
21. **CI: tests on every PR, evals when the key is present** · chore · a failing test blocks the merge; evals skip cleanly without a key.
22. **Runbook and first-week watch table** · chore · `runbook.md` has six entries; a `docs/watch.md` table with the six Friday numbers.
23. **Infrastructure as Code: `.railway/railway.ts` replaces `railway.json`** · chore · `railway config plan` reports no drift; the digest service has start command `python -m app.digest` and schedule `30 4 * * *`; no `railway.json` or `railway.cron.json` in the repo.
24. **Deploy guide: `docs/deploy-railway.md`** · chore · one document covers the first deploy with the CLI, every variable and secret with where it is set, the dashboard path, day-two operations and troubleshooting; README links to it.
25. **Session token survives a '|' byte in the signature** · bug · a token reads back for 1,000 consecutive seconds; `pytest -q` is green 20 runs in a row.
26. **Windows instructions** · chore · README, `practice.md` and `docs/deploy-railway.md` give PowerShell commands wherever Windows differs from macOS and Linux; shell scripts keep LF line endings on Windows checkouts (`.gitattributes`).
27. **.env loader drops inline comments** · bug · a copy of `.env.example` loads with `load_settings()`; a quoted value keeps a `#`.
28. **Public repository** · chore · the history is one commit under GitHub's noreply address; no personal email, live URL or workspace name in any tracked file; Railway deploys from the new repo.
30. **Assistant stops re-checking and retracting answers it already checked (issue #2)** · bug · eval cases can carry earlier turns (`history`); `no_recheck_after_off_topic` and `no_false_retraction` pass; `no_number_without_tool` still passes; `pytest -q` is green.
31. **System prompt names the branches; the assistant stops inventing them (issue #3)** · bug · `build_system()` output contains the four branch names from the data; the eval `no_invented_branch` passes; `pytest -q` is green.
32. **Deploy guide checks that work: seed report in the log, SSH key for the private database (issue #4)** · bug · startup prints `seed report: … sales_seeded: N` (tested); `docs/deploy-railway.md` gets from no SSH key to a row count with each command listed, and no longer says a local `railway run` can read the database. (The seed report now reads `suppliers_seeded, folder_datasets, tables_loaded, tables_dropped, skipped`; ticket 33.)
33. **Make the app work for any dataset** · feature · every folder under `data/` is a tab and the bakery files live in `data/bakery/`; a user can add up to 3 datasets by upload or link, each with its own tab and chats; the model reads the tab's schema and answers with `data_query` / `data_rows`; links to private addresses are refused; the app has a light/dark theme; spec §7 checks 13 to 18 pass, `pytest -q` is green (79) and `python -m evals.run` passes (13/13 on 2026-11-07).
34. **Several files per tab, rename a tab, download a chat, dark by default** · feature · a tab takes 1 to 3 files (added together or later) and can be renamed; a chat exports as Markdown or HTML (owner only, raw HTML in replies shown as text); the page opens in dark; Google Sheets links to published sheets and to Excel files in Drive import; spec §7 checks 18 to 21 pass and `pytest -q` is green (93).

## Later (not scheduled)

- Alembic migrations once production data exists.
- Conversation purge at 90 days.
- Per-client deployment template (second bakery).
