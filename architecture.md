# architecture.md — Data Copilot

The drawing, where each box runs, the limits, and every decision with its reason. Keep it current in the same commit as the change.

## Components

```
 Browser (UI)            Backend (FastAPI, one service)              Anthropic
 static/index.html  --->  /api/chat  --> agent loop --------------->  Claude API
 app.js, styles.css <---  SSE stream <-- text + tool events          (model, per token)
                             |        \
                             |         tools: data_query · data_rows  (+ lookup_supplier · log_invoice on the Bakery tab)
                             v
                          Postgres (Railway plugin)  <---  cron service: python -m app.digest (07:30)
                          users · conversations · messages · runs · invoices · suppliers · datasets + one table per loaded file
```

Four layers, one external dependency. Everything on the left and in the middle is ours and runs on Railway; the model is rented per token.

The UI is plain HTML, CSS and JS with no framework: one tab per dataset (the tab decides which conversations are listed and which dataset a new chat uses), an Add dataset dialog (1 to 3 files or links become one tab; the same dialog adds files to an existing added tab), a rename dialog, an Export menu on the open chat, and a dark-first theme made of CSS variables. The page opens dark; the sidebar switch changes it to light, and only a chosen light is remembered in that browser's `localStorage`, never on the server.

## Flows

- **Chat turn.** UI posts `{conversation_id?, dataset_id, message, image?}` → auth cookie checked → three limits checked → the conversation's dataset resolved (a new conversation takes the tab it was started in; an existing one keeps its own) → user message saved → system prompt for that dataset (today's date, the schema of its tables with value lists and ranges read from the data, the owner's notes from `dataset.yaml`) + last 20 messages + current message → model stream. Text deltas are forwarded to the UI as they arrive. If the model asks for a tool, the backend runs it, appends the result, and calls the model again (max 6 rounds). At the end: one assistant message, one `runs` row with tokens and cost. A dropped connection saves no partial assistant message; the run is marked `aborted`.
- **Invoice** (Bakery tab only; other tabs refuse images and do not offer the tools). The image travels inside the same turn as a content block plus an `image reference` string. The model calls `lookup_supplier`, then `log_invoice` with `source_ref` = that reference. The tool, not the prompt, enforces: duplicate → existing row; unreadable amount → `needs_a_look`; over ₪5,000 → `flagged`.
- **Add a dataset.** `POST /api/datasets/upload` (raw body, no multipart) or `POST /api/datasets/link` → size and count limits → for a link: the address is checked (http/https, every resolved IP public, re-checked on each of at most 3 redirects, download capped while streaming) → the file is parsed (CSV: delimiter and encoding detected, UTF-8 / Hebrew cp1255; Excel: one table per sheet) → each column is typed from its values (number, date, datetime, text) and its stats recorded → one real table per file is created and filled → the tab appears. A bad file leaves no tables and no row behind. A tab holds up to `MAX_FILES_PER_DATASET` (3) files: the first file creates the tab, `POST /api/datasets/{id}/upload` or `/link` adds the others (a file with the same content is refused; a failed file removes only its own tables, never the ones already loaded). The model sees every table of the tab in its schema but queries one table at a time.
- **Folder datasets.** At every start `sync_folder_datasets` scans `data/`: a subfolder is one dataset (tab) whose files are its tables, an optional `dataset.yaml` gives it a title, description, notes for the model, starter questions and features; a loose file is a dataset of its own. A file whose SHA-256 is unchanged is not reloaded; a changed file is dropped and reloaded; a vanished file or folder is dropped. A file that cannot be read is skipped and named in the seed report; it never stops the start.
- **Digest.** The cron service runs `app/digest.py`: reads yesterday's invoices (only the Bakery tab logs them) and the runs of every tab, prints six lines, POSTs them to `DIGEST_WEBHOOK_URL` if set. `POST /internal/digest` with `X-Cron-Secret` does the same by hand.

## API

All routes need the session cookie except `/health`, `/api/login`, `/api/logout` and `/internal/digest` (which needs the cron secret instead).

