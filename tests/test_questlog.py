import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app.questlog import HISTORY_PAGE, QuestError, QuestLog, period_of
from app.storage import LocalBackend, S3Backend

TZ = ZoneInfo("Europe/Prague")


class Clock:
    def __init__(self, *dt):
        self.t = datetime(*dt, tzinfo=TZ).timestamp()

    def set(self, *dt):
        self.t = datetime(*dt, tzinfo=TZ).timestamp()

    def __call__(self):
        return self.t


@pytest.fixture
def clock():
    return Clock(2026, 11, 21, 10, 0)     # a Saturday


@pytest.fixture
def ql(tmp_path, clock):
    return QuestLog(LocalBackend(tmp_path), clock=clock)


def quest(ql, title):
    return next(q for q in ql.view()["quests"] if q["title"] == title)


def test_periods():
    from datetime import date
    assert period_of("daily", date(2026, 11, 21)) == "2026-11-21"
    assert period_of("weekly", date(2026, 11, 22)) == "2026-W47"    # Sunday: same ISO week
    assert period_of("weekly", date(2026, 11, 23)) == "2026-W48"    # Monday: new week
    assert period_of("weekly", date(2027, 1, 1)) == "2026-W53"


def test_add_validates(ql):
    with pytest.raises(QuestError):
        ql.add({"title": "   ", "kind": "daily"})
    with pytest.raises(QuestError):
        ql.add({"title": "x", "kind": "monthly"})
    with pytest.raises(QuestError):
        ql.add({"title": "x", "kind": "once", "due": "tomorrow"})
    q = ql.add({"title": "  Water   plants ", "kind": "daily"})
    assert q["title"] == "Water plants"


def test_daily_resets_at_midnight(ql, clock, tmp_path):
    q = ql.add({"title": "Stretch", "kind": "daily"})
    ql.complete(q["id"])
    assert quest(ql, "Stretch")["done"]
    with pytest.raises(QuestError):
        ql.complete(q["id"])                     # once per day
    clock.set(2026, 11, 21, 23, 59)
    assert quest(ql, "Stretch")["done"]
    clock.set(2026, 11, 22, 0, 0)
    assert not quest(ql, "Stretch")["done"]
    ql.complete(q["id"])
    assert len(ql.history()["entries"]) == 2
    # Saved as plain JSON
    data = json.loads((tmp_path / "questlog.json").read_text())
    assert data["quests"][0]["done"]["period"] == "2026-11-22"


def test_weekly_resets_on_monday(ql, clock):
    q = ql.add({"title": "Groceries", "kind": "weekly"})
    ql.complete(q["id"])
    clock.set(2026, 11, 22, 20, 0)               # Sunday
    assert quest(ql, "Groceries")["done"]
    clock.set(2026, 11, 23, 0, 1)                # Monday
    assert not quest(ql, "Groceries")["done"]


def test_uncheck_recurring(ql):
    q = ql.add({"title": "Read", "kind": "daily"})
    ql.complete(q["id"])
    ql.uncheck(q["id"])
    assert not quest(ql, "Read")["done"]
    assert ql.history()["entries"] == []
    with pytest.raises(QuestError):
        ql.uncheck(q["id"])


def test_one_time_quest_leaves_and_undo_brings_it_back(ql, tmp_path):
    q = ql.add({"title": "Renew passport", "kind": "once", "due": "2026-12-01", "notes": "photo!"})
    entry = ql.complete(q["id"])
    assert ql.view()["quests"] == []
    assert "quest" not in entry
    [h] = ql.history()["entries"]
    assert h["title"] == "Renew passport" and h["kind"] == "once"
    assert (tmp_path / "history" / "2026-11.json").exists()
    ql.undo(entry["id"])
    back = quest(ql, "Renew passport")
    assert back["id"] == q["id"] and back["due"] == "2026-12-01" and back["notes"] == "photo!"
    assert ql.history()["entries"] == []


def test_undo_old_recurring_entry_keeps_current_state(ql, clock):
    q = ql.add({"title": "Stretch", "kind": "daily"})
    old = ql.complete(q["id"])
    clock.set(2026, 11, 22, 9, 0)
    ql.complete(q["id"])
    ql.undo(old["id"])
    assert quest(ql, "Stretch")["done"]          # today's check stays
    assert len(ql.history()["entries"]) == 1


def test_edit_reorder_delete(ql):
    a = ql.add({"title": "A", "kind": "daily"})
    b = ql.add({"title": "B", "kind": "daily"})
    ql.reorder([b["id"], a["id"]])
    assert [q["title"] for q in ql.view()["quests"]] == ["B", "A"]
    ql.update(a["id"], {"title": "A2", "kind": "once", "due": "2026-12-24"})
    a2 = quest(ql, "A2")
    assert a2["kind"] == "once" and a2["due"] == "2026-12-24" and "done" not in a2
    ql.update(a["id"], {"kind": "weekly"})
    assert quest(ql, "A2")["done"] is False and "due" not in quest(ql, "A2")
    ql.delete(b["id"])
    assert [q["title"] for q in ql.view()["quests"]] == ["A2"]


def test_history_pages_by_month(ql, clock):
    q = ql.add({"title": "Daily", "kind": "daily"})
    # 3 months with 30 entries each
    for month in (9, 10, 11):
        for day in range(1, 31):
            clock.set(2026, month, day, 8, 0)
            ql.complete(q["id"])
    page1 = ql.history()
    # whole months until >= HISTORY_PAGE entries: November + October
    assert len(page1["entries"]) == 60 >= HISTORY_PAGE
    assert page1["entries"][0]["at"].startswith("2026-11-30")
    assert page1["more"] == "2026-10"
    page2 = ql.history(before=page1["more"])
    assert len(page2["entries"]) == 30 and page2["more"] is None
    assert page2["entries"][-1]["at"].startswith("2026-09-01")


