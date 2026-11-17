"""test_docagent.py — 문서함이 대화·과제·초안·알림과 이어지는가."""
import io
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pytest

from biseo import actions as act
from biseo import brain, docagent, docread, observe
from biseo import tasks as tk
from docfixtures import make_docx
from test_docs import env  # noqa: F401
from test_templates import FIELDS, _doc

AGENT = {"name": "비서", "heir": False}
SOON = (datetime.now(timezone(timedelta(hours=9))) + timedelta(days=3)).replace(microsecond=0).isoformat()


def turn(*answers):
    it = iter(answers)
    return lambda msgs: {"reply": "", "actions": [], "user_facts": [], "task_answers": [], "suggest": [], **next(it)}


@pytest.fixture
def chat(env, monkeypatch):
    monkeypatch.setattr(observe, "note", lambda *a, **k: None)
    return env


# ---------------------------------------------------------------- 번호 → id
def test_catalog_numbers_and_unknown_refs_are_dropped(env):
    d = env.put("통신문.docx", make_docx())
    text, idx = docagent.catalog(env.sb)
    assert "문서1: 통신문.docx" in text and idx["문서1"]["id"] == d["id"]
    assert docagent.resolve_args("doc_edit", {"doc": "D1", "instruction": "x"}, idx)["doc_id"] == d["id"]
    assert docagent.resolve_args("doc_edit", {"doc": "D9", "instruction": "x"}, idx) is None
    assert docagent.resolve_args("doc_edit", {"doc_id": 999, "instruction": "x"}, idx) is None   # 진짜 id 를 직접 넣어도 안 됨
    r = docagent.resolve_args("reminder", {"text": "제출", "due_at": SOON, "doc": "D1"}, idx)
    assert r["doc_id"] == d["id"] and "doc" not in r


# ---------------------------------------------------------------- 대화
def test_chat_reads_doc_then_answers_and_actions_are_tainted(chat, monkeypatch):
    d = chat.put("통신문.docx", make_docx())
    seen = []
    answers = turn(
        {"reply": "읽어 볼게요", "actions": [{"kind": "read_doc", "args": {"doc": "D1", "question": "날짜"}}]},
        {"reply": "5월 20일이에요. 하루 전에 알려 드릴게요.",
         "actions": [{"kind": "reminder", "args": {"text": "체험학습 준비", "due_at": SOON, "doc": "D1"},
                      "because": ["D1"]}]})
    monkeypatch.setattr(brain, "_ask", lambda msgs: (seen.append(msgs[-1]["content"]), answers(msgs))[1])
    act.KINDS  # noqa
    out = brain.respond(chat.sb, AGENT, "통신문 체험학습 언제야? 알림도 걸어 줘")
    assert "[문서 자료]" in seen[-1] and "5월 20일" in seen[-1]          # 두 번째 물음에 문서 글이 들어감
    rem = out["actions"][0]
    assert rem["kind"] == "reminder" and rem["tainted"] and rem["status"] == "proposed"
    assert rem["args"]["doc_id"] == d["id"]
    assert out["docs_read"] == [{"id": d["id"], "name": "통신문.docx"}]
    j = chat.sb.rows("judgments")[-1]
    assert any(e["kind"] == "doc" and e["id"] == d["id"] for e in j["evidence"])
    act.approve(chat.sb, rem["id"])                                      # 승인하면 문서에 붙은 알림
    assert chat.sb.rows("reminders")[0]["doc_id"] == d["id"]


