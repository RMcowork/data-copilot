# Deploying Data Copilot on Railway

How to put this app on Railway from nothing, where every secret goes, and the Railway CLI commands for the first deploy and for day two. `architecture.md` ("Hosting", "Secrets") records the decisions; `runbook.md` is for when it breaks; this file is the walkthrough. It describes the project as it runs on 2026-10-27, with Railway CLI 5.71.

Commands are written for macOS and Linux (zsh or bash). On Windows, use PowerShell 7: the `railway`, `git`, `gh` and `npm` commands run there unchanged, and wherever Windows differs, a **Windows (PowerShell)** block follows the macOS and Linux one. Command Prompt (`cmd.exe`) is not covered, because it keeps single quotes as part of a value.

## 1. What you are deploying

| Service | What it is | Built from | Runs |
|---|---|---|---|
| `web` | the UI and the API, one FastAPI process | this repo, by Railway's Railpack builder from `requirements.txt` and `.python-version` (3.12); no Dockerfile | `uvicorn app.main:app --host 0.0.0.0 --port $PORT`; health check `GET /health`, 120 s; restarts on failure, 5 retries |
| `digest` | the morning digest, a cron service | the same repo and the same build | `python -m app.digest` at `30 4 * * *` UTC (07:30 Israel in summer, 06:30 in winter); exits in seconds; never restarts |
| `Postgres` | Railway's Postgres plugin on a 500 MB volume | Railway | hands out `DATABASE_URL`; the app rewrites `postgresql://` to `postgresql+psycopg://` itself |

Three places hold the configuration, and they do not overlap:

- **The repo** holds the build inputs (`requirements.txt`, `.python-version`) and the service settings in `.railway/railway.ts`: start commands, health check, restart policy, cron schedule, region, replicas, volume. Railway does not read that file on its own; `railway config apply` pushes it (section 4, step 5).
- **Railway's Variables tab** holds every secret and every tunable, per service. The IaC file leaves variables unmanaged on purpose (`variables: { managed: false }`), so no value is ever in Git.
- **GitHub** holds the code. Railway's GitHub app pulls `main` on every push once it has access to the repo (section 4, step 2). CI (`.github/workflows/ci.yml`) runs the tests on every PR, and the evals only when an `ANTHROPIC_API_KEY` Actions secret exists.

```
 push to main ──► Railway's GitHub app ──► Railpack build ──► web     (uvicorn, /health, public domain)
                                                         └──► digest  (cron 04:30 UTC, exits)

 Variables tab:  web holds the values ──► digest reads them as ${{web.NAME}}
                 Postgres plugin ───────► both read ${{Postgres.DATABASE_URL}}
```

## 2. Before you start

Accounts:

- A GitHub account that can administer the repo (`RMcowork/data-copilot`).
- A Railway account. The Hobby plan covers the $4 to $6 a month this needs (README, "Cost").
- An Anthropic Console account with an API key from https://platform.claude.com/settings/keys. The model bill is separate from Railway's.

Tools on your machine:

| Tool | Why | macOS | Windows | Check |
|---|---|---|---|---|
| Railway CLI 5.71 or newer | everything below | `brew install railway` | `scoop install railway` (below) | `railway --version` |
| Node.js LTS and npm | only so the CLI can evaluate `.railway/railway.ts` | `brew install node` | `winget install OpenJS.NodeJS.LTS` | `node --version` |
| Python 3.12 | `pytest -q` before you deploy | `brew install python@3.12` | `winget install Python.Python.3.12` | `python3 --version`; on Windows `py -3.12 --version` |
| Git | the repo | comes with Xcode's command line tools | `winget install Git.Git` | `git --version` |
| `gh` (optional) | PRs and Actions secrets from the terminal | `brew install gh` | `winget install GitHub.cli` | `gh auth status` |
| `openssl` | generates the two random secrets on macOS and Linux | ships with the system | not needed (section 3.4) | `openssl version` |
| An SSH key (optional) | looking inside the private database (section 6) | `ssh-keygen -t ed25519` if `~/.ssh` has no `.pub` file | the same command; OpenSSH ships with Windows 10 and 11 | `railway ssh keys list` |
| PowerShell 7 | the Windows shell for this guide | not needed | `winget install Microsoft.PowerShell` | `$PSVersionTable.PSVersion` |