| Route | Does |
|---|---|
| `POST /api/login` · `POST /api/logout` · `GET /api/me` | shared password + typed name |
| `GET /api/datasets` | the tabs: name, tables, columns, features, starter questions, slots used and limits |
| `POST /api/datasets/upload?filename=&name=` | add a dataset from the raw file body (CSV or Excel) |
| `POST /api/datasets/link` | add a dataset from `{url, name}`; the server downloads it (public addresses only) |
| `POST /api/datasets/{id}/upload?filename=` · `POST /api/datasets/{id}/link` | add one more file (raw body) or link to an added tab, up to 3 files in all; built-in tabs answer 400 |
| `PATCH /api/datasets/{id}` | rename a tab (`{name}`); a built-in tab's new name is kept in `dataset_renames` so `dataset.yaml` does not overwrite it |
| `DELETE /api/datasets/{id}` | delete an added dataset and its chats; built-in datasets answer 400 |
| `GET /api/conversations?dataset_id=` | the user's chats in one tab; no `dataset_id` lists all |
| `GET /api/conversations/{id}/messages` | the messages of one of the user's chats |
| `GET /api/conversations/{id}/export?format=md\|html` | the chat as a download: Markdown, or a self-contained web page (replies rendered with raw HTML switched off); only the chat's owner, never cached |
| `POST /api/chat` | one turn, streamed as SSE; `{conversation_id?, dataset_id, message, image?}` |
| `GET /api/invoices` · `GET /api/stats` | the Invoices panel; spend today and messages this hour |
| `POST /internal/digest` | the digest by hand, with `X-Cron-Secret` |
| `GET /health` | 200 only when the database answers and a model key is set |

## Storage

Postgres on Railway (`DATABASE_URL` from the plugin); SQLite locally and in tests. Tables and their keys:

| Table | Key | Notes |
|---|---|---|
| users | name (unique) | typed at login; no passwords per user |
| conversations | id | belongs to one user; title = first message |
| messages | id | role, text only; `has_image` flag; image bytes are never stored |
| runs | id | one per model turn: tokens, cost_usd, tool_calls, status, duration |
| invoices | `source_ref` (unique) · (supplier, invoice_number) | the dedup keys; status logged / needs_a_look; flagged |
| suppliers | id | seeded from data/bakery/suppliers.csv; the invoice tool's reference list |
| datasets | id · slug (unique) | one per tab: name, notes, features, source (folder / upload / link) |
| dataset_tables | id · physical_name (unique) | one per loaded file or sheet: typed column schema as JSON, row count, file hash |
| `ds<dataset>_<n>` | `_row_id` | the data itself, one real table per file, typed columns; read-only through `data_query` / `data_rows` |
| conversation_datasets | conversation_id | which tab a conversation belongs to; no row = the default (Bakery) tab, so chats from before datasets need no migration |
| schema_version | version | 1; created with `create_all` at start |

Deleting an added dataset drops its `ds<dataset>_<n>` tables, its `dataset_tables` and `datasets` rows, and the conversations linked to it (their runs keep the cost but lose the link).

## Model

`claude-opus-5-5`, effort `medium`, max 2,048 output tokens, adaptive thinking (the model's default). Server-side fallbacks on (`fallbacks: "default"`), so a safety refusal on the primary model is retried on a substitute before we show the user anything. System prompt cached (`cache_control`), tools first and stable so the prefix stays cacheable. Change the model or effort in Variables, not in code; run the evals after. The code default is Opus 5.5; the eval run of 2026-11-07 (13/13) used `claude-sonnet-5-5`, which is cheaper (README, "Cost"). Whichever model runs in production must be the one the evals passed on.

## Limits

| Layer | Number | Where enforced |
|---|---|---|
| Users → us | 30 messages per user per hour; images ≤ 5 MB | `app/limits.py`, before the model is called |
| Us → model | SDK retries with backoff on 429/529 (2 retries) | Anthropic SDK client |
| Us → wallet | $3.00 per local day across the app | `app/limits.py` from the `runs` table |
| Tool loop | max 6 model rounds per turn; tool results truncated at 12,000 chars | `app/agent.py` |
| Added data | 3 datasets of up to 3 files each, 25 MB, 200,000 rows, 100 columns; links public-only | `app/datasets.py`, `app/fetch.py` (`MAX_DATASET_MB`, `MAX_DATASET_ROWS`, `MAX_UPLOADED_DATASETS`, `MAX_FILES_PER_DATASET`) |

## Hosting

One Railway project, three things in it: the **web service**, built by Railway's default Railpack builder straight from `requirements.txt` with the Python version in `.python-version` (no Dockerfile), the **Postgres plugin**, and the **digest cron service** from the same repo (start command `python -m app.digest`, schedule `30 4 * * *`; Railway cron is UTC, so that is 07:30 Israel in summer and 06:30 in winter). All three are described in one file, `.railway/railway.ts` (Railway's Infrastructure as Code): start commands, the `/health` check, restart policies and the cron schedule. Railway does not read that file at deploy time; `railway config plan` shows the drift between the file and the project, and `railway config apply` applies it. The file does not manage variables: secrets live only in the Variables tab. The npm package under `.railway/` exists only so the CLI can evaluate the file, and it stays out of the repo root so Railpack keeps seeing a Python project. Deploy on push to `main`. Expected hosting cost: $4 to $6 a month (see README); the model bill is separate. Added datasets live in the same Postgres database as everything else, on a 500 MB volume (alerts at 80, 95 and 100% in `railway.ts`): three files near their limits (200,000 rows each) could come close to filling it, so watch the DB size number in `docs/watch.md` and resize the volume before it is full.

