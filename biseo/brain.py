"""
brain.py — 한 번의 대화 턴.

흐름
  사용자 말 → 번호류 가리기 → 정체성·사실·업무 기록으로 문맥 구성
  → LLM 이 {답, 하자고 하는 일, 사용자 사실} 을 JSON 으로 낸다
  → 검색이 필요하면 한 번 찾아보고 다시 묻는다 (최대 1회)
  → 하자고 한 일은 actions.propose 가 등급대로 처리
  → 사용자 사실은 absorb 관문을 지난다

몰랑이 outbox 의 원칙을 행동에도 쓴다:
  "했다"는 LLM 이 정하지 않는다. 실행 여부와 결과는 시스템이 정하고,
  화면은 actions 행의 상태를 보여준다.
"""
import json
import os
from datetime import datetime, timedelta, timezone

from . import actions as act
from . import search
from . import tasks as tk
from . import skills as sk
from .absorb import absorb
from .identity import system_prompt
from .privacy import mask

KST = timezone(timedelta(hours=9))

CONTRACT = """
반드시 JSON 객체 하나로만 답해. 다른 글자는 쓰지 마.
{
  "reply": "사용자에게 할 말",
  "actions": [{"kind": "...", "args": {...}, "reason": "왜 하자고 하는지 한 줄"}],
  "user_facts": ["..."],
  "task_answers": [{"task_id": 숫자, "answer": "..."}]
}

actions 에 쓸 수 있는 kind 와 args
  search        {"query": "검색어"}          모르는 최신 정보가 필요할 때만
  note          {"text": "남길 내용"}        사용자가 기록해 달라고 할 때
  reminder      {"text": "알릴 말", "due_at": "2026-11-03T15:00:00+09:00"}
  draft         {"to": "받는 사람", "text": "초안"}   글을 대신 써 달라고 할 때
  start_task    {"goal": "과제 목표 한 문장", "skill_id": 숫자 또는 null}
                아래 추가 기능 중 딱 맞는 게 있으면 skill_id 를 넣어.
                여러 걸음이 필요한 일(조사하고 비교하기, 준비 목록 만들기, 계획 세우기 등)일 때.
                과제로 맡으면 대화 밖에서도 한 걸음씩 진행하고, 묻거나 끝나면 알려 준다.
                그 자리에서 바로 답할 수 있는 간단한 일은 과제로 만들지 마.
  create_skill  {"name": "짧은 이름", "description": "한 줄 설명",
                 "instructions": "이 일을 할 때 따를 방식", "tools": ["search","note","reminder","draft" 중 필요한 것]}
                사용자가 "~하는 기능 만들어 줘"라고 하거나, 같은 종류의 부탁이 대화 기록에서
                여러 번 반복된 게 보일 때만. 기본 도구로 할 수 없는 일은 기능으로 만들지 마.
  send_message  아직 못 하는 일. 보내 달라고 하면 draft 로 대신 만들고 그렇다고 말해.

규칙
- 하자고 한 일이 "이미 끝났다"고 말하지 마. 실행할지는 시스템과 사용자가 정한다.
  "메모로 남겨 둘게요", "알림을 걸어 둘게요"처럼 말하면 충분하다.
- 할 일이 없으면 actions 는 [].
- task_answers: 아래 [답을 기다리는 과제]의 질문에 사용자가 이번 말로 답했으면 그 답을 넣어. 아니면 [].
- user_facts: 이번 사용자 말에서 사용자가 '자기 자신'에 대해 직접 밝힌 오래 갈 사실만,
  "사용자는 ..." 꼴의 짧은 평서문으로. 업무 내용(메일·일정 세부·문서), 다른 사람의 정보,
  네 추측, 네가 한 말은 절대 넣지 마. 없으면 [].
"""


def _client():
    from openai import OpenAI
    return OpenAI(api_key=os.environ["OPENAI_API_KEY"])