On Linux, take Python, Node and Git from the distribution, and the CLI from npm (`npm i -g @railway/cli`, Node 16 or newer) or Railway's script (`bash <(curl -fsSL railway.com/install.sh)`).

**Windows (PowerShell).** Scoop is the route Railway documents for Windows. In a regular PowerShell window, not one run as administrator:

```powershell
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
Invoke-RestMethod -Uri https://get.scoop.sh | Invoke-Expression
scoop install railway
```

The first line lets PowerShell run local scripts, which Scoop's installer and the Python virtual environment's activation script both need; it applies to your user only. Open a new window afterwards so `railway`, `py` and `node` are on the PATH. WSL also works: inside it, follow the Linux instructions.

Then sign in, on any system:

```bash
railway login
```

It opens the browser. On a machine without one, `railway login --browserless` prints a link and a short code to use from any device. The CLI keeps its token in `~/.railway/config.json` (on Windows, `.railway\config.json` in your user folder). That token can read every variable in every project you can see, so treat the file like a password, and `railway logout` on a shared machine.

## 3. Every variable, and where it goes

### 3.1 The secrets

Six names are secrets or carry one. Their values live in Railway's Variables tab and nowhere else: not in `.env.example`, not in `railway.ts`, not in a commit message, not in a chat with Claude Code (practice.md, "Never").

| Name | What it is | Where the value comes from | Set on |
|---|---|---|---|
| `APP_PASSWORD` | the one team password everyone types at login (spec.md §1); compared byte for byte in `app/auth.py` | chosen by Dana | `web`; `digest` by reference |
| `APP_SECRET` | signs the session cookie; 32+ random characters | generated, section 3.4 | `web`; `digest` by reference |
| `ANTHROPIC_API_KEY` | the model key | https://platform.claude.com/settings/keys | `web`; `digest` by reference; optionally a second copy as a GitHub Actions secret so CI runs the evals |
| `CRON_SECRET` | guards `POST /internal/digest`, the by-hand way to run the digest (header `X-Cron-Secret`); without it that endpoint always answers 401 | generated, section 3.4 | `web`; `digest` by reference |
| `DATABASE_URL` | the Postgres connection string, password included | Railway's plugin; never typed. Set as the reference `${{Postgres.DATABASE_URL}}` | `web` and `digest` |
| `DIGEST_WEBHOOK_URL` | optional. A Slack- or Telegram-style incoming webhook that receives `{"text": ...}`. The URL itself is the credential | the chat tool's "incoming webhook" settings | `digest`; also `web` if `/internal/digest` should post |

Why the digest references a password and a key it never uses: `app/config.py` refuses to start in production without `APP_PASSWORD`, `APP_SECRET` and `ANTHROPIC_API_KEY`, and the digest imports the same settings. A reference (`${{web.APP_SECRET}}`) keeps one value in one place, so rotating it on `web` rotates it everywhere.

### 3.2 The settings

Optional in the code (`app/config.py` has the same defaults), but set them anyway so the dashboard shows what is in force. `APP_ENV` is the exception: it is required, see its row.

| Name | Production value | Meaning |
|---|---|---|
| `APP_ENV` | `production` | turns on the startup check for the three secrets and the `secure` flag on the cookie. Without it a missing `APP_PASSWORD` silently becomes `bakery` and a missing `APP_SECRET` becomes a new random value on every start, which signs everyone out at each deploy |
| `MODEL` | `claude-opus-5-5` | the chat model. Change it only with an eval run before and after (practice.md). The 2026-11-07 evals (13/13) ran on `claude-sonnet-5-5`, the cheaper choice; deploy the model the evals passed on |
| `MODEL_EFFORT` | `medium` | `low`, `medium` or `high` |
| `HISTORY_LIMIT` | `20` | messages resent per turn |
| `RATE_LIMIT_PER_HOUR` | `30` | per user |
| `DAILY_CAP_USD` | `3.00` | hard stop for the whole app per local day; raise it only with Dana's agreement (runbook) |
| `MAX_UPLOAD_MB` | `5` | invoice photo size |
| `MAX_DATASET_MB` | `25` | largest file a user can upload or link as a dataset |
| `MAX_DATASET_ROWS` | `200000` | most rows in one table of an uploaded or linked file |
| `MAX_UPLOADED_DATASETS` | `3` | datasets users can add in the UI (the `data/` folders do not count) |
| `MAX_FILES_PER_DATASET` | `3` | files (or links) in one added dataset, i.e. in one tab |
| `TIMEZONE` | `Asia/Jerusalem` | the local day for the cap, and which day the digest calls "yesterday" |
| `PYTHONUNBUFFERED` | `1` | so `print` reaches Railway's logs at once; the digest's six lines depend on it |
| `MAX_TOKENS` | not set (2048) | model output cap |
| `SESSION_HOURS` | not set (72) | cookie lifetime |

