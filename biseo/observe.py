"""
observe.py — 비서가 사용 패턴을 관찰하고, 버튼을 만들자고 제안한다.

스마트폰과 AI 홈 화면의 차이: 아이콘이 고정이 아니라, 비서가 쓰는 걸 보며
"이런 버튼이 있으면 편하겠다"를 스스로 알아챈다. 단, 제안만 한다.
실제로 생길지는 사람이 확인함에서 정한다 (지금 기능 제안과 같은 방식).

제한선 (멋대로 늘리지 않게)
  · 같은 종류를 SUGGEST_AT 번 이상 했을 때만 제안
  · 한 번에 하나만, 이미 제안했거나 거절한 건 다시 안 함
  · 민감한 내용은 norm_key·sample 에서 가린다 (privacy.mask)
"""
from .norm import normalize
from .privacy import mask

SUGGEST_AT = 3
PIN_AT = 5          # 단축 버튼이 이만큼 쓰이면 홈에 두자고 제안


def note(sb, app: str, text: str):
    """명령 하나를 관찰 기록에 더한다. 검색·과제를 쓸 때마다 호출된다."""
    if app not in ("search", "tasks"):
        return
    sample = mask(str(text or "")).strip()[:120]
    key = normalize(sample)[:120]
    if len(key) < 2:
        return
    rows = sb.table("usage_patterns").select("id,count").eq("app", app).eq("norm_key", key) \
             .limit(1).execute().data
    if rows:
        sb.table("usage_patterns").update({"count": (rows[0].get("count") or 1) + 1}) \
          .eq("id", rows[0]["id"]).execute()
    else:
        sb.table("usage_patterns").insert({"app": app, "norm_key": key, "sample": sample}).execute()


def maybe_pin(sb) -> dict | None:
    """자주 눌리는 단축 버튼을 홈에 고정하자고 제안한다."""
    from . import actions as act
    hot = sb.table("shortcuts").select("*").gte("uses", PIN_AT).eq("pinned", False) \
            .order("uses", desc=True).limit(1).execute().data
    if not hot:
        return None
    s0 = hot[0]
    # 이미 이 버튼으로 홈 고정 제안이 떠 있으면 다시 안 함
    open_props = sb.table("actions").select("args").eq("kind", "pin_shortcut") \
                   .eq("status", "proposed").execute().data or []
    if any((p.get("args") or {}).get("shortcut_id") == s0["id"] for p in open_props):
        return None
    return act.propose(sb, "pin_shortcut",
                       {"shortcut_id": s0["id"], "label": s0["label"]},
                       reason=f"'{s0['label']}' 버튼을 {s0['uses']}번 쓰셨어요. 홈 화면에 둘까요?",
                       origin="self")


def maybe_suggest(sb) -> dict | None:
    """자주 쓰는데 아직 버튼이 없는 명령을 하나 골라, 버튼 만들기를 제안한다."""
    from . import actions as act
    hot = sb.table("usage_patterns").select("*") \
            .gte("count", SUGGEST_AT).eq("suggested", False).eq("dismissed", False) \
            .order("count", desc=True).limit(1).execute().data
    if not hot:
        return None
    pat = hot[0]
    if (pat.get("count") or 0) < SUGGEST_AT:   # 코드에서도 한 번 더 확인
        return None
    # 이미 같은 내용의 버튼이 있으면 제안하지 않는다
    existing = sb.table("shortcuts").select("id").eq("app", pat["app"]).execute().data or []
    for e in sb.table("shortcuts").select("text").eq("app", pat["app"]).execute().data or []:
        if normalize(e["text"])[:120] == pat["norm_key"]:
            sb.table("usage_patterns").update({"suggested": True}).eq("id", pat["id"]).execute()
            return None
    label = pat["sample"][:16]
    row = act.propose(sb, "suggest_shortcut",
                      {"app": pat["app"], "label": label, "text": pat["sample"],
                       "count": pat["count"], "pattern_id": pat["id"]},
                      reason=f"'{pat['sample']}' 을(를) {pat['count']}번 하셨어요. 버튼으로 만들어 둘까요?",
                      origin="self")
    sb.table("usage_patterns").update({"suggested": True}).eq("id", pat["id"]).execute()
    return row