def _ask(messages: list) -> dict:
    res = _client().chat.completions.create(
        model=os.environ.get("OPENAI_MODEL", "gpt-4o-mini"),
        messages=messages,
        response_format={"type": "json_object"},
        temperature=0.6,
    )
    try:
        out = json.loads(res.choices[0].message.content or "{}")
    except json.JSONDecodeError:
        out = {}
    out.setdefault("reply", "")
    out["actions"] = [a for a in out.get("actions") or [] if isinstance(a, dict)]
    out["user_facts"] = [f for f in out.get("user_facts") or [] if isinstance(f, str)]
    out["task_answers"] = [x for x in out.get("task_answers") or [] if isinstance(x, dict)]
    return out


def respond(sb, agent: dict, user_text: str) -> dict:
    text = mask(user_text.strip())[:4000]
    now = datetime.now(KST).strftime("%Y-%m-%d (%a) %H:%M")

    facts = (sb.table("facts_active").select("text,trust")
             .order("strength", desc=True).limit(30).execute().data) or []
    notes = [r["content"] for r in (sb.table("task_memory").select("content")
             .eq("kind", "note").order("id", desc=True).limit(10).execute().data or [])]
    history = (sb.table("episodes").select("role,text")
               .order("id", desc=True).limit(8).execute().data or [])[::-1]

    waiting = (sb.table("tasks").select("id,goal,question").eq("status", "waiting_user")
               .order("id", desc=True).limit(5).execute().data) or []
    open_tasks = (sb.table("tasks").select("id,goal,status")
                  .in_("status", ["open", "waiting_approval"]).limit(5).execute().data) or []
    extra = ""
    if waiting:
        extra += "\n\n[답을 기다리는 과제]\n" + "\n".join(
            f"#{w['id']} {w['goal']} — 질문: {w['question']}" for w in waiting)
    if open_tasks:
        extra += "\n\n[진행 중인 과제]\n" + "\n".join(
            f"#{o['id']} {o['goal']} ({o['status']})" for o in open_tasks)

    extra += sk.for_prompt(sb)
    messages = [{"role": "system",
                 "content": system_prompt(agent["name"], facts, notes, now) + extra
                 + "\n\n" + CONTRACT}]
    for h in history:
        messages.append({"role": "user" if h["role"] == "user" else "assistant",
                         "content": h["text"]})
    messages.append({"role": "user", "content": text})

    out = _ask(messages)

    # 검색이 필요하다고 하면 한 번 찾아보고 다시 묻는다
    wanted = [a for a in out["actions"] if a.get("kind") == "search"]
    if wanted and search.available():
        q = str((wanted[0].get("args") or {}).get("query", ""))[:200]
        act.propose(sb, "search", {"query": q}, wanted[0].get("reason", ""))
        try:
            ctx = search.to_context(search.brave(q))
        except Exception:
            ctx = "[검색 실패 — 모른다고 솔직히 말할 것]"
        messages.append({"role": "assistant", "content": json.dumps(out, ensure_ascii=False)})
        messages.append({"role": "user", "content":
                         ctx + "\n\n위 자료를 참고해 다시 답해. 출처가 있으면 짧게 밝혀."})
        out = _ask(messages)
    out["actions"] = [a for a in out["actions"] if a.get("kind") != "search"]

    reply = out["reply"].strip() or "잠깐 생각이 엉켰어요. 한 번만 다시 말해 줄래요?"

    done = []
    for a in out["actions"][:3]:
        done.append(act.propose(sb, str(a.get("kind", "")), a.get("args") or {},
                                str(a.get("reason", "")), origin="user"))

    answered = []
    waiting_ids = {w["id"] for w in waiting}
    for ta in (out.get("task_answers") or [])[:3]:
        try:
            tid = int(ta.get("task_id"))
            if tid in waiting_ids and str(ta.get("answer", "")).strip():
                tk.answer(sb, tid, str(ta["answer"]))
                answered.append(tid)
        except (ValueError, TypeError, AttributeError):
            continue

    sb.table("episodes").insert([{"role": "user", "text": text},
                                 {"role": "agent", "text": reply}]).execute()
    absorbed = absorb(sb, out["user_facts"], text, reply, agent["name"])
    sb.table("audit").insert({"kind": "turn", "detail": {
        "actions": [d.get("kind") for d in done], "absorbed": absorbed,
        "searched": bool(wanted)}}).execute()

    return {"reply": reply, "actions": done, "answered_tasks": answered}