Never set on Railway: `MODEL_PROVIDER` (tests only; `fake` answers with scripted replies), `PORT` (Railway injects it and the start command reads `$PORT`), anything starting with `RAILWAY_` (injected).

### 3.3 What is set on which service

- **web**: `APP_ENV`, `APP_PASSWORD`, `APP_SECRET`, `ANTHROPIC_API_KEY`, `CRON_SECRET`, `DATABASE_URL` (reference), `MODEL`, `MODEL_EFFORT`, `HISTORY_LIMIT`, `RATE_LIMIT_PER_HOUR`, `DAILY_CAP_USD`, `MAX_UPLOAD_MB`, `TIMEZONE`, `PYTHONUNBUFFERED`. Optional: `DIGEST_WEBHOOK_URL`.
- **digest**: `APP_ENV`, `APP_PASSWORD`, `APP_SECRET`, `ANTHROPIC_API_KEY`, `CRON_SECRET`, `MODEL`, `TIMEZONE`, each as `${{web.NAME}}`; `DATABASE_URL` as `${{Postgres.DATABASE_URL}}`; `PYTHONUNBUFFERED=1` as a plain value. Optional: `DIGEST_WEBHOOK_URL` as a plain value.
- **Postgres**: nothing to set. Railway manages its own variables.

### 3.4 Entering secrets without leaking them

A generated secret goes straight from `openssl` to Railway. It never appears on screen or in shell history, and `--stdin` strips the trailing newline (checked with CLI 5.71):

```bash
openssl rand -hex 32 | railway variable set APP_SECRET --service web --stdin --skip-deploys
```

A value you already have (the team password, the Anthropic key) goes in one of two ways. In the dashboard: the service → **Variables** → **New Variable** → paste → then the purple **Deploy** button at the top, because edits are staged until you press it. Or from the terminal, with `read -s` so the value is not echoed and not recorded:

```bash
read -rs V && printf '%s' "$V" | railway variable set ANTHROPIC_API_KEY --service web --stdin --skip-deploys; unset V
```

Type or paste the value (nothing shows), press Enter. `--skip-deploys` holds the redeploy until every variable is in; drop it for a one-off change that should deploy at once.

**Windows (PowerShell).** Piping text into a program in PowerShell appends a line ending, which could end up inside the value, so pass the value as an argument built from a variable instead. The command line then holds the variable's name, not its value, so the secret stays out of PowerShell's history. Define the generator once per window, then set the value:

```powershell
function New-Secret { $b = New-Object byte[] 32; [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($b); -join ($b | ForEach-Object { $_.ToString('x2') }) }
railway variable set "APP_SECRET=$(New-Secret)" --service web --skip-deploys
```

For a value you already have, `Read-Host -AsSecureString` reads it without showing it:

```powershell
$v = Read-Host 'ANTHROPIC_API_KEY' -AsSecureString
railway variable set "ANTHROPIC_API_KEY=$([System.Net.NetworkCredential]::new('', $v).Password)" --service web --skip-deploys
Remove-Variable v
```

If the value itself contains a double quote, enter it in the dashboard instead.

What prints secrets, so never paste its output into a chat, an issue or a commit: `railway variable list` (plain, `--kv` or `--json`), `railway run env`, `railway shell`, and `railway config pull --include-variables`, which would also write them into the authoring file.

## 4. First deployment, step by step

Start from a clean clone of `main` with `pytest -q` green, signed in to Railway and GitHub.

### Step 1: project and database

```bash
railway init --name data-copilot
railway add --database postgres
```

`railway init` creates the project and links this folder to it (the link is stored in `~/.railway/config.json`; on another machine, `railway link` picks the existing project). The database service is called `Postgres`, and it must keep that name: `.railway/railway.ts` and the `${{Postgres.DATABASE_URL}}` references use it.

### Step 2: let Railway's GitHub app see the repo

