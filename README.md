# Data Copilot

A chat app to ask questions about your data, built the Lesson 9 way: a chat UI, a backend with an agent loop and structured read-only tools, Postgres, and a morning digest, all on Railway for a few dollars a month. It comes with one built-in dataset, a bakery chain's sales and supplier prices (the **Bakery** tab, where staff can also log supplier invoices from a photo), and you can add your own.

## Use your own data

- **In the app**: click **+ Add dataset**, upload a CSV or Excel file, or paste a link to one (a Google Sheets link works if the sheet is shared as "anyone with the link"). You get a new tab to chat with that data. Up to 3 added datasets, 25 MB and 200,000 rows each; delete one (✕) to free a slot. The data is stored in the database, not in Git, so private data belongs here.
- **In the repo**: put files under `data/`. A subfolder is one tab and the files in it are its tables, so related files stay together (`data/retail/orders.csv`, `data/retail/customers.csv`); a loose file is a tab of its own. They load at start and reload when a file changes. The repository is public, so use this only for data that may be public. An optional `dataset.yaml` in the folder sets the tab's `title`, `description`, `notes` for the assistant, `starters` (suggested questions) and `features`; see `data/bakery/dataset.yaml`.

Columns are typed from their values (numbers, dates, text), so any table works. The assistant never writes SQL: it picks a metric, a grouping and filters, and the backend checks them against the schema.

The plan lives in five files: [spec.md](spec.md) (what), [architecture.md](architecture.md) (how and where), [practice.md](practice.md) (how we work), [runbook.md](runbook.md) (when it breaks), [docs/tickets.md](docs/tickets.md) (the work, as issues). `CLAUDE.md` points Claude Code at the first three.

## Run it locally

macOS and Linux:

```bash
cp .env.example .env         # set APP_PASSWORD, APP_SECRET, ANTHROPIC_API_KEY
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Windows, in PowerShell 7 with Python 3.12 (`winget install Python.Python.3.12`):

```powershell
Copy-Item .env.example .env  # set APP_PASSWORD, APP_SECRET, ANTHROPIC_API_KEY
py -3.12 -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn app.main:app --reload
```

If PowerShell refuses to run `Activate.ps1` ("running scripts is disabled on this system"), run `Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser` once, or skip activation and start each command with `.venv\Scripts\python -m`, for example `.venv\Scripts\python -m uvicorn app.main:app --reload`.

Open http://localhost:8000, type a name and the password. The first start loads the bakery dataset (29,115 sales lines and 10 suppliers) into SQLite in a couple of seconds.

Try, on the Bakery tab: "What sold best in the last week of August 2026?", "Revenue by branch for August 2026", or attach an invoice photo. Then add a file of your own.

## Tests and evals

```bash
pytest -q                    # model mocked; seconds; no key needed
python -m evals.run          # real model; one turn per case; appends evals/history.csv
python -m evals.run --judge  # also grades wording with a cheaper model
```

The same three commands work in PowerShell once `.venv` is active.

Tests prove the deterministic parts (parsing, dedup, limits, streaming, the tool loop with a scripted model). Evals prove the agent still behaves after a prompt, tool or model change. CI runs tests on every PR, and evals when the `ANTHROPIC_API_KEY` secret is set.

## Deploy on Railway

The short version follows; the full walkthrough for macOS, Linux and Windows, with every variable and secret, the CLI commands, the dashboard path, day-two operations and troubleshooting, is [docs/deploy-railway.md](docs/deploy-railway.md).

1. **Repo.** Push this folder to a private GitHub repo. Create the issues with `scripts/create_issues.sh` if you want the board (a bash script: on Windows, run it from Git Bash).
2. **Project and services.** With the CLI (`brew install railway` on macOS, `scoop install railway` on Windows): `railway login`, `railway init --name data-copilot`, `railway add --database postgres`, then `railway add --service web --repo <owner>/data-copilot --branch main` and the same with `--service digest`. Railway's GitHub app needs access to the repo (Railway → Account Settings → Integrations → GitHub), or builds from GitHub fail with "repository not accessible".
3. **Settings from code.** Once: `npm install --prefix .railway`. Then from the repo root `railway config plan` and `railway config apply`: `.railway/railway.ts` sets both start commands, the `/health` check, restart policies and the digest's cron schedule (`30 4 * * *`, UTC; 07:30 Israel in summer, 06:30 in winter). Railpack builds from `requirements.txt` with the Python version in `.python-version`, no Dockerfile.
4. **Variables** on the web service: `APP_ENV=production`, `APP_PASSWORD`, `APP_SECRET` (`openssl rand -hex 32`, or the PowerShell generator in the guide), `ANTHROPIC_API_KEY`, `DATABASE_URL` as a reference to `${{Postgres.DATABASE_URL}}`, `MODEL=claude-opus-5-5`, `DAILY_CAP_USD=3.00`, `CRON_SECRET`, optionally `DIGEST_WEBHOOK_URL`, `TIMEZONE=Asia/Jerusalem`. Redeploy.
5. **Digest variables.** The digest service references the web service's values: `APP_ENV=${{web.APP_ENV}}`, and the same for `APP_PASSWORD`, `APP_SECRET`, `ANTHROPIC_API_KEY`, `CRON_SECRET`, `MODEL` and `TIMEZONE`, plus `DATABASE_URL=${{Postgres.DATABASE_URL}}`. Set `DIGEST_WEBHOOK_URL` here if the digest should post somewhere. The service runs for seconds and exits.
6. **First check.** Open the URL, sign in, ask a sales question, attach an invoice. `GET /health` must be 200. Then run through spec.md §7.

Deploy on push to `main` is on by default. Rollback is Deployments → previous deploy → Redeploy.

## Cost (as of October 2026)

| Item | Estimate |
|---|---|
| Railway web service (0.05–0.1 vCPU, ~200 MB) | $3–4 / month |
| Railway Postgres (small, 1 GB) | $1–2 / month |
| Cron service (seconds a day) | cents |
| **Railway total** | **$4–6 / month**, inside the Hobby plan's $5 most months |
| Claude, 300 turns a day with history (Opus 5.5) | ~$18 / month; Sonnet 5.5 ~$9 |

The model is the bill. `HISTORY_LIMIT`, `MODEL_EFFORT` and prompt caching are the three levers; `DAILY_CAP_USD` is the brake.

## Layout

```
app/            backend: main.py (API), agent.py (loop), datasets.py (load any CSV/Excel), fetch.py (safe link download), tools/, db.py, auth.py, limits.py, costs.py, digest.py
app/prompts/    system.md — the agent's job description; invoices.md — added on tabs with the invoices feature
static/         the chat UI (plain HTML, CSS, JS)
data/           one subfolder per built-in dataset (data/bakery/: bakery_sales.csv, suppliers.csv, dataset.yaml)
tests/          pytest, model mocked
evals/          cases.yaml, run.py, history.csv
docs/           tickets.md, watch.md, deploy-railway.md
```
