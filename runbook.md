# runbook.md — when it breaks

Written while everything works. Each entry is a sequence: click here, run this, check that. Add an entry after every incident.

## App is down (health check red, URL not loading)

1. Railway → the web service → **Deployments**. Is the latest deploy red? Open its logs; the last lines say why (usually a missing variable or a failed import).
2. Click the previous green deploy → **Redeploy**. The app is back on the old version within a minute.
3. Open an issue with the log lines. Fix forward through a PR. Do not edit anything in the Railway shell.

## Model errors (replies fail with "try again in a minute")

1. Check `https://status.anthropic.com`. If there is an incident, wait; the SDK already retries 429/529.
2. Check the day cap: `GET /api/stats` (signed in). If `spend_today_usd ≥ daily_cap_usd`, the app is doing its job; raise `DAILY_CAP_USD` in Variables only if Dana agrees to the cost.
3. Check the key: `/health` says `ANTHROPIC_API_KEY missing` if the variable was lost. Set it in Variables and redeploy.
4. Look at the `runs` table for `status = 'error'` and the `error` column.

## A key leaked (in a commit, a chat, a screenshot)

1. **Rotate first.** platform.claude.com → API keys → revoke the key, create a new one. For `APP_PASSWORD` or `APP_SECRET`: generate new values.
2. Update the Railway Variables, redeploy. Sessions will be invalidated when `APP_SECRET` changes; users sign in again.
3. Only then clean Git history (`git filter-repo` or a fresh repo for a small project) and force-push. Tell anyone who cloned.
4. Add a `bug` issue describing how it leaked, and a rule to practice.md if one is missing.

## Wrong rows written (an invoice logged twice, wrong amount, wrong supplier)

1. Pause the cron service so the digest does not report it again.
2. Find the turn: `runs` table → by time → `conversation_id`; the UI's Invoices panel shows the rows.
3. Fix forward via a ticket: a migration or a one-off script committed to the repo, reviewed, run once. Never edit rows in the database console by hand.
4. If the cause was the prompt or the tool rules, add an eval case first, then fix.

## Database restore

1. Railway → Postgres → **Backups** → pick the snapshot → restore into the **staging** environment's database.
2. Point a staging deploy at it; open the app; check Invoices, a Bakery question and any dataset added in the UI.
3. Promote: restore into production in the same way, during a quiet hour; redeploy the web service.

## Deploy is stuck or the health check never passes

1. Logs → look for the seed report (`tables_loaded: 2`, and `skipped: []`). A first deploy loads the data/ files and can take a minute; the health check timeout is 120 s. A file named under `skipped` could not be read: fix it and redeploy.
2. If the database URL is wrong, `/health` returns 503 with `db:` in `problems`. Check the `DATABASE_URL` reference variable.
3. Rollback to the previous deploy if users are waiting; fix in a PR.

## Who to call

- You (the implementer), then Dana (owner, decides on cost and data).
- Railway status: `https://status.railway.com` · Anthropic status: `https://status.anthropic.com`.

## A dataset someone added is wrong, stuck or in the way

- **Wrong data**: delete its tab (the ✕) and add the file again. Datasets added in the UI are only in Postgres, so keep the original files elsewhere; a restore of the database brings them back.
- **No free slot** (3 of 3 added): delete one tab. Built-in datasets (the `data/` folders) do not count and cannot be deleted in the UI.
- **A `data/` file changed but the tab did not**: the tab reloads on the next deploy or restart, when the file's content differs from what was loaded. Check the seed report line.
- **The database is filling up** (Railway's volume alert at 80%): `SELECT name, row_count FROM dataset_tables ORDER BY row_count DESC` shows which tables are big. Delete an added dataset you no longer need, or enlarge the Postgres volume.
- **A link is refused**: only public http(s) links to a CSV or Excel file are accepted; a link that returns a web page (a sign-in, a sheet not shared as "anyone with the link") is refused. Upload the file instead.
