# practice.md — how work is done on this project

Lesson 4's loop, grown up. Every ticket, by you or by Claude Code, goes through these eight steps. `CLAUDE.md` points here; this file is the process.

## On every ticket

1. **Read the issue.** Restate it in one line. If it does not fit `spec.md`, stop and say so; do not widen the ticket.
2. **Plan.** Three to five lines naming the files you will touch and the test you will add. Wait for approval before writing code.
3. **Branch.** `git checkout -b <issue>-<slug>` from an up-to-date `main`, e.g. `12-stream-reply`.
4. **Build** the smallest change that meets the ticket's "Done when" lines. No drive-by refactors.
5. **Test.** Run `pytest -q`. Add the test the ticket names. If the system prompt, the tools, the model or the effort changed, also run `python -m evals.run` and paste the pass rate.
6. **Docs.** If a decision changed, update `architecture.md` in the same commit (drawing, limits or the decisions table). If a check changed, update `spec.md` §7.
7. **Commit.** One ticket, one commit, the number in the message: `Stream chat reply token by token (#12)`.
8. **Pull request.** Diff, test output, eval numbers, the issue link. A bug found on the way becomes a new issue, labelled `bug`, linked from the PR; it is not fixed silently in this branch.

## Never

- Edit `.env`, print a secret, or paste a key into chat, a file or a commit message. If a key reaches Git history: rotate it first, then clean history (runbook.md).
- Modify the files under `data/` or write to any dataset table. Datasets are read-only; the one exception is a change the owner asked for in words (moving the bakery files to `data/bakery/` on 2026-11-07), and it is recorded in architecture.md. A dataset the team adds in the UI is changed only by deleting it and adding the file again.
- Commit private data. The repository is public, so `data/` holds only data that may be public; private data is added through the UI upload, which stores it in the database.
- Push to `main`. Merge through a PR only, with CI green.
- Skip step 5. A red test is a stop, not a note.
- Change the model, effort or system prompt without an eval run before and after.
- Write a number into a reply without a tool result behind it (this applies to the agent; the eval `no_number_without_tool` checks it).

## Reviewing a PR (yours or Claude Code's)

- Does the diff do only what the ticket says?
- Is there a test for each "Done when" line, and does it fail without the change?
- Did `architecture.md` change if a decision did?
- Are the three numbers in the PR: tests passed, eval pass rate against the baseline, tokens per eval run?
- Does `data/` hold only data that may be public (the repository is public)?
- Does `grep -rn "sk-ant" .` come back empty (in PowerShell, `git grep -n "sk-ant"`, which checks tracked files), and is `.env` still untracked?

## Local commands

```bash
cp .env.example .env            # fill in APP_PASSWORD, APP_SECRET, ANTHROPIC_API_KEY
python -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt
uvicorn app.main:app --reload   # http://localhost:8000
pytest -q                       # model mocked, no network, seconds
python -m evals.run             # real model, costs cents, writes evals/history.csv
python -m app.digest            # print yesterday's digest
```

On Windows, in PowerShell 7, the first two lines become the three below; the rest is the same.

```powershell
Copy-Item .env.example .env
py -3.12 -m venv .venv; .venv\Scripts\Activate.ps1
pip install -r requirements.txt
```
