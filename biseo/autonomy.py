"""
autonomy.py — 상황별 자율 (정반합 동의 관문).

공정 비교 실험(arcogit 연구 9)에서 가장 잘 버틴 구조를 비서진의 '묻지 않고 해도 되나'에 옮긴 것.

  정 (공유 규칙)   종류 단위의 규칙: 지금까지의 승격 규칙(같은 종류를 수정 없이 5번 승인)과
                   종류 전체의 받아들임 비율.
  반 (자기 사정)   상황 단위의 기록: '알림 × 주중 × 아침 × 내가 시킨 것' 같은 상황마다
                   받아들임·넘김을 감쇠 합으로 센다 (가환: 순서와 무관).
  합 (동의)        이 상황에서 상황 기록이 종류 규칙보다 꾸준히 더 맞혀 왔을 때만(Π > π*)
                   상황 기록을 따른다. 그 전에는 지금까지의 규칙 그대로.

  예) 알림은 늘 받아들이시는데 주말 저녁 알림만 자주 넘기시면 → 주말 저녁 알림만 다시 여쭙는다.
      그 넘김이 종류 전체의 승격을 지우지도 않는다 (보호대가 변칙을 흡수하고 핵은 지킨다).

안전 (바꾸지 않는 것)
  tier 1·2 는 원래대로 바로, tier 4(남에게 닿는 일)는 언제나 묻는다.
  검색·문서 글을 본 뒤에 나온 일(오염)과 바깥에서 들어온 일은 상황 기록이 아무리 좋아도 묻는다.
  이 모듈이 실패해도 지금까지의 규칙으로 돌아간다.

시대 기록: 상황의 결정(자동 ↔ 묻기)이 바뀌면 audit 에 'autonomy' 로 남긴다 (언제, 왜 바뀌었나).
"""
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))
LAM = 0.8          # 상황 기록 감쇠 (같은 상황이 다시 나올 때마다)
MU = 0.3           # 동의 점수 Π 의 반응 속도
PI_STAR = 0.05     # 상황 기록이 종류 규칙보다 이만큼은 꾸준히 더 맞혀야 따른다
N_MIN = 3          # 이 상황에서 최소 이만큼은 겪어야 한다
AUTO_RATE = 0.8    # 상황 받아들임 비율이 이 이상이면 묻지 않음
ASK_RATE = 0.5     # 이 미만이면 다시 묻기
AUTO_MIN_ACC = 3.0 # 묻지 않으려면 받아들임(감쇠 합)이 이만큼은 쌓여야
TABLE = "action_context"


def _now():
    return datetime.now(timezone.utc).isoformat()


def _clock():
    return datetime.now(timezone.utc)


def _part(h: int) -> str:
    return "아침" if 5 <= h < 11 else "낮" if 11 <= h < 17 else "저녁" if 17 <= h < 22 else "밤"


def context(kind: str, origin: str, when=None) -> tuple:
    """(상황 열쇠, 사람이 읽는 이름)"""
    if isinstance(when, str):
        when = datetime.fromisoformat(when.replace("Z", "+00:00"))
    t = (when or _clock()).astimezone(KST)
    wk = "주말" if t.weekday() >= 5 else "주중"
    who = "비서 제안" if origin == "self" else "부탁"
    from .actions import KINDS
    label = f"{wk} {_part(t.hour)} {KINDS.get(kind, {}).get('label', kind)} ({who})"
    return f"{kind}|{origin}|{wk}|{_part(t.hour)}", label


def _row(sb, ctx: str) -> dict:
    rows = sb.table(TABLE).select("*").eq("ctx", ctx).limit(1).execute().data
    return rows[0] if rows else {"ctx": ctx, "acc": 0.0, "rej": 0.0, "n": 0, "pi": 0.0, "mode": None}


def _rate(r: dict):
    tot = (r.get("acc") or 0) + (r.get("rej") or 0)
    return (r["acc"] / tot) if tot > 0 else None


def _verdict(r: dict):
    """상황 기록이 동의를 얻었으면 'auto' 또는 'ask', 아니면 None (규칙 그대로)."""
    if (r.get("n") or 0) < N_MIN or (r.get("pi") or 0) <= PI_STAR:
        return None
    rate = _rate(r)
    if rate is None:
        return None
    if rate >= AUTO_RATE and (r.get("acc") or 0) >= AUTO_MIN_ACC:
        return "auto"
    if rate < ASK_RATE:
        return "ask"
    return None


def decide(sb, kind: str, origin: str, base_auto: bool, when=None) -> tuple:
    """tier 3, 오염 아님, 바깥 아님일 때만 부른다. (묻지 않을지, 근거 한 줄 또는 None)"""
    ctx, label = context(kind, origin, when)
    r = _row(sb, ctx)
    v = _verdict(r)
    if v is None:
        return base_auto, None
    if v == "auto":
        msg = None if base_auto else f"{label}은 최근 기록에서 거의 늘 받아들이셔서 바로 했어요."
        return True, msg
    return False, (f"{label}은 자주 넘기셔서 이번엔 여쭤봐요." if base_auto else
                   f"{label}은 자주 넘기셔서 여쭤봐요.")


