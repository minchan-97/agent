"""
skills.py — 비서가 할 줄 아는 일.

기본 기능   코드에 있다. 지울 수 없다. 사이드바 위쪽에 잠겨서 보인다.
추가 기능   skills 테이블에 산다. 사용자가 부탁하거나, 비서가 반복되는 일을
            알아채고 제안해서(확인함 → 승인) 생긴다. 지울 수 있다.

추가 기능은 코드가 아니다
  "지시문 + 쓸 수 있는 기본 도구 목록"이다. 실행하면 그 지시문을 품은 과제가
  만들어지고, 과제는 늘 하던 대로 한 걸음씩, 등급 규칙대로 움직인다.
  그래서 기능이 늘어도 비서가 할 수 있는 행동의 범위는 넓어지지 않는다.
  (자가 코드 생성을 넣지 않기로 한 이유가 그대로 지켜진다)
"""
from .privacy import mask

ALLOWED_TOOLS = ("search", "note", "reminder", "draft", "event")

TOOL_LABEL = {"search": "검색", "note": "메모", "reminder": "알림", "draft": "초안", "event": "일정"}

BUILTIN = [
    {"key": "search",   "name": "검색",      "description": "모르는 최신 정보를 찾아봐요"},
    {"key": "note",     "name": "메모",      "description": "기억해 둘 일을 적어 둬요"},
    {"key": "reminder", "name": "알림 예약", "description": "정한 시각에 폰으로 알려 줘요"},
    {"key": "event",    "name": "일정",      "description": "일정을 넣고, 알림과 메모를 붙여요"},
    {"key": "draft",    "name": "글 초안",   "description": "메시지나 글을 대신 써 둬요"},
    {"key": "task",     "name": "과제 맡기", "description": "여러 걸음이 필요한 일을 틈틈이 진행해요"},
]


def _clean_tools(tools) -> list:
    if not isinstance(tools, (list, tuple)):
        return []
    return sorted({str(t) for t in tools if str(t) in ALLOWED_TOOLS})


APP_NAMES = ("events", "notes", "reminders", "drafts", "search", "tasks")


def create(sb, name: str, description: str, instructions: str, tools, origin="user", app=None) -> dict:
    name = mask(str(name or "")).strip()[:20]
    instructions = mask(str(instructions or "")).strip()[:1500]
    if not name or len(instructions) < 5:
        raise ValueError("기능 이름과 하는 일을 적어 주세요")
    if sb.table("skills").select("id").eq("name", name).limit(1).execute().data:
        raise ValueError(f"'{name}' 이름의 기능이 이미 있어요")
    row = {
        "name": name, "description": mask(str(description or "")).strip()[:200],
        "instructions": instructions, "tools": _clean_tools(tools),
        "origin": origin}
    if app in APP_NAMES:
        row["app"] = app
    return sb.table("skills").insert(row).execute().data[0]


def get(sb, skill_id):
    try:
        sid = int(skill_id)
    except (TypeError, ValueError):
        return None
    rows = sb.table("skills").select("*").eq("id", sid).limit(1).execute().data
    return rows[0] if rows else None


def list_for_app(sb, app: str) -> list:
    return sb.table("skills").select("id,name,description,tools,uses,icon,tint") \
             .eq("app", app).order("id", desc=False).execute().data or []


def list_custom(sb) -> list:
    return sb.table("skills").select("id,name,description,tools,origin,uses,pinned,icon,tint,app,created_at") \
             .order("id", desc=False).execute().data or []


def list_pinned(sb) -> list:
    return [s for s in list_custom(sb) if s.get("pinned")]


def set_pinned(sb, skill_id: int, on: bool) -> dict:
    if not get(sb, skill_id):
        raise ValueError("없는 기능이에요")
    sb.table("skills").update({"pinned": bool(on)}).eq("id", skill_id).execute()
    return {"id": skill_id, "pinned": bool(on)}


def run(sb, skill_id: int, text: str = ""):
    """기능 실행 = 그 기능의 방식을 품은 과제를 만든다 (홈 아이콘에서도 이걸 부른다)."""
    from . import tasks as tk
    sk = get(sb, skill_id)
    if not sk:
        raise ValueError("없는 기능이에요")
    goal = f"[{sk['name']}] {text.strip()}" if text.strip() else f"[{sk['name']}] 실행"
    return tk.create(sb, goal, skill_id=skill_id)


def remove(sb, skill_id: int) -> dict:
    if not get(sb, skill_id):
        raise ValueError("없는 기능이에요")
    sb.table("skills").delete().eq("id", skill_id).execute()
    return {"removed": skill_id}


def mark_used(sb, skill: dict):
    sb.table("skills").update({"uses": (skill.get("uses") or 0) + 1}).eq("id", skill["id"]).execute()


DESIGN = """
너는 한 앱 안에 새 '기능'을 설계한다. 기능은 코드가 아니라 '지시문 + 쓸 도구'의 조합이다.
쓸 수 있는 도구는 이것뿐이다: search(검색), note(메모), reminder(알림), draft(초안), event(일정).
이 도구들의 조합으로 할 수 있는 일만 설계해라. 그 밖의 일(코드 실행, 외부 전송 등)은 설계하지 마라.
JSON 하나로만.
{
  "ok": true/false,            // 이 도구들로 만들 수 있으면 true
  "name": "짧은 기능 이름(최대 12자)",
  "description": "한 줄 설명",
  "instructions": "이 기능을 실행할 때 비서가 따를 방식 (구체적으로)",
  "tools": ["필요한 도구만"],
  "why_not": "ok=false 일 때, 왜 이 도구들로는 안 되는지 한 줄"
}
이 기능은 '{app}' 앱에 들어간다. 그 앱의 맥락(예: 일정이면 일정·알림 중심)에 맞게 설계해라.
"""


def design_from_request(sb, app: str, request: str, ask_llm) -> dict:
    """사용자가 앱 안에서 말로 요구한 것을 기능으로 설계해 만든다 (자가 코드 없음)."""
    if app not in APP_NAMES:
        raise ValueError("이 앱에는 기능을 추가할 수 없어요")
    req = mask(str(request or "")).strip()[:300]
    if len(req) < 2:
        raise ValueError("어떤 기능을 원하는지 적어 주세요")
    out = ask_llm([{"role": "system", "content": DESIGN.replace("{app}", app)},
                   {"role": "user", "content": f"원하는 기능: {req}"}])
    if not out.get("ok"):
        raise ValueError(out.get("why_not") or "그 기능은 지금 도구로는 만들 수 없어요")
    return create(sb, out.get("name"), out.get("description"), out.get("instructions"),
                  out.get("tools"), origin="user", app=app)


def for_prompt(sb) -> str:
    """대화 쪽 LLM 이 '어떤 추가 기능이 있는지' 알게 하는 짧은 목록."""
    rows = list_custom(sb)
    if not rows:
        return ""
    return "\n\n[추가 기능 — start_task 에 skill_id 를 넣어 쓸 수 있다]\n" + "\n".join(
        f"#{r['id']} {r['name']}: {r['description']}" for r in rows[:15])
