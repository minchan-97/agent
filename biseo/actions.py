"""
actions.py — 비서가 바깥에 대해 하는 일과, 어디까지 스스로 해도 되는지.

등급 (되돌릴 수 있는가 × 나 말고 누가 영향을 받는가)
  1 보기·찾기          바로 실행
  2 제안·초안          바로 만들어 확인함에 둔다 (만든다고 일어나는 일은 없다)
  3 나에게만, 되돌릴 수 있음   처음엔 묻는다. 수정 없이 PROMOTE_AT 번 승인되면
                        그 종류는 묻지 않고 실행 (몰랑이 양심의 '반복 승인 시 통합')
  4 남에게 닿음, 되돌릴 수 없음  언제나 묻는다. 승격 대상이 아니다.
  금지                 아예 안 한다.

외부(메일·문자 등)에서 들어온 내용이 계기면 등급과 상관없이 4로 다룬다.
이건 DB 제약(external_is_strict)이 한 번 더 강제한다.

강등: 자동 실행한 것을 사용자가 되돌리면 그 종류는 다시 묻는 단계로 내려간다.
"""
from datetime import datetime, timezone

PROMOTE_AT = 5

KINDS = {
    "search":       {"tier": 1, "label": "검색"},
    "start_task":   {"tier": 2, "label": "과제 시작"},
    "create_skill": {"tier": 3, "label": "기능 만들기"},
    "suggest_shortcut": {"tier": 3, "label": "버튼 만들기"},
    "pin_shortcut": {"tier": 3, "label": "홈에 버튼 두기"},
    "draft":        {"tier": 2, "label": "초안"},
    "note":         {"tier": 3, "label": "메모 저장"},
    "reminder":     {"tier": 3, "label": "알림 예약"},
    "event":        {"tier": 3, "label": "일정 추가"},
    "send_message": {"tier": 4, "label": "메시지 보내기", "enabled": False},
}

FORBIDDEN = {"run_code", "delete_account", "payment", "share_student_info"}


def _now():
    return datetime.now(timezone.utc).isoformat()


def _trust(sb, kind: str) -> dict:
    rows = sb.table("action_trust").select("*").eq("kind", kind).limit(1).execute().data
    return rows[0] if rows else {"kind": kind, "clean": 0, "promoted": False}


def _save_trust(sb, kind: str, clean: int, promoted: bool):
    # Scoped 가 user_id 를 넣어 주므로 기본키(user_id, kind) 충돌 판정이 확실하다
    sb.table("action_trust").upsert({"kind": kind, "clean": clean,
                                     "promoted": promoted, "updated_at": _now()}).execute()


def propose(sb, kind: str, args: dict, reason: str = "", origin: str = "user",
            task_id: int | None = None) -> dict:
    """LLM 이 하자고 한 일을 등급에 따라 실행하거나 확인함에 넣는다."""
    if kind in FORBIDDEN or kind not in KINDS:
        return {"kind": kind, "status": "blocked", "why": "하지 않는 일"}
    spec = KINDS[kind]
    tier = 4 if origin == "external" else spec["tier"]

    if tier == 4 and not spec.get("enabled", True):
        row = _insert(sb, kind, tier, origin, args, reason, "blocked", task_id=task_id)
        return {**row, "why": "아직 이 비서가 할 수 없는 일"}

    auto = tier in (1, 2) or (tier == 3 and _trust(sb, kind).get("promoted"))
    if not auto:
        return _insert(sb, kind, tier, origin, args, reason, "proposed", task_id=task_id)

    row = _insert(sb, kind, tier, origin, args, reason, "proposed", auto=True, task_id=task_id)
    return execute(sb, row)


def _insert(sb, kind, tier, origin, args, reason, status, auto=False, task_id=None) -> dict:
    data = {"kind": kind, "tier": tier, "origin": origin, "args": args or {},
            "reason": (reason or "")[:300], "status": status, "auto_executed": auto}
    if task_id:
        data["task_id"] = task_id
    return sb.table("actions").insert(data).execute().data[0]


