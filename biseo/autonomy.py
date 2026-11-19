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

관문핵 구조(GateCore) 보강 (arcogit 연구 10·11)
  이웃 나눠 세기   상황 열쇠는 서로 떨어진 칸이라, 처음 겪는 상황(기록 N_MIN 미만)은 아무것도 몰랐다.
                   이제 같은 종류·같은 출처의 가까운 상황(시간대 고리 거리, 주중/주말 차이)에 커널 무게를
                   주어 기록을 빌려 본다. 빌린 '자동'은 가까운 상황 하나 이상이 이미 동의를 얻어 자동이고,
                   가까운 상황 중 '묻기'가 없을 때만 낸다. 기록을 옮겨 적지 않고 판단할 때만 빌린다 (가환 유지).
  가짜 예외 막기   하루 이틀 몰린 받아들임으로 바로 자동이 되지 않게, 자동은 서로 다른 날 DAYS_MIN 일 이상
                   겪은 상황에서만 낸다. 묻기 쪽은 안전한 방향이라 막지 않는다. 막힌 상황은 '지켜보는 중'으로 보인다.
  혁명과 재해석   (연구 2·7) 지금까지는 상황마다 따로만 바뀌고 종류 규칙(핵)은 그대로였다. 이제 같은 종류에서
                   동의를 얻은 상황 R_MIN 개 이상이(시간대 2곳 이상에 걸쳐) 핵과 반대쪽을 가리키고, 핵 쪽을 가리키는
                   상황이 하나도 없는 날이 서로 다른 날 REV_DAYS 일 이어지면 종류 규칙 자체를 바꾼다(혁명).
                   하루라도 조건이 깨지면 처음부터 다시 센다(꾸준함). 바뀐 뒤에는 그 상황들의 옛 기록을 REINTERP 배로
                   줄여 새 규칙 아래에서 다시 읽고(재해석), 상황들은 새 규칙을 따른다. 사용자는 종류 규칙을 원래대로 되돌릴 수 있다.
                   혁명도 tier 3·오염 아님·바깥 아님에서만 쓰인다 (안전 규칙은 그대로).
