"""
absorb.py — 사용자에 대한 사실 흡수 (몰랑이 molang_store 의 관문을 물려받음).

원칙 (몰랑이에서 오염을 겪고 얻은 것)
  1. 사용자 본인의 말에서 나온 것만 사실이 된다.
     LLM 이 "사용자 말에서 뽑았다"고 해도, 실제로는 자기 답을 섞을 수 있다.
     그래서 답변과 많이 겹치면 검역소로 보낸다 (assistant echo).
  2. 같은 말은 새로 쌓지 않고 seen 만 올린다.
  3. 상태(state)는 만료가 있다.
  4. 업무 기록은 여기를 지나가지 않는다. 아예 다른 테이블이다.
"""
from datetime import datetime, timezone

from .norm import normalize, jaccard, is_agent_echo, classify_kind, TTL

ECHO_OVERLAP = 0.6


def absorb(sb, candidates: list, user_text: str, reply: str, agent_name: str) -> dict:
    stats = {"added": 0, "seen": 0, "quarantined": 0}
    for raw in candidates[:5]:
        text = (raw or "").strip()
        if len(text) < 4:
            continue
        # 사용자 말과 거의 안 겹치는데 답변과는 겹친다 → 비서 말이 섞인 것
        if is_agent_echo(text, agent_name) or (
                jaccard(text, reply) >= ECHO_OVERLAP and jaccard(text, user_text) < 0.2):
            sb.table("quarantine").insert({"text": text, "reason": "assistant_echo"}).execute()
            stats["quarantined"] += 1
            continue
        key = normalize(text)[:120]
        if not key:
            continue
        found = sb.table("facts").select("id,seen").eq("norm_key", key).limit(1).execute().data
        if found:
            sb.table("facts").update({"seen": found[0]["seen"] + 1,
                                      "updated_at": datetime.now(timezone.utc).isoformat()}
                                     ).eq("id", found[0]["id"]).execute()
            stats["seen"] += 1
            continue
        kind = classify_kind(text)
        row = {"text": text, "norm_key": key, "kind": kind, "source": "user",
               "trust": "derived", "strength": 0.6}
        if TTL.get(kind):
            row["expires_at"] = (datetime.now(timezone.utc) + TTL[kind]).isoformat()
        sb.table("facts").insert(row).execute()
        stats["added"] += 1
    return stats
