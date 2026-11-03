"""
tick.py — pg_cron 이 10분마다 깨우면 한 걸음씩.

몰랑이 outbox 원칙 그대로: 말을 걸지 여부와 이유는 규칙이 정한다.
이번 판에는 LLM 을 아예 부르지 않는다. 계기가 없으면 아무 말도 안 한다.

계기
  due        예약한 알림 시각이 됐다
  waiting    확인함에 3시간 넘게 기다리는 일이 있다 (하루 한 번만)

service role 로 돌기 때문에 RLS 가 없다.
→ 모든 쿼리에 user_id 를 직접 건다. 빠뜨리면 남의 행을 건드린다.
"""
from datetime import datetime, timedelta, timezone

from . import notify

KST = timezone(timedelta(hours=9))
MAX_PER_DAY = 6
WAIT_HOURS = 3


def _sent_today(sb, uid: str) -> int:
    start = datetime.now(KST).replace(hour=0, minute=0, second=0, microsecond=0)
    r = (sb.table("outbox").select("id", count="exact").eq("user_id", uid)
         .eq("notified", True).gte("created_at", start.isoformat()).execute())
    return r.count or 0


def _say(sb, agent: dict, rule: str, body: str, title: str) -> bool:
    uid = agent["user_id"]
    res = notify.send(agent.get("ntfy_topic") or "", title, body)
    sb.table("outbox").insert({"user_id": uid, "rule": rule, "body": body,
                               "notified": bool(res.get("ok"))}).execute()
    return bool(res.get("ok"))


def step_user(sb, agent: dict) -> dict:
    uid, name = agent["user_id"], agent.get("name") or "비서"
    now = datetime.now(timezone.utc)
    out = {"due": 0, "waiting": 0}

    # 1. 시각이 된 알림 — 하루 상한과 상관없이 보낸다 (사용자가 직접 걸어 둔 것이니까)
    due = (sb.table("actions").select("id,args").eq("user_id", uid)
           .eq("status", "scheduled").lte("due_at", now.isoformat()).limit(5).execute().data) or []
    for row in due:
        text = str((row.get("args") or {}).get("text", "")) or "예약해 둔 알림이에요."
        _say(sb, agent, "due", text, f"{name}의 알림")
        sb.table("actions").update({"status": "done", "done_at": now.isoformat()}
                                   ).eq("id", row["id"]).eq("user_id", uid).execute()
        out["due"] += 1

    if _sent_today(sb, uid) >= MAX_PER_DAY:
        return out

    # 2. 오래 기다리는 확인 — 하루 한 번만
    cutoff = (now - timedelta(hours=WAIT_HOURS)).isoformat()
    waiting = (sb.table("actions").select("id", count="exact").eq("user_id", uid)
               .eq("status", "proposed").lte("created_at", cutoff).execute())
    if waiting.count:
        start = datetime.now(KST).replace(hour=0, minute=0, second=0, microsecond=0)
        already = (sb.table("outbox").select("id").eq("user_id", uid).eq("rule", "waiting")
                   .gte("created_at", start.isoformat()).limit(1).execute().data)
        if not already:
            _say(sb, agent, "waiting", f"확인을 기다리는 일이 {waiting.count}개 있어요.",
                 f"{name}")
            out["waiting"] = waiting.count
    return out


def run(sb) -> dict:
    agents = (sb.table("agents").select("user_id,name,ntfy_topic")
              .not_.is_("name", "null").execute().data) or []
    report = {}
    for a in agents:
        try:
            report[a["user_id"][:8]] = step_user(sb, a)
        except Exception as e:  # 한 사람이 실패해도 다른 사람은 계속
            report[a["user_id"][:8]] = {"error": str(e)[:120]}
    return report
