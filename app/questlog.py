"""The quest log: active quests in one JSON file, completed quests in one history file per month.

Storage layout (relative to the local folder or the S3 bucket folder):

    questlog.json                      active quests (small, rewritten on every change)
    history/2026-11.json               everything completed in November 2026
    backups/questlog-2026-11-21.json   the main file as it was before the first change of each day

Quest kinds:
    once    done once, then it leaves the active list (its history entry keeps a copy, so undo restores it)
    daily   checked off for today; unchecked again from local midnight
    weekly  checked off for this week; unchecked again from Monday, local midnight (ISO weeks)
    monthly checked off for this month; unchecked again on the 1st

Periods roll over lazily: a recurring quest stores the period it was last checked off in, and it
shows as done only while that is the current period. Nothing has to run at midnight.

Earlier periods can be viewed and changed too: which quests were done in, say, last week is read
from the history (every completion entry records its period). Checking a quest off for a past
period adds an entry dated at the start of that period, marked "backfilled".
"""

from __future__ import annotations

import copy
import json
import logging
import re
import secrets
import threading
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from .storage import StorageError

log = logging.getLogger("questlog")

MAIN = "questlog.json"
HISTORY = "history/"
BACKUPS = "backups/"
KINDS = ("once", "daily", "weekly", "monthly")
RECURRING = ("daily", "weekly", "monthly")
MAX_TITLE, MAX_NOTES, MAX_QUESTS = 200, 5000, 2000
MONTH_RE = re.compile(r"^\d{4}-\d{2}$")
HISTORY_PAGE = 50   # a history request returns whole months until it has at least this many entries


class QuestError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


def _new_id() -> str:
    return secrets.token_hex(4)


def _dumps(data) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


def period_of(kind: str, d: date) -> str | None:
    """'2026-11-21' for daily, '2026-W47' for weekly, '2026-11' for monthly, None for one-time quests."""
    if kind == "daily":
        return d.isoformat()
    if kind == "weekly":
        y, w, _ = d.isocalendar()
        return f"{y}-W{w:02d}"
    if kind == "monthly":
        return f"{d.year}-{d.month:02d}"
    return None


def period_start(kind: str, period: str) -> date:
    if kind == "daily":
        return date.fromisoformat(period)
    if kind == "weekly":
        y, w = period.split("-W")
        return date.fromisocalendar(int(y), int(w), 1)
    y, m = period.split("-")
    return date(int(y), int(m), 1)