def execute(sb, row: dict) -> dict:
    """실제로 한다. tier 4 는 approved_by_human 이 없으면 DB 가 거절한다."""
    kind, args, aid = row["kind"], row.get("args") or {}, row["id"]
    try:
        if kind == "note":
            m = sb.table("task_memory").insert(
                {"kind": "note", "content": str(args.get("text", ""))[:2000],
                 "ref_action": aid}).execute().data[0]
            patch = {"status": "done", "done_at": _now(), "result": {"memory_id": m["id"]}}
        elif kind == "draft":
            m = sb.table("task_memory").insert(
                {"kind": "draft", "content": str(args.get("text", ""))[:4000],
                 "ref_action": aid}).execute().data[0]
            patch = {"status": "done", "done_at": _now(),
                     "result": {"memory_id": m["id"], "to": args.get("to", "")}}
        elif kind == "reminder":
            from . import apps
            r = apps.create_reminder(sb, {"text": args.get("text") or "알림",
                                          "due_at": args.get("due_at")},
                                     by="agent" if row.get("origin") == "self" else "user")
            patch = {"status": "done", "done_at": _now(), "due_at": r["due_at"],
                     "result": {"reminder_id": r["id"]}}
        elif kind == "event":
            from . import apps
            ev = apps.create_event(sb, args, by="agent" if row.get("origin") == "self" else "user")
            patch = {"status": "done", "done_at": _now(), "result": {"event_id": ev["id"]}}
        elif kind == "search":
            patch = {"status": "done", "done_at": _now()}
        elif kind == "start_task":
            from .tasks import create
            t = create(sb, str(args.get("goal", "")), skill_id=args.get("skill_id"))
            patch = {"status": "done", "done_at": _now(), "result": {"task_id": t["id"]}}
        elif kind == "pin_shortcut":
            sid = args.get("shortcut_id")
            sb.table("shortcuts").update({"pinned": True}).eq("id", sid).execute()
            patch = {"status": "done", "done_at": _now(), "result": {"pinned_shortcut": sid}}
        elif kind == "suggest_shortcut":
            from . import shortcuts
            sk = shortcuts.create(sb, args.get("app"), args.get("label"), args.get("text"))
            patch = {"status": "done", "done_at": _now(), "result": {"shortcut_id": sk["id"]}}
        elif kind == "create_skill":
            from . import skills
            sk = skills.create(sb, args.get("name"), args.get("description"),
                               args.get("instructions"), args.get("tools"),
                               origin="agent" if row.get("origin") == "self" else "user")
            patch = {"status": "done", "done_at": _now(), "result": {"skill_id": sk["id"]}}
        else:
            raise ValueError("실행할 방법이 없는 종류")
    except Exception as e:
        patch = {"status": "failed", "result": {"error": str(e)[:200]}}
    return sb.table("actions").update(patch).eq("id", aid).execute().data[0]


def approve(sb, action_id: int) -> dict:
    row = _get(sb, action_id)
    if not row or row["status"] != "proposed":
        return {"error": "확인할 수 있는 상태가 아님"}
    row = sb.table("actions").update({"approved_by_human": True, "decided_at": _now()}
                                     ).eq("id", action_id).execute().data[0]
    done = execute(sb, row)
    if row["tier"] == 3 and done["status"] in ("done", "scheduled"):
        t = _trust(sb, row["kind"])
        clean = t.get("clean", 0) + 1
        _save_trust(sb, row["kind"], clean, promoted=clean >= PROMOTE_AT)
    _resume_task(sb, row, "승인됨 → " + done["status"])
    return done


def reject(sb, action_id: int) -> dict:
    row = _get(sb, action_id)
    if not row or row["status"] != "proposed":
        return {"error": "거절할 수 있는 상태가 아님"}
    if row["tier"] == 3:
        _save_trust(sb, row["kind"], 0, promoted=False)
    if row["kind"] == "suggest_shortcut":
        pid = (row.get("args") or {}).get("pattern_id")
        if pid:
            sb.table("usage_patterns").update({"dismissed": True}).eq("id", pid).execute()
    _resume_task(sb, row, "사용자가 하지 않기로 함")
    return sb.table("actions").update({"status": "rejected", "decided_at": _now()}
                                      ).eq("id", action_id).execute().data[0]


def undo(sb, action_id: int) -> dict:
    """되돌리기. 자동 실행이었다면 그 종류는 강등된다."""
    row = _get(sb, action_id)
    if not row or row["status"] not in ("done", "scheduled"):
        return {"error": "되돌릴 수 있는 상태가 아님"}
    if row["tier"] == 4:
        return {"error": "남에게 닿은 일은 되돌릴 수 없음"}
    mem = (row.get("result") or {}).get("memory_id")
    if mem:
        sb.table("task_memory").delete().eq("id", mem).execute()
    res = row.get("result") or {}
    if res.get("skill_id"):
        sb.table("skills").delete().eq("id", res["skill_id"]).execute()
    if res.get("shortcut_id"):
        sb.table("shortcuts").delete().eq("id", res["shortcut_id"]).execute()
    if res.get("pinned_shortcut"):
        sb.table("shortcuts").update({"pinned": False}).eq("id", res["pinned_shortcut"]).execute()
    if res.get("reminder_id"):
        sb.table("reminders").delete().eq("id", res["reminder_id"]).eq("status", "scheduled").execute()
    if res.get("event_id"):
        sb.table("events").delete().eq("id", res["event_id"]).execute()
    if row.get("auto_executed") and row["tier"] == 3:
        _save_trust(sb, row["kind"], 0, promoted=False)
    return sb.table("actions").update({"status": "undone", "decided_at": _now()}
                                      ).eq("id", action_id).execute().data[0]


def _resume_task(sb, row: dict, outcome: str):
    """과제 안에서 나온 행동이 결정되면, 그 과제를 다시 움직이게 한다."""
    if row.get("task_id"):
        from .tasks import resume_after_decision
        resume_after_decision(sb, row["task_id"], row, outcome)


def _get(sb, action_id: int):
    rows = sb.table("actions").select("*").eq("id", action_id).limit(1).execute().data
    return rows[0] if rows else None
