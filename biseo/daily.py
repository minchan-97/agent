"""
daily.py — 매일 정한 시각에 비서가 먼저 생각해서 "오늘 하면 좋을 것"을 건넨다.

대화로 시작하는 수동 비서가 아니라, 먼저 움직이는 비서.
  · 생각 시간(agents.think_at, "HH:MM", 한국 시각)은 사용자가 정한다. 비우면 끈다.
  · 그 시각이 지난 첫 크론 틱에 한 번: 오늘·가까운 일정, 알림, 열린 과제, 메모, 사용자 사실,
    최근 대화를 보고 제안을 최대 3개 만든다 → agent_state 'daily.plan' 에 오늘 날짜로 둔다
  · 알림 한 통: 브리핑 한 줄 + 첫 제안. 홈 화면 위젯에 제안 카드가 뜬다.

울타리 (몰랑이·actions 원칙 그대로)
  · 제안은 제안일 뿐 실행하지 않는다. 사용자가 '맡기기'를 눌러야 움직이고,
    그때도 actions 등급 규칙을 그대로 지난다 (남에게 닿는 일은 여전히 확인함으로).
  · 생각 재료에 검색 결과·과제 결과 같은 바깥 글은 넣지 않는다 (목표·상태만).
    바깥 글이 아침 제안을 조종하지 못하게.
  · 일정·숫자를 지어내지 않는다: 재료에 없는 날짜·사실로 제안하지 말 것.
  · AI 가 계속 실패하면 세 번째에 AI 없이 브리핑만 보내고 그날은 끝낸다.
"""
import re
import secrets
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))
KEY = "daily.plan"
TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
DEFAULT_TIME = "07:30"
MAX_ITEMS = 3
MAX_FAILS = 3
KINDS = ("task", "note", "reminder", "event", "none")


def _now():
    return datetime.now(KST)


def valid_time(v) -> bool:
    return v is None or bool(TIME_RE.match(str(v)))


def think_at(agent: dict):
    """비어 있으면(None) 끈 것. 컬럼이 없던 예전 계정은 기본값."""
    if "think_at" not in agent:
        return DEFAULT_TIME
    return agent.get("think_at")


# ---------------- 상태 ----------------
def load(sb):
    rows = sb.table("agent_state").select("value").eq("key", KEY).limit(1).execute().data
    return rows[0]["value"] if rows else None


def today_plan(sb):
    p = load(sb)
    return p if p and p.get("date") == _now().date().isoformat() else None


def _save(sb, plan: dict):
    sb.table("agent_state").upsert({"key": KEY, "value": plan,
                                    "updated_at": datetime.now(timezone.utc).isoformat()}).execute()


def is_due(sb, agent: dict, now=None) -> bool:
    t = think_at(agent)
    if not t or not TIME_RE.match(t):
        return False
    now = now or _now()
    hh, mm = map(int, t.split(":"))
    if (now.hour, now.minute) < (hh, mm):
        return False
    p = load(sb)
    if p and p.get("date") == now.date().isoformat() and (p.get("done") or p.get("fails", 0) >= MAX_FAILS):
        return False
    return True


# ---------------- 재료 ----------------
def _hm(iso):
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).astimezone(KST).strftime("%m/%d %H:%M")
    except ValueError:
        return ""


def gather(sb) -> dict:
    now = _now()
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    from . import apps
    evs = apps.events_between(sb, start.isoformat(), (start + timedelta(days=4)).isoformat(), limit=12)
    rems = sb.table("reminders").select("text,due_at").eq("status", "scheduled") \
             .lt("due_at", (start + timedelta(days=2)).isoformat()) \
             .order("due_at", desc=False).limit(8).execute().data or []
    # 과제는 목표·상태만 (결과·기록에는 검색으로 가져온 바깥 글이 있을 수 있다)
    tasks = sb.table("tasks").select("goal,status,question") \
              .in_("status", ["open", "waiting_user", "waiting_approval", "stopped"]) \
              .limit(8).execute().data or []
    notes = sb.table("task_memory").select("title,content").eq("kind", "note") \
              .order("updated_at", desc=True).limit(5).execute().data or []
    facts = sb.table("facts_active").select("text").order("strength", desc=True).limit(12).execute().data or []
    talk = (sb.table("episodes").select("role,text").order("id", desc=True).limit(10).execute().data or [])[::-1]
    pending = sb.table("actions").select("id", count="exact").eq("status", "proposed").execute().count or 0
    return {"events": evs, "reminders": rems, "tasks": tasks, "notes": notes,
            "facts": facts, "talk": talk, "pending": pending}


