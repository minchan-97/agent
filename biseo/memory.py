"""
memory.py — '비서가 나에 대해 아는 것'을 사용자가 보고 고친다.

컴패니언·비서 앱의 기억은 대개 블랙박스다. 이 비서는 반대로 간다:
  · 비서가 사용자에 대해 저장한 사실을 전부 보여 준다 (확실한 것 / 아직 확실하지 않은 것)
  · 사용자가 '맞아요'를 누르면 그때만 확실(human)이 된다 — 몰랑이 cogito anchor 그대로
  · 비서 말이 섞였을지 몰라 검역소에 보류한 것도 보여 주고, 사용자가 살리거나 버린다
  · 비서가 이 사람에게 맞춰 가는 방식(말투·방향·성찰)과, heir 면 자란 나다움도 보여 준다
  · 무엇이든 지울 수 있고, 한 번에 전부 잊게 할 수도 있다

여기서 하는 모든 변경은 사용자가 직접 누른 것이다. 그래서 source='user' 로만 쓴다.
"""
from datetime import datetime, timezone

from .norm import classify_kind, normalize, TTL
from .privacy import mask

GROWTH_KEYS = ("growth.style", "growth.direction", "growth.reflection")
GROWTH_LABEL = {"growth.style": "이 사람에게 맞춘 말투·방식",
                "growth.direction": "이 사람을 돕는 방향",
                "growth.reflection": "요즘 스스로 챙기는 것"}


def _now():
    return datetime.now(timezone.utc)


def _alive(f: dict, now) -> bool:
    if (f.get("strength") or 0) <= 0.2:
        return False
    exp = f.get("expires_at")
    if exp:
        try:
            if datetime.fromisoformat(str(exp).replace("Z", "+00:00")) <= now:
                return False
        except ValueError:
            pass
    return True


def overview(sb, agent: dict) -> dict:
    now = _now()
    facts = sb.table("facts").select("id,text,kind,trust,seen,created_at,updated_at,expires_at,strength") \
              .order("updated_at", desc=True).limit(300).execute().data or []
    facts = [f for f in facts if _alive(f, now)]
    sure = [f for f in facts if f["trust"] == "human"]
    unsure = [f for f in facts if f["trust"] != "human"]
    quarantine = sb.table("quarantine").select("id,text,reason,created_at") \
                   .order("id", desc=True).limit(50).execute().data or []
    quarantine = [q for q in quarantine if not q.get("resolved")]
    rows = sb.table("agent_state").select("key,value,updated_at").in_("key", list(GROWTH_KEYS)).execute().data or []
    growth = []
    for k in GROWTH_KEYS:
        r = next((x for x in rows if x["key"] == k), None)
        if r:
            v = r["value"]
            text = v.get("text", "") if isinstance(v, dict) else str(v)
            if text.strip():
                growth.append({"key": k, "label": GROWTH_LABEL[k], "text": text, "updated_at": r.get("updated_at")})
    out = {"sure": sure, "unsure": unsure, "quarantine": quarantine, "growth": growth}
    if agent.get("heir"):
        out["persona"] = agent.get("persona") or ""
    return out


def _one_fact(sb, fact_id):
    rows = sb.table("facts").select("*").eq("id", int(fact_id)).limit(1).execute().data
    if not rows:
        raise LookupError("없는 기억이에요")
    return rows[0]


def approve(sb, fact_id) -> dict:
    """사용자가 '맞아요' → 확실한 사실 (사람 승인 없이 human 은 없다: DB 제약 human_needs_approval)."""
    _one_fact(sb, fact_id)
    return sb.table("facts").update({"trust": "human", "approved_by_human": True, "strength": 1.0,
                                     "updated_at": _now().isoformat()}).eq("id", int(fact_id)).execute().data[0]