Open https://github.com/apps/railway-app/installations/new (the same page as Railway → Account Settings → Integrations → GitHub → Configure). Pick the account that owns the repo, choose **Only select repositories** → `data-copilot`, save.

Do this before step 3. A service created while the app has no access keeps the repo as its source but gets no push trigger, and installing the app later does not add one: builds from GitHub fail with `Repository "RMcowork/data-copilot" not found or is not accessible`, and a push to `main` deploys nothing, silently. If the services already exist, reconnect each one once the app has access; the command creates the trigger and starts a build of `main` at once:

```bash
railway service source connect --repo RMcowork/data-copilot --branch main --service web
railway service source connect --repo RMcowork/data-copilot --branch main --service digest
```

Step 6 has the workaround for a repo the app cannot see at all (`railway up`).

### Step 3: the two services

```bash
railway add --service web --repo RMcowork/data-copilot --branch main
railway add --service digest --repo RMcowork/data-copilot --branch main
```

Each starts a build at once. That first deploy fails or boots in local mode (no variables yet); ignore it. The next deploy replaces it.

### Step 4: variables

On `web`, the plain values first. These lines work as they are in zsh, bash and PowerShell. The single quotes matter in all three: without them the shell tries to expand `${{...}}`, and the command fails or the reference arrives broken.

```bash
railway variable set --service web --skip-deploys APP_ENV=production MODEL=claude-sonnet-5-5 MODEL_EFFORT=medium
railway variable set --service web --skip-deploys HISTORY_LIMIT=20 RATE_LIMIT_PER_HOUR=30 DAILY_CAP_USD=3.00
railway variable set --service web --skip-deploys MAX_UPLOAD_MB=5 TIMEZONE=Asia/Jerusalem PYTHONUNBUFFERED=1
railway variable set --service web --skip-deploys 'DATABASE_URL=${{Postgres.DATABASE_URL}}'
```

Then the four secrets (section 3.4):

```bash
openssl rand -hex 32 | railway variable set APP_SECRET --service web --stdin --skip-deploys
openssl rand -hex 32 | railway variable set CRON_SECRET --service web --stdin --skip-deploys
read -rs V && printf '%s' "$V" | railway variable set APP_PASSWORD --service web --stdin --skip-deploys; unset V
read -rs V && printf '%s' "$V" | railway variable set ANTHROPIC_API_KEY --service web --stdin --skip-deploys; unset V
```

**Windows (PowerShell).** The same four, as in section 3.4:

```powershell
function New-Secret { $b = New-Object byte[] 32; [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($b); -join ($b | ForEach-Object { $_.ToString('x2') }) }
railway variable set "APP_SECRET=$(New-Secret)" --service web --skip-deploys
railway variable set "CRON_SECRET=$(New-Secret)" --service web --skip-deploys
$v = Read-Host 'APP_PASSWORD' -AsSecureString
railway variable set "APP_PASSWORD=$([System.Net.NetworkCredential]::new('', $v).Password)" --service web --skip-deploys
$v = Read-Host 'ANTHROPIC_API_KEY' -AsSecureString
railway variable set "ANTHROPIC_API_KEY=$([System.Net.NetworkCredential]::new('', $v).Password)" --service web --skip-deploys
Remove-Variable v
```

On `digest`, references only, plus the unbuffered flag. Again the same in every shell:

```bash
railway variable set --service digest --skip-deploys 'APP_ENV=${{web.APP_ENV}}' 'APP_PASSWORD=${{web.APP_PASSWORD}}'
railway variable set --service digest --skip-deploys 'APP_SECRET=${{web.APP_SECRET}}' 'ANTHROPIC_API_KEY=${{web.ANTHROPIC_API_KEY}}'
railway variable set --service digest --skip-deploys 'CRON_SECRET=${{web.CRON_SECRET}}' 'MODEL=${{web.MODEL}}' 'TIMEZONE=${{web.TIMEZONE}}'
railway variable set --service digest --skip-deploys 'DATABASE_URL=${{Postgres.DATABASE_URL}}' PYTHONUNBUFFERED=1
```

If the digest should post somewhere, add the webhook the same way as a secret:

```bash
read -rs V && printf '%s' "$V" | railway variable set DIGEST_WEBHOOK_URL --service digest --stdin --skip-deploys; unset V
```

**Windows (PowerShell):**

