"""
shortcuts.py — 내가 만드는 명령 버튼.

각 앱(검색·맡은 일) 안에 뜨는 버튼. 누르면 저장해 둔 명령을 바로 실행한다.
  · 검색 앱 버튼  → 그 검색어로 자율 탐색을 돌린다
  · 과제 앱 버튼  → 그 목표로 과제를 시작한다

기능(skill)과 비슷하지만 더 가볍다. 기능은 "방식(지시문) + 도구"까지 담은
재사용 묶음이고, 단축 버튼은 "이 명령 한 줄을 다시"에 가깝다.
"""
from .privacy import mask

APPS = ("search", "tasks")


def create(sb, app: str, label: str, text: str) -> dict:
    if app not in APPS:
        raise ValueError("검색 또는 맡은 일 앱에만 버튼을 만들 수 있어요")
    label = mask(str(label or "")).strip()[:20]
    text = mask(str(text or "")).strip()[:300]
    if not label or len(text) < 2:
        raise ValueError("버튼 이름과 내용을 적어 주세요")
    kind = "search" if app == "search" else "task"
    icon = "search" if kind == "search" else "bolt"
    return sb.table("shortcuts").insert({
        "app": app, "label": label, "kind": kind, "text": text, "icon": icon}).execute().data[0]


def list_for(sb, app: str) -> list:
    return sb.table("shortcuts").select("*").eq("app", app) \
             .order("id", desc=False).execute().data or []


def get(sb, sid: int):
    rows = sb.table("shortcuts").select("*").eq("id", int(sid)).limit(1).execute().data
    return rows[0] if rows else None


def remove(sb, sid: int) -> dict:
    if not get(sb, sid):
        raise ValueError("없는 버튼이에요")
    sb.table("shortcuts").delete().eq("id", sid).execute()
    return {"removed": sid}


def run(sb, sid: int):
    """버튼을 누르면: 과제면 과제를 만들고, 검색이면 과제로 감싸서 탐색하게 한다."""
    from . import tasks as tk
    s = get(sb, sid)
    if not s:
        raise ValueError("없는 버튼이에요")
    sb.table("shortcuts").update({"uses": (s.get("uses") or 0) + 1}).eq("id", sid).execute()
    if s["kind"] == "task":
        return tk.create(sb, s["text"])
    # 검색 버튼: "~ 찾아서 정리" 과제로 만들어, 자율 탐색 + 정리까지 가게 한다
    return tk.create(sb, f"{s['text']} — 최신 정보를 찾아 핵심만 정리", icon="search", tint="indigo")
