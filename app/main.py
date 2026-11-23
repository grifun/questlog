"""Questlog web server: a JSON API over QuestLog plus one static page.

The quest log is cached in memory and written through to storage, so run a single process.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .questlog import QuestError, QuestLog
from .storage import LocalBackend, s3_backend_from_env, s3_related_names

_log = logging.getLogger("questlog")
if not _log.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(levelname)s:     [%(name)s] %(message)s"))
    _log.addHandler(_handler)
    _log.setLevel(logging.INFO)

ROOT = Path(__file__).parent
STATIC = ROOT / "static"
DATA_DIR = Path(os.environ.get("QUESTLOG_DATA_DIR") or ROOT.parent / "data").resolve()
TIMEZONE = os.environ.get("QUESTLOG_TZ", "").strip() or "Europe/Prague"
PASSWORD = os.environ.get("QUESTLOG_PASSWORD", "")
LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost"}
COOKIE = "questlog_auth"


def _make_log() -> QuestLog:
    backend = s3_backend_from_env()
    if backend is None:
        names = s3_related_names()
        _log.info("Storage: local folder %s (S3 is off because S3FOLDER is not set; storage-related "
                  "variables this app can see: %s)", DATA_DIR, ", ".join(names) if names else "none")
        backend = LocalBackend(DATA_DIR)
    else:
        used = backend.env_names
        _log.info("Storage: S3 %s (endpoint %s; settings from %s)", backend.location,
                  backend.client.meta.endpoint_url, ", ".join(f"{k}={v}" for k, v in used.items()))
        if not ({"key", "secret"} <= used.keys() or os.environ.get("AWS_ACCESS_KEY_ID")):
            _log.error("S3 credentials missing: set S3_ACCESS_KEY_ID and S3_SECRET_ACCESS_KEY")
    ql = QuestLog(backend, tz=TIMEZONE)
    _log.info("Time zone %s; %d active quests loaded", TIMEZONE, len(ql.state["quests"]))
    return ql


QL = _make_log()
app = FastAPI(title="Questlog")
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.middleware("http")
async def no_stale_pages(request: Request, call_next):
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store" if request.url.path.startswith("/api/") else "no-cache"
    return response


@app.exception_handler(QuestError)
async def quest_error(_: Request, e: QuestError):
    return JSONResponse({"detail": e.message}, status_code=e.status)


# ---------- auth: one password (QUESTLOG_PASSWORD), remembered in a cookie ----------

def _token() -> str:
    # Derived from the password, so changing the password logs every browser out.
    return hmac.new(PASSWORD.encode(), b"questlog-session-v1", hashlib.sha256).hexdigest()


def _authorized(request: Request) -> bool:
    if PASSWORD:
        return hmac.compare_digest(request.cookies.get(COOKIE, ""), _token())
    return bool(request.client) and request.client.host in LOCAL_HOSTS


def _require(request: Request) -> None:
    if not _authorized(request):
        if PASSWORD:
            raise HTTPException(401, "Log in first.")
        raise HTTPException(403, "Without QUESTLOG_PASSWORD set, only the computer running the server can use the app.")


class Login(BaseModel):
    password: str


@app.post("/api/login")
async def login(body: Login, request: Request, response: Response):
    if not PASSWORD:
        raise HTTPException(400, "No password is set (QUESTLOG_PASSWORD).")
    if not hmac.compare_digest(body.password.encode(), PASSWORD.encode()):
        await asyncio.sleep(1)       # slows down guessing
        _log.warning("Wrong password from %s", request.client.host if request.client else "?")
        raise HTTPException(401, "Wrong password.")
    response.set_cookie(COOKIE, _token(), max_age=400 * 24 * 3600, httponly=True, samesite="lax",
                        secure=request.url.scheme == "https")
    return {"ok": True}


@app.post("/api/logout")
async def logout(response: Response):
    response.delete_cookie(COOKIE)
    return {"ok": True}


# ---------- pages ----------

@app.api_route("/", methods=["GET", "HEAD"])
async def index():
    return FileResponse(STATIC / "index.html")


@app.get("/healthz")
async def healthz():
    return {"ok": True}


# ---------- quests ----------

class QuestIn(BaseModel):
    title: str | None = None
    kind: str | None = None
    notes: str | None = None
    due: str | None = None


def _fields(body: QuestIn) -> dict:
    return body.model_dump(exclude_unset=True)


async def _view() -> dict:
    return {**await asyncio.to_thread(QL.view), "login": bool(PASSWORD)}


@app.get("/api/state")
async def state(request: Request):
    _require(request)
    return await _view()


@app.post("/api/quests")
async def add_quest(body: QuestIn, request: Request):
    _require(request)
    await asyncio.to_thread(QL.add, _fields(body))
    return await _view()


@app.patch("/api/quests/{qid}")
async def edit_quest(qid: str, body: QuestIn, request: Request):
    _require(request)
    await asyncio.to_thread(QL.update, qid, _fields(body))
    return await _view()


@app.delete("/api/quests/{qid}")
async def delete_quest(qid: str, request: Request):
    _require(request)
    await asyncio.to_thread(QL.delete, qid)
    return await _view()


@app.post("/api/quests/{qid}/done")
async def check_off(qid: str, request: Request, period: str | None = None):
    """Check a quest off now, or (daily/weekly/monthly quests) for an earlier `period`."""
    _require(request)
    entry = await asyncio.to_thread(QL.complete, qid, period)
    return {"entry": entry, **await _view()}


@app.post("/api/quests/{qid}/undone")
async def uncheck(qid: str, request: Request, period: str | None = None):
    _require(request)
    await asyncio.to_thread(QL.uncheck, qid, period)
    return await _view()


@app.get("/api/period")
async def period(kind: str, request: Request, period: str | None = None):
    """Which daily/weekly/monthly quests were checked off in a period (default: the current one)."""
    _require(request)
    return await asyncio.to_thread(QL.period_view, kind, period)


class Order(BaseModel):
    ids: list[str]


@app.put("/api/order")
async def reorder(body: Order, request: Request):
    _require(request)
    await asyncio.to_thread(QL.reorder, body.ids)
    return await _view()


# ---------- history ----------

@app.get("/api/history")
async def history(request: Request, before: str | None = None):
    _require(request)
    return await asyncio.to_thread(QL.history, before)


@app.post("/api/history/{entry_id}/undo")
async def undo_entry(entry_id: str, request: Request):
    _require(request)
    await asyncio.to_thread(QL.undo, entry_id)
    return await _view()
