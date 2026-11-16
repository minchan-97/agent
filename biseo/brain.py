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
import re
from datetime import datetime, timedelta, timezone

from . import actions as act
from . import search
from . import explore as ex
from . import observe
from . import growth
from . import heir
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
  "actions": [{"kind": "...", "args": {...}, "reason": "왜 하자고 하는지 한 줄", "because": ["U", "F1"]}],
  "user_facts": ["..."],
  "task_answers": [{"task_id": 숫자, "answer": "..."}],
  "suggest": [{"kind": "note|reminder|draft|event|start_task", "label": "버튼에 쓸 짧은 말", "args": {...}}]
}

actions 에 쓸 수 있는 kind 와 args
  search        {"query": "검색어"}          모르는 최신 정보가 필요할 때만
  note          {"text": "남길 내용"}        사용자가 기록해 달라고 할 때
  reminder      {"text": "알릴 말", "due_at": "2026-11-03T15:00:00+09:00", "repeat": 선택}
                repeat: "daily"(매일) | "weekdays"(평일) | "weekly"(매주) | "monthly"(매월).
                "매주 화요일", "매일 아침"처럼 반복을 말했을 때만 넣고, due_at 은 첫 회차 시각으로.
  draft         {"to": "받는 사람", "text": "초안"}   글을 대신 써 달라고 할 때
  event         {"title": "일정 이름", "starts_at": "2026-11-05T15:00:00+09:00",
                 "ends_at": 선택, "place": 선택, "remind_before_min": 선택(숫자), "note": 선택,
                 "repeat": 선택(daily|weekdays|weekly|monthly)}
                "매주 화요일 협의회"처럼 반복하는 일정이면 repeat 를 넣고 starts_at 은 다가올 첫 회차로.
                사용자의 약속·회의·행사 같은 일정. "~분 전에 알려 줘"가 붙으면
                reminder 를 따로 만들지 말고 remind_before_min 에 넣어 (일정과 연결된다).
  start_task    {"goal": "과제 목표 한 문장", "skill_id": 숫자 또는 null}
                아래 추가 기능 중 딱 맞는 게 있으면 skill_id 를 넣어.
                여러 걸음이 필요한 일(조사하고 비교하기, 준비 목록 만들기, 계획 세우기 등)일 때.
                과제로 맡으면 대화 밖에서도 한 걸음씩 진행하고, 묻거나 끝나면 알려 준다.
                그 자리에서 바로 답할 수 있는 간단한 일은 과제로 만들지 마.
  create_skill  {"name": "짧은 이름", "description": "한 줄 설명",
                 "instructions": "이 일을 할 때 따를 방식", "tools": ["search","note","reminder","draft","event" 중 필요한 것]}
                사용자가 "~하는 기능 만들어 줘"라고 하거나, 같은 종류의 부탁이 대화 기록에서
                여러 번 반복된 게 보일 때만. 기본 도구로 할 수 없는 일은 기능으로 만들지 마.
  send_message  아직 못 하는 일. 보내 달라고 하면 draft 로 대신 만들고 그렇다고 말해.

규칙
- because: 그 행동의 근거가 된 줄의 번호. U = 사용자의 이번 말, F번호 = 아는 사실, N번호 = 업무 기록.
  근거가 사용자의 이번 말뿐이면 ["U"]. 근거를 댈 수 없는 행동은 하지 마.
- 하자고 한 일이 "이미 끝났다"고 말하지 마. 실행할지는 시스템과 사용자가 정한다.
  "메모로 남겨 둘게요", "알림을 걸어 둘게요"처럼 말하면 충분하다.
- 할 일이 없으면 actions 는 [].
- suggest: 사용자가 "해 줘"라고 시키진 않았지만 도움이 될 만한 행동을, 실행하지 말고 '제안'으로 담아라.
  예: 약속을 말하면 {"kind":"event","label":"일정에 추가","args":{...}}, 할 일을 말하면 {"kind":"note","label":"메모로 저장",...},
  마감을 말하면 {"kind":"reminder","label":"알림 걸기",...}. 사용자는 버튼으로 수락한다. args 는 actions 와 같은 형식.
  확실히 시킨 일은 actions 로, 시키지 않았지만 유용한 건 suggest 로. 둘은 겹치지 않게. 없으면 [].