```powershell
$v = Read-Host 'DIGEST_WEBHOOK_URL' -AsSecureString
railway variable set "DIGEST_WEBHOOK_URL=$([System.Net.NetworkCredential]::new('', $v).Password)" --service digest --skip-deploys
Remove-Variable v
```

To check what is set, open the Variables tab in the dashboard; the CLI listing prints values.

### Step 5: service settings from code

```bash
npm install --prefix .railway
railway config plan
railway config apply --yes
```

The three lines are the same in PowerShell. `npm install` runs once per machine and installs the `railway` npm package the CLI needs to evaluate `.railway/railway.ts`; it stays under `.railway/` so Railpack keeps seeing a Python project, and `node_modules` is gitignored. `plan` prints the difference between the file and the project; `apply` makes the project match: both start commands, the `/health` check with its 120 s timeout, the restart policies, the digest's schedule, region `iad`, one replica each, the volume. Run `plan` again and it says "already up to date".

Skip this step and `web` starts with Railpack's own guess at a start command, which is wrong for this layout (the app is `app/main.py`, not `main.py`), and `digest` has no schedule, so it runs once and never again.

### Step 6: deploy

With GitHub access in place (step 2), redeploy both services so they start with the variables:

```bash
railway redeploy --service web --yes
railway redeploy --service digest --yes
```

Without GitHub access, upload the folder instead. `railway up` zips the current directory (honouring `.gitignore`, so `.env` and `.venv` stay home), builds it and deploys it. It deploys what is on disk, committed or not, so run it from a clean checkout of `main`:

```bash
railway up --service web --ci
railway up --service digest --ci
```

`--ci` streams the build log and exits; without it the command keeps following the deploy log (Ctrl-C leaves, the deploy continues).

Give `web` a URL:

```bash
railway domain --service web
```

It generates `web-production-xxxx.up.railway.app`. For a custom domain, `railway domain copilot.example.com --service web` prints the DNS record to add. Railway routes to the port the app listens on (`$PORT`); no `--port` needed.

### Step 7: check it

```bash
curl -s https://<your-domain>/health
```

Expect 200 with `"status": "ok"`, `"env": "production"`, `"db": "postgres"` and `"problems": []`. A 503 names the problem (section 7). Then the password check:

```bash
curl -s -X POST https://<your-domain>/api/login -H 'content-type: application/json' -d '{"name":"Check","password":"wrong"}'
```

Expect 401 `Wrong password.`

**Windows (PowerShell).** Windows PowerShell 5.1 maps `curl` to a different command, so use PowerShell's own client. The second command must end in a 401 Unauthorized error:

```powershell
Invoke-RestMethod https://<your-domain>/health
Invoke-RestMethod -Method Post -Uri https://<your-domain>/api/login -ContentType 'application/json' -Body '{"name":"Check","password":"wrong"}'
```

Now open the URL in a browser, sign in with a name and the real password, ask "What sold best in the last week of August 2026?", attach an invoice, and run through the twelve checks in spec.md §7.

The digest, off schedule:

```bash
railway cron run --service digest --yes
railway cron list --service digest
railway logs --service digest -d -n 20
```

`cron run` enqueues the run and returns; it can take a few minutes to show in `cron list`. The log must end with the six digest lines and `[sent to webhook: True]` (or `False` when `DIGEST_WEBHOOK_URL` is not set).

`web`'s first start loads the `data/` folders (the Bakery tab: 29,115 sales rows and 10 supplier rows) and seeds the 10 suppliers of the invoice tool, which can take up to a minute (the health timeout is 120 s). `railway logs --service web -d -n 50` shows it as one line, `seed report: schema_version: 1, suppliers_seeded: 10, folder_datasets: 1, tables_loaded: 2, tables_dropped: 0, skipped: []`. Later deploys print `0` for `suppliers_seeded` and `tables_loaded`: a file is reloaded only when its content changes. Datasets added through the UI live in Postgres and survive every deploy; they are not in this line.

Last, if the evals should run in CI: `gh secret set ANTHROPIC_API_KEY --repo RMcowork/data-copilot` asks for the value without echoing it. Without the secret the evals job prints "skipped" and only the tests gate the merge.

## 5. The same steps in the dashboard

For whoever prefers clicking. Project → **+ New** → **Database** → PostgreSQL. **+ New** → **GitHub Repo** → `data-copilot` (this is where the app-access prompt from step 2 appears) → rename the service to `web`; the same again for `digest`. Service → **Variables** → **Raw Editor** → paste `NAME=value` lines, references included → **Deploy** the staged changes from the banner. Until you press it, nothing is applied, and `railway variable list` does not show the staged values. Service → **Settings** → **Networking** → **Generate Domain**.

