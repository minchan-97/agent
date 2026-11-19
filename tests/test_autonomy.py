"""test_autonomy.py — 상황별 자율(정반합 동의 관문)이 비서진의 승격 규칙 위에서 안전하게 동작하는가."""
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from biseo import actions as act, autonomy as au
from fakedb import FakeDB

WEEKDAY_AM = datetime(2026, 11, 16, 0, 30, tzinfo=timezone.utc)    # 월 09:30 KST
WEEKEND_PM = datetime(2026, 11, 21, 10, 30, tzinfo=timezone.utc)   # 토 19:30 KST
WEEKEND_NIGHT = datetime(2026, 11, 21, 13, 30, tzinfo=timezone.utc)  # 토 22:30 KST (저녁의 이웃)
WD = [WEEKDAY_AM + timedelta(days=i) for i in range(3)]              # 월·화·수 아침


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
    for i in range(5):                                        # 주중 아침: 5번 승인 → 종류 승격
        act.approve(sb, propose(sb, monkeypatch, WD[i % 3])["id"])
    assert promoted(sb)
    for _ in range(4):                                        # 주말 저녁: 계속 넘김
        r = propose(sb, monkeypatch, WEEKEND_PM)
        if r["status"] == "proposed":
            act.reject(sb, r["id"])
        else:
            act.undo(sb, r["id"])
    assert au._verdict(au._row(sb, "note|user|주말|저녁")) == "ask"
    for i in range(3):                                        # 주중 아침은 여러 날 계속 받아들임
        r = propose(sb, monkeypatch, WD[i])
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


def _accept_days(sb, monkeypatch, whens):
    for w in whens:
        r = propose(sb, monkeypatch, w)
        if r["status"] == "proposed":
            act.approve(sb, r["id"])


def test_burst_on_one_day_is_held_not_automatic(monkeypatch):
    """가짜 예외 막기: 하루에 몰린 받아들임으로는 자동이 되지 않고 '지켜보는 중'으로 보인다."""
    sb = FakeDB()
    for _ in range(3):                                        # 다른 상황에서 넘김 → 종류 규칙은 묻는 쪽
        act.reject(sb, propose(sb, monkeypatch, WEEKEND_PM)["id"])
    _accept_days(sb, monkeypatch, [WEEKDAY_AM] * 5)
    r = au._row(sb, "note|user|주중|아침")
    assert au._raw(r) == "auto" and au._held(r) and au._verdict(r) is None
    assert any("주중 아침" in h["label"] for h in au.overview(sb)["held"])
    assert au._verdict({**r, "days": au.DAYS_MIN}) == "auto"   # 막은 것은 오직 '날 수'
    au.learn(sb, {"kind": "note", "origin": "user", "created_at": (WEEKDAY_AM + timedelta(days=1)).isoformat()}, +1.0)
    assert au._row(sb, "note|user|주중|아침")["days"] == 2     # 다른 날 겪으면 날 수가 는다


def test_kernel_neighbours_and_ring():
    k = au.kernel
    assert k("note|user|주중|저녁", "note|user|주중|밤") > 0.4           # 시간대 한 칸
    assert abs(k("note|user|주중|밤", "note|user|주중|아침") - k("note|user|주중|저녁", "note|user|주중|밤")) < 1e-9  # 고리
    assert k("note|user|주중|저녁", "note|user|주말|저녁") < au.K_NEAR     # 주중↔주말은 약하게
    assert k("note|user|주중|저녁", "note|self|주중|저녁") == 0          # 출처가 다르면 빌리지 않음
    assert k("note|user|주중|저녁", "event|user|주중|저녁") == 0         # 종류가 다르면 빌리지 않음


def test_new_context_borrows_from_consented_neighbour(monkeypatch):
    """이웃 나눠 세기: 처음 겪는 주말 밤 메모는 주말 저녁 기록을 빌려 바로 한다."""
    sb = FakeDB()
    sat, sun = WEEKEND_PM, WEEKEND_PM + timedelta(days=1)
    for _ in range(4):                                        # 주중 아침에는 넘김 → 종류 기록은 '안 받음' 쪽
        au.learn(sb, {"kind": "note", "origin": "user", "created_at": WEEKDAY_AM.isoformat()}, -1.0)
    for w in [sat, sat, sun, sun, sat + timedelta(days=7)]:   # 주말 저녁만 늘 받아들임 (종류 승격과 무관하게)
        au.learn(sb, {"kind": "note", "origin": "user", "created_at": w.isoformat()}, +1.0)
    assert au._verdict(au._row(sb, "note|user|주말|저녁")) == "auto"
    assert not promoted(sb)
    r = propose(sb, monkeypatch, WEEKEND_NIGHT)
    assert r["status"] == "done" and "가까운 상황" in (r.get("auto_basis") or "")
    assert "주말 저녁" in r["auto_basis"]


def test_borrowing_never_overrides_safety(monkeypatch):
    sb = FakeDB()
    sat, sun = WEEKEND_PM, WEEKEND_PM + timedelta(days=1)
    _accept_days(sb, monkeypatch, [sat, sat, sun, sun, sat + timedelta(days=7)])
    r = propose(sb, monkeypatch, WEEKEND_NIGHT, tainted=True)
    assert r["status"] == "proposed"                          # 오염된 일은 이웃 기록이 좋아도 묻는다


def test_neighbour_that_asks_blocks_borrowed_auto(monkeypatch):
    own = {"ctx": "note|user|주말|밤", "acc": 0.0, "rej": 0.0, "n": 0, "pi": 0.0}
    good = {"ctx": "note|user|주말|저녁", "acc": 4.0, "rej": 0.0, "n": 5, "pi": 0.3, "days": 3, "label": "주말 저녁 메모"}
    bad = {"ctx": "note|user|주말|아침", "acc": 0.0, "rej": 4.0, "n": 5, "pi": 0.3, "days": 3, "label": "주말 아침 메모"}
    assert au.borrow(own, [good])[0] == "auto"
    assert au.borrow(own, [good, bad])[0] is None              # 가까운 이웃이 '묻기'면 빌린 자동은 내지 않는다
    assert au.borrow({**own, "n": 3}, [good])[0] is None       # 자기 기록이 충분하면 빌리지 않는다
