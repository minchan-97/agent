"""
judge.py — 판단 노드와 판단 회상 (몰랑이 trees 를 비서에 옮김).

몰랑이에서 그대로 가져온 것 (95번 pkl 의 trees 구조)
  · 판단 트리 = 노드(prompt·directive) + 가중치 있는 전이(transitions) + 학습률 lr(0.12)
  · 판단 한 번 = 뿌리에서 끝 노드까지 걷기. 갈림길마다 확률(at_probs)과 고른 길(choices)을 남긴다
  · 고른 노드의 directive 를 답을 만드는 지시로 넣는다 (판단 방식이 말투가 아니라 구조에서 나온다)
  · 판단 기록(history): 경로·확률·근거(sources)·답·피드백(-1 / 0.5 / 1)·검증 여부
  · 자라난 노드: '아는 것과 모르는 것 가르기', '근거끼리 맞춰 보기', '아직 모르는 쪽을 물어볼 거리로'
    — 몰랑이가 실제로 키운 노드 문장을 그대로 씨앗으로 심었다

비서에서 더한 것
  · 판단 회상: 새 판단을 할 때 같은 트리의 지난 판단 중 비슷하고 잘 됐던 것을 꺼내 근거로 쓰고,
    무엇을 회상했는지도 기록한다 → '왜?' 화면에서 "지난번 이런 판단이 받아들여져서" 를 보여 준다
  · 근거 번호: 판단 재료 한 줄마다 [E1] 같은 번호를 달고, 결론이 어느 줄에서 나왔는지 인용하게 한다
    → 사용자가 근거를 눌러 실제 일정·기억·대화로 갈 수 있다
  · 트리 상태는 계정마다 agent_state 'judge.trees' (공용으로 자라면 한 사람의 판단 습관이 남에게 샌다)

몰랑이의 전이 갱신식은 pkl 에 들어 있지 않아서, 같은 lr 로 '고른 길 += lr × 피드백' 뒤 정규화하는
가장 단순한 형태로 두었다 (몰랑이 코드와 맞춰야 하면 _learn 한 곳만 바꾸면 된다).
"""
import random
from datetime import datetime, timezone

from .norm import jaccard

STATE_KEY = "judge.trees"
LR = 0.12
MIN_W = 0.02
RECALL_K = 3

# 몰랑이가 키운 노드 (95번 pkl 에서 그대로)
GROWN = {
    "know": ("아는 것과 모르는 것 가르기",
             "지금까지 알아낸 근거로 말할 수 있는 것과, 아직 근거가 없어 모른다고 해야 하는 것을 먼저 나눈 뒤 답한다."),
    "cross": ("근거끼리 맞춰 보기",
              "쌓인 근거들이 서로 맞는지, 어긋나는 게 있는지 먼저 견준다. 어긋나면 어느 쪽이 더 확실한지 말한다."),
    "ask": ("아직 모르는 쪽을 물어볼 거리로",
            "상대가 말한 것 중 무엇을 더 알면 판단이 달라지는지 짚는다."),
}


def _tree(prefix, first, second, last, branch_w=0.4):
    """몰랑이 트리 모양: 0 → 1 → {끝, 자란 노드들} → 끝."""
    nodes = {
        f"{prefix}_0": {"prompt": first[0], "directive": first[1], "is_terminal": False},
        f"{prefix}_1": {"prompt": second[0], "directive": second[1], "is_terminal": False},
        f"{prefix}_2": {"prompt": last[0], "directive": last[1], "is_terminal": True},
    }
    trans = {f"{prefix}_0": {f"{prefix}_1": 1.0}, f"{prefix}_1": {f"{prefix}_2": branch_w}, f"{prefix}_2": {}}
    rest = (1.0 - branch_w) / len(GROWN)
    for k, (p, d) in GROWN.items():
        nid = f"grown_{prefix}_{k}"
        nodes[nid] = {"prompt": p, "directive": d, "is_terminal": False}
        trans[f"{prefix}_1"][nid] = rest
        trans[nid] = {f"{prefix}_2": 1.0}
    return {"nodes": nodes, "transitions": trans, "root_id": f"{prefix}_0", "lr": LR}