def learn(sb, row: dict, outcome: float, weight: float = 1.0) -> dict:
    """결정(승인 +1, 거절·되돌림 −1)이 나면 상황 기록과 종류 기록을 고치고, 동의 점수를 갱신한다."""
    kind, origin = row["kind"], row.get("origin") or "user"
    ctx, label = context(kind, origin, row.get("created_at"))
    kctx = f"{kind}|*"
    r, k = _row(sb, ctx), _row(sb, kctx)
    y = outcome > 0
    # 예측: 반(상황 기록) 대 정(종류 규칙)
    rs, rk = _rate(r), _rate(k)
    rule_pred = True if rk is None else rk >= 0.5
    if rs is not None:
        hit_s, hit_r = float((rs >= 0.5) == y), float(rule_pred == y)
        r["pi"] = (1 - MU) * (r.get("pi") or 0.0) + MU * (hit_s - hit_r)
    for x in (r, k):                                     # 감쇠 합 (가환)
        x["acc"] = (x.get("acc") or 0.0) * LAM + (weight if y else 0.0)
        x["rej"] = (x.get("rej") or 0.0) * LAM + (0.0 if y else weight)
        x["n"] = (x.get("n") or 0) + 1
    prev, after = r.get("mode"), _verdict(r)
    now = _now()
    if after != prev:                                    # 시대 기록: 이 상황의 결정이 바뀜
        sb.table("audit").insert({"kind": "autonomy", "detail": {
            "ctx": ctx, "label": label, "from": prev, "to": after,
            "acc": round(r["acc"], 2), "rej": round(r["rej"], 2), "pi": round(r.get("pi") or 0.0, 3)}}).execute()
        r["changed_at"] = now
    r["mode"] = after
    for x, key in ((r, ctx), (k, kctx)):
        sb.table(TABLE).upsert({"ctx": key, "kind": kind, "label": label if key == ctx else kind,
                                "acc": x["acc"], "rej": x["rej"], "n": x["n"], "pi": x.get("pi") or 0.0,
                                "mode": x.get("mode"), "changed_at": x.get("changed_at"), "updated_at": now}).execute()
    return r


def explains_reject(sb, row: dict) -> bool:
    """이 넘김이 '이 상황만의 사정'으로 설명되나 (그러면 종류 전체의 승격은 지우지 않는다)."""
    ctx, _ = context(row["kind"], row.get("origin") or "user", row.get("created_at"))
    return _verdict(_row(sb, ctx)) == "ask"


def safe_decide(sb, kind, origin, base_auto, when=None):
    try:
        return decide(sb, kind, origin, base_auto, when)
    except Exception as e:
        print("AUTONOMY_DECIDE_SKIP", type(e).__name__, str(e)[:120])
        return base_auto, None


def safe_learn(sb, row, outcome, weight=1.0):
    try:
        return learn(sb, row, outcome, weight)
    except Exception as e:
        print("AUTONOMY_LEARN_SKIP", type(e).__name__, str(e)[:120])
        return None


def safe_explains(sb, row) -> bool:
    try:
        return explains_reject(sb, row)
    except Exception:
        return False


def overview(sb) -> dict:
    """설정 화면: 상황별로 따로 정해진 것과 최근 바뀐 기록."""
    rows = [r for r in (sb.table(TABLE).select("*").execute().data or []) if not str(r["ctx"]).endswith("|*")]
    special = []
    for r in rows:
        v = _verdict(r)
        if v:
            tot = (r.get("acc") or 0) + (r.get("rej") or 0)
            special.append({"ctx": r["ctx"], "label": r.get("label") or r["ctx"], "mode": v,
                            "rate": round(r["acc"] / tot, 2) if tot else None, "n": r.get("n"),
                            "changed_at": r.get("changed_at")})
    hist = (sb.table("audit").select("*").eq("kind", "autonomy").order("id", desc=True).limit(10).execute().data) or []
    return {"special": sorted(special, key=lambda x: x["label"]),
            "history": [{"at": h.get("created_at"), **(h.get("detail") or {})} for h in hist],
            "tracked": len(rows)}


def reset(sb, ctx: str) -> dict:
    """사용자가 이 상황의 따로 정해진 것을 지운다 → 다시 종류 규칙을 따른다."""
    sb.table(TABLE).delete().eq("ctx", ctx).execute()
    sb.table("audit").insert({"kind": "autonomy", "detail": {"ctx": ctx, "from": "user_reset", "to": None}}).execute()
    return {"ok": True}