## Secrets

`ANTHROPIC_API_KEY`, `APP_PASSWORD`, `APP_SECRET`, `DATABASE_URL`, `CRON_SECRET`, `DIGEST_WEBHOOK_URL`. Locally in `.env` (gitignored); on Railway in the service's Variables tab, shared with the cron service through a reference variable. `.env.example` lists the names with no values. The grep test in practice.md must come back empty on every commit.

## Observability

- Railway logs per service (requests, errors, and the startup seed report: `suppliers_seeded`, `folder_datasets`, `tables_loaded`, `tables_dropped`, and `skipped` with the reason for any `data/` file that could not be read).
- `runs` table: every model turn with status (`ok`, `refusal`, `error`, `aborted`), tokens and cost. The digest reads it.
- `GET /api/stats`: spend today vs cap, messages this hour.
- `evals/history.csv`: pass rate per run, per git sha, with tokens and cost.

## Decisions

| Date | Decision | Reason | Would change it if |
|---|---|---|---|
| 2026-10-17 | One service serves UI and API | Ten users; a CDN or a second service is cost and a second deploy for nothing | Traffic from the public web, or a separate frontend team |
| 2026-10-17 | Postgres over Airtable | Chat history grows fast; Airtable's record cap and rate limit would be hit within weeks | Non-developers must edit rows by hand daily |
| 2026-10-17 | Structured query tool, no SQL from the model (`sales_query`, since 2026-11-07 the generic `data_query` / `data_rows`) | Least access: the model cannot read or change anything but the tables of the open tab, and every column name is checked against the stored schema; testable | A genuine need for ad-hoc analysis (joins, window functions) the parameters cannot express |
| 2026-10-17 | Images not stored | Cheaper, smaller database, fewer privacy questions; the row is the record | A requirement to show the original invoice later |
| 2026-10-17 | Shared password + typed name, not accounts | One business, ten people, no admin; per-user limits still work | A second client, or a client who wants per-person access control |
| 2026-10-17 | `create_all` + schema_version, no migration tool yet | Schema v1 has no history to migrate; keep the dependency list short | The first schema change after production data exists → add Alembic |
| 2026-10-17 | Digest as a Railway cron service, not an external pinger | Same repo, same variables, runs for seconds, costs cents | Railway removes cron, or the digest needs to run in several regions |
| 2026-10-27 | Railpack build, no Dockerfile | The Dockerfile only pinned Python and ran pip; Railway's default builder does the same from `requirements.txt` and `.python-version`, one file less to keep current | A system package (apt) or a multi-stage build is needed, or Railway drops Railpack |
| 2026-10-27 | Infrastructure as Code (`.railway/railway.ts`) instead of `railway.json` | Railway deprecated config-as-code files (they stop working on 2026-12-01); one IaC file describes both services, so the cron service needs no config path of its own | Railway changes the IaC format again, or a setting the DSL cannot express |
| 2026-11-04 | Chat history is resent as text only; the system prompt says earlier replies were tool-checked and must not be re-checked or retracted unasked | The first live chats showed the model, seeing its old figures without the tool calls behind them, re-running tools on unrelated turns and telling users their searches "never ran" (issue #2). A prompt rule fixes it with no schema change | The eval `no_recheck_after_off_topic` regresses → store a compact tool trace per message and replay it (a schema change, so add Alembic first) |
| 2026-10-28 | Public repository, history restarted as one commit | No secrets live in the code (they are in Variables) and the dataset is mock data; a fresh history keeps the owner's personal email out, and the old private repo is kept as `bakery-copilot-archive` | Real client data or client-specific configuration has to be committed |
| 2026-11-07 | Any CSV/Excel file becomes a typed table; the schema is stored and shown to the model, nothing is hard-coded to the bakery | The app must work for any data. Typing columns from their values lets one tool serve every dataset; the schema in the prompt (value lists, ranges) keeps the model from inventing names, the lesson of issue #3 | Files too big for these limits, or analysis that needs joins: add declared relations between tables, or a read-only SQL role |
| 2026-11-07 | A tab = a dataset; up to 3 added by users, built-in ones come from `data/` folders | Separation (a chat cannot see another tab's data), a small bounded Postgres footprint, and related files (one folder) analysed together | A need for per-person datasets or more than a handful: add ownership, then quotas |
| 2026-11-07 | Conversation → tab in a new `conversation_datasets` table, not a column on `conversations` | `create_all` cannot alter an existing table and production already holds conversations; a new table needs no migration, and "no row" means the default tab | The next schema change that cannot be a new table → add Alembic, as decided on 2026-10-17 |
| 2026-11-07 | Uploaded data is stored as tables in Postgres; the file is not kept | Railway's disk is wiped on every deploy and the repo is public, so neither can hold private data; images are treated the same way | A need to download or re-parse the original: add object storage |
| 2026-11-07 | Links are fetched by the server only for public http(s) addresses, re-checked on every redirect | A server that fetches arbitrary URLs can be pointed at internal services (SSRF). Known limit: the address is resolved once for the check and again by the HTTP client, so a hostile DNS record could in theory change in between | Links to private sources are needed: add an allow-list per deployment, or pin the resolved IP |
| 2026-11-07 | The repository is standalone: `RMcowork/data-copilot`, no longer a GitHub fork of `MaceTenth/bakery-copilot`; the old fork was renamed `RMcowork/data-copilot-old` | A fork carries its parent's banner and link and GitHub has no "unfork" button, so a new empty repo took the name and the history was pushed to it (`main` as it was, then the feature branch through a PR). `railway.ts` and the deploy guide point at the new repo | n/a |
| 2026-11-07 | The UI was restyled with plain CSS variables and system fonts, light and dark, instead of a CSS framework; the theme choice is kept in `localStorage` | No new dependency or build step; one stylesheet is easy to review; the preference is per browser, so it needs no server state | A design system the team wants to share across apps |
| 2026-11-07 | The `data/` files were moved into `data/bakery/` | One folder = one tab keeps the sales and supplier files together and every dataset on the same footing; the data itself was not changed | n/a |
| 2026-11-07 | The page opens in dark; only a chosen light is remembered (this replaces "follow the device") | The owner asked for dark as the default; the sidebar switch stays for anyone who prefers light | Users ask for "follow the device" again |
| 2026-11-07 | A tab takes up to 3 files (not one), added in one go or later; each file is its own table | Related files (orders and customers) belong together; the one-file tab forced a new tab per file and used up the 3 slots. Folders already worked this way | Questions that need joins across the files (not in v1) |
| 2026-11-07 | A tab's name is kept in `datasets.name`; a built-in tab's rename is also stored in a new `dataset_renames` table | A folder tab re-reads its title from `dataset.yaml` at every start and would undo a rename; a new table needs no migration (same reason as `conversation_datasets`) | Renames should also reach the YAML (a repo change, so a PR) |
| 2026-11-07 | A chat is exported by the server as Markdown or HTML, with `markdown-it-py` (raw HTML off); sharing is the browser's share sheet or a copy, never a public link | A downloaded page must not run a script a model reply contained; the app has one shared password, so a public link would publish business data to anyone with the URL | A need to share with people outside the team: add expiring, revocable links |
