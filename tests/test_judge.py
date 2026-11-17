"""test_judge.py — 몰랑이 판단 노드·판단 회상을 비서가 쓰는가."""
import os
import random
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pytest

from biseo import actions as act
from biseo import brain, daily, judge, observe
from fakedb import FakeDB


@pytest.fixture
def sb(monkeypatch):
    monkeypatch.setattr(observe, "note", lambda *a, **k: None)
    return FakeDB()


def test_walk_follows_molang_tree_shape(sb):
    w = judge.walk(sb, "recommendation", rng=random.Random(1))
    assert w["path"][0] == "recommendation_0" and w["path"][-1] == "recommendation_2"
    for p in w["at_probs"]:
        assert abs(sum(p["options"].values()) - 1) < 0.01 and p["chose"] in p["options"]
    assert "판단의 방식" in judge.directive_text(w)
    grown = [n for n in judge.SEED_TREES["recommendation"]["nodes"] if n.startswith("grown_")]
    assert {judge.SEED_TREES["recommendation"]["nodes"][g]["prompt"] for g in grown} == \
           {"아는 것과 모르는 것 가르기", "근거끼리 맞춰 보기", "아직 모르는 쪽을 물어볼 거리로"}


def test_feedback_moves_weights_toward_accepted_path(sb):
    w = judge.walk(sb, "response", rng=random.Random(3))
    judge.record(sb, w, summary="메모 저장", context="회의 메모", evidence=[], recalled=[],
                 ref_kind="action", ref_id=1)
    a, b = w["path"][1], w["path"][2]
    before = judge.load_trees(sb)["response"]["transitions"][a][b]
    judge.feedback(sb, "action", 1, 1.0)
    after = judge.load_trees(sb)["response"]["transitions"][a][b]
    assert after > before
    assert abs(sum(judge.load_trees(sb)["response"]["transitions"][a].values()) - 1) < 0.01


def test_recall_brings_similar_accepted_judgments_only(sb):
    w = judge.walk(sb, "recommendation")
    judge.record(sb, w, summary="협의회 안건 정리", context="동학년 협의회 안건", evidence=[], recalled=[],
                 ref_kind="daily", ref_id="a")
    judge.record(sb, w, summary="협의회 자료 출력", context="동학년 협의회 자료", evidence=[], recalled=[],
                 ref_kind="daily", ref_id="b")
    judge.record(sb, w, summary="점심 메뉴", context="점심 식당", evidence=[], recalled=[],
                 ref_kind="daily", ref_id="c")
    judge.feedback(sb, "daily", "b", -1.0)                       # 넘긴 판단은 떠올리지 않는다
    got = judge.recall(sb, "recommendation", "내일 동학년 협의회 안건")
    assert [r["summary"] for r in got] == ["협의회 안건 정리"]


def test_daily_items_carry_cited_evidence_and_explain(sb, monkeypatch):
    sb.table("events").insert({"title": "6학년 동학년 협의회", "starts_at": datetime.now(daily.KST).replace(hour=23, minute=0).isoformat()}).execute()
    plan_out = {"greeting": "협의회가 있어요.", "items": [
        {"title": "안건 정리해 두기", "why": "오늘 협의회", "evidence": ["E1", "Z9"], "kind": "task", "args": {}}]}
    seen = []
    plan = daily.make(sb, {"name": "하루"}, lambda m: seen.append(m) or plan_out)
    assert "[E1]" in seen[0][1]["content"] and "판단의 방식" in seen[0][0]["content"]
    item = plan["items"][0]
    why = judge.explain(sb, "daily", item["id"])
    assert [e["tag"] for e in why["evidence"]] == ["E1"], "없는 번호(Z9)는 근거로 남지 않는다"
    assert why["evidence"][0]["kind"] == "event" and why["steps"][0]["prompt"] == "상황 파악"
    daily.decide(sb, item["id"], False)
    assert judge.explain(sb, "daily", item["id"])["feedback"] == -1.0


def test_chat_action_records_because_and_learns_on_approve(sb, monkeypatch):
    sb.table("facts").insert({"text": "사용자는 매주 화요일 협의회가 있다", "norm_key": "x", "trust": "human",
                              "approved_by_human": True, "strength": 1.0}).execute()
    sb.tables["facts_active"] = sb.rows("facts")
    monkeypatch.setattr(brain, "_ask", lambda msgs: {
        "reply": "메모해 둘게요", "user_facts": [], "task_answers": [], "suggest": [],
        "actions": [{"kind": "note", "args": {"text": "협의회 안건"}, "reason": "안건을 잊지 않게", "because": ["U", "F1"]}]})
    out = brain.respond(sb, {"name": "하루"}, "협의회 안건 메모해 줘")
    aid = out["actions"][0]["id"]
    why = judge.explain(sb, "action", aid)
    assert [e["kind"] for e in why["evidence"]] == ["talk", "fact"]
    assert "화요일" in why["evidence"][1]["text"]
    act.approve(sb, aid)
    assert judge.explain(sb, "action", aid)["feedback"] == 1.0


def test_explain_missing(sb):
    with pytest.raises(LookupError):
        judge.explain(sb, "task", 999)


def test_brief_gives_one_line_why_per_item():
    from fakedb import FakeDB
    from biseo import judge as J
    sb = FakeDB()
    w = J.walk(sb, "recommendation")
    w["path"] = list(w["path"]) + ["grown_x"]
    J.record(sb, w, summary="a", context="c", recalled=[{"id": 1}], ref_kind="daily", ref_id="ab12",
             evidence=[{"tag": "U", "kind": "talk", "text": "말"}, {"tag": "E1", "kind": "event", "text": "내일 10:00 협의회"}])
    out = J.brief(sb, "daily", ["ab12", "none"])
    assert out["ab12"] == {"because": [{"kind": "event", "text": "내일 10:00 협의회"}], "grown": True, "recalled": 1}
    assert "none" not in out and J.brief(sb, "daily", []) == {}