Do not set the start command, health check, cron schedule or restart policy by hand under Settings → Deploy: the file is the record, `railway config plan` reports hand edits as drift, and the next `railway config apply` undoes them. Change the file instead (section 6).

## 6. Day two

**Ship a change.** Branch → PR → CI green → merge. Railway builds `main` on push once the GitHub app has access; `railway deployment list --service web` shows each deployment with its commit and status, and `railway logs --service web -b` follows the build, `-d` the running app.

**Change a service setting** (schedule, health timeout, retries, replicas, region). Edit `.railway/railway.ts` in a ticket branch, run `railway config plan`, put the plan's output in the PR, merge, then `railway config apply --yes`. Railway never applies the file by itself; the apply is a deliberate step by a person with the CLI. If a decision changed, `architecture.md` changes in the same commit (practice.md step 6).

**Change a variable.** Dashboard → edit → **Deploy**, or `railway variable set NAME=value --service web`, which redeploys `web` at once unless you pass `--skip-deploys`. The digest reads its `${{web.NAME}}` references at its next run. `MODEL` and `MODEL_EFFORT` need an eval run before and after.

**Rotate a secret.** runbook.md, "A key leaked". Order: new value at the source (the Anthropic console, or a random value as in section 3.4), set it on `web` (the digest follows by reference), redeploy, and only then clean Git history if it was in a commit. Rotating `APP_SECRET` signs everyone out. Rotating `APP_PASSWORD` changes only what new logins must type; existing cookies stay valid for up to 72 hours. Rotating `CRON_SECRET` means telling whoever calls `/internal/digest`.

**Roll back.** Dashboard → `web` → **Deployments** → the previous green deploy → **Redeploy** (runbook, "App is down"). From the CLI, `railway redeploy --service web --yes` redeploys only the latest build, and `railway redeploy --service web --commit <sha> --yes` deploys one specific commit once (needs GitHub access).

**Run the digest by hand.** `railway cron run --service digest --yes`, or the endpoint with the `CRON_SECRET` value from the Variables tab, typed or pasted at the hidden prompt:

```bash
read -rs CRON_SECRET && curl -s -X POST https://<your-domain>/internal/digest -H "X-Cron-Secret: $CRON_SECRET"; unset CRON_SECRET
```

**Windows (PowerShell):**

```powershell
$v = Read-Host 'CRON_SECRET' -AsSecureString
Invoke-RestMethod -Method Post -Uri https://<your-domain>/internal/digest -Headers @{ 'X-Cron-Secret' = [System.Net.NetworkCredential]::new('', $v).Password }
Remove-Variable v
```

It returns the text and whether it was sent to the webhook.

**Look at the database.** Read only: rows are never edited by hand (practice.md). The database has no public address, because `railway add --database postgres` creates no TCP proxy, and that is the safer way to keep it. So every way in goes over SSH, and Railway must know your SSH key first. Once per machine:

```bash
railway ssh keys add
```

It registers the public key it finds in `~/.ssh` (on Windows, `.ssh` in your user folder); `railway ssh keys github` imports the keys of your GitHub account instead. The first connection asks you to trust `ssh.railway.com`: answer `yes` in an interactive terminal. A script cannot answer, and fails with `Host key verification failed.`

Then `railway connect Postgres` opens `psql` through an SSH tunnel (`psql` must be installed locally; on Windows it comes with the PostgreSQL installer from postgresql.org). Without `psql`, run Python inside `web`, which reaches the database over Railway's private network and prints no password:

```bash
railway ssh --service web -- python -c "from sqlalchemy import text; from app.db import engine; print(engine.connect().execute(text('select name, row_count from dataset_tables')).all())"
```

Expect `bakery_sales` with `29115` rows and `suppliers` with `10`, plus one row per table of any dataset added in the UI. `railway ssh --service web -- python -m app.digest` prints the digest the same way. `railway run --service web -- <command>` is different: it runs the command on your machine with the service's variables, the real key included, so use it for one-off work and never `railway run env`. Its `DATABASE_URL` points at Railway's private network, which your machine cannot reach.

