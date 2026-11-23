# Questlog

A personal to-do list with four kinds of quests:

- **One-time quests**, with an optional deadline picked from a calendar. Overdue ones are shown in red, and quests with a deadline are listed first. Checking one off moves it to the history.
- **Daily quests** are checked off for today and come back unchecked at local midnight.
- **Weekly quests** are checked off for the week and come back unchecked on Monday at midnight.
- **Monthly quests** are checked off for the month and come back unchecked on the 1st.

Daily, weekly and monthly quests are in the left column, one-time quests in the right, on phones too.

**Earlier days, weeks and months:** each daily, weekly and monthly section has a switcher (`‹ Today ›`, `‹ This week ›`, `‹ This month ›`). Use **‹** to go back as far as you like and see which quests you checked off then. You can check off or uncheck quests for that period too, e.g. if you forgot to tick one yesterday. Click the highlighted period name to jump back to the current one. A quest checked off later for an earlier period shows in the history on that period's first day, with **—** instead of a time.

The **History** tab lists everything you've checked off, newest first and grouped by day, with no limit on how far back it goes. Older months load as you scroll. You can filter by text or by kind. **↶** removes an entry: for a one-time quest this reopens it, and for a daily, weekly or monthly quest it unchecks it for that period. Right after checking off a one-time quest, a toast also offers **Undo**.

Click a quest (or its ✎) to edit its title, notes, kind or deadline. 🗑 deletes it after a confirmation (its history stays). Drag the **⋮⋮** handle to reorder.

## Storage

Plain JSON files, either in a local folder or in an S3 bucket folder:

```
questlog.json                      active quests (rewritten on every change)
history/2026-11.json               everything checked off in November 2026, one file per month
backups/questlog-2026-11-21.json   questlog.json as it was before that day's first change
```

- **S3** (same setup as azkviz): set `S3FOLDER` to `bucket` or `bucket/folder`. Filebase is the default endpoint.
- **Local folder** otherwise: `data/`, or whatever `QUESTLOG_DATA_DIR` names.

Every change is written straight to the storage. You can also edit the files by hand, or upload them to the bucket. The app picks up a changed `questlog.json` on the next request: at once for a local folder, within 10 s for S3. If the file isn't valid JSON, the app shows an error and refuses to save until you fix it, so your hand edit isn't overwritten.

## Access

This is a one-person app. Set `QUESTLOG_PASSWORD`, then log in once per browser (the cookie lasts 400 days). Changing the password logs every browser out. Without a password, only the computer running the server can use the app.

## Run locally

```bash
python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/uvicorn app.main:app --reload
.venv/bin/pytest
```

## Deploy

Same as azkviz. Use the Render blueprint (`render.yaml`) or the `Dockerfile`. Run **one process with one worker**. On hosts with a temporary disk (Render free tier), use S3, or you lose your quests on every redeploy.

| Environment variable | Default | Meaning |
|---|---|---|
| `QUESTLOG_PASSWORD` | *(unset: localhost only)* | login password |
| `QUESTLOG_TZ` | `Europe/Prague` | time zone for midnight and Monday resets |
| `S3FOLDER` | *(unset: local folder)* | `bucket` or `bucket/folder` |
| `S3_ACCESS_KEY_ID` / `S3_SECRET_ACCESS_KEY` | | S3 credentials (`AWS_*` also work) |
| `S3_ENDPOINT` | `https://s3.filebase.io` | S3 endpoint |
| `S3_REGION` | `auto` | signing region |
| `QUESTLOG_DATA_DIR` | `data/` | local folder (when S3 isn't used) |
| `PORT` | 8000 (Docker) | port to listen on |

## Layout

```
app/main.py         HTTP routes + password login
app/questlog.py     quests, daily/weekly/monthly periods, history months, sync with storage
app/storage.py      local folder or S3 backend
app/static/         index.html, css/style.css, js/app.js, js/datefield.js (calendar picker)
tests/test_questlog.py
```
