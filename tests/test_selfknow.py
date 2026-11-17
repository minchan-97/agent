"""test_selfknow.py — 비서가 자기 기능과 실제 쓰임을 근거로 답하는가."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from biseo import actions as act
from biseo import brain, observe, selfknow
from fakedb import FakeDB

AGENT = {"name": "비서", "heir": False}


def seed():
    sb = FakeDB()
    for _ in range(3):
        act.propose(sb, "draft", {"to": "학부모", "text": "안내"})
    sb.table("actions").insert({"kind": "send_message", "tier": 4, "origin": "user", "status": "blocked"}).execute()
    sb.table("episodes").insert([{"role": "user", "text": "이 메일함 정리해 줘"},
                                 {"role": "agent", "text": "메일함은 아직 연결할 수 없어요."},
                                 {"role": "user", "text": "고마워"}, {"role": "agent", "text": "네"}]).execute()
    return sb


def test_usage_counts_real_use_and_unmet_asks():
    u = selfknow.usage(seed())
    assert u["by_cap"]["draft"] == 3
    assert "이 메일함 정리해 줘" in u["unmet"] and any("메시지" in t for t in u["unmet"])
    lines = dict(selfknow.evidence_lines(u))
    assert "초안 3번" in lines["K1"] and any("한 번도 안 쓴" in t for t in lines.values())


def test_prompt_is_short_unless_asked_about_itself():
    sb = seed()
    short, ev = selfknow.for_prompt(sb, "내일 3시 회의 넣어 줘")
    assert ev == {} and len(short) < 300 and "일정" in short
    long, ev = selfknow.for_prompt(sb, "무슨 기능을 더 추가하면 좋을까?")
    assert "[지금은 못 하는 일" in long and "[K1]" in long and ev["K1"]["kind"] == "usage"
    assert "보안·서버·성능" in long                      # 만드는 사람의 일은 제안하지 않게


def test_chat_suggestion_cites_usage_in_why(monkeypatch):
    sb = seed()
    monkeypatch.setattr(observe, "note", lambda *a, **k: None)
    seen = []

    def ask(msgs):
        seen.append(msgs[0]["content"])
        return {"reply": "메일 연결이 가장 도움이 될 것 같아요.", "actions": [
            {"kind": "note", "args": {"text": "새 기능 후보: 메일 연결"}, "reason": "메일 정리 부탁을 못 해 드림", "because": ["K3", "K9"]}],
            "user_facts": [], "task_answers": [], "suggest": []}
    monkeypatch.setattr(brain, "_ask", ask)
    out = brain.respond(sb, AGENT, "앞으로 어떤 기능이 있으면 좋을까?")
    assert "[나에 대해 — 내가 실제로 할 수 있는 일]" in seen[0]
    j = sb.rows("judgments")[-1]
    assert any(e["kind"] == "usage" for e in j["evidence"])          # '왜'에 쓰임 근거가 그대로
    assert out["actions"][0]["kind"] == "note"


def test_overview_lists_every_capability_with_counts():
    o = selfknow.overview(seed())
    names = [c["name"] for c in o["capabilities"]]
    assert "문서" in names and "양식" in names and "왜" in names
    assert next(c for c in o["capabilities"] if c["key"] == "draft")["used"] == 3
    assert o["cannot"]


def test_vague_improvement_answer_is_replaced_with_grounded_one():
    sb = seed()
    vague = ("더 개선하면 좋은 점은 사용자와의 소통을 더욱 원활하게 하고, 개인 맞춤형 기능을 강화하는 것이에요.")
    assert selfknow.asks_improvement("더 개선하면 좋은 점은?") and selfknow.is_vague(vague)
    good = selfknow.improve_answer(sb)
    assert "아직 안 써 보신 양식" in good                         # 안 써 본 기능 하나, 쓰는 법과 함께
    assert "이 메일함 정리해 줘" in good                          # 못 해 드린 부탁 → 새 기능 후보
    assert "원활" not in good and "보안" not in good and "— 제안·행동마다" not in good


def test_frequent_searches_become_a_button_and_why_is_never_called_unused():
    sb = FakeDB()
    for i in range(12):
        act.propose(sb, "search", {"query": f"울산 날씨 {i}시"})
    for _ in range(5):
        act.propose(sb, "note", {"text": "x"})
    sb.table("episodes").insert([{"role": "user", "text": "오늘 날씨 어때"}] * 12).execute()
    u = selfknow.usage(sb)
    assert u["top_search"][0] == ("날씨", 12)
    out = selfknow.improve_answer(sb, u)
    assert "버튼 만들어 줘" in out and "'날씨'를 12번 물어보셨어요" in out
    assert "검색 버튼" not in out and "아직 안 써 보신 왜" not in out
    assert "새 기능은 아직 근거가 없어요" in out


def test_unmet_looks_at_recent_talk_not_oldest():
    sb = FakeDB()
    sb.table("episodes").insert([{"role": "user", "text": "ㅇ"}, {"role": "agent", "text": "네"}] * 450).execute()
    sb.table("episodes").insert([{"role": "user", "text": "구글 캘린더랑 연동해 줘"},
                                 {"role": "agent", "text": "구글 캘린더는 아직 연결돼 있지 않아요."}]).execute()
    assert "구글 캘린더랑 연동해 줘" in selfknow.usage(sb)["unmet"]


def test_improvement_with_no_history_says_too_early():
    out = selfknow.improve_answer(FakeDB())
    assert "판단하기 일러요" in out


def test_brain_replaces_vague_improvement_reply(monkeypatch):
    sb = seed()
    vague = {"reply": "사용자 경험을 향상하고 맞춤형 기능을 강화하면 좋아요.", "actions": [], "user_facts": []}
    monkeypatch.setattr(brain, "_ask", lambda m: dict(vague))
    out = brain.respond(sb, AGENT, "너 뭐 더 개선하면 좋을까?")
    assert "쓰신 걸 보면" in out["reply"] and "맞춤형" not in out["reply"]


def test_task_searches_and_words_the_user_never_said_are_not_counted():
    sb = FakeDB()
    for i in range(30):      # 맡은 일이 스스로 한 검색
        act.propose(sb, "search", {"query": "초등교사 연수 자료"}, origin="self", task_id=1)
    for i in range(10):      # 사람 부탁이지만 모델이 직업을 덧붙인 검색
        act.propose(sb, "search", {"query": "초등교사 울산 날씨"})
    sb.table("episodes").insert([{"role": "user", "text": "울산 날씨 알려 줘"}] * 10).execute()
    top = dict(selfknow.usage(sb)["top_search"])
    assert "초등교사" not in top and top.get("날씨") == 10


def test_compare_question_gets_honest_structural_answer(monkeypatch):
    sb = seed()
    fluff = {"reply": "첫째, 사용자 맞춤형 서비스에 중점을 두고… 둘째, 정직하고 다정함이 있습니다. "
                      "셋째, 지속적인 개선으로 사용자 경험을 향상시키려 노력합니다.", "actions": [], "user_facts": []}
    assert selfknow.is_vague(fluff["reply"]) and selfknow.asks_compare("너가 다른 llm보다 더 나은 점은?")
    monkeypatch.setattr(brain, "_ask", lambda m: dict(fluff))
    out = brain.respond(sb, AGENT, "너가 다른 GPT나 LLM보다 더 나은 점이 뭐야?")["reply"]
    assert selfknow.model_name() in out and "앞서지 않아요" in out      # 두뇌는 같다고 솔직히
    assert "한글" in out and "판단하는 구조" in out and "맞춤형" not in out and "다정" not in out
    assert not selfknow.is_vague(out)
    long, _ = selfknow.for_prompt(sb, "챗GPT랑 뭐가 달라?")
    assert "성격 자랑" in long