"""
import math
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))
LAM = 0.8          # 상황 기록 감쇠 (같은 상황이 다시 나올 때마다)
MU = 0.3           # 동의 점수 Π 의 반응 속도
PI_STAR = 0.05     # 상황 기록이 종류 규칙보다 이만큼은 꾸준히 더 맞혀야 따른다
N_MIN = 3          # 이 상황에서 최소 이만큼은 겪어야 한다
AUTO_RATE = 0.8    # 상황 받아들임 비율이 이 이상이면 묻지 않음
ASK_RATE = 0.5     # 이 미만이면 다시 묻기
AUTO_MIN_ACC = 3.0 # 묻지 않으려면 받아들임(감쇠 합)이 이만큼은 쌓여야
DAYS_MIN = 2       # 자동은 서로 다른 날 이만큼 이상 겪은 상황에서만 (가짜 예외 막기)
KH = 0.8           # 이웃 커널 폭 (시간대 한 칸 ≈ 0.46, 주중↔주말 ≈ 0.17)
K_NEAR = 0.3       # 이 이상 가까운 이웃만 동의·반대 근거로 본다
BORROW_MIN_ACC = 1.5  # 빌린 받아들임(커널 무게 곱)이 이만큼은 돼야 (이웃이 이미 동의를 얻은 상황이라 절반)
PARTS = ["아침", "낮", "저녁", "밤"]
R_MIN = 3          # 혁명: 같은 쪽을 가리키는 동의 얻은 상황 수
REV_DAYS = 3       # 혁명: 조건이 이어진 서로 다른 날 수
REINTERP = 0.3     # 재해석: 혁명 뒤 옛 상황 기록에 곱하는 무게
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


def _raw(r: dict):
    """기간 조건을 보기 전의 판단."""
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


def _held(r: dict) -> bool:
    """자동이 될 만하지만 겪은 날이 적어 지켜보는 중인가. (예전 표에는 days 가 없다 → 막지 않음)"""
    d = r.get("days")
    return _raw(r) == "auto" and d is not None and d < DAYS_MIN


def _verdict(r: dict):
    """상황 기록이 동의를 얻었으면 'auto' 또는 'ask', 아니면 None (규칙 그대로)."""
    v = _raw(r)
    return None if (v == "auto" and _held(r)) else v


def _split(ctx: str):
    p = str(ctx).split("|")
    return p if len(p) == 4 else None


def kernel(a: str, b: str) -> float:
    """두 상황 열쇠의 가까움 (같은 종류·출처일 때만). 시간대는 고리(밤 다음 아침)."""
    pa, pb = _split(a), _split(b)
    if not pa or not pb or pa[:2] != pb[:2] or pa[2:] == pb[2:]:
        return 0.0
    if pa[3] not in PARTS or pb[3] not in PARTS:
        return 0.0
    dp = abs(PARTS.index(pa[3]) - PARTS.index(pb[3])); dp = min(dp, 4 - dp)
    dw = 1.5 if pa[2] != pb[2] else 0.0
    return math.exp(-(dp * dp + dw * dw) / (2 * KH * KH))


def borrow(own: dict, rows: list):
    """처음 겪는 상황: 가까운 상황의 기록을 커널 무게로 나눠 받아 본다. (판단, 가장 가까운 근거 행) 또는 (None, None)."""
    if (own.get("n") or 0) >= N_MIN:
        return None, None
    near = sorted(((kernel(own["ctx"], r["ctx"]), r) for r in rows), key=lambda x: -x[0])
    near = [(w, r) for w, r in near if w > 0.01]
    if not near:
        return None, None
    acc = (own.get("acc") or 0.0) + sum(w * (r.get("acc") or 0.0) for w, r in near)
    rej = (own.get("rej") or 0.0) + sum(w * (r.get("rej") or 0.0) for w, r in near)
    if acc + rej <= 0:
        return None, None
    rate = acc / (acc + rej)
    close = [(w, r, _verdict(r)) for w, r in near if w >= K_NEAR]
    asks = [r for w, r, v in close if v == "ask"]
    autos = [r for w, r, v in close if v == "auto"]
    if asks and rate < ASK_RATE:
        return "ask", asks[0]
    own_bad = (own.get("rej") or 0.0) > (own.get("acc") or 0.0)
    if autos and not asks and not own_bad and rate >= AUTO_RATE and acc >= BORROW_MIN_ACC:
        return "auto", autos[0]
    return None, None


def _all_rows(sb, kind: str) -> tuple:
    """(상황 행들, 종류 행) — 한 번의 조회로."""
    rows = sb.table(TABLE).select("*").eq("kind", kind).execute().data or []
    return [r for r in rows if _split(r["ctx"])], next((r for r in rows if r["ctx"] == f"{kind}|*"), None)


def _kind_rows(sb, kind: str, origin: str) -> list:
    return [r for r in _all_rows(sb, kind)[0] if _split(r["ctx"])[1] == origin]


def _promoted(sb, kind: str) -> bool:
    from .actions import _trust
    return bool(_trust(sb, kind).get("promoted"))


def core_of(krow, promoted: bool) -> str:
    """지금 종류 규칙(핵): 혁명으로 바뀌었으면 그 값, 아니면 승격 규칙."""
    m = (krow or {}).get("mode")
    return m if m in ("auto", "ask") else ("auto" if promoted else "ask")


def pressure(rows: list, core: str) -> tuple:
    """핵과 반대쪽을 가리키는 동의 얻은 상황들. (바꿀 방향 또는 None, 그 상황들)"""
    want = "ask" if core == "auto" else "auto"
    vs = [(r, _verdict(r)) for r in rows]
    agree = [r for r, v in vs if v == want]

    def leans_core(r, v):
        # 동의 점수가 없어도(종류 기록과 같은 쪽이라 Π 가 안 큼) 충분히 겪고 핵 쪽 비율이면 반대표
        if v == core:
            return True
        rate = _rate(r)
        if v is not None or rate is None or (r.get("n") or 0) < N_MIN:
            return False
        return rate >= AUTO_RATE if core == "auto" else rate < ASK_RATE
    against = [r for r, v in vs if leans_core(r, v)]
    slots = {tuple(_split(r["ctx"])[2:]) for r in agree}
    return (want if len(agree) >= R_MIN and len(slots) >= 2 and not against else None), agree


def _kind_label(kind: str) -> str:
    from .actions import KINDS
    return KINDS.get(kind, {}).get("label", kind)


def decide(sb, kind: str, origin: str, base_auto: bool, when=None) -> tuple:
    """tier 3, 오염 아님, 바깥 아님일 때만 부른다. (묻지 않을지, 근거 한 줄 또는 None)"""
    ctx, label = context(kind, origin, when)
    allrows, krow = _all_rows(sb, kind)
    rows = [x for x in allrows if _split(x["ctx"])[1] == origin]
    r = next((x for x in rows if x["ctx"] == ctx), None) or {"ctx": ctx, "acc": 0.0, "rej": 0.0, "n": 0, "pi": 0.0}
    v = _verdict(r)
    if v is None:
        bv, src = borrow(r, [x for x in rows if x["ctx"] != ctx])
        if bv is None:
            m = (krow or {}).get("mode")
            if m in ("auto", "ask") and (m == "auto") != base_auto:      # 혁명으로 바뀐 종류 규칙
                kl = _kind_label(kind)
                return (True, f"{kl}은 여러 상황에서 꾸준히 받아들이셔서 종류 규칙이 '바로'로 바뀌었어요.") if m == "auto" else \
                       (False, f"{kl}은 여러 상황에서 꾸준히 넘기셔서 종류 규칙이 '여쭤보기'로 바뀌었어요.")
            return base_auto, None
        near = src.get("label") or src["ctx"]
        if bv == "auto":
            return True, (None if base_auto else f"{label}은 처음이지만, 가까운 상황({near})에서 거의 늘 받아들이셔서 바로 했어요.")
        return False, f"{label}은 처음이라, 가까운 상황({near})에서 자주 넘기신 걸 보고 여쭤봐요."
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
    day = _day(row.get("created_at"))
    for x in (r, k):                                     # 감쇠 합 (가환)
        x["acc"] = (x.get("acc") or 0.0) * LAM + (weight if y else 0.0)
        x["rej"] = (x.get("rej") or 0.0) * LAM + (0.0 if y else weight)
        x["n"] = (x.get("n") or 0) + 1
        if x.get("last_day") != day:                     # 서로 다른 날 수 (가짜 예외 막기)
            x["days"] = (x.get("days") or 0) + 1
            x["last_day"] = day
    prev, after = r.get("mode"), _verdict(r)
    now = _now()
    if after != prev:                                    # 시대 기록: 이 상황의 결정이 바뀜
        sb.table("audit").insert({"kind": "autonomy", "detail": {
            "ctx": ctx, "label": label, "from": prev, "to": after,
            "acc": round(r["acc"], 2), "rej": round(r["rej"], 2), "pi": round(r.get("pi") or 0.0, 3)}}).execute()
        r["changed_at"] = now
    r["mode"] = after
    try:
        _revolution(sb, kind, ctx, r, k, day, now)
    except Exception as e:                                # 혁명 단계가 실패해도 상황 학습은 그대로
        print("AUTONOMY_REVOLUTION_SKIP", type(e).__name__, str(e)[:120])
    for x, key in ((r, ctx), (k, kctx)):
        rec = {"ctx": key, "kind": kind, "label": label if key == ctx else kind,
               "acc": x["acc"], "rej": x["rej"], "n": x["n"], "pi": x.get("pi") or 0.0,
               "mode": x.get("mode"), "changed_at": x.get("changed_at"), "updated_at": now}
        _save(sb, rec, x)
    return r


def _save(sb, rec: dict, x: dict):
    """새 열(날 수·혁명 기록)이 없는 예전 표에서도 저장되게."""
    full = {**rec, "days": x.get("days") or 0, "last_day": x.get("last_day"),
            "rival": x.get("rival") or 0, "rival_day": x.get("rival_day")}
    for data in (full, {k: v for k, v in full.items() if k not in ("rival", "rival_day")}, rec):
        try:
            sb.table(TABLE).upsert(data).execute()
            return
        except Exception:
            continue


def _revolution(sb, kind, ctx, r, k, day, now):
    """같은 종류의 여러 상황이 서로 다른 날 꾸준히 핵과 반대쪽이면 핵(종류 규칙)을 바꾸고 옛 기록을 재해석한다."""
    rows, _ = _all_rows(sb, kind)
    rows = [r if x["ctx"] == ctx else x for x in rows]
    if not any(x["ctx"] == ctx for x in rows):
        rows.append(r)
    promoted = _promoted(sb, kind)
    core = core_of(k, promoted)
    want, agree = pressure(rows, core)
    if not want:
        k["rival"], k["rival_day"] = 0, None              # 하루라도 깨지면 처음부터 (꾸준함)
        return
    if k.get("rival_day") != day:
        k["rival"], k["rival_day"] = (k.get("rival") or 0) + 1, day
    if k["rival"] < REV_DAYS:
        return
    base = "auto" if promoted else "ask"
    k["mode"] = None if want == base else want            # 승격 규칙과 같아지면 덮어쓴 것을 지운다
    k["changed_at"], k["rival"], k["rival_day"] = now, 0, None
    for x in agree:                                       # 재해석: 옛 사례를 새 규칙 아래에서 가볍게
        x["acc"] = (x.get("acc") or 0.0) * REINTERP
        x["rej"] = (x.get("rej") or 0.0) * REINTERP
        x["pi"] = (x.get("pi") or 0.0) * REINTERP
        x["mode"], x["changed_at"] = None, now
        if x is not r:
            _save(sb, {"ctx": x["ctx"], "kind": kind, "label": x.get("label"), "acc": x["acc"], "rej": x["rej"],
                       "n": x.get("n") or 0, "pi": x["pi"], "mode": None, "changed_at": now, "updated_at": now}, x)
    sb.table("audit").insert({"kind": "autonomy", "detail": {
        "ctx": f"{kind}|*", "label": _kind_label(kind), "revolution": True, "from": core, "to": want,
        "contexts": [x.get("label") or x["ctx"] for x in agree][:6], "days": REV_DAYS}}).execute()


def _day(when) -> str:
    if isinstance(when, str):
        when = datetime.fromisoformat(when.replace("Z", "+00:00"))
    return (when or _clock()).astimezone(KST).strftime("%Y-%m-%d")


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
    allr = sb.table(TABLE).select("*").execute().data or []
    rows = [r for r in allr if not str(r["ctx"]).endswith("|*")]
    kinds = [r for r in allr if str(r["ctx"]).endswith("|*")]
    cores = [{"kind": r["kind"], "label": _kind_label(r["kind"]), "mode": r["mode"], "changed_at": r.get("changed_at")}
             for r in kinds if r.get("mode") in ("auto", "ask")]
    rival = [{"kind": r["kind"], "label": _kind_label(r["kind"]), "days": r.get("rival"), "need": REV_DAYS}
             for r in kinds if (r.get("rival") or 0) > 0]
    special, held = [], []
    for r in rows:
        v = _verdict(r)
        if v:
            tot = (r.get("acc") or 0) + (r.get("rej") or 0)
            special.append({"ctx": r["ctx"], "label": r.get("label") or r["ctx"], "mode": v,
                            "rate": round(r["acc"] / tot, 2) if tot else None, "n": r.get("n"),
                            "changed_at": r.get("changed_at")})
        elif _held(r):
            held.append({"ctx": r["ctx"], "label": r.get("label") or r["ctx"], "days": r.get("days"), "need": DAYS_MIN})
    hist = (sb.table("audit").select("*").eq("kind", "autonomy").order("id", desc=True).limit(10).execute().data) or []
    return {"special": sorted(special, key=lambda x: x["label"]),
            "history": [{"at": h.get("created_at"), **(h.get("detail") or {})} for h in hist],
            "held": sorted(held, key=lambda x: x["label"]),
            "cores": cores, "rival": rival,
            "tracked": len(rows)}


def facts(sb, days: int = 30) -> dict:
    """자기 개선 질문용: 자율 판단 구조의 실제 상태 (숫자로)."""
    ov = overview(sb)
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    acts = sb.table("actions").select("status,tier,auto_executed,auto_basis,created_at").gte("created_at", since).execute().data or []
    basis = [a.get("auto_basis") or "" for a in acts]
    return {"days": days, "tracked": ov["tracked"], "special": ov["special"], "held": ov["held"],
            "cores": ov["cores"], "rival": ov["rival"],
            "changes": len(ov["history"]), "history": ov["history"][:3],
            "auto_done": sum(1 for a in acts if a.get("auto_executed")),
            "asked": sum(1 for a in acts if not a.get("auto_executed") and a.get("tier") == 3
                         and a.get("status") in ("proposed", "rejected", "done", "scheduled", "undone")),
            "undone": sum(1 for a in acts if a.get("status") == "undone"),
            "borrowed": sum(1 for s in basis if "가까운 상황" in s),
            "ctx_based": sum(1 for s in basis if s and "가까운 상황" not in s),
            "decided": len(acts)}


def reset(sb, ctx: str) -> dict:
    """사용자가 이 상황의 따로 정해진 것을 지운다 → 다시 종류 규칙을 따른다."""
    sb.table(TABLE).delete().eq("ctx", ctx).execute()
    sb.table("audit").insert({"kind": "autonomy", "detail": {"ctx": ctx, "from": "user_reset", "to": None}}).execute()
    return {"ok": True}


def core_reset(sb, kind: str) -> dict:
    """사용자가 혁명으로 바뀐 종류 규칙을 원래(승격 규칙)대로 되돌린다."""
    rows = sb.table(TABLE).select("*").eq("ctx", f"{kind}|*").limit(1).execute().data
    if rows:
        k = rows[0]
        prev = k.get("mode")
        k.update({"mode": None, "rival": 0, "rival_day": None, "changed_at": _now()})
        _save(sb, {"ctx": k["ctx"], "kind": kind, "label": k.get("label") or kind, "acc": k.get("acc") or 0.0,
                   "rej": k.get("rej") or 0.0, "n": k.get("n") or 0, "pi": k.get("pi") or 0.0, "mode": None,
                   "changed_at": k["changed_at"], "updated_at": k["changed_at"]}, k)
        sb.table("audit").insert({"kind": "autonomy", "detail": {"ctx": f"{kind}|*", "label": _kind_label(kind),
                                  "revolution": True, "from": "user_reset", "was": prev, "to": None}}).execute()
    return {"ok": True}
