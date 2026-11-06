"""
tick.py — pg_cron 이 10분마다 깨우면, 사람마다 한 걸음씩.

틱은 두 종류
  1분 틱  (?only=due)  시각이 된 알림만. LLM 을 안 불러서 싸고 빠르다
  10분 틱              아래 전부

순서 (사람마다)
  1. due      예약한 알림 시각이 됐으면 보낸다
  2. task     열린 과제 하나를 한 걸음 진행한다 ← 대화 밖에서 일하는 부분
  3. waiting  확인함에 3시간 넘게 기다리는 일이 있으면 하루 한 번 알린다

말을 걸지 여부와 이유는 규칙이 정한다 (몰랑이 outbox 원칙).
과제 걸음에서 나온 알림도 같은 하루 상한을 따른다. 단, 직접 걸어 둔 알림(due)은 예외.

service role 로 돌지만, 모든 접근은 Scoped 를 거쳐 그 사람 행만 건드린다.
"""
import time
from datetime import datetime, timedelta, timezone

from . import notify
from . import push
from . import observe
from . import growth
from . import heir
from . import tasks as tk
from .db import Scoped

KST = timezone(timedelta(hours=9))
MAX_PER_DAY = 6
BRIEF_HOUR = 7          # 아침 브리핑을 보낼 시각 (이 시각 이후 첫 틱)
WAIT_HOURS = 3
TIME_BUDGET = 45      # 초. Vercel 함수 시간 안에 끝나도록


def _today_start():
    return datetime.now(KST).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()


def _sent_today(sb) -> int:
    r = (sb.table("outbox").select("id", count="exact").eq("notified", True)
         .gte("created_at", _today_start()).execute())
    return r.count or 0


def _say(sb, agent: dict, rule: str, body: str, title: str, url: str = "/", force=False) -> bool:
    """
    앱 자체 알림이 먼저. 알림을 켠 기기가 하나도 없을 때만 ntfy 로 보낸다.
    sb 는 Scoped(service role) 이고, push 는 그 안의 raw 클라이언트로 그 사람 기기만 찾는다.
    """
    if not force and _sent_today(sb) >= MAX_PER_DAY:
        sb.table("outbox").insert({"rule": rule, "body": body, "notified": False}).execute()
        return False
    ok = False
    try:
        res = push.send(sb.raw, sb.uid, title, body, url)
        ok = res.get("sent", 0) > 0
        no_device = res.get("devices", 0) == 0
    except Exception:
        no_device = True
    if not ok and no_device and agent.get("ntfy_topic"):
        ok = bool(notify.send(agent["ntfy_topic"], title, body).get("ok"))
    sb.table("outbox").insert({"rule": rule, "body": body, "notified": ok}).execute()
    return ok


def _waiting_text(sb, count: int) -> str:
    """'확인할 일 n개'가 아니라 무엇인지 보이게: 첫 항목 이름 + 나머지 수."""
    from . import actions as act
    rows = sb.table("actions").select("kind,args").eq("status", "proposed") \
             .order("id", desc=False).limit(1).execute().data or []
    if not rows:
        return f"확인을 기다리는 일이 {count}개 있어요."
    r = rows[0]
    g = r.get("args") or {}
    what = act.KINDS.get(r["kind"], {}).get("label", r["kind"])
    detail = g.get("title") or g.get("name") or g.get("text") or g.get("goal") or ""
    head = f"{what} ‘{str(detail)[:24]}’" if detail else what
    return head + (f" 외 {count - 1}개가 확인을 기다려요." if count > 1 else "이(가) 확인을 기다려요.")


def _grow_once(sb, agent: dict, ask_llm):
    """하루 한 번만, 대화가 있었으면 바탕을 다듬는다."""
    already = (sb.table("outbox").select("id").eq("rule", "grew")
               .gte("created_at", _today_start()).limit(1).execute().data)
    if already:
        return
    msgs = (sb.table("episodes").select("role,text").order("id", desc=True)
            .limit(12).execute().data or [])[::-1]
    if len(msgs) < 2:
        return
    try:
        conv = [{"role": m["role"], "text": m["text"]} for m in msgs]
        growth.grow(sb, conv, ask_llm, deep=bool(agent.get("heir")))
        if agent.get("heir"):
            heir.evolve(sb, agent, conv, growth.load(sb), ask_llm)
        # 하루 한 번 표시 (알림은 아님 → notified=False 로 기록만)
        sb.table("outbox").insert({"rule": "grew", "body": "", "notified": False}).execute()
    except Exception:
        pass


