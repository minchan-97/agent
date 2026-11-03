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
from .absorb import absorb
from .identity import system_prompt
from .privacy import mask

KST = timezone(timedelta(hours=9))

CONTRACT = """
반드시 JSON 객체 하나로만 답해. 다른 글자는 쓰지 마.
{
  "reply": "사용자에게 할 말",
  "actions": [{"kind": "...", "args": {...}, "reason": "왜 하자고 하는지 한 줄"}],
  "user_facts": ["..."]
}

actions 에 쓸 수 있는 kind 와 args
  search        {"query": "검색어"}          모르는 최신 정보가 필요할 때만
  note          {"text": "남길 내용"}        사용자가 기록해 달라고 할 때
  reminder      {"text": "알릴 말", "due_at": "2026-11-03T15:00:00+09:00"}
  draft         {"to": "받는 사람", "text": "초안"}   글을 대신 써 달라고 할 때
  send_message  아직 못 하는 일. 보내 달라고 하면 draft 로 대신 만들고 그렇다고 말해.

규칙
- 하자고 한 일이 "이미 끝났다"고 말하지 마. 실행할지는 시스템과 사용자가 정한다.
  "메모로 남겨 둘게요", "알림을 걸어 둘게요"처럼 말하면 충분하다.
- 할 일이 없으면 actions 는 [].
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

    messages = [{"role": "system",
                 "content": system_prompt(agent["name"], facts, notes, now) + "\n\n" + CONTRACT}]
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

    sb.table("episodes").insert([{"role": "user", "text": text},
                                 {"role": "agent", "text": reply}]).execute()
    absorbed = absorb(sb, out["user_facts"], text, reply, agent["name"])
    sb.table("audit").insert({"kind": "turn", "detail": {
        "actions": [d.get("kind") for d in done], "absorbed": absorbed,
        "searched": bool(wanted)}}).execute()

    return {"reply": reply, "actions": done}