**A staging environment.** `railway environment new staging --duplicate production` creates copies of the three services with the same variables. Give it its own `APP_PASSWORD`, then `railway config apply --environment staging`. runbook.md's database-restore entry assumes one exists.

**Remove things.** `railway down --service web --yes` removes the latest deployment and keeps the service; `railway service delete --service web --yes` removes a service; `railway delete` removes the whole project, Postgres volume included.

## 7. When something goes wrong

| Symptom | Cause | Fix |
|---|---|---|
| Build log: `Repository "…" not found or is not accessible` | Railway's GitHub app has no access to the private repo | section 4 step 2; meanwhile `railway up --service web --ci` from a clean `main` |
| A push to `main` starts no build, although the app has access and the service shows the repo as its source | the service has no push trigger: it was created before the app had access, and the trigger is not added retroactively | `railway service source connect --repo RMcowork/data-copilot --branch main --service web`, and the same for `digest` |
| Deploy log: `RuntimeError: Missing required variables in production: APP_PASSWORD, …` | `APP_ENV=production` is set on that service but the named variables are not | set them (on `digest`, as `${{web.NAME}}`), then redeploy |
| `/health` is 503 with `"problems": ["ANTHROPIC_API_KEY missing"]` | the key is not set on `web`, or was pasted in the dashboard and never deployed | Variables → check → **Deploy**; or `railway redeploy --service web --yes` |
| `/health` is 503 with `"problems": ["db: …"]` | `DATABASE_URL` is not the reference, or the database service is not named `Postgres` | set `DATABASE_URL=${{Postgres.DATABASE_URL}}` |
| Variables pasted in the dashboard, but the app behaves as if they were missing | the edit is staged, not deployed | press **Deploy** on the banner; the CLI listing shows only applied values |
| `railway config plan`: "The Railway TypeScript SDK is not installed" | `.railway/node_modules` is missing on this machine | `npm install --prefix .railway` |
| `railway config plan` keeps reporting `web deploy.restartPolicyType (null → "ON_FAILURE")` | `ON_FAILURE` is Railway's default; declaring it in the file produces a permanent false drift | leave it out; the file sets only `restartPolicyMaxRetries` |
| Deploy log: `ModuleNotFoundError: No module named 'main'`, or "no start command" | the start command from `railway.ts` was never applied | `railway config apply --yes` |
| The build uses Python 3.13 | `.python-version` is missing from the repo root | it must be there with `3.12`; Railpack reads it |
| The first deploy's health check times out | loading the data/ files takes up to a minute | wait for the 120 s timeout; `railway logs --service web -d` shows `tables_loaded: 2`; later deploys are fast |
| The digest runs at the wrong hour | cron schedules are UTC | `30 4 * * *` is 07:30 IDT and 06:30 IST; change it in `railway.ts`, plan, apply |
| `railway logs --service digest` shows nothing for a run | you are reading another deployment, or output was buffered | `railway cron list --service digest` for the run, then `railway logs <deployment-id> --service digest -d`; keep `PYTHONUNBUFFERED=1` |
| `railway api` answers "Not Authorized" to GitHub queries | the CLI token cannot see the GitHub integration | use the dashboard for anything about repo access |
| `railway ssh` or `railway connect Postgres`: `No registered SSH keys found` | the database is private, so both go over SSH, and Railway has no key of yours | `railway ssh keys add` (section 6) |
| `railway ssh`: `Host key verification failed.` | the first connection to `ssh.railway.com` needs a yes, and a script cannot give one | run the same command once in an interactive terminal and answer `yes` |
| Login refused with the right password | `APP_PASSWORD` carries a stray space, newline or quotes; the comparison is byte for byte | set it again (section 3.4) |
| Windows: `… cannot be loaded because running scripts is disabled on this system` | PowerShell's default execution policy blocks local scripts (Scoop's installer, `Activate.ps1`) | `Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser`, once |
| Windows: `railway`, `py` or `node` "is not recognized" right after installing | the window was opened before the install changed the PATH | open a new PowerShell window |
| Windows: a `${{...}}` reference arrives empty or as `}` | it was in double quotes, and PowerShell expanded it as a variable | single quotes, as in step 4 |
| Windows: `curl` prompts for parameters or fails with `Invoke-WebRequest` errors | Windows PowerShell 5.1 maps `curl` to `Invoke-WebRequest` | `Invoke-RestMethod` as in step 7, or `curl.exe` |
| Windows: `UnicodeEncodeError: 'charmap' codec can't encode character '₪'` when `python -m app.digest` writes to a file or a pipe | redirected output uses the Windows code page, which has no ₪ | `$env:PYTHONUTF8 = '1'` in that window |
| Windows: `$'\r': command not found` from `scripts/create_issues.sh` in Git Bash | the script was checked out with Windows line endings | `.gitattributes` now keeps `.sh` files LF; in an older clone, delete the file and run `git checkout -- scripts/create_issues.sh` |

