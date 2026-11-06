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
from . import tasks as tk
from .db import Scoped

KST = timezone(timedelta(hours=9))
MAX_PER_DAY = 6
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

    # 3. 버튼 제안 — 자주 쓰는 명령이 있으면 하나 올린다 (확인함으로)
    if allow_task:
        try:
            observe.maybe_suggest(sb)
        except Exception:
            pass

    # 4. 오래 기다리는 확인 — 하루 한 번
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
    agents = (raw_sb.table("agents").select("user_id,name,ntfy_topic")
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