def brief_line(g: dict) -> str:
    """AI 없이 규칙으로 만드는 한 줄 (알림·실패 시 대체)."""
    today = _now().date().isoformat()
    todays = [e for e in g["events"] if _hm(e["starts_at"]) and
              datetime.fromisoformat(str(e["starts_at"]).replace("Z", "+00:00")).astimezone(KST).date().isoformat() == today]
    bits = []
    if todays:
        first = todays[0]
        more = f" 외 {len(todays) - 1}건" if len(todays) > 1 else ""
        bits.append(f"오늘 일정 {len(todays)}건 (첫 일정 {_hm(first['starts_at'])[-5:]} {first['title']}{more})")
    else:
        bits.append("오늘 등록된 일정은 없어요")
    if g["pending"]:
        bits.append(f"확인할 일 {g['pending']}건")
    asking = sum(1 for t in g["tasks"] if t["status"] == "waiting_user")
    if asking:
        bits.append(f"답을 기다리는 질문 {asking}건")
    return ", ".join(bits) + "."


PROMPT = """
너는 이 사람의 개인 비서다. 지금은 하루를 시작하며 비서가 먼저 생각하는 시간이다.
아래 재료만 보고 "오늘 하면 좋을 것"을 최대 3개 골라 제안해라. JSON 하나로만.
{
  "greeting": "아침 인사 겸 오늘을 한 문장으로 (재료에 근거해서)",
  "items": [
    {"title": "제안 한 줄 (무엇을 할지, 짧게)",
     "why": "왜 오늘인지 한 문장 (재료의 어떤 일정·과제·말에서 나왔는지)",
     "kind": "task | note | reminder | event | none",
     "args": {}}
  ]
}
kind 와 args
  task      {"goal": "비서가 맡아서 할 일 한 문장"}       조사·정리·초안처럼 비서가 해 줄 수 있는 일
  reminder  {"text": "알릴 말", "due_at": "2026-11-17T15:00:00+09:00"}   오늘 이후 시각만
  note      {"text": "남길 내용"}
  event     {"title": "...", "starts_at": "...+09:00"}               사용자가 말한 약속인데 일정에 없을 때만
  none      {}                                                       사용자가 직접 할 일 (비서는 못 함)

규칙
- 재료에 실제로 있는 것에서만 나와야 한다. 재료에 없는 날짜·시각·사람·숫자를 만들어 내지 마라.
- 할 게 별로 없으면 1개만, 아무것도 없으면 items 는 [] 로 두고 greeting 으로 가볍게 인사만 해라.
- 남에게 메시지 보내기·결제·삭제는 제안하지 마라.
- 이미 일정·알림으로 잡혀 있는 걸 또 잡자고 하지 마라. 대신 그 준비를 제안해라.
- 명령하지 말고 권하는 말투로. 존댓말.
"""


def _material_text(g: dict, now: datetime) -> str:
    def lines(rows, f):
        return "\n".join("- " + f(r) for r in rows) or "- (없음)"
    return (
        f"지금: {now.strftime('%Y-%m-%d (%a) %H:%M')} (한국)\n\n"
        f"[가까운 일정]\n{lines(g['events'], lambda e: f'{_hm(e['starts_at'])} {e['title']}' + (f' @{e['place']}' if e.get('place') else ''))}\n\n"
        f"[걸어 둔 알림]\n{lines(g['reminders'], lambda r: f'{_hm(r['due_at'])} {r['text']}')}\n\n"
        f"[맡은 일]\n{lines(g['tasks'], lambda t: f'{t['goal']} ({t['status']})' + (f' 질문: {t['question']}' if t.get('question') else ''))}\n\n"
        f"[최근 메모]\n{lines(g['notes'], lambda n: (n.get('title') or '') + ' ' + str(n['content'])[:120])}\n\n"
        f"[이 사람에 대해 아는 것]\n{lines(g['facts'], lambda f: f['text'])}\n\n"
        f"[최근 대화]\n{lines(g['talk'], lambda m: ('사용자' if m['role'] == 'user' else '나') + ': ' + str(m['text'])[:160])}\n\n"
        f"확인함에 기다리는 일: {g['pending']}건"
    )