def period_end(kind: str, period: str) -> date:
    start = period_start(kind, period)
    if kind == "daily":
        return start
    if kind == "weekly":
        return start + timedelta(days=6)
    return date(start.year + start.month // 12, start.month % 12 + 1, 1) - timedelta(days=1)


def shift_period(kind: str, period: str, step: int) -> str:
    """The period just before (step < 0) or after (step > 0) the given one."""
    if step < 0:
        return period_of(kind, period_start(kind, period) - timedelta(days=1))
    return period_of(kind, period_end(kind, period) + timedelta(days=1))


def check_period(kind: str, period) -> str:
    try:
        ok = period_of(kind, period_start(kind, str(period))) == period
    except (ValueError, TypeError):
        ok = False
    if not ok:
        raise QuestError("That isn't a valid period.")
    return period


# ---------- validation ----------

def _clean_fields(data: dict, is_new: bool) -> dict:
    """Validated user-editable fields from data."""
    out = {}
    if is_new or "kind" in data:
        kind = data.get("kind", "once")
        if kind not in KINDS:
            raise QuestError("Unknown quest kind.")
        out["kind"] = kind
    if is_new or "title" in data:
        title = " ".join(str(data.get("title", "")).split())
        if not title:
            raise QuestError("Give the quest a title.")
        if len(title) > MAX_TITLE:
            raise QuestError(f"The title is too long (max {MAX_TITLE} characters).")
        out["title"] = title
    if "notes" in data:
        notes = str(data.get("notes") or "").strip()
        if len(notes) > MAX_NOTES:
            raise QuestError(f"The notes are too long (max {MAX_NOTES} characters).")
        out["notes"] = notes
    if "due" in data:
        due = data.get("due") or None
        if due is not None:
            try:
                due = date.fromisoformat(str(due)).isoformat()
            except ValueError:
                raise QuestError("The due date isn't a valid date.")
        out["due"] = due
    return out


def _normalize_quest(q) -> dict | None:
    """Fill in defaults for a quest loaded from the file (it may have been edited by hand)."""
    if not isinstance(q, dict) or q.get("kind") not in KINDS or not str(q.get("title", "")).strip():
        return None
    out = {
        "id": str(q.get("id") or _new_id()),
        "title": str(q["title"]).strip(),
        "kind": q["kind"],
        "notes": str(q.get("notes") or ""),
        "created": str(q.get("created") or ""),
        "order": q.get("order") if isinstance(q.get("order"), (int, float)) else 0,
    }
    if q["kind"] == "once":
        out["due"] = q.get("due") or None
    else:
        done = q.get("done") if isinstance(q.get("done"), dict) else {}
        out["done"] = {"period": done.get("period"), "entry": done.get("entry")} if done.get("period") else None
    return out


def _normalize(data) -> dict:
    if not isinstance(data, dict):
        raise ValueError("the top level must be a JSON object")
    quests, seen = [], set()
    for raw in data.get("quests") or []:
        q = _normalize_quest(raw)
        if q and q["id"] not in seen:
            seen.add(q["id"])
            quests.append(q)
    return {"version": 1, "quests": quests}


# ---------- the log ----------

class QuestLog:
    def __init__(self, backend, tz: str = "Europe/Prague", clock=time.time, min_interval: float | None = None):
        self.backend = backend
        self.tz = ZoneInfo(tz)
        self.clock = clock
        self.min_interval = backend.min_interval if min_interval is None else min_interval
        self.lock = threading.RLock()
        self.state = _normalize({})
        self.version: str | None = None      # version of questlog.json we last read or wrote
        self.error: str | None = None        # why the main file can't be used (then saving is refused)
        self.checked = 0.0
        self.months: dict[str, tuple[str, list]] = {}   # month -> (version, entries), a read cache
        self.backup_day: str | None = None
        self._pending: dict[str, tuple[str | None, list]] = {}
        with self.lock:
            self._load()

    def now(self) -> datetime:
        return datetime.fromtimestamp(self.clock(), self.tz)

    # ----- loading and syncing -----
    def _load(self) -> None:
        self.checked = time.monotonic()
        try:
            got = self.backend.read(MAIN)
        except StorageError as e:
            self.error = f"Can't read {self.backend.where(MAIN)}: {e}"
            log.error(self.error)
            return
        if got is None:
            self.state, self.version, self.error = _normalize({}), None, None
            return
        text, version = got
        try:
            self.state = _normalize(json.loads(text))
        except ValueError as e:
            # Probably a hand edit gone wrong. Keep what we had and refuse to overwrite the file,
            # so the edit can be fixed instead of being lost.
            self.error = f"{self.backend.where(MAIN)} is not valid: {e}. Fix the file; saving is off until then."
            self.version = version
            log.error(self.error)
            return
        self.version, self.error = version, None

    def refresh(self, force: bool = False) -> None:
        """Reload questlog.json if it changed outside this app (checked at most every min_interval s)."""
        with self.lock:
            if not force and time.monotonic() - self.checked < self.min_interval:
                return
            self.checked = time.monotonic()
            try:
                version = self.backend.version(MAIN)
            except StorageError as e:
                self.error = f"Can't reach the storage: {e}"
                log.error(self.error)
                return
            if version != self.version or self.error:
                if version != self.version:
                    log.info("%s changed outside the app; reloading", self.backend.where(MAIN))
                self._load()

    # ----- the public view -----
    def _find(self, qid: str) -> dict:
        for q in self.state["quests"]:
            if q["id"] == qid:
                return q
        raise QuestError("That quest doesn't exist (anymore).", 404)

    def _view_quest(self, q: dict, today: date) -> dict:
        v = copy.deepcopy(q)
        if q["kind"] != "once":
            done = q["done"]
            v["done"] = bool(done and done["period"] == period_of(q["kind"], today))
        return v

    def view(self) -> dict:
        with self.lock:
            self.refresh()
            now = self.now()
            today = now.date()
            return {
                "now": now.isoformat(timespec="seconds"),
                "today": today.isoformat(),
                "quests": [self._view_quest(q, today) for q in sorted(self.state["quests"], key=lambda q: q["order"])],
                # the current period of each recurring kind, and the one before it (for "‹" navigation)
                "periods": {k: {"period": period_of(k, today), "prev": shift_period(k, period_of(k, today), -1)}
                            for k in RECURRING},
                "storage": self.backend.location,
                "error": self.error,
            }

    # ----- history files -----
    def _read_month(self, month: str, version: str | None = None) -> tuple[str | None, list]:
        """(raw text or None, entries) of a month file; uses the cache when version matches."""
        cached = self.months.get(month)
        if version is not None and cached and cached[0] == version:
            return None, cached[1]
        name = f"{HISTORY}{month}.json"
        got = self.backend.read(name)
        if got is None:
            self.months.pop(month, None)
            return None, []
        text, ver = got
        try:
            data = json.loads(text)
            raw = data.get("entries") if isinstance(data, dict) else data
            entries = [e for e in raw or [] if isinstance(e, dict) and e.get("id")]
        except (ValueError, AttributeError, TypeError) as e:
            raise QuestError(f"{self.backend.where(name)} is not valid ({e}). Fix the file first.", 502)
        self.months[month] = (ver, entries)
        return text, entries

    def _month_for_edit(self, month: str) -> list:
        """Entries of a month, read fresh from storage, to be written back when the change commits."""
        if month not in self._pending:
            text, entries = self._read_month(month)
            self._pending[month] = (text, copy.deepcopy(entries))
        return self._pending[month][1]

    def history(self, before: str | None = None) -> dict:
        """Completed quests, newest first: whole months older than `before` (YYYY-MM) until there
        are at least HISTORY_PAGE entries. `more` is the value to pass as `before` next time
        (None when everything has been returned)."""
        if before is not None and not MONTH_RE.match(before):
            raise QuestError("Bad month.")
        with self.lock:
            try:
                listing = self.backend.list(HISTORY)
                months = sorted(((name[len(HISTORY):-5], ver) for name, ver in listing.items()), reverse=True)
                months = [(m, v) for m, v in months if MONTH_RE.match(m) and (before is None or m < before)]
                out, more = [], None
                for i, (month, ver) in enumerate(months):
                    _, entries = self._read_month(month, ver)
                    out.extend(sorted(entries, key=lambda e: str(e.get("at", "")), reverse=True))
                    if len(out) >= HISTORY_PAGE and i + 1 < len(months):
                        more = month
                        break
            except StorageError as e:
                raise QuestError(f"Can't read the history: {e}", 502)
            return {"entries": [{k: v for k, v in e.items() if k != "quest"} for e in out], "more": more}

    def _entries_for(self, kind: str, period: str) -> dict[str, str]:
        """{quest id: history entry id} for completions of `kind` quests in `period`, read from
        the history month files the period touches (a week can span two months)."""
        months = sorted({period_start(kind, period).strftime("%Y-%m"), period_end(kind, period).strftime("%Y-%m")})
        listing = self.backend.list(HISTORY)
        out: dict[str, str] = {}
        for month in months:
            name = f"{HISTORY}{month}.json"
            if month in self._pending:          # being changed right now (inside _mutate)
                entries = self._pending[month][1]
            elif name in listing:
                entries = self._read_month(month, listing[name])[1]
            else:
                continue
            for e in entries:
                if e.get("kind") == kind and e.get("period") == period and e.get("quest_id"):
                    out.setdefault(e["quest_id"], e["id"])
        return out

    def period_view(self, kind: str, period: str | None = None) -> dict:
        """Which `kind` quests were checked off in `period` (default: the current one)."""
        if kind not in RECURRING:
            raise QuestError("Unknown quest kind.")
        with self.lock:
            self.refresh()
            current = period_of(kind, self.now().date())
            period = current if period is None else check_period(kind, period)
            if period > current:
                raise QuestError("That period hasn't started yet.")
            try:
                done = self._entries_for(kind, period)
            except StorageError as e:
                raise QuestError(f"Can't read the history: {e}", 502)
            if period == current:      # the main file is authoritative for the current period
                for q in self.state["quests"]:
                    if q["kind"] == kind and q.get("done") and q["done"]["period"] == period:
                        done[q["id"]] = q["done"]["entry"]
            return {"kind": kind, "period": period, "current": period == current,
                    "start": period_start(kind, period).isoformat(), "end": period_end(kind, period).isoformat(),
                    "prev": shift_period(kind, period, -1),
                    "next": shift_period(kind, period, 1) if period < current else None,
                    "done": done}

    # ----- changing things -----
    def _mutate(self, fn):
        """Run fn on the state and write the result (history months first, then the main file).
        If any write fails, the in-memory state and already written months are put back."""
        with self.lock:
            self.refresh(force=True)
            if self.error:
                raise QuestError(self.error, 503)
            before = copy.deepcopy(self.state)
            self._pending = {}
            written: list[str] = []
            try:
                result = fn()
                for month, (_, entries) in self._pending.items():
                    ver = self.backend.write(f"{HISTORY}{month}.json", _dumps({"month": month, "entries": entries}))
                    self.months[month] = (ver, entries)
                    written.append(month)
                self._backup()
                self.version = self.backend.write(MAIN, _dumps(self.state))
                return result
            except Exception as e:
                self.state = before
                for month in written:      # best effort: put back what the history said before
                    old = self._pending[month][0]
                    try:
                        self.backend.write(f"{HISTORY}{month}.json", old if old is not None
                                           else _dumps({"month": month, "entries": []}))
                    except StorageError:
                        log.exception("Could not roll back history %s", month)
                    self.months.pop(month, None)
                if isinstance(e, StorageError):
                    log.error("Save failed: %s", e)
                    raise QuestError(f"Saving failed: {e}", 502)
                raise
            finally:
                self._pending = {}

    def _backup(self) -> None:
        """Once a day, keep a copy of the main file as it was before the day's first change."""
        today = self.now().date().isoformat()
        if self.backup_day == today or self.version is None:
            return
        got = self.backend.read(MAIN)
        if got is not None:
            self.backend.write(f"{BACKUPS}questlog-{today}.json", got[0])
        self.backup_day = today

    def add(self, data: dict) -> dict:
        fields = _clean_fields(data, True)

        def do():
            if len(self.state["quests"]) >= MAX_QUESTS:
                raise QuestError("Too many active quests.")
            kind = fields["kind"]
            q = {"id": _new_id(), "title": fields["title"], "kind": kind, "notes": fields.get("notes", ""),
                 "created": self.now().isoformat(timespec="seconds"),
                 "order": max((x["order"] for x in self.state["quests"]), default=0) + 1}
            if kind == "once":
                q["due"] = fields.get("due")
            else:
                q["done"] = None
            self.state["quests"].append(q)
            return q
        return self._mutate(do)

    def update(self, qid: str, data: dict) -> dict:
        def do():
            q = self._find(qid)
            fields = _clean_fields(data, False)
            kind = fields.pop("kind", q["kind"])
            if kind != q["kind"]:
                q.pop("due", None)
                q.pop("done", None)
                q["kind"] = kind
            if kind == "once":
                q.setdefault("due", None)
            else:
                fields.pop("due", None)
                q.setdefault("done", None)
            q.update(fields)
            return q
        return self._mutate(do)

    def delete(self, qid: str) -> None:
        self._mutate(lambda: self.state["quests"].remove(self._find(qid)))

    def reorder(self, ids: list[str]) -> None:
        def do():
            pos = {qid: i for i, qid in enumerate(ids)}
            # Quests missing from the list keep their relative order, after the listed ones.
            ranked = sorted(self.state["quests"], key=lambda q: (pos.get(q["id"], len(ids)), q["order"]))
            for i, q in enumerate(ranked):
                q["order"] = i + 1
        self._mutate(do)

    def complete(self, qid: str, period: str | None = None) -> dict:
        """Check a quest off: now, or (recurring quests only) for an earlier period."""
        def do():
            q = self._find(qid)
            now = self.now()
            entry = {"quest_id": q["id"], "title": q["title"], "kind": q["kind"],
                     "at": now.isoformat(timespec="seconds")}
            if q["kind"] == "once":
                entry["quest"] = copy.deepcopy(q)       # so undo can bring it back
                self.state["quests"].remove(q)
            else:
                current = period_of(q["kind"], now.date())
                target = current if period is None else check_period(q["kind"], period)
                if target > current:
                    raise QuestError("That period hasn't started yet.")
                entry["period"] = target
                if target == current:
                    if q["done"] and q["done"]["period"] == current:
                        raise QuestError("Already done for this period.", 409)
                else:
                    if q["id"] in self._entries_for(q["kind"], target):
                        raise QuestError("Already done for that period.", 409)
                    # Dated at the start of the period it counts for, so the history shows it there.
                    start = datetime.combine(period_start(q["kind"], target), datetime.min.time(), self.tz)
                    entry.update(at=start.isoformat(timespec="seconds"), backfilled=True,
                                 recorded=now.isoformat(timespec="seconds"))
            month = entry["at"][:7]
            entry = {"id": f"{month}.{_new_id()}", **entry}
            if q["kind"] != "once" and entry["period"] == period_of(q["kind"], now.date()):
                q["done"] = {"period": entry["period"], "entry": entry["id"]}
            self._month_for_edit(month).append(entry)
            return {k: v for k, v in entry.items() if k != "quest"}
        return self._mutate(do)

    def uncheck(self, qid: str, period: str | None = None) -> dict:
        """Untick a recurring quest for the current period (or an earlier one): removes its history entry."""
        with self.lock:
            self.refresh(force=True)
            q = self._find(qid)
            if q["kind"] == "once":
                raise QuestError("One-time quests are reopened from the history.", 409)
            current = period_of(q["kind"], self.now().date())
            target = current if period is None else check_period(q["kind"], period)
            done = q.get("done")
            if target == current and done and done["period"] == current:
                entry_id = done["entry"]
            else:
                try:
                    entry_id = self._entries_for(q["kind"], target).get(q["id"])
                except StorageError as e:
                    raise QuestError(f"Can't read the history: {e}", 502)
            if not entry_id:
                raise QuestError("This quest isn't checked off for that period.", 409)
            return self.undo(entry_id)

    def undo(self, entry_id: str) -> dict:
        """Remove a history entry and take back what it did: untick the recurring quest if it's
        still its period, or bring a one-time quest back to the active list."""
        month = entry_id.split(".", 1)[0]
        if not MONTH_RE.match(month):
            raise QuestError("Unknown history entry.", 404)

        def do():
            entries = self._month_for_edit(month)
            entry = next((e for e in entries if e.get("id") == entry_id), None)
            if entry is None:
                raise QuestError("That history entry doesn't exist (anymore).", 404)
            entries.remove(entry)
            q = next((x for x in self.state["quests"] if x["id"] == entry.get("quest_id")), None)
            if entry.get("kind") == "once":
                if q is None and isinstance(entry.get("quest"), dict):
                    restored = _normalize_quest(entry["quest"])
                    if restored:
                        self.state["quests"].append(restored)
            elif q is not None and q.get("done") and q["done"].get("entry") == entry_id:
                q["done"] = None
            return {k: v for k, v in entry.items() if k != "quest"}
        return self._mutate(do)