def _morning_brief(sb, agent: dict):
    """아침에 오늘 일정·확인할 일을 한 줄로 먼저 건넨다. 규칙이 정한다 (LLM 없음)."""
    now = datetime.now(KST)
    if now.hour < BRIEF_HOUR:
        return
    already = (sb.table("outbox").select("id").eq("rule", "brief")
               .gte("created_at", _today_start()).limit(1).execute().data)
    if already:
        return
    from . import apps
    h = apps.today(sb)
    evs = h.get("events") or []
    bits = []
    if evs:
        first = evs[0]
        when_s = first["starts_at"]
        try:
            hm = datetime.fromisoformat(str(when_s).replace("Z", "+00:00")).astimezone(KST).strftime("%H:%M")
        except ValueError:
            hm = ""
        more = f" 외 {len(evs) - 1}건" if len(evs) > 1 else ""
        bits.append(f"오늘 일정 {len(evs)}건 (첫 일정 {hm} {first['title']}{more})")
    else:
        bits.append("오늘 등록된 일정은 없어요")
    if h.get("pending"):
        bits.append(f"확인할 일 {h['pending']}건")
    if h.get("asking"):
        bits.append(f"답을 기다리는 질문 {h['asking']}건")
    name = agent.get("name") or "비서"
    _say(sb, agent, "brief", "좋은 아침이에요. " + ", ".join(bits) + ".", f"{name} · 아침 브리핑", "/")


def step_user(sb, agent: dict, ask_llm, allow_task=True, only_due=False) -> dict:
    name = agent.get("name") or "비서"
    now = datetime.now(timezone.utc)
    out = {"due": 0, "task": None, "waiting": 0}

    # 1. 시각이 된 알림 — 가져가기(claim)에 성공한 것만 보낸다. 틱이 겹쳐도 한 번만 간다
    from . import apps
    for r in apps.claim_due(sb, now.isoformat()):
        url = f"/?open=events:{r['event_id']}" if r.get("event_id") else "/?open=reminders"
        _say(sb, agent, "due", r["text"], f"{name} · 알림", url, force=True)
        apps.mark_sent(sb, r["id"])
        out["due"] += 1

    if only_due:          # 1분 틱: 알림만 보고 끝 (LLM 안 부름)
        return out

    # 2. 과제 한 걸음
    if allow_task:
        tid = tk.pick_next(sb)
        if tid:
            res = tk.step(sb, agent, tid, ask_llm)
            out["task"] = {"id": tid, "status": (res["task"] or {}).get("status")}
            if res.get("notify"):
                _say(sb, agent, "task", res["notify"], f"{name} · 맡은 일", "/?open=tasks")

    # 3. 성장 — 하루 한 번, 최근 대화로 바탕을 조금 다듬는다 (heir 면 더 깊게)
    if allow_task:
        _grow_once(sb, agent, ask_llm)

    # 4. 아침 브리핑 — 하루 한 번, 아침에 먼저 말을 건다
    _morning_brief(sb, agent)

    # 5. 버튼·홈 제안 — 자주 쓰는 걸 하나 올린다 (확인함으로)
    if allow_task:
        try:
            if not observe.maybe_suggest(sb):
                observe.maybe_pin(sb)
        except Exception:
            pass

    # 6. 오래 기다리는 확인 — 하루 한 번
    cutoff = (now - timedelta(hours=WAIT_HOURS)).isoformat()
    waiting = (sb.table("actions").select("id", count="exact").eq("status", "proposed")
               .lte("created_at", cutoff).execute())
    if waiting.count:
        already = (sb.table("outbox").select("id").eq("rule", "waiting")
                   .gte("created_at", _today_start()).limit(1).execute().data)
        if not already:
            _say(sb, agent, "waiting", _waiting_text(sb, waiting.count), f"{name} · 확인함", "/?open=inbox")
            out["waiting"] = waiting.count
    return out


def run(raw_sb, ask_llm, only_due=False) -> dict:
    started = time.monotonic()
    agents = (raw_sb.table("agents").select("user_id,name,ntfy_topic,heir")
              .not_.is_("name", "null").execute().data) or []
    report = {}
    for a in agents:
        key = a["user_id"][:8]
        try:
            sb = Scoped(raw_sb, a["user_id"])
            # 시간이 모자라면 알림만 처리하고 과제 걸음은 다음 틱으로 미룬다
            allow = time.monotonic() - started < TIME_BUDGET
            report[key] = step_user(sb, a, ask_llm, allow_task=allow, only_due=only_due)
        except Exception as e:  # 한 사람이 실패해도 다른 사람은 계속
            report[key] = {"error": str(e)[:160]}
    return report