def _clean_items(raw) -> list:
    out = []
    for it in (raw or [])[:MAX_ITEMS]:
        if not isinstance(it, dict):
            continue
        title = str(it.get("title", "")).strip()[:80]
        if not title:
            continue
        kind = it.get("kind") if it.get("kind") in KINDS else "none"
        args = it.get("args") if isinstance(it.get("args"), dict) else {}
        if kind == "task" and not str(args.get("goal", "")).strip():
            args = {"goal": title}
        out.append({"id": secrets.token_hex(3), "title": title,
                    "why": str(it.get("why", "")).strip()[:160],
                    "kind": kind, "args": args, "state": "new"})
    return out


def make(sb, agent: dict, ask_llm, now=None) -> dict:
    """오늘의 제안을 만든다. 실패하면 예외를 그대로 올린다 (부르는 쪽이 횟수를 센다)."""
    from .privacy import mask
    now = now or _now()
    g = gather(sb)
    out = ask_llm([{"role": "system", "content": PROMPT},
                   {"role": "user", "content": mask(_material_text(g, now))}])
    plan = {"date": now.date().isoformat(), "at": now.isoformat(), "done": True,
            "greeting": str(out.get("greeting", "")).strip()[:160] or "좋은 아침이에요.",
            "brief": brief_line(g), "items": _clean_items(out.get("items"))}
    _save(sb, plan)
    return plan


def _fail(sb, now):
    p = load(sb)
    if not p or p.get("date") != now.date().isoformat():
        p = {"date": now.date().isoformat(), "fails": 0, "done": False, "items": []}
    p["fails"] = p.get("fails", 0) + 1
    _save(sb, p)
    return p


def maybe_run(sb, agent: dict, ask_llm, say, now=None) -> dict | None:
    """
    크론에서 부른다. 시각이 됐고 오늘 아직 안 했으면 만들고 알린다.
    say(rule, body, title, url) 로 알림을 보낸다 (tick._say).
    """
    now = now or _now()
    if not is_due(sb, agent, now):
        return None
    name = agent.get("name") or "비서"
    try:
        plan = make(sb, agent, ask_llm, now)
    except Exception as e:
        print("DAILY_FAIL", type(e).__name__, str(e)[:160])
        p = _fail(sb, now)
        if p["fails"] >= MAX_FAILS:     # AI 없이 브리핑만 보내고 오늘은 끝
            line = brief_line(gather(sb))
            p.update(done=True, greeting="좋은 아침이에요.", brief=line)
            _save(sb, p)
            say("brief", "좋은 아침이에요. " + line, f"{name} · 아침 브리핑", "/")
        return None
    first = next((i for i in plan["items"]), None)
    body = plan["brief"] + (f"\n오늘의 제안: {first['title']}" + (f" 외 {len(plan['items']) - 1}개" if len(plan["items"]) > 1 else "")
                            if first else "")
    say("brief", body, f"{name} · 오늘", "/")
    return plan


# ---------------- 사용자 쪽 ----------------
def decide(sb, item_id: str, accept: bool) -> dict:
    """
    제안 하나를 맡기거나 넘긴다. 맡기면:
      task → 과제를 만든다 (화면이 바로 달리게 한다)
      note·reminder·event → actions 등급 규칙을 그대로 지난다 (사용자가 직접 누른 것이므로 origin=user)
      none → 표시만 바꾼다 (사용자가 할 일)
    """
    from . import actions as act
    from . import tasks as tk
    plan = today_plan(sb)
    if not plan:
        raise LookupError("오늘의 제안이 없어요")
    item = next((i for i in plan.get("items", []) if i["id"] == item_id), None)
    if not item:
        raise LookupError("없는 제안이에요")
    if item["state"] != "new":
        raise ValueError("이미 정한 제안이에요")
    result = {}
    if accept:
        if item["kind"] == "task":
            t = tk.create(sb, str(item["args"].get("goal") or item["title"]))
            result = {"task": t}
        elif item["kind"] in ("note", "reminder", "event"):
            result = {"action": act.propose(sb, item["kind"], item["args"],
                                            "오늘의 제안: " + item["title"], origin="user")}
        item["state"] = "accepted"
    else:
        item["state"] = "dismissed"
    _save(sb, plan)
    return {"item": item, **result}