SEED_TREES = {
    # 오늘의 제안
    "recommendation": _tree(
        "recommendation",
        ("상황 파악", "오늘·가까운 일정, 맡은 일, 최근 말 가운데 오늘과 이어지는 것만 골라낸다."),
        ("후보 만들기", "고른 것마다 비서가 해 줄 수 있는 일 또는 사용자가 하면 좋을 일을 후보로 만든다."),
        ("근거 대고 추천", "후보마다 재료의 어느 줄([E번호])에서 나왔는지 밝히고, 근거가 약한 후보는 버린다."),
    ),
    # 대화 한 턴 (행동이 생길 수 있는 판단)
    "response": _tree(
        "response",
        ("요청 파악", "상대가 지금 무엇을 바라는지, 시킨 일과 그냥 하는 말을 가른다."),
        ("할 일 정하기", "시킨 일은 행동으로, 시키지 않았지만 도움이 될 일은 제안으로 나눈다."),
        ("확인된 것으로 답하기", "아는 사실([F번호])과 재료에 있는 것만으로 답하고, 행동마다 근거를 밝힌다."),
    ),
    # 맡은 일 마무리
    "task": _tree(
        "task",
        ("목표 다시 보기", "처음 맡은 목표가 정확히 무엇이었는지 다시 확인한다."),
        ("모은 것 견주기", "지금까지 모은 자료와 결과가 목표에 답하는지 본다."),
        ("근거로 정리", "자료에 있는 것만으로 결과를 정리하고, 모자란 점은 한계로 밝힌다."),
    ),
}


def _now():
    return datetime.now(timezone.utc).isoformat()


# ---------------- 트리 상태 (계정별) ----------------
def load_trees(sb) -> dict:
    rows = sb.table("agent_state").select("value").eq("key", STATE_KEY).limit(1).execute().data
    trees = dict(rows[0]["value"]) if rows else {}
    for name, seed in SEED_TREES.items():          # 새로 생긴 트리는 씨앗으로 채운다
        trees.setdefault(name, {"transitions": seed["transitions"], "uses": 0})
    return trees


def _save_trees(sb, trees):
    sb.table("agent_state").upsert({"key": STATE_KEY, "value": trees, "updated_at": _now()}).execute()


# ---------------- 판단 걷기 ----------------
def walk(sb, tree: str, rng=None) -> dict:
    """뿌리에서 끝 노드까지 걷는다. 돌려주는 값: path, at_probs, directives (답 만들 때 넣을 지시)."""
    rng = rng or random.Random()
    seed = SEED_TREES[tree]
    trans = load_trees(sb)[tree]["transitions"]
    cur, path, at_probs = seed["root_id"], [seed["root_id"]], []
    for _ in range(8):
        opts = {k: float(v) for k, v in (trans.get(cur) or {}).items() if k in seed["nodes"]}
        if not opts:
            break
        total = sum(opts.values()) or 1.0
        probs = {k: round(v / total, 4) for k, v in opts.items()}
        r, acc, chose = rng.random(), 0.0, None
        for k, p in probs.items():
            acc += p
            if r <= acc:
                chose = k
                break
        chose = chose or max(probs, key=probs.get)
        at_probs.append({"at": cur, "options": probs, "chose": chose})
        path.append(chose)
        cur = chose
    nodes = seed["nodes"]
    return {"tree": tree, "path": path, "at_probs": at_probs,
            "steps": [{"id": n, "prompt": nodes[n]["prompt"], "directive": nodes[n]["directive"]} for n in path]}


def directive_text(w: dict) -> str:
    """고른 경로의 지시를 프롬프트에 넣을 글로."""
    return "[이번 판단의 방식 — 차례대로 따를 것]\n" + "\n".join(
        f"{i}. {s['prompt']}: {s['directive']}" for i, s in enumerate(w["steps"], 1))


# ---------------- 판단 회상 ----------------
def recall(sb, tree: str, context: str, k: int = RECALL_K) -> list:
    """같은 트리의 지난 판단 중 지금과 비슷하고, 받아들여졌던(피드백 ≥ 0.5) 것을 꺼낸다."""
    rows = sb.table("judgments").select("id,summary,context,feedback,created_at") \
             .eq("tree", tree).order("id", desc=True).limit(80).execute().data or []
    scored = []
    for r in rows:
        if (r.get("feedback") if r.get("feedback") is not None else 0.5) < 0.5:
            continue
        sim = jaccard(context or "", (r.get("context") or "") + " " + (r.get("summary") or ""))
        if sim > 0.05:
            scored.append((sim, r))
    scored.sort(key=lambda x: (-x[0], -x[1]["id"]))
    return [{"id": r["id"], "summary": r["summary"], "feedback": r.get("feedback"),
             "at": r["created_at"], "sim": round(s, 3)} for s, r in scored[:k]]


