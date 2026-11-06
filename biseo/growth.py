"""
growth.py — 비서가 쓸수록 그 사람에게 맞게 자라는 세 층. 모든 계정 공통.

몰랑이에서 물려받은 것: "자라는 상태는 계정마다 하나씩"(agent_state), 그리고
변화는 한 번에 통째로 덮지 않고 조금씩 녹여 간다(마르코프·SOM이 그랬듯).

세 층
  1. 계정별 성장 (growth.style)
     이 사람이 선호하는 말투·호칭·답 길이. 대화가 쌓이며 천천히 조정된다.
  2. 자기 목적 (growth.direction)
     "이 사람을 이렇게 돕는다"는 비서 나름의 방향. 고정 목적(돕기) 위에 얹히는,
     이 사람에 한정된 방향성. 가족 계정에선 방향을 바꿀 뿐 '돕기' 자체는 못 버린다.
  3. 자기 성찰 (growth.reflection)
     "요즘 내가 이 사람을 잘 돕고 있나"를 가끔 돌아본 메모. 사용자에게 티 내지 않고
     다음 응답의 태도에만 반영한다.

heir(몰랑이의 자식) 계정에선 이 셋이 더 깊게 작동하고 persona 와 묶인다.
그 깊은 부분은 heir.py 가 담당하고, 여기서는 공통 바탕만 둔다.
"""
import json
from datetime import datetime, timezone

MAX_STYLE = 600
MAX_DIRECTION = 500
MAX_REFLECTION = 600


def _now():
    return datetime.now(timezone.utc).isoformat()


def load(sb) -> dict:
    rows = sb.table("agent_state").select("key,value") \
             .in_("key", ["growth.style", "growth.direction", "growth.reflection"]) \
             .execute().data or []
    out = {}
    for r in rows:
        v = r["value"]
        out[r["key"].split(".", 1)[1]] = v.get("text", "") if isinstance(v, dict) else str(v)
    return out


def _save(sb, key: str, text: str):
    sb.table("agent_state").upsert(
        {"key": f"growth.{key}", "value": {"text": text, "at": _now()}}).execute()


def for_prompt(sb) -> str:
    """대화 system prompt 에 덧붙일, 이 사람에게 맞춰진 바탕."""
    g = load(sb)
    parts = []
    if g.get("style"):
        parts.append("[이 사람에게 맞춘 방식]\n" + g["style"])
    if g.get("direction"):
        parts.append("[이 사람을 돕는 나의 방향]\n" + g["direction"])
    if g.get("reflection"):
        parts.append("[요즘 내가 스스로 챙기는 것 — 티 내지 말고 태도로만]\n" + g["reflection"])
    return ("\n\n" + "\n\n".join(parts)) if parts else ""


# ---------------- 천천히 자라기 ----------------
GROW = """
너는 한 사람의 비서다. 최근 대화를 보고, 이 사람에게 맞춰 '네 바탕'을 조금만 다듬어라.
한 번에 크게 바꾸지 말고, 기존 것을 유지하며 살짝 녹여라. JSON 하나로만.
{
  "style": "이 사람이 선호하는 말투·호칭·답 길이 (2~3문장, 기존 것 반영해 갱신)",
  "direction": "이 사람을 어떻게 도우면 좋은지 나의 방향 (2~3문장). 단, '돕는다'는 목적 자체는 바꾸지 마라",
  "reflection": "요즘 내가 더 신경 쓸 점 한두 가지 (사용자에게 드러내지 않는다)"
}
근거 없는 추측은 넣지 마라. 최근 대화에서 실제로 보인 것만.
"""

LIMITS = {"style": MAX_STYLE, "direction": MAX_DIRECTION, "reflection": MAX_REFLECTION}


def grow(sb, recent_msgs: list, ask_llm, deep: bool = False):
    """가끔(크론) 호출돼 세 바탕을 조금씩 갱신한다. deep=heir 계정."""
    cur = load(sb)
    convo = "\n".join(f"[{m['role']}] {m['text']}" for m in recent_msgs[-12:])[:2500]
    sys = GROW
    if deep:
        sys += ("\n이 계정은 특별하다. direction 과 reflection 을 더 깊게, 네가 어떤 존재로 "
                "이 사람 곁에 있고 싶은지까지 담아도 된다. 하지만 여전히 '돕는다'를 버리지 마라.")
    user = (f"지금 내 바탕:\nstyle: {cur.get('style','(없음)')}\n"
            f"direction: {cur.get('direction','(없음)')}\n"
            f"reflection: {cur.get('reflection','(없음)')}\n\n"
            f"최근 대화:\n{convo or '(없음)'}\n\n조금만 다듬어라.")
    out = ask_llm([{"role": "system", "content": sys}, {"role": "user", "content": user}])
    for k in ("style", "direction", "reflection"):
        v = str(out.get(k, "")).strip()
        if v and v != cur.get(k):
            _save(sb, k, v[:LIMITS[k]])
    sb.table("audit").insert({"kind": "growth", "detail": {"deep": deep}}).execute()
