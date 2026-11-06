"""
apps.py — 홈 화면 앱들이 다루는 항목.

  일정  events         채팅에서도, 일정 앱에서도 만들고 고친다
  알림  reminders      혼자 있거나, 일정에 붙는다 (offset_min = 일정 몇 분 전)
  메모  task_memory(note)   일정에 붙을 수 있다
  초안  task_memory(draft)

연동 규칙
  · 일정 시각이 바뀌면, 그 일정에 "몇 분 전"으로 붙은 알림이 같이 움직인다.
  · 일정을 지우면 붙은 알림도 지워진다 (DB on delete cascade).
    붙은 메모는 남고 연결만 풀린다 (on delete set null). 메모는 사용자의 글이니까.
  · 이미 보낸 알림은 움직이지 않는다.

여기 항목들은 업무 기억이다. 사용자 사실(facts)로 흡수되지 않는다.
"""
from datetime import datetime, timedelta, timezone

from .privacy import mask


def _now():
    return datetime.now(timezone.utc)


def _iso(v, field="시각") -> str:
    try:
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"{field} 형식이 맞지 않아요")
    if d.tzinfo is None:
        raise ValueError(f"{field}에 시간대가 없어요")
    return d.isoformat()


def _one(sb, table, item_id):
    rows = sb.table(table).select("*").eq("id", int(item_id)).limit(1).execute().data
    if not rows:
        raise LookupError("없는 항목이에요")
    return rows[0]


def _text(v, n, field):
    t = mask(str(v or "")).strip()
    if not t:
        raise ValueError(f"{field}을(를) 적어 주세요")
    return t[:n]


# ======================= 일정 =======================
def list_events(sb, start: str | None = None, end: str | None = None) -> list:
    q = sb.table("events").select("*")
    if start:
        q = q.gte("starts_at", _iso(start))
    if end:
        q = q.lte("starts_at", _iso(end))
    return q.order("starts_at", desc=False).limit(200).execute().data or []


def get_event(sb, event_id) -> dict:
    ev = _one(sb, "events", event_id)
    ev["reminders"] = sb.table("reminders").select("*").eq("event_id", ev["id"]) \
                        .order("due_at", desc=False).execute().data or []
    ev["notes"] = sb.table("task_memory").select("id,title,content,updated_at") \
                    .eq("event_id", ev["id"]).eq("kind", "note").execute().data or []
    return ev


def create_event(sb, data: dict, by="user") -> dict:
    row = {"title": _text(data.get("title"), 100, "일정 이름"),
           "starts_at": _iso(data.get("starts_at"), "시작 시각"),
           "all_day": bool(data.get("all_day")), "created_by": by}
    if data.get("ends_at"):
        row["ends_at"] = _iso(data["ends_at"], "끝 시각")
    if data.get("place"):
        row["place"] = mask(str(data["place"]))[:100]
    ev = sb.table("events").insert(row).execute().data[0]
    # 연동: 만들 때 "몇 분 전 알림"을 같이 걸 수 있다
    if data.get("remind_before_min") not in (None, ""):
        add_event_reminder(sb, ev, int(data["remind_before_min"]), by=by)
    if str(data.get("note") or "").strip():
        create_note(sb, {"content": data["note"], "event_id": ev["id"]}, by=by)
    return ev


def add_event_reminder(sb, ev: dict, minutes: int, by="user") -> dict:
    minutes = max(0, min(10080, int(minutes)))
    start = datetime.fromisoformat(str(ev["starts_at"]).replace("Z", "+00:00"))
    if minutes == 0:
        label = "지금 시작"
    elif minutes % 1440 == 0:
        label = f"{minutes // 1440}일 전"
    elif minutes % 60 == 0:
        label = f"{minutes // 60}시간 전"
    else:
        label = f"{minutes}분 전"
    return sb.table("reminders").insert({
        "text": f"{ev['title']} ({label})"[:300],
        "due_at": (start - timedelta(minutes=minutes)).isoformat(),
        "event_id": ev["id"], "offset_min": minutes, "status": "scheduled",
        "created_by": by}).execute().data[0]


def update_event(sb, event_id, data: dict) -> dict:
    old = _one(sb, "events", event_id)
    old_title = old["title"]
    patch = {"updated_at": _now().isoformat()}
    if "title" in data:
        patch["title"] = _text(data["title"], 100, "일정 이름")
    if "starts_at" in data:
        patch["starts_at"] = _iso(data["starts_at"], "시작 시각")
    if "ends_at" in data:
        patch["ends_at"] = _iso(data["ends_at"], "끝 시각") if data["ends_at"] else None
    if "place" in data:
        patch["place"] = mask(str(data["place"] or ""))[:100] or None
    if "all_day" in data:
        patch["all_day"] = bool(data["all_day"])
    ev = sb.table("events").update(patch).eq("id", event_id).execute().data[0]

    # 연동: 시각이 바뀌면 붙은 알림이 따라 움직인다 (보내지 않은 것만)
    if "starts_at" in patch or "title" in patch:
        start = datetime.fromisoformat(str(ev["starts_at"]).replace("Z", "+00:00"))
        rows = sb.table("reminders").select("*").eq("event_id", ev["id"]) \
                 .eq("status", "scheduled").execute().data or []
        for r in rows:
            if r.get("offset_min") is None:
                continue
            sb.table("reminders").update({
                "due_at": (start - timedelta(minutes=r["offset_min"])).isoformat(),
                "text": r["text"].replace(old_title, ev["title"], 1),
                "updated_at": _now().isoformat()}).eq("id", r["id"]).execute()
    return ev