def test_chat_edit_and_template_and_new_doc(chat, monkeypatch):
    d = chat.put("통신문.docx", _doc())
    from biseo import templates as tpl
    tpl.create(chat.sb, d["id"], "체험학습 안내", FIELDS[:2])
    monkeypatch.setattr(brain, "_ask", turn({"reply": "할게요", "actions": [
        {"kind": "doc_edit", "args": {"doc": "D1", "instruction": "장소를 강당으로"}},
        {"kind": "doc_make", "args": {"template": "T1", "text": "6월 3일 박물관"}},
        {"kind": "doc_new", "args": {"title": "회의록", "text": "# 학년 협의\n- 체험학습 장소 확정\n- 예산 검토"}}]}))
    out = brain.respond(chat.sb, AGENT, "고치고, 양식으로 만들고, 회의록도 파일로")
    assert [a["kind"] for a in out["actions"]] == ["doc_edit", "doc_make", "doc_new"]
    assert all(a["status"] == "proposed" and not a["tainted"] for a in out["actions"])   # 처음엔 묻는다 (tier 3)
    # 승인 → 실제 파일. docs._llm 은 수정 지시·칸 값을 차례로 낸다
    chat.reply = lambda m: ({"ops": [{"op": "replace", "find": "경주 일대", "with": "강당"}], "summary": "장소 변경"}
                            if "수정 지시" in m[0]["content"] else {"values": {"날짜": "6월 3일(수)", "장소": "국립중앙박물관"}})
    before = len(chat.sb.rows("docs"))
    done = [act.approve(chat.sb, a["id"]) for a in out["actions"]]
    assert [x["status"] for x in done] == ["done", "done", "done"], [x.get("result") for x in done]
    assert len(chat.sb.rows("docs")) == before + 3
    new = [r for r in chat.sb.rows("docs") if r["name"] == "회의록.docx"][0]
    from docx import Document
    doc = Document(io.BytesIO(chat.store.files[new["path"]]))
    assert doc.paragraphs[0].style.name.startswith("Heading") and doc.paragraphs[1].style.name == "List Bullet"
    # 되돌리면 비서가 만든 파일이 지워진다
    act.undo(chat.sb, done[2]["id"])
    assert not [r for r in chat.sb.rows("docs") if r["name"] == "회의록.docx"] and new["path"] not in chat.store.files


# ---------------------------------------------------------------- 과제
def test_task_reads_doc_and_following_action_is_tainted(chat):
    chat.put("통신문.docx", make_docx())
    t = tk.create(chat.sb, "통신문 읽고 준비물 메모해 줘")
    it = iter([{"thought": "읽자", "action": {"kind": "read_doc", "args": {"doc": "D1", "question": "준비물"}}, "status": "continue"},
               {"thought": "메모", "action": {"kind": "note", "args": {"text": "도시락, 물"}}, "status": "continue"}])
    ask = lambda m: next(it)  # noqa: E731
    tk.step(chat.sb, AGENT, t["id"], ask)
    log = tk.get(chat.sb, t["id"])["log"]
    assert log[-1]["outcome"].startswith("[문서 자료]") and "도시락" in log[-1]["outcome"]
    res = tk.step(chat.sb, AGENT, t["id"], ask)
    note = [a for a in chat.sb.rows("actions") if a["kind"] == "note"][0]
    assert note["tainted"] and res["task"]["status"] == "waiting_approval"


def test_task_unknown_doc_number_does_nothing(chat):
    t = tk.create(chat.sb, "문서 고치기")
    tk.step(chat.sb, AGENT, t["id"], lambda m: {"thought": "x", "action": {"kind": "doc_edit", "args": {"doc": "D3", "instruction": "y"}},
                                                  "status": "continue"})
    assert not [a for a in chat.sb.rows("actions") if a["kind"] == "doc_edit"]


# ---------------------------------------------------------------- 초안 ↔ 문서
def test_draft_to_new_doc_template_and_edit(chat):
    d = chat.put("통신문.docx", _doc())
    from biseo import templates as tpl
    t = tpl.create(chat.sb, d["id"], "안내", FIELDS[:2])["template"]
    dr = act.propose(chat.sb, "draft", {"to": "학부모", "text": "체험학습 안내\n6월 3일 국립중앙박물관으로 갑니다."})
    draft_id = dr["result"]["memory_id"]
    r1 = docagent.from_draft(chat.sb, draft_id, "new")
    assert r1["doc"]["name"] == "체험학습 안내.docx"
    chat.reply = {"values": {"날짜": "6월 3일", "장소": "국립중앙박물관"}}
    r2 = docagent.from_draft(chat.sb, draft_id, "template", template_id=t["id"])
    assert "국립중앙박물관" in docread.read("docx", chat.store.files[chat.sb.rows("docs")[-1]["path"]])["text"]
    chat.reply = {"ops": [{"op": "replace", "find": "경주 일대", "with": "국립중앙박물관"}], "summary": "반영"}
    r3 = docagent.from_draft(chat.sb, draft_id, "edit", doc_id=d["id"])
    assert r3["doc"]["parent_id"] == d["id"] and r2["empty"] == []