def _clean_text(text) -> str:
    t = mask(str(text or "")).strip()
    if len(t) < 2:
        raise ValueError("내용을 적어 주세요")
    if len(t) > 200:
        raise ValueError("200자 안으로 적어 주세요")
    return t


def update(sb, fact_id, text) -> dict:
    """사용자가 고쳐 쓴 문장은 사용자의 말이므로 바로 확실한 사실이 된다."""
    _one_fact(sb, fact_id)
    t = _clean_text(text)
    key = normalize(t)[:120] or t[:120]
    clash = sb.table("facts").select("id").eq("norm_key", key).limit(1).execute().data
    if clash and int(clash[0]["id"]) != int(fact_id):
        sb.table("facts").delete().eq("id", int(fact_id)).execute()       # 같은 말이 이미 있으면 하나로
        return approve(sb, clash[0]["id"])
    return sb.table("facts").update({"text": t, "norm_key": key, "kind": classify_kind(t),
                                     "trust": "human", "approved_by_human": True, "strength": 1.0,
                                     "updated_at": _now().isoformat()}).eq("id", int(fact_id)).execute().data[0]


def remove(sb, fact_id) -> dict:
    _one_fact(sb, fact_id)
    sb.table("facts").delete().eq("id", int(fact_id)).execute()
    sb.table("audit").insert({"kind": "memory_forget", "detail": {"fact": int(fact_id)}}).execute()
    return {"removed": int(fact_id)}


def add(sb, text) -> dict:
    """사용자가 직접 알려 주는 사실: 처음부터 확실."""
    t = _clean_text(text)
    key = normalize(t)[:120] or t[:120]
    found = sb.table("facts").select("id").eq("norm_key", key).limit(1).execute().data
    if found:
        return approve(sb, found[0]["id"])
    kind = classify_kind(t)
    row = {"text": t, "norm_key": key, "kind": kind, "source": "user", "trust": "human",
           "approved_by_human": True, "strength": 1.0}
    if TTL.get(kind):
        row["expires_at"] = (_now() + TTL[kind]).isoformat()
    return sb.table("facts").insert(row).execute().data[0]


def quarantine_decide(sb, qid, accept: bool) -> dict:
    rows = sb.table("quarantine").select("*").eq("id", int(qid)).limit(1).execute().data
    if not rows or rows[0].get("resolved"):
        raise LookupError("없는 항목이에요")
    q = rows[0]
    result = {}
    if accept:                      # "제 얘기 맞아요" → 사용자가 확인했으니 확실한 사실로
        result["fact"] = add(sb, q["text"])
    sb.table("quarantine").update({"resolved": "accepted" if accept else "discarded"}).eq("id", int(qid)).execute()
    return {"id": int(qid), "resolved": "accepted" if accept else "discarded", **result}


def reset_growth(sb, agent: dict, key: str) -> dict:
    """맞춰 가는 방식 하나를 지운다 (다시 처음부터 배운다). persona 는 heir 계정만."""
    if key == "persona":
        if not agent.get("heir"):
            raise ValueError("이 계정에는 나다움이 없어요")
        sb.table("agents").update({"persona": None, "updated_at": _now().isoformat()}).execute()
        return {"reset": "persona"}
    if key not in GROWTH_KEYS:
        raise ValueError("없는 항목이에요")
    sb.table("agent_state").delete().eq("key", key).execute()
    return {"reset": key}


def forget_all(sb, agent: dict) -> dict:
    """비서가 나에 대해 아는 것을 전부 잊게 한다. 일정·메모·대화 같은 '내 기록'은 남는다."""
    sb.table("facts").delete().gte("id", 0).execute()
    sb.table("quarantine").delete().gte("id", 0).execute()
    for k in GROWTH_KEYS:
        sb.table("agent_state").delete().eq("key", k).execute()
    if agent.get("heir"):
        sb.table("agents").update({"persona": None}).execute()
    sb.table("audit").insert({"kind": "memory_forget_all", "detail": {}}).execute()
    return {"forgot": True}