def delete_event(sb, event_id) -> dict:
    ev = _one(sb, "events", event_id)
    sb.table("events").delete().eq("id", ev["id"]).execute()
    return {"deleted": ev["id"]}


# ======================= 알림 =======================
def list_reminders(sb, include_past=False) -> list:
    q = sb.table("reminders").select("*")
    if not include_past:
        q = q.in_("status", ["scheduled", "sending"])
    return q.order("due_at", desc=not include_past).limit(100).execute().data or []


def create_reminder(sb, data: dict, by="user") -> dict:
    row = {"text": _text(data.get("text"), 300, "알림 내용"),
           "due_at": _iso(data.get("due_at"), "알림 시각"), "status": "scheduled",
           "created_by": by}
    if data.get("event_id"):
        _one(sb, "events", data["event_id"])          # 내 일정인지 확인
        row["event_id"] = int(data["event_id"])
    return sb.table("reminders").insert(row).execute().data[0]


def update_reminder(sb, reminder_id, data: dict) -> dict:
    r = _one(sb, "reminders", reminder_id)
    if r["status"] != "scheduled":
        raise ValueError("이미 보냈거나 취소한 알림은 고칠 수 없어요")
    patch = {"updated_at": _now().isoformat()}
    if "text" in data:
        patch["text"] = _text(data["text"], 300, "알림 내용")
    if "due_at" in data:
        patch["due_at"] = _iso(data["due_at"], "알림 시각")
        patch["offset_min"] = None      # 직접 시각을 정하면 일정을 따라가지 않는다
    return sb.table("reminders").update(patch).eq("id", r["id"]).execute().data[0]


def delete_reminder(sb, reminder_id) -> dict:
    r = _one(sb, "reminders", reminder_id)
    sb.table("reminders").delete().eq("id", r["id"]).execute()
    return {"deleted": r["id"]}


def claim_due(sb, now_iso: str) -> list:
    """
    보낼 차례인 알림을 '가져간다'. scheduled → sending 으로 바꾸는 데 성공한 것만 돌려준다.
    1분 틱과 10분 틱이 겹쳐도, 한 알림은 한쪽만 가져가므로 두 번 가지 않는다.
    """
    due = sb.table("reminders").select("id").eq("status", "scheduled") \
            .lte("due_at", now_iso).limit(10).execute().data or []
    got = []
    for d in due:
        rows = sb.table("reminders").update({"status": "sending"}) \
                 .eq("id", d["id"]).eq("status", "scheduled").execute().data
        if rows:
            got.append(rows[0])
    return got


def mark_sent(sb, reminder_id):
    sb.table("reminders").update({"status": "sent", "sent_at": _now().isoformat()}) \
      .eq("id", reminder_id).execute()


# ======================= 메모·초안 =======================
def list_memory(sb, kind: str) -> list:
    return sb.table("task_memory").select("*").eq("kind", kind) \
             .order("updated_at", desc=True).limit(100).execute().data or []


def create_note(sb, data: dict, by="user") -> dict:
    row = {"kind": "note", "content": _text(data.get("content"), 4000, "메모 내용")}
    if data.get("title"):
        row["title"] = mask(str(data["title"]))[:60]
    if data.get("event_id"):
        _one(sb, "events", data["event_id"])
        row["event_id"] = int(data["event_id"])
    return sb.table("task_memory").insert(row).execute().data[0]


def update_memory(sb, item_id, data: dict) -> dict:
    m = _one(sb, "task_memory", item_id)
    patch = {"updated_at": _now().isoformat()}
    if "content" in data:
        patch["content"] = _text(data["content"], 4000, "내용")
    if "title" in data:
        patch["title"] = mask(str(data["title"] or ""))[:60] or None
    if "event_id" in data:
        if data["event_id"]:
            _one(sb, "events", data["event_id"])
            patch["event_id"] = int(data["event_id"])
        else:
            patch["event_id"] = None
    return sb.table("task_memory").update(patch).eq("id", m["id"]).execute().data[0]


def delete_memory(sb, item_id) -> dict:
    m = _one(sb, "task_memory", item_id)
    sb.table("task_memory").delete().eq("id", m["id"]).execute()
    return {"deleted": m["id"]}


# ======================= 홈 화면 요약 =======================
def today(sb) -> dict:
    kst = timezone(timedelta(hours=9))
    start = datetime.now(kst).replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    events = sb.table("events").select("id,title,starts_at,place") \
               .gte("starts_at", start.isoformat()).lt("starts_at", end.isoformat()) \
               .order("starts_at", desc=False).limit(5).execute().data or []
    nxt = sb.table("reminders").select("id,text,due_at").eq("status", "scheduled") \
            .gte("due_at", _now().isoformat()).order("due_at", desc=False).limit(1).execute().data or []
    pending = sb.table("actions").select("id", count="exact").eq("status", "proposed").execute().count or 0
    asking = sb.table("tasks").select("id", count="exact").eq("status", "waiting_user").execute().count or 0
    return {"events": events, "next_reminder": nxt[0] if nxt else None,
            "pending": pending, "asking": asking}
