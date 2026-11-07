# Watch — the six numbers, every Friday

One row per week. The trend is the only early warning a small product gets (Lesson 9, slide 30).

| Week ending | Errors (runs not ok) | Model spend ($) | p50 first token (s) | DB size (MB) | Eval pass rate | Notes |
|---|---|---|---|---|---|---|
| | | | | | | first week after deploy |

Where each number comes from:

- **Errors**: `SELECT count(*) FROM runs WHERE status <> 'ok' AND created_at > now() - interval '7 days'` (Railway → Postgres → Data), or the digest's "Runs not OK" line summed over the week.
- **Model spend**: platform.claude.com → Usage, or `SELECT sum(cost_usd) FROM runs` for the week.
- **First token**: Railway logs (`/api/chat` request durations) or a stopwatch on three real questions.
- **DB size**: Railway → Postgres → Metrics. Added datasets live here: if it climbs, `SELECT name, row_count FROM dataset_tables ORDER BY row_count DESC` shows which table is big (volume: 500 MB).
- **Eval pass rate**: last row of `evals/history.csv` after a weekly `python -m evals.run`.
