"""
layout.py — 홈 위젯 배치 (계정마다 하나, agent_state 'home.layout').

핸드폰 홈처럼: 여러 페이지를 좌우로 넘기고, 페이지마다 위젯을 깔고 옮기고 크기를 바꾼다.
화면이 보낸 배치를 그대로 믿지 않고, 여기서 모양을 검사해 걸러서 저장한다.
  · 위젯 종류·크기는 정해진 목록 안에서만
  · 페이지 최대 MAX_PAGES 개, 페이지당 위젯 MAX_PER_PAGE 개
  · 새 기능(pin) 위젯은 이 계정의 기능·버튼 id 만 가리킨다 (남의 것을 가리켜도 RLS 가 막지만, 모양부터 거른다)
"""
import re

KEY = "home.layout"
MAX_PAGES = 5
MAX_PER_PAGE = 8
APPS = ("chat", "search", "events", "notes", "reminders", "tasks", "drafts", "skills", "inbox", "memory", "docs", "settings")

# 종류 → 가능한 크기 (첫 번째가 기본)
TYPES = {
    "hero":     ("wide",),            # 지금 가장 중요한 한 가지
    "plan":     ("wide",),            # 오늘 하면 좋을 것
    "inbox":    ("small", "wide"),
    "task":     ("small", "wide"),
    "events":   ("wide",),            # 오늘 일정 목록
    "reminder": ("small", "wide"),    # 다음 알림
    "notes":    ("small", "wide"),    # 최근 메모
    "app":      ("small",),           # 앱 바로가기
    "pin":      ("small", "wide"),    # 새 기능(사용자·비서가 만든 기능/버튼)
}

DEFAULT = {"pages": [[
    {"id": "w1", "type": "hero", "size": "wide"},
    {"id": "w2", "type": "plan", "size": "wide"},
    {"id": "w3", "type": "inbox", "size": "small"},
    {"id": "w4", "type": "task", "size": "small"},
]], "removed_pins": []}

_ID = re.compile(r"^[A-Za-z0-9_-]{1,16}$")


def _clean_widget(w: dict):
    if not isinstance(w, dict):
        return None
    t = w.get("type")
    if t not in TYPES or not _ID.match(str(w.get("id", ""))):
        return None
    size = w.get("size") if w.get("size") in TYPES[t] else TYPES[t][0]
    out = {"id": str(w["id"]), "type": t, "size": size}
    if t == "app":
        if w.get("app") not in APPS:
            return None
        out["app"] = w["app"]
    if t == "pin":
        try:
            out["ref"] = int(w.get("ref"))
        except (TypeError, ValueError):
            return None
        out["skill"] = bool(w.get("skill"))
        out["label"] = str(w.get("label") or "")[:20]
        out["icon"] = str(w.get("icon") or "bolt")[:12]
        out["origin"] = "agent" if w.get("origin") == "agent" else "user"
    return out


def clean(layout) -> dict:
    """화면에서 온 배치를 검사해 저장할 모양으로. 이상한 건 버리고, 다 비면 기본 배치."""
    if not isinstance(layout, dict):
        raise ValueError("배치 모양이 아니에요")
    pages, seen = [], set()
    for page in (layout.get("pages") or [])[:MAX_PAGES]:
        if not isinstance(page, list):
            continue
        ws = []
        for w in page:
            cw = _clean_widget(w)
            if cw and cw["id"] not in seen and len(ws) < MAX_PER_PAGE:
                seen.add(cw["id"])
                ws.append(cw)
        if ws:
            pages.append(ws)
    removed = [str(x)[:24] for x in (layout.get("removed_pins") or []) if isinstance(x, (str, int))][:100]
    return {"pages": pages or [list(DEFAULT["pages"][0])], "removed_pins": removed}


def load(sb):
    rows = sb.table("agent_state").select("value").eq("key", KEY).limit(1).execute().data
    return rows[0]["value"] if rows else None


def save(sb, layout) -> dict:
    from datetime import datetime, timezone
    v = clean(layout)
    sb.table("agent_state").upsert({"key": KEY, "value": v,
                                    "updated_at": datetime.now(timezone.utc).isoformat()}).execute()
    return v
