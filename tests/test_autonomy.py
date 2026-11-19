"""test_autonomy.py — 상황별 자율(정반합 동의 관문)이 비서진의 승격 규칙 위에서 안전하게 동작하는가."""
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from biseo import actions as act, autonomy as au
from fakedb import FakeDB

WEEKDAY_AM = datetime(2026, 11, 16, 0, 30, tzinfo=timezone.utc)    # 월 09:30 KST
WEEKEND_PM = datetime(2026, 11, 21, 10, 30, tzinfo=timezone.utc)   # 토 19:30 KST


def at(monkeypatch, when):
    monkeypatch.setattr(au, "_clock", lambda: when)


def propose(sb, monkeypatch, when, kind="note", tainted=False):
    at(monkeypatch, when)
    row = act.propose(sb, kind, {"text": "x"}, origin="user", tainted=tainted)
    sb.table("actions").update({"created_at": when.isoformat()}).eq("id", row["id"]).execute()
    return sb.table("actions").select("*").eq("id", row["id"]).execute().data[0]


def promoted(sb, kind="note"):
    return act._trust(sb, kind).get("promoted")


def test_context_names():
    k, label = au.context("note", "user", WEEKEND_PM)
    assert k == "note|user|주말|저녁" and "주말 저녁 메모 저장" in label


def test_weekend_exception_is_absorbed_and_weekdays_stay_automatic(monkeypatch):
    sb = FakeDB()
    for _ in range(5):                                        # 주중 아침: 5번 승인 → 종류 승격
        act.approve(sb, propose(sb, monkeypatch, WEEKDAY_AM)["id"])
    assert promoted(sb)
    for _ in range(4):                                        # 주말 저녁: 계속 넘김
        r = propose(sb, monkeypatch, WEEKEND_PM)
        if r["status"] == "proposed":
            act.reject(sb, r["id"])
        else:
            act.undo(sb, r["id"])
    assert au._verdict(au._row(sb, "note|user|주말|저녁")) == "ask"
    for _ in range(3):                                        # 주중 아침은 계속 받아들임
        r = propose(sb, monkeypatch, WEEKDAY_AM)
        if r["status"] == "proposed":
            act.approve(sb, r["id"])
    wd = propose(sb, monkeypatch, WEEKDAY_AM)
    assert wd["status"] == "done"                             # 주중 아침은 바로 (규칙이든 상황 기록이든)
    wk = propose(sb, monkeypatch, WEEKEND_PM)                 # 주말 저녁만 묻는다
    assert wk["status"] == "proposed" and "주말 저녁" in (wk.get("auto_basis") or "")
    act.reject(sb, wk["id"])
    assert propose(sb, monkeypatch, WEEKDAY_AM)["status"] == "done"   # 주말 넘김이 주중을 흔들지 않는다
    hist = au.overview(sb)
    assert any(s["mode"] == "ask" and "주말 저녁" in s["label"] for s in hist["special"])
    assert any(h.get("to") == "ask" for h in hist["history"])


def test_context_can_allow_where_kind_rule_would_ask(monkeypatch):
    sb = FakeDB()
    for _ in range(4):                                        # 다른 상황에서는 넘김 → 종류 규칙은 '받지 않음' 쪽
        act.reject(sb, propose(sb, monkeypatch, WEEKEND_PM)["id"])
    for _ in range(6):                                        # 주중 아침에는 늘 받아들임
        r = propose(sb, monkeypatch, WEEKDAY_AM)
        if r["status"] == "proposed":
            act.approve(sb, r["id"])
    r = propose(sb, monkeypatch, WEEKDAY_AM)
    assert r["status"] == "done" and r["auto_executed"]
    assert "주중 아침" in (r.get("auto_basis") or "") or promoted(sb)


def test_safety_rules_do_not_change(monkeypatch):
    sb = FakeDB()
    for _ in range(8):
        r = propose(sb, monkeypatch, WEEKDAY_AM)
        if r["status"] == "proposed":
            act.approve(sb, r["id"])
    assert propose(sb, monkeypatch, WEEKDAY_AM)["status"] == "done"
    assert propose(sb, monkeypatch, WEEKDAY_AM, tainted=True)["status"] == "proposed"   # 오염은 언제나 묻기
    at(monkeypatch, WEEKDAY_AM)
    assert act.propose(sb, "send_message", {"to": "a", "text": "b"})["status"] == "blocked"


def test_failure_falls_back_to_kind_rule(monkeypatch):
    sb = FakeDB()
    def boom(*a, **k):
        raise RuntimeError("no table")
    monkeypatch.setattr(au, "_row", boom)
    r = propose(sb, monkeypatch, WEEKDAY_AM)
    assert r["status"] == "proposed"                          # 승격 전이니 묻기 (원래 규칙)
    act.approve(sb, r["id"])                                  # 학습이 실패해도 승인은 된다


def test_user_can_reset_a_context(monkeypatch):
    sb = FakeDB()
    for _ in range(4):
        act.reject(sb, propose(sb, monkeypatch, WEEKEND_PM)["id"])
    for _ in range(3):
        r = propose(sb, monkeypatch, WEEKDAY_AM)
        if r["status"] == "proposed":
            act.approve(sb, r["id"])
    au.reset(sb, "note|user|주말|저녁")
    assert not sb.table("action_context").select("*").eq("ctx", "note|user|주말|저녁").execute().data


def test_explained_reject_keeps_kind_promotion(monkeypatch):
    sb = FakeDB()
    sb.table("action_trust").upsert({"kind": "note", "clean": 5, "promoted": True}).execute()
    sb.table("action_context").upsert({"ctx": "note|user|주말|저녁", "kind": "note", "acc": 0.2, "rej": 3.0,
                                       "n": 5, "pi": 0.4, "mode": "ask"}).execute()
    r = propose(sb, monkeypatch, WEEKEND_PM)
    assert r["status"] == "proposed"                          # 종류는 승격됐지만 이 상황은 묻는다
    act.reject(sb, r["id"])
    assert promoted(sb)                                       # 보호대가 변칙을 흡수: 핵(종류 승격)은 그대로
    assert propose(sb, monkeypatch, WEEKDAY_AM)["status"] == "done"