def test_external_edit_is_picked_up(ql, tmp_path):
    ql.add({"title": "A", "kind": "once"})
    data = json.loads((tmp_path / "questlog.json").read_text())
    data["quests"].append({"title": "Added by hand", "kind": "weekly"})
    (tmp_path / "questlog.json").write_text(json.dumps(data))
    assert {q["title"] for q in ql.view()["quests"]} == {"A", "Added by hand"}


def test_broken_file_blocks_saving(ql, tmp_path):
    ql.add({"title": "A", "kind": "once"})
    (tmp_path / "questlog.json").write_text("{ broken")
    v = ql.view()
    assert v["error"] and [q["title"] for q in v["quests"]] == ["A"]
    with pytest.raises(QuestError):
        ql.add({"title": "B", "kind": "once"})
    assert (tmp_path / "questlog.json").read_text() == "{ broken"     # not overwritten


def test_daily_backup(ql, tmp_path, clock):
    ql.add({"title": "A", "kind": "once"})       # first write: nothing to back up yet
    ql.add({"title": "B", "kind": "once"})
    backup = json.loads((tmp_path / "backups" / "questlog-2026-11-21.json").read_text())
    assert [q["title"] for q in backup["quests"]] == ["A"]


def test_failed_save_rolls_back(ql, monkeypatch):
    from app.storage import StorageError
    q = ql.add({"title": "Daily", "kind": "daily"})
    real = ql.backend.write

    def failing(name, text):
        if name == "questlog.json":
            raise StorageError("disk full")
        return real(name, text)
    monkeypatch.setattr(ql.backend, "write", failing)
    with pytest.raises(QuestError):
        ql.complete(q["id"])
    monkeypatch.setattr(ql.backend, "write", real)
    assert not quest(ql, "Daily")["done"]
    assert ql.history()["entries"] == []


# ---------- S3 (moto fakes S3 in memory) ----------

@pytest.fixture
def s3():
    moto = pytest.importorskip("moto")
    import boto3
    with moto.mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket="quests")
        yield client


def test_s3_backend(s3, clock):
    ql = QuestLog(S3Backend("quests", "me", client=s3), clock=clock, min_interval=0)
    q = ql.add({"title": "Gym", "kind": "weekly"})
    ql.complete(q["id"])
    keys = sorted(o["Key"] for o in s3.list_objects_v2(Bucket="quests")["Contents"])
    assert keys == ["me/backups/questlog-2026-11-21.json", "me/history/2026-11.json", "me/questlog.json"]
    # A second instance (e.g. after a restart) sees the same state
    ql2 = QuestLog(S3Backend("quests", "me", client=s3), clock=clock, min_interval=0)
    assert quest(ql2, "Gym")["done"]
    assert len(ql2.history()["entries"]) == 1
    # Hand edit uploaded to the bucket is picked up
    data = json.loads(s3.get_object(Bucket="quests", Key="me/questlog.json")["Body"].read())
    data["quests"][0]["title"] = "Gym 3x"
    s3.put_object(Bucket="quests", Key="me/questlog.json", Body=json.dumps(data).encode())
    assert ql.view()["quests"][0]["title"] == "Gym 3x"


def test_s3_env_config():
    from app.storage import s3_config_from_env
    assert s3_config_from_env({}) is None
    cfg = s3_config_from_env({"s3_folder": " 'bucket/sub/' ", "S3_ACCESS_KEY_ID": "k", "S3_SECRET_ACCESS_KEY": "s"})
    assert cfg["bucket"] == "bucket" and cfg["prefix"] == "sub"
    assert cfg["client_kwargs"]["endpoint_url"] == "https://s3.filebase.io"


# ---------- HTTP API ----------

@pytest.fixture
def client(tmp_path, monkeypatch, clock):
    monkeypatch.setenv("QUESTLOG_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("S3FOLDER", raising=False)
    from fastapi.testclient import TestClient
    import app.main as main
    monkeypatch.setattr(main, "QL", QuestLog(LocalBackend(tmp_path), clock=clock))
    monkeypatch.setattr(main, "PASSWORD", "sesame")
    return TestClient(main.app)


def test_api_requires_login(client):
    assert client.get("/api/state").status_code == 401
    assert client.get("/").status_code == 200                      # the page itself is public
    assert client.post("/api/login", json={"password": "nope"}).status_code == 401
    assert client.post("/api/login", json={"password": "sesame"}).status_code == 200
    s = client.get("/api/state").json()
    assert s["quests"] == [] and s["login"] is True


def test_api_flow(client):
    client.post("/api/login", json={"password": "sesame"})
    s = client.post("/api/quests", json={"title": "Run", "kind": "daily"}).json()
    qid = s["quests"][0]["id"]
    r = client.post(f"/api/quests/{qid}/done")
    assert r.status_code == 200 and r.json()["quests"][0]["done"] is True
    assert client.post(f"/api/quests/{qid}/done").status_code == 409
    assert client.post(f"/api/quests/{qid}/undone").json()["quests"][0]["done"] is False
    s = client.post("/api/quests", json={"title": "Taxes", "kind": "once", "due": "2027-03-31"}).json()
    tid = next(q["id"] for q in s["quests"] if q["title"] == "Taxes")
    entry = client.post(f"/api/quests/{tid}/done").json()["entry"]
    assert [e["title"] for e in client.get("/api/history").json()["entries"]] == ["Taxes"]
    s = client.post(f"/api/history/{entry['id']}/undo").json()
    assert any(q["title"] == "Taxes" for q in s["quests"])
    assert client.patch(f"/api/quests/{qid}", json={"title": ""}).status_code == 400
    assert client.delete("/api/quests/nope").status_code == 404