- 경기 일정·결과, 날씨, 가격, 뉴스, 개봉·출시일처럼 바뀌는 바깥 사실은 네 기억으로 답하지 마.
  search 를 요청하고, reply 에는 날짜·시간·상대·숫자를 쓰지 말고 "찾아볼게요" 정도만 써.
- task_answers: 아래 [답을 기다리는 과제]의 질문에 사용자가 이번 말로 답했으면 그 답을 넣어. 아니면 [].
- user_facts: 이번 사용자 말에서 사용자가 '자기 자신'에 대해 직접 밝힌 오래 갈 사실만,
  "사용자는 ..." 꼴의 짧은 평서문으로. 업무 내용(메일·일정 세부·문서), 다른 사람의 정보,
  네 추측, 네가 한 말은 절대 넣지 마. 없으면 [].
"""


# 규칙이 먼저 (몰랑이 outbox 원칙): 바뀌는 바깥 사실을 묻는 말이면,
# 모델이 검색을 안 하겠다고 해도 시스템이 검색을 강제한다.
_LIVE_FACT = re.compile(
    r"(일정|경기|대진|라인업|결과|스코어|점수|순위|중계|날씨|기온|강수|미세먼지|"
    r"가격|시세|환율|주가|지수|코스피|코스닥|나스닥|증시|장중|비트코인|금값|유가|"
    r"뉴스|속보|개봉|발매|출시|공연|티켓|운행|휴무|영업시간)")
_PERSONAL = re.compile(r"(내|제|우리|나의|저의)\s*(일정|스케줄)")

NO_SEARCH = ("[검색을 쓸 수 없음] 확인된 자료가 없다. 날짜·시간·상대·점수·가격 같은 구체적인 사실을 "
             "하나도 만들어 내지 말고, 지금은 확인할 수 없다고 솔직히 말해. 공식 사이트에서 확인하라고 안내해도 좋다.")
GROUNDED = ("위 자료에 실제로 있는 내용만 써서 다시 답해. 자료에 없는 날짜·시간·상대·점수·숫자는 "
            "절대 만들어 내지 마. 자료가 질문에 답하지 못하면 그렇다고 말해.\n"
            "- 각 자료의 '작성' 시각을 보고, 그 숫자가 언제 기준인지 답에 밝혀 (예: \"오후 3시 30분 마감 기준\").\n"
            "- 작성 시각이 다른 자료의 숫자를 한 답 안에 섞지 마. 가장 최근 자료 하나를 기준으로 삼아.\n"
            "- 답 끝에 \"출처: 매체 이름, 작성 시각\"을 한 줄로 붙여.")
NO_TODAY = ("[오늘 작성된 자료를 찾지 못함] 검색은 했지만 오늘 날짜 자료가 없었다. 예전 자료의 숫자를 "
            "오늘 것처럼 말하지 말고, 오늘 자료를 찾지 못했다고 솔직히 말해. 원하면 가장 최근 자료가 "
            "언제 것인지 밝히고 다시 찾아보겠다고 해도 된다.")


def needs_live_facts(text: str) -> bool:
    return bool(_LIVE_FACT.search(text)) and not _PERSONAL.search(text)


class LLMUnavailable(Exception):
    """LLM 이 지금 답할 수 없음 (시간 초과, 요청 한도, 연결 실패, 서버 오류).
    화면에는 '잠시 뒤 다시'로 보여 주고, 과제는 다음 틱에 다시 시도한다."""


LLM_TIMEOUT = float(os.environ.get("LLM_TIMEOUT", "25"))   # 초. Vercel 함수 시간 안에 끝나도록
LLM_RETRIES = 2                                            # 일시 오류는 SDK 가 지수 대기로 재시도

_client_cache = {}


def _client():
    # 함수가 따뜻할 때(warm) 연결을 다시 쓰도록 한 번 만든 클라이언트를 재사용한다
    if "c" not in _client_cache:
        from openai import OpenAI
        _client_cache["c"] = OpenAI(api_key=os.environ["OPENAI_API_KEY"],
                                    timeout=LLM_TIMEOUT, max_retries=LLM_RETRIES)
    return _client_cache["c"]


def _ask(messages: list) -> dict:
    try:
        import openai
        transient = (openai.APITimeoutError, openai.APIConnectionError,
                     openai.RateLimitError, openai.InternalServerError)
    except Exception:          # SDK 가 없거나 버전이 달라도 아래에서 일반 오류로 처리
        transient = ()
    try:
        res = _client().chat.completions.create(
            model=os.environ.get("OPENAI_MODEL", "gpt-4o-mini"),
            messages=messages,
            response_format={"type": "json_object"},
            temperature=0.6,
        )
    except transient as e:
        raise LLMUnavailable(type(e).__name__) from e
    try:
        out = json.loads(res.choices[0].message.content or "{}")
    except json.JSONDecodeError:
        out = {}
    out.setdefault("reply", "")
    out["actions"] = [a for a in out.get("actions") or [] if isinstance(a, dict)]
    out["user_facts"] = [f for f in out.get("user_facts") or [] if isinstance(f, str)]
    out["task_answers"] = [x for x in out.get("task_answers") or [] if isinstance(x, dict)]
    out["suggest"] = [x for x in out.get("suggest") or [] if isinstance(x, dict) and x.get("kind")][:4]
    return out


def respond(sb, agent: dict, user_text: str) -> dict:
    text = mask(user_text.strip())[:4000]
    now = datetime.now(KST).strftime("%Y-%m-%d (%a) %H:%M")

    facts = (sb.table("facts_active").select("id,text,trust")
             .order("strength", desc=True).limit(30).execute().data) or []
    note_rows = (sb.table("task_memory").select("id,content")
                 .eq("kind", "note").order("id", desc=True).limit(10).execute().data or [])
    # 판단 근거 번호: 행동마다 어느 줄을 근거로 했는지 인용하게 한다 ('왜?' 화면이 그 줄로 데려간다)
    evidence_index = {"U": {"tag": "U", "kind": "talk", "id": None, "text": text[:160]}}
    for i, f in enumerate(facts, 1):
        evidence_index[f"F{i}"] = {"tag": f"F{i}", "kind": "fact", "id": f.get("id"), "text": f["text"][:160]}
        f["text"] = f"[F{i}] {f['text']}"
    notes = []
    for i, n in enumerate(note_rows, 1):
        evidence_index[f"N{i}"] = {"tag": f"N{i}", "kind": "note", "id": n.get("id"), "text": str(n["content"])[:160]}
        notes.append(f"[N{i}] {n['content']}")
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
    # 몰랑이 판단 노드: 대화 판단 트리를 걷고, 비슷했던 지난 판단을 회상한다
    from . import judge
    try:
        jw = judge.walk(sb, "response")
        recalled = judge.recall(sb, "response", text)
        extra += "\n\n" + judge.directive_text(jw) + ("\n\n" + judge.recall_text(recalled) if recalled else "")
    except Exception as e:          # 판단 기록 표가 아직 없어도 대화는 된다
        print("JUDGE_WALK_SKIP", type(e).__name__, str(e)[:120])
        jw, recalled = None, []
    messages = [{"role": "system",
                 "content": system_prompt(agent["name"], facts, notes, now)
                 + growth.for_prompt(sb) + heir.for_prompt(agent) + extra
                 + "\n\n" + CONTRACT}]
    for h in history:
        messages.append({"role": "user" if h["role"] == "user" else "assistant",
                         "content": h["text"]})
    messages.append({"role": "user", "content": text})

    out = _ask(messages)

    # 검색: 모델이 원하거나, 바뀌는 바깥 사실을 묻는 말이면 강제
    wanted = [a for a in out["actions"] if a.get("kind") == "search"]
    own_work = any(a.get("kind") in ("note", "reminder", "draft", "event") for a in out["actions"])
    forced = not wanted and not own_work and needs_live_facts(text)
    searched = False
    saw_external = False      # 두 번째 답이 검색 결과(바깥 글)를 보고 나왔는가
    trail = []
    if wanted or forced:
        q = str((wanted[0].get("args") or {}).get("query", "")) if wanted else text
        q = q.strip()[:200]
        if search.available() and q:
            act.propose(sb, "search", {"query": q},
                        "바뀌는 사실이라 확인" if forced else (wanted[0].get("reason", "")))
            today_only = search.wants_today(text) or search.wants_today(q)
            try:
                observe.note(sb, "search", q)
                found_pack = ex.explore(q, today_only, _ask)   # 스스로 각도를 바꿔 파고든다
                found = found_pack["results"]
                trail = found_pack["trail"]
                searched = found_pack["found"]
                if found:
                    saw_external = True
                    ctx = search.to_context(found, found_pack["today_only"]) + "\n\n" + GROUNDED
                    note = ex.trail_text(trail)
                    if note:
                        ctx += "\n\n[참고: 네가 거친 " + note + "\n답 끝에 어떻게 찾았는지 한 줄로 덧붙여도 좋다.]"
                elif today_only:
                    ctx = NO_TODAY + "\n\n" + ex.trail_text(trail)
                else:
                    ctx = "[검색 결과 없음]\n" + NO_SEARCH + "\n\n" + ex.trail_text(trail)
            except Exception:
                ctx = "[검색 실패]\n" + NO_SEARCH
        else:
            ctx = NO_SEARCH
        # 첫 답은 버리고(지어낸 내용이 들어 있을 수 있다) 자료를 근거로 다시 답하게 한다
        messages.append({"role": "assistant", "content": json.dumps(out, ensure_ascii=False)})
        messages.append({"role": "user", "content": ctx})
        out = _ask(messages)
    out["actions"] = [a for a in out["actions"] if a.get("kind") != "search"]

    reply = out["reply"].strip() or "잠깐 생각이 엉켰어요. 한 번만 다시 말해 줄래요?"

    # 검색 결과를 본 뒤의 행동은 사용자가 시킨 것과 웹페이지 글이 시킨 것을 구분할 수 없다.
    # 그래서 오염 표시를 붙여, 승격돼 있어도 반드시 묻게 한다.
    done = []
    for a in out["actions"][:3]:
        row = act.propose(sb, str(a.get("kind", "")), a.get("args") or {},
                          str(a.get("reason", "")), origin="user", tainted=saw_external)
        done.append(row)
        # 행동마다 판단 하나: 이 행동이 어느 근거(이번 말·사실·기록)에서 나왔는지
        if jw and row.get("id"):
            tags = [str(t).strip("[] ") for t in (a.get("because") or ["U"]) if isinstance(t, (str, int))][:4] or ["U"]
            ev = [evidence_index[t] for t in tags if t in evidence_index] or [evidence_index["U"]]
            if saw_external:
                ev.append({"tag": "W", "kind": "web", "id": None, "text": "이번 답에 쓴 검색 자료"})
            try:
                judge.record(sb, jw, summary=f"{act.KINDS.get(row['kind'], {}).get('label', row['kind'])}: "
                                             f"{a.get('reason') or ''}".strip(": "),
                             context=text, evidence=ev, recalled=recalled,
                             ref_kind="action", ref_id=row["id"], tainted=saw_external)
            except Exception as e:
                print("JUDGE_RECORD_SKIP", type(e).__name__, str(e)[:120])

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
        "searched": searched, "forced_search": forced, "tainted": saw_external,
        "search_tries": len(trail)}}).execute()

    return {"reply": reply, "actions": done, "answered_tasks": answered,
            "suggest": out.get("suggest", [])}
