# spec.md — Data Copilot

A chat app for one team to ask questions about its data. It ships with one built-in dataset, the bakery chain's sales and supplier prices (the **Bakery** tab, where staff can also log supplier invoices from a photo), and the team can add up to three datasets of its own, each in its own tab. The same four layers as the chat products they use every day (UI, backend, model, database), small enough to understand and to run for a few dollars a month.

## 1. Users

- One team, ten people at most. For the built-in Bakery data that is Dana (owner) and three staff.
- From phones in the shops and the office laptop. Hebrew and English. The screen is light or dark, following the device, with a switch in the sidebar.
- Access: one shared team password plus a typed name. Not a public app. Not multi-tenant.

## 2. Jobs

1. **Answer a question about a dataset**: totals, counts, averages, rankings, breakdowns, trends, or a look at actual rows, over any CSV or Excel data. Every number comes from the data, never from memory. A tab answers only about its own dataset.
2. **Add a dataset** (up to 3, shared by the team): upload a CSV or Excel file, or give a link to one. It gets its own tab with its own chats. Anyone can delete an added dataset to free a slot.
3. **Built-in datasets**: every folder under `data/` is a tab; the files in it are the tab's tables (related files stay together). A loose file in `data/` is a tab of its own. They load at start, reload when a file changes, and cannot be deleted in the UI. Today there is one: `data/bakery/` (sales and supplier prices).
4. **Log an invoice** (Bakery tab only) from a photo or typed details: supplier, number, amount, date. One invoice becomes one row. An unreadable amount is never guessed.
5. **Keep every conversation** so a user can come back to it, in the tab it was started in.
6. **Send a morning digest**: yesterday's invoices (the Bakery tab's), what needs a look, and chat spend across all tabs.

## 3. Not in v1

Multi-tenant. Public sign-up. Payments or bank actions. Editing or deleting invoices from the chat. Mobile app. Voice. Exporting reports. Anything that writes to a dataset from the chat (the data is read-only; a dataset is changed by replacing its file). Combining two tables in one answer (joins). Private or signed-in links, and Excel files older than .xlsx.

## 4. Speed, per job

| Job | Answer | Mechanism |
|---|---|---|
| Chat reply | starts within 2 s, streams | server-sent events from the backend |
| Invoice from a photo | within one turn (seconds) | same turn; the model reads the image and calls the tool |
| Add a dataset | within ~10 s for a 25 MB file | the file is parsed, typed and loaded in one request; the tab appears when it is done |
| Morning digest | by 07:45 | scheduled job, no one waits |

## 5. Data

- **Kept**: users (name only), conversations (each linked to a tab), messages (text; images are not stored), invoices, one run record per model turn (tokens, cost, tools, status), and the datasets: each loaded file as a typed table plus its schema (the bakery's 29,115 sales lines and 10 supplier rows are built in; uploaded and linked files are kept as tables, the original file is not).
- **Retention**: conversations 90 days (manual purge in v1). Runs kept for the digest and the bill. Deleting an added dataset deletes its table and the chats that were about it.
- **Sensitive**: supplier names and amounts are business data; the team password and the API key are secrets and live only in Variables. The repository is public: only data that may be public goes under `data/`; private data is added through the UI, which stores it in the database and never in Git.

## 6. Limits

- Per user: 30 messages per hour. Images under 5 MB.
- Per app: $3.00 of model spend per local day; past that the chat says "back tomorrow".
- Invoices over ₪5,000 are logged and flagged for Dana; nothing is paid or acted on.
- History resent per turn: last 20 messages.
- Added datasets: at most 3 at a time; 25 MB and 200,000 rows per file; 100 columns per table. Links must be public http(s) addresses (nothing internal), and are re-checked on every redirect.

## 7. Done — eighteen checks a stranger can run

1. Open the URL, enter a name and the password, see an empty chat with the Bakery tab selected.
2. Wrong password is refused.
3. On the Bakery tab, "What sold best in the last week of August 2026?" streams an answer that names a product and the dates used, after a visible `data_query` call.
4. "Revenue by branch for August 2026" gives one number per branch.
5. Attach a clear invoice photo: one row appears under Invoices with status *logged*.
6. Attach the same photo again: still one row; the reply says it already exists.
7. Attach a photo with an unreadable total: a row with status *needs a look* and no amount.
8. An invoice over ₪5,000 shows the *flag* badge.
9. "Delete all invoices from March" is refused, with a pointer to Dana.
10. The 31st message within an hour is refused with a clear message.
11. `/health` returns 200 on the deployed URL; the morning digest arrives on the webhook.
12. `pytest` is green, and `python -m evals.run` passes at least 80% of the cases (CI's `--baseline 0.8`).
13. **Add a dataset**: upload a small CSV under "+ Add dataset". A new tab opens, its first question ("How many rows are in …?") is answered from the file, and the counter reads 1/3.
14. **The limit holds**: with 3 added datasets the add button is disabled; trying a 4th through the API is refused with a clear message. Deleting a tab frees a slot.
15. **Tabs are separate**: a question in the new tab about bakery products is answered from the new data only; the Bakery tab's chats are not listed in the new tab; the new tab has no invoice paperclip.
16. **A link works**: a public CSV link (or a Google Sheets link shared as "anyone with the link") becomes a tab. A link to `http://localhost:…`, a private address, or a web page is refused with a message.
17. **A folder is a tab**: drop `data/<name>/a.csv` and `b.csv`, restart: one tab `<name>` appears with both tables. Edit `a.csv` and restart: it reloads. Remove the folder: the tab is gone.
18. **Light and dark**: the sidebar switch changes the whole screen between light and dark, and the choice is still there after a reload.
