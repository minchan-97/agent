"""test_contra.py — 답의 판단이 확정된 판단(자기 모형·이번에 한 일)과 어긋나면 잡고, 다시 쓰게 하고, 배운다."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from biseo import brain, contra, judge
from fakedb import FakeDB

AGENT = {"name": "비서", "heir": False}


def types(reply, acted=(), text=""):
    return [f["type"] for f in contra.check(reply, list(acted), text)]


def test_said_it_did_but_nothing_happened():
    assert types("금요일 9시에 알림 맞춰 뒀어요.") == ["did"]
    assert types("일정에 넣었어요.", [{"kind": "event", "status": "done"}]) == []
    f = contra.check("알림 설정했어요.", [{"kind": "reminder", "status": "proposed"}])[0]
    assert "확인을 기다리는" in f["against"] and "확인함" in f["fix"]


def test_promised_what_it_cannot_do():
    assert types("학부모님께 카톡으로 보내 드릴게요.") == ["can"]
    assert types("메일 보냈어요.") == ["can"]
    assert types("보내기는 직접 못 해요. 초안을 써 드릴게요.") == []


def test_said_it_cannot_do_what_it_can():
    f = contra.check("죄송해요, 알림은 설정할 수 없어요.", [])
    assert [x["type"] for x in f] == ["cannot"] and "할 수 있어요" in f[0]["fix"]
    assert types("구글 캘린더 연동은 아직 못 해요.") == []          # 실제로 못 하는 일 → 모순 아님


def test_ordinary_replies_pass():
    for r in ["내일 울산은 맑아요.", "알림 맞춰 둘까요?", "지난번 만들었던 문서 말씀이시죠?",
              "일정은 아직 넣지 않았어요.", "좋아요, 다음에 또 말씀해 주세요."]:
        assert types(r) == [], r


def test_loop_rewrites_then_repairs_and_learns(monkeypatch):
    sb = FakeDB()
    before = judge.load_trees(sb)["response"]["transitions"]
    before = {k: dict(v) for k, v in before.items()}
    replies = iter([{"reply": "네, 금요일 9시 알림 맞춰 뒀어요.", "actions": [], "user_facts": []},
                    {"reply": "알림 맞춰 놨어요!", "actions": [], "user_facts": []}])      # 다시 써도 그대로
    monkeypatch.setattr(brain, "_ask", lambda m: next(replies))
    out = brain.respond(sb, AGENT, "금요일 9시에 알려 줘")
    assert "맞춰 놨" not in out["reply"] and "아직 넣지 않았어요" in out["reply"]
    after = judge.load_trees(sb)["response"]["transitions"]
    assert after != before                                       # 모순이 난 길을 약하게 배웠다
    audit = [r for r in sb.table("audit").select("*").execute().data if r["kind"] == "turn"][-1]
    assert audit["detail"]["contradictions"][0]["type"] == "did"


def test_loop_accepts_good_rewrite(monkeypatch):
    sb = FakeDB()
    seen = []
    replies = iter([{"reply": "카톡으로 보내 드릴게요.", "actions": [], "user_facts": []},
                    {"reply": "보내기는 직접 못 해요. 초안을 써 드릴까요?", "actions": [], "user_facts": []}])

    def ask(m):
        seen.append(m[-1]["content"])
        return next(replies)
    monkeypatch.setattr(brain, "_ask", ask)
    out = brain.respond(sb, AGENT, "엄마한테 내일 늦는다고 보내 줘")
    assert out["reply"] == "보내기는 직접 못 해요. 초안을 써 드릴까요?"
    assert "[모순 검사" in seen[-1] and "못 하는 일" in seen[-1]