def test_draft_from_doc_links_back(chat):
    d = chat.put("통신문.docx", make_docx())
    chat.reply = {"to": "학부모", "text": "안녕하세요. 체험학습은 5월 20일입니다."}
    r = docagent.draft_from_doc(chat.sb, d["id"], "학부모 안내 문자")
    row = [m for m in chat.sb.rows("task_memory") if m["id"] == r["draft_id"]][0]
    assert row["doc_id"] == d["id"] and "5월 20일" in row["content"]
    assert docagent.linked(chat.sb, d["id"])["drafts"][0]["id"] == r["draft_id"]


# ---------------------------------------------------------------- 올린 문서에서 할 일 찾기
def test_scan_proposes_future_items_only_with_doc_evidence(chat):
    d = chat.put("통신문.docx", make_docx())
    past = "2020-01-01T09:00:00+09:00"
    chat.reply = {"items": [
        {"kind": "reminder", "args": {"text": "동의서 제출", "due_at": SOON}, "reason": "제출 기한", "quote": "5월 20일"},
        {"kind": "event", "args": {"title": "지난 행사", "starts_at": past}, "quote": "x"},
        {"kind": "reminder", "args": {"text": "지어낸 근거", "due_at": SOON}, "quote": "문서에 없는 문장"},
        {"kind": "reminder", "args": {"text": "넷째는 버림 (최대 3개)", "due_at": SOON}}]}
    made = docagent.scan(chat.sb, d)
    assert [m["args"]["text"] for m in made] == ["동의서 제출", "지어낸 근거"]
    assert all(m["status"] == "proposed" and m["tainted"] and m["args"]["doc_id"] == d["id"] for m in made)
    ev = chat.sb.rows("judgments")
    assert ev[0]["evidence"][0]["text"] == "5월 20일" and ev[1]["evidence"][0]["text"] == "통신문.docx"   # 없는 인용은 문서 이름으로
    assert len(docagent.linked(chat.sb, d["id"])["pending"]) == 2


def test_internal_numbers_never_reach_the_user(chat):
    d = chat.put("현장체험학습 가정통신문.hwpx", __import__("docfixtures").make_hwpx(["체험학습 5월 20일"]))
    for i in range(10):                                   # 최근 8개 밖으로 밀려난 오래된 문서
        chat.put(f"기타{i}.docx", make_docx())
    text, idx = docagent.catalog(chat.sb, "가정통신문 날짜 바꿔 줘")
    assert "현장체험학습 가정통신문.hwpx" in text            # 이름이 닮아서 목록에 들어옴
    key = next(k for k, v in idx.items() if v["id"] == d["id"])
    assert docagent.humanize(f"{key} 문서가 맞나요? [D99] 도요", idx) == "'현장체험학습 가정통신문.hwpx' 문서가 맞나요? 그 문서 도요"
    assert docagent.humanize("문서 2개와 양식3장을 봤어요", idx) == "문서 2개와 양식3장을 봤어요"
    assert docagent.norm_key("[문서 3]") == "문서3" and docagent.norm_key("doc3") == "문서3" and docagent.norm_key("B3") == ""
    # 과제가 묻는 말에도 번호 대신 이름
    t = tk.create(chat.sb, "가정통신문 날짜 바꾸기")
    tk.step(chat.sb, AGENT, t["id"], lambda m: {"thought": "x", "action": None, "status": "ask_user",
                                                  "question": f"{key}의 날짜를 무엇으로 바꿀까요?"})
    q = tk.get(chat.sb, t["id"])["question"]
    assert "문서" not in q.split("'")[0] and "현장체험학습 가정통신문.hwpx" in q