def recall_text(recalled: list) -> str:
    if not recalled:
        return ""
    return "[판단 회상 — 비슷한 상황에서 받아들여졌던 지난 판단]\n" + "\n".join(
        f"- {r['summary']}" for r in recalled)


# ---------------- 기록 · 피드백 ----------------
def record(sb, w: dict, *, summary: str, context: str, evidence: list, recalled: list,
           ref_kind: str, ref_id, tainted: bool = False) -> dict:
    """판단 하나를 남긴다. evidence: [{"tag","kind","id","text"}], recalled: recall() 결과."""
    row = {"tree": w["tree"], "path": w["path"], "at_probs": w["at_probs"],
           "steps": [{"id": s["id"], "prompt": s["prompt"]} for s in w["steps"]],
           "evidence": evidence[:12], "recalled": recalled[:RECALL_K],
           "summary": (summary or "")[:300], "context": (context or "")[:1500],
           "ref_kind": ref_kind, "ref_id": str(ref_id) if ref_id is not None else None,
           "tainted": bool(tainted)}
    return sb.table("judgments").insert(row).execute().data[0]


def _learn(trans: dict, path: list, fb: float, lr: float = LR):
    for a, b in zip(path, path[1:]):
        opts = trans.get(a) or {}
        if b not in opts or len(opts) < 2:
            continue
        opts[b] = max(MIN_W, opts[b] + lr * fb)
        total = sum(opts.values())
        for k in opts:
            opts[k] = max(MIN_W, opts[k] / total)


def feedback(sb, ref_kind: str, ref_id, fb: float):
    """사용자의 결정(맡김·넘김·승인·거절·되돌림)으로 그 판단을 채점하고, 트리의 전이를 배운다."""
    rows = sb.table("judgments").select("id,tree,path,feedback").eq("ref_kind", ref_kind) \
             .eq("ref_id", str(ref_id)).order("id", desc=True).limit(1).execute().data
    if not rows:
        return None
    j = rows[0]
    sb.table("judgments").update({"feedback": fb, "decided_at": _now()}).eq("id", j["id"]).execute()
    trees = load_trees(sb)
    t = trees.get(j["tree"])
    if t:
        _learn(t["transitions"], j["path"] or [], fb)
        t["uses"] = int(t.get("uses") or 0) + 1
        _save_trees(sb, trees)
    return j["id"]


def safe_feedback(sb, ref_kind, ref_id, fb):
    """판단 기록이 없거나 표가 아직 없어도 본래 동작은 멈추지 않게."""
    try:
        return feedback(sb, ref_kind, ref_id, fb)
    except Exception as e:
        print("JUDGE_FEEDBACK_SKIP", type(e).__name__, str(e)[:120])
        return None


# ---------------- 왜? (설명) ----------------
def explain(sb, ref_kind: str, ref_id) -> dict:
    rows = sb.table("judgments").select("*").eq("ref_kind", ref_kind).eq("ref_id", str(ref_id)) \
             .order("id", desc=True).limit(1).execute().data
    if not rows:
        raise LookupError("이 일에 대한 판단 기록이 없어요")
    j = rows[0]
    probs = {p["at"]: p for p in (j.get("at_probs") or [])}
    steps = []
    for s in j.get("steps") or []:
        p = probs.get(s["id"])
        steps.append({**s, "grown": s["id"].startswith("grown_")})
    # 각 걸음에서 고른 길의 확률 (다음 노드 기준)
    for i, p in enumerate(j.get("at_probs") or []):
        if i + 1 < len(steps):
            steps[i + 1]["p"] = p["options"].get(p["chose"])
            steps[i + 1]["alts"] = len(p["options"])
    return {"id": j["id"], "tree": j["tree"], "summary": j.get("summary"), "steps": steps,
            "evidence": j.get("evidence") or [], "recalled": j.get("recalled") or [],
            "feedback": j.get("feedback"), "tainted": j.get("tainted"), "at": j.get("created_at")}
