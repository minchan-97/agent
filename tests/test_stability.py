"""
test_stability.py — 겹침 방지, 오류 기록, 갇힌 알림 복구.
"""
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from biseo import apps
from biseo import observe
from biseo import tasks as tk
from biseo import tick
from fakedb import FakeDB

AGENT = {"name": "비서", "heir": False}


def _sb(monkeypatch):
    monkeypatch.setattr(observe, "note", lambda *a, **k: None)
    return FakeDB()


def test_lease_is_exclusive(monkeypatch):
    sb = _sb(monkeypatch)
    t = tk.create(sb, "겹침 시험")
    assert tk.lease(sb, t["id"]) is True
    assert tk.lease(sb, t["id"]) is False, "이미 잡힌 과제는 두 번째가 못 잡아야 한다"
    tk.release(sb, t["id"])
    assert tk.lease(sb, t["id"]) is True


def test_leased_task_is_skipped_by_cron(monkeypatch):
    sb = _sb(monkeypatch)
    t = tk.create(sb, "겹침 시험")
    tk.lease(sb, t["id"])
    called = []
    res = tick.run_task_steps(sb, AGENT, t["id"], lambda m: called.append(1) or {})
    assert res["status"] == "busy" and not called
    assert tk.pick_next(sb) is None, "잡힌 과제는 다음 후보로 고르지 않는다"


def test_step_error_is_recorded_and_released(monkeypatch):
    sb = _sb(monkeypatch)
    t = tk.create(sb, "오류 시험")

    def boom(msgs):
        raise TimeoutError("LLM 시간 초과")

    res = tick.run_task_steps(sb, AGENT, t["id"], boom)
    got = tk.get(sb, t["id"])
    assert res["status"] == "error" and got["status"] == "open"
    assert got["log"][-1]["outcome"].startswith(tk.ERROR_MARK)
    assert tk.lease(sb, t["id"]), "오류가 나도 잡은 것은 풀려야 한다"


def test_three_errors_in_a_row_stop_the_task(monkeypatch):
    sb = _sb(monkeypatch)
    t = tk.create(sb, "오류 시험")

    def boom(msgs):
        raise ConnectionError("down")

    notes = [tick.run_task_steps(sb, AGENT, t["id"], boom)["notify"] for _ in range(3)]
    assert tk.get(sb, t["id"])["status"] == "stopped"
    assert notes[0] is None and notes[1] is None and "멈췄어요" in notes[2]


def test_stuck_sending_reminder_is_retried(monkeypatch):
    sb = _sb(monkeypatch)
    now = datetime.now(timezone.utc)
    old = (now - timedelta(minutes=30)).isoformat()
    sb.table("reminders").insert({"text": "갇힌 알림", "due_at": old, "status": "sending",
                                  "updated_at": old}).execute()
    sb.table("reminders").insert({"text": "막 가져간 알림", "due_at": old, "status": "sending",
                                  "updated_at": now.isoformat()}).execute()
    got = apps.claim_due(sb, now.isoformat())
    assert [r["text"] for r in got] == ["갇힌 알림"], "10분 넘게 갇힌 것만 다시 보낸다"


def test_claim_due_only_once(monkeypatch):
    sb = _sb(monkeypatch)
    now = datetime.now(timezone.utc).isoformat()
    sb.table("reminders").insert({"text": "a", "due_at": now, "status": "scheduled",
                                  "updated_at": now}).execute()
    first = apps.claim_due(sb, now)
    second = apps.claim_due(sb, now)
    assert len(first) == 1 and second == []


# ---------------- 체감 속도: 이어 달리기 ----------------
def _always(ans):
    return lambda msgs: dict(ans)


def test_run_chains_steps_until_stop_point(monkeypatch):
    """한 번 부르면 한 걸음이 아니라, 끝날 때까지(또는 한도까지) 이어 달린다."""
    sb = _sb(monkeypatch)
    t = tk.create(sb, "메모 세 개 남기기")
    answers = iter([
        {"thought": "1", "action": {"kind": "draft", "args": {"to": "나", "text": "a"}}, "status": "continue"},
        {"thought": "2", "action": {"kind": "draft", "args": {"to": "나", "text": "b"}}, "status": "continue"},
        {"thought": "3", "action": None, "status": "done", "result": "끝"},
        {"verdict": "done", "quality": 4, "gap": "", "result": "끝"},     # 끝내기 전 자기 점검
    ])
    res = tick.run_task_steps(sb, AGENT, t["id"], lambda m: next(answers), max_steps=8, budget=30)
    assert res["steps"] == 3 and tk.get(sb, t["id"])["status"] == "done"


def test_cron_tick_moves_several_tasks(monkeypatch):
    """한 과제가 멈출 지점에 닿으면, 같은 틱의 남은 시간으로 다음 과제도 진행한다."""
    sb = _sb(monkeypatch)
    a = tk.create(sb, "첫 번째 일")
    b = tk.create(sb, "두 번째 일")
    ask = _always({"thought": "필요한 정보", "action": None, "status": "ask_user",
                   "question": "언제까지인가요?"})
    tick.step_user(sb, {**AGENT, "user_id": "u"}, ask)
    assert tk.get(sb, a["id"])["status"] == "waiting_user"
    assert tk.get(sb, b["id"])["status"] == "waiting_user"