## 8. Railway CLI cheat sheet

Most commands take `--service`, `--environment`, `--project`, `--json` and `--yes`. Without `--service` the CLI uses the linked service shown by `railway status`, so scripts should always pass it. They run the same in PowerShell, with one exception: Windows PowerShell 5.1 drops double quotes inside an argument, which breaks `railway api` queries; PowerShell 7 passes them through.

| Command | What it does |
|---|---|
| `railway login`, `railway logout`, `railway whoami` | sign in (browser; `--browserless` for a code), sign out, who am I |
| `railway init --name data-copilot` | create a project and link this folder to it |
| `railway link` | link this folder to an existing project (`-p`, `-e`, `-s` to pick) |
| `railway status` | the linked project, environment and service |
| `railway list` | every project you can see |
| `railway add --database postgres` | add the Postgres plugin |
| `railway add --service web --repo owner/repo --branch main` | add a service built from GitHub |
| `railway service source connect --repo owner/repo --branch main --service web` | connect, or reconnect, a service to GitHub; creates the push trigger and builds the branch |
| `railway service list` | services in the environment |
| `railway variable set K=V K2=V2 --service web --skip-deploys` | set values; `railway variable set K --stdin` reads one value from stdin |
| `railway variable list --service web` | list variables; prints values, never paste the output |
| `railway variable delete K --service web` | delete one |
| `railway config plan`, `railway config apply --yes` | diff, then apply `.railway/railway.ts` to the project |
| `railway config pull` | import the live project into the authoring file (`--force` overwrites); for comparing, not for committing blind |
| `railway up --service web --ci` | build and deploy the current folder, no GitHub needed |
| `railway redeploy --service web --yes` | redeploy the latest build; `--from-source` pulls the latest commit; `--commit <sha>` deploys that commit once |
| `railway deployment list --service web` | deployments with id, status and commit |
| `railway logs --service web -d -n 100` | deploy logs; `-b` build, `--http` requests; `-f '@level:error'` filters; omit `-n` to stream |
| `railway domain --service web` | generate a public domain; `railway domain example.com --service web` adds a custom one |
| `railway cron run --service digest --yes`, `railway cron list --service digest` | run the digest now; recent runs |
| `railway ssh keys add`, `railway ssh keys list` | register your SSH key with Railway (needed for the two below); list registered keys |
| `railway connect Postgres` | `psql` through an SSH tunnel |
| `railway ssh --service web -- <command>` | run a command inside the running `web` container, which can reach the database |
| `railway run --service web -- <command>` | run a local command with the service's variables (it cannot reach the private database) |
| `railway environment list`, `railway environment new staging --duplicate production` | environments |
| `railway open` | the project in the dashboard |
| `railway api '<graphql>'` | raw GraphQL against Railway's API; `railway api describe <Type>` prints a type |
| `railway down --service web --yes`, `railway service delete --service web --yes`, `railway delete` | remove the latest deployment, a service, the project |

## 9. This deployment, as of 2026-10-27

- Railway: project `data-copilot`, environment `production`, services `web`, `digest` and `Postgres`, region `iad`, with a generated `*.up.railway.app` domain. The address is not in this repo: find it in the dashboard (`web` → Settings → Networking) or with `railway domain list --service web`. `railway config plan` reports no drift.
- Set and verified: every variable in section 3 on both services, except `DIGEST_WEBHOOK_URL`, so the digest prints to the log only. `/health` answers 200; a wrong password gets 401; an off-schedule digest run completed with exit 0.
- Deploys from GitHub work: the app was installed on 2026-10-27, both services were reconnected with `railway service source connect` (section 4, step 2, since the first `railway add` had left no push trigger), and the head of `main` then built and deployed from GitHub on both services, with `/health` at 200 afterwards.
- Not done yet: no `DIGEST_WEBHOOK_URL`, and no `ANTHROPIC_API_KEY` Actions secret, so CI skips the evals.
