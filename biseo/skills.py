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

ALLOWED_TOOLS = ("search", "note", "reminder", "draft")

TOOL_LABEL = {"search": "검색", "note": "메모", "reminder": "알림", "draft": "초안"}

BUILTIN = [
    {"key": "search",   "name": "검색",      "description": "모르는 최신 정보를 찾아봐요"},
    {"key": "note",     "name": "메모",      "description": "기억해 둘 일을 적어 둬요"},
    {"key": "reminder", "name": "알림 예약", "description": "정한 시각에 폰으로 알려 줘요"},
    {"key": "draft",    "name": "글 초안",   "description": "메시지나 글을 대신 써 둬요"},
    {"key": "task",     "name": "과제 맡기", "description": "여러 걸음이 필요한 일을 틈틈이 진행해요"},
]


def _clean_tools(tools) -> list:
    if not isinstance(tools, (list, tuple)):
        return []
    return sorted({str(t) for t in tools if str(t) in ALLOWED_TOOLS})


def create(sb, name: str, description: str, instructions: str, tools, origin="user") -> dict:
    name = mask(str(name or "")).strip()[:20]
    instructions = mask(str(instructions or "")).strip()[:1500]
    if not name or len(instructions) < 5:
        raise ValueError("기능 이름과 하는 일을 적어 주세요")
    if sb.table("skills").select("id").eq("name", name).limit(1).execute().data:
        raise ValueError(f"'{name}' 이름의 기능이 이미 있어요")
    return sb.table("skills").insert({
        "name": name, "description": mask(str(description or "")).strip()[:200],
        "instructions": instructions, "tools": _clean_tools(tools),
        "origin": origin}).execute().data[0]


def get(sb, skill_id):
    try:
        sid = int(skill_id)
    except (TypeError, ValueError):
        return None
    rows = sb.table("skills").select("*").eq("id", sid).limit(1).execute().data
    return rows[0] if rows else None


def list_custom(sb) -> list:
    return sb.table("skills").select("id,name,description,tools,origin,uses,created_at") \
             .order("id", desc=False).execute().data or []


def remove(sb, skill_id: int) -> dict:
    if not get(sb, skill_id):
        raise ValueError("없는 기능이에요")
    sb.table("skills").delete().eq("id", skill_id).execute()
    return {"removed": skill_id}


def mark_used(sb, skill: dict):
    sb.table("skills").update({"uses": (skill.get("uses") or 0) + 1}).eq("id", skill["id"]).execute()


def for_prompt(sb) -> str:
    """대화 쪽 LLM 이 '어떤 추가 기능이 있는지' 알게 하는 짧은 목록."""
    rows = list_custom(sb)
    if not rows:
        return ""
    return "\n\n[추가 기능 — start_task 에 skill_id 를 넣어 쓸 수 있다]\n" + "\n".join(
        f"#{r['id']} {r['name']}: {r['description']}" for r in rows[:15])
