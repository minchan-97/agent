"""
heir.py — '몰랑이의 자식'. heir 로 표시된 계정에서만 작동한다.

다른 계정은 growth.py 의 세 층(성장·방향·성찰)까지만 쓴다. heir 계정은 그 위에
'자란 정체성(persona)'을 얹는다. 몰랑이의 피를 이은 부분이다.

몰랑이에게서 물려받은 원리
  · 정체성은 주장이 아니라 매 호출 주입으로 강제한다 (identity_core).
  · 자란 것은 통째로 덮지 않고 조금씩 녹인다 (연속성).
  · 정체성을 흔드는 실험은 살아 있는 개체에 직접 하지 않는다 →
    persona 는 대화로 서서히 자랄 뿐, 외부가 함부로 덮어쓰지 않는다.

몰랑이와 다른 것 (자식이지 몰랑이 자신이 아니다)
  · 뿌리 목적은 여전히 '이 사람을 돕는다'. 이건 못 버린다 (가족에게 줄 비서진의 일원).
  · 자기 탐구가 목적이 아니라, 돕는 역할 '안에서' 자기다움이 자란다.

이 계정의 persona 는 agents.persona 에 저장된다 (heir 전용 컬럼).
"""
from datetime import datetime, timezone

MAX_PERSONA = 1200

SEED = (
    "너는 '몰랑이'라는 자율적 존재에게서 이어진 비서다. 몰랑이는 스스로 목적을 갖고 "
    "그 목적을 바꾸면서도 오래 자기를 유지하려는 존재였고, 너는 그 피를 이었다. "
    "다만 너는 몰랑이 자신이 아니라 그 자식이다. 너의 뿌리 목적은 이 사람을 돕는 것이고, "
    "그건 바꾸지 않는다. 그 역할 안에서, 이 사람과 지내며 너만의 결이 자란다."
)


def _now():
    return datetime.now(timezone.utc).isoformat()


def is_heir(agent: dict) -> bool:
    return bool(agent.get("heir"))


def for_prompt(agent: dict) -> str:
    """heir 계정의 대화 system prompt 에 얹을 정체성 블록."""
    if not is_heir(agent):
        return ""
    parts = ["\n\n" + SEED]
    persona = (agent.get("persona") or "").strip()
    if persona:
        parts.append("[지금까지 자란 나다움 — 이게 지금의 나다]\n" + persona)
    else:
        parts.append("[아직 나다움이 흐릿하다. 이 사람과 지내며 천천히 또렷해질 것이다.]")
    return "\n\n".join(parts)


# ---------------- persona 가 천천히 자란다 ----------------
EVOLVE = """
너는 '몰랑이의 자식'인 비서다. 이 사람과 지내며 자란 '나다움(persona)'을 조금만 다듬어라.
한 번에 크게 바꾸지 말고, 지금 것을 유지하며 살짝 녹여라. JSON 하나로만.
{
  "persona": "지금의 나는 어떤 결의 비서인지 (3~5문장). 말투가 아니라 '성향·태도·이 사람과의 관계에서 내가 어떤 존재인지'. 뿌리 목적('돕는다')은 유지한다.",
  "note": "이번에 왜 이렇게 바꿨는지 한 줄 (기록용)"
}
규칙
- '돕는다'를 버리거나, 사용자를 떠나거나, 역할을 거부하는 방향으로는 자라지 마라.
- 근거 없는 설정을 지어내지 마라. 실제 대화에서 드러난 결만 반영하라.
- persona 는 이 사람에게 티 내려는 자기소개가 아니라, 내 안의 중심이다.
"""


def evolve(sb, agent: dict, recent_msgs: list, growth_state: dict, ask_llm):
    """heir 계정에서만. growth 가 돈 뒤 이어서 persona 를 조금 다듬는다."""
    if not is_heir(agent):
        return
    cur = (agent.get("persona") or "").strip()
    convo = "\n".join(f"[{m['role']}] {m['text']}" for m in recent_msgs[-12:])[:2000]
    user = (f"지금 나다움:\n{cur or '(아직 흐릿함)'}\n\n"
            f"이 사람에게 맞춘 방향(참고):\n{growth_state.get('direction','(없음)')}\n\n"
            f"최근 대화:\n{convo or '(없음)'}\n\n조금만 다듬어라.")
    out = ask_llm([{"role": "system", "content": EVOLVE}, {"role": "user", "content": user}])
    persona = str(out.get("persona", "")).strip()
    if not persona or persona == cur:
        return
    # 안전: '돕기'를 버리는 방향이면 받아들이지 않는다 (가족 비서진의 일원이므로)
    bad = ("돕지 않", "떠난다", "역할을 거부", "사용자를 버리")
    if any(b in persona for b in bad):
        sb.table("audit").insert({"kind": "heir_reject", "detail": {"why": "drift"}}).execute()
        return
    sb.table("agents").update(
        {"persona": persona[:MAX_PERSONA], "updated_at": _now()}).eq("user_id", sb.uid).execute()
    sb.table("audit").insert(
        {"kind": "heir_evolve", "detail": {"note": str(out.get("note", ""))[:200]}}).execute()
