"""
explore.py — 자율 검색 탐색.

한 번 던지고 끝내지 않는다. 비서가 스스로 결과를 채점하고, 부족하면
'왜 부족한지' 진단해서 다음 검색을 다른 각도로 바꾼다. 충분하면 멈춘다.

섞는 방식 (빠르기와 비용의 균형)
  1바퀴  원래 검색어 하나만 던진다. 쉬운 질문은 여기서 끝 → 검색 1번, 판단 1번.
  2바퀴~ 부족하면, 진단을 바탕으로 서로 다른 각도 2~3개를 '동시에' 던지고
         모인 결과를 한 번에 채점한다. 차례로 4~5바퀴 돌던 것이 보통 2바퀴에 끝난다.
  "실패에서 배우기"는 남는다: 각도는 앞 바퀴의 진단을 보고 정한다.

자유는 넓게 (각도를 어떻게 바꿀지는 비서가 정한다)
울타리는 분명하게
  · 판단(바퀴)은 max_tries 번까지, 한 바퀴에 동시 검색은 PARALLEL 개까지
  · 동시에 보내는 수는 BRAVE_CONCURRENCY 로 묶는다 (요금제의 초당 한도에 맞춰 조절)
  · 이미 던진 검색어와 거의 같으면(토큰 80% 겹침) 버린다
  · 끝까지 못 찾으면 지어내지 말고 "이런 각도를 시도했지만 못 찾았다"고 보고

이 모듈은 검색을 '하고', 과정을 기록으로 돌려줄 뿐 답을 짓지 않는다.
답은 brain/tasks 가 모인 자료를 근거로 짓는다 (시점·출처 규칙은 그대로 적용).
"""
import os
import threading
from concurrent.futures import ThreadPoolExecutor

from . import search
from .norm import jaccard

MAX_TRIES = 3             # 판단 바퀴 수 (1바퀴 단일 + 최대 2바퀴 병렬)
PARALLEL = 3              # 한 바퀴에 동시에 던질 각도 수
MERGED_MAX = 10           # 한 바퀴에 채점할 결과 수 (주소가 같은 건 하나로)
TOO_SIMILAR = 0.8
# Brave 무료·낮은 요금제는 초당 요청 수가 적다. 1 로 두면 동시에 보내지 않고 차례로 보낸다
# (그래도 판단은 한 바퀴에 한 번이라 예전보다 빠르다).
_GATE = threading.BoundedSemaphore(max(1, int(os.environ.get("BRAVE_CONCURRENCY", "3"))))

PLAN = """
너는 검색으로 사실을 찾는 중이다. JSON 객체 하나로만 답해.
{
  "verdict": "enough" | "retry" | "giveup",
  "reason": "지금 결과를 어떻게 봤는지 한 문장 (무엇이 부족한지 진단)",
  "next_queries": ["retry 일 때 다음에 동시에 던질 검색어 2~3개. 서로 다른 각도로"],
  "today_only": true | false
}

각도를 바꾸는 법(예시일 뿐, 스스로 판단해라)
- 결과가 너무 넓다 → 더 구체적인 말, 고유명사, 기간을 넣어 좁힌다
- 쓰는 말이 어긋났다 → 동의어·정식 명칭·영어 용어로 바꾼다
- 출처가 부실하다 → 기관·공식 사이트 이름을 넣거나 'site:' 로 범위를 준다
- 너무 오래된 것뿐이다 → 올해·이번 달, 또는 today_only 를 켠다
- 다른 측면이 필요하다 → 원인·반응·비교 등 다른 각을 질문에 넣는다
next_queries 는 서로 겹치지 않게, 위 방법 중 서로 다른 것을 섞어라. 어느 하나는 맞도록.

enough: 질문에 답할 근거가 모였다. giveup: 각도를 여러 번 바꿔도 안 나온다.
이미 던진 검색어를 되풀이하지 마라.
"""


def _similar_to_prior(q: str, tried: list) -> bool:
    return any(jaccard(q, t) >= TOO_SIMILAR for t in tried)


def _one(query: str, today_only: bool) -> dict:
    with _GATE:
        try:
            got = search.find(query, today_only, count=6)
            return {"query": query, "results": got["results"], "error": None}
        except Exception as e:
            return {"query": query, "results": [], "error": str(e)[:80]}


def _search_many(queries: list, today_only: bool) -> list:
    """여러 각도를 동시에. 하나가 실패해도 나머지는 쓴다. 돌려주는 순서는 던진 순서."""
    if len(queries) == 1:
        return [_one(queries[0], today_only)]
    with ThreadPoolExecutor(max_workers=len(queries)) as pool:
        return list(pool.map(lambda q: _one(q, today_only), queries))


def _merge(batches: list) -> list:
    """주소가 같은 결과는 하나로. 각 검색의 1등, 2등… 순으로 번갈아 섞어 한쪽에 치우치지 않게."""
    seen, out = set(), []
    for rank in range(max((len(b["results"]) for b in batches), default=0)):
        for b in batches:
            if rank < len(b["results"]):
                r = b["results"][rank]
                key = r.get("url") or r.get("title")
                if key not in seen:
                    seen.add(key)
                    out.append(r)
    return out[:MERGED_MAX]


def _next_queries(decision: dict, tried: list) -> list:
    raw = decision.get("next_queries")
    if not isinstance(raw, list):
        raw = [decision.get("next_query", "")]       # 예전 형식도 받는다
    out = []
    for q in raw:
        q = str(q or "").strip()[:200]
        if q and not _similar_to_prior(q, tried + out):
            out.append(q)
    return out[:PARALLEL]


def explore(question: str, today_hint: bool, ask_llm, max_tries: int = MAX_TRIES) -> dict:
    """
    ask_llm(messages) -> dict  (brain._ask)
    돌려주는 값:
      {"results": [...], "today_only": bool, "trail": [{query, verdict, reason}], "found": bool}
    """
    if not search.available():
        return {"results": [], "today_only": today_hint, "trail": [], "found": False,
                "note": "검색을 쓸 수 없음 (BRAVE_API_KEY 없음)"}

    rounds = max(1, min(int(max_tries), MAX_TRIES))
    today_only = today_hint
    queries = [question.strip()[:200]]
    tried, trail, best = [], [], []

    for attempt in range(1, rounds + 1):
        batches = _search_many(queries, today_only)
        tried.extend(queries)
        errors = [b for b in batches if b["error"]]
        results = _merge([b for b in batches if not b["error"]])
        if errors and not results:
            trail.append({"query": " / ".join(queries), "verdict": "error",
                          "reason": errors[0]["error"]})
            break
        if len(results) > len(best):
            best = results

        last = attempt == rounds
        decision = _judge(question, queries, results, today_only, tried, ask_llm, last)
        trail.append({"query": " / ".join(queries), "verdict": decision["verdict"],
                      "reason": decision.get("reason", ""), "hits": len(results),
                      "parallel": len(queries)})

        if decision["verdict"] == "enough" or last:
            return {"results": results or best, "today_only": today_only,
                    "trail": trail, "found": bool(results or best)}
        if decision["verdict"] == "giveup":
            return {"results": best, "today_only": today_only, "trail": trail,
                    "found": bool(best)}

        today_only = bool(decision.get("today_only", today_only))
        nxt = _next_queries(decision, tried)
        if not nxt:
            # 다 이전과 비슷하면 한 번 더, 분명히 다르게 바꾸라고 되돌린다
            retry = _judge(question, queries, results, today_only, tried, ask_llm, last,
                           nudge="방금 제안한 검색어들이 이전과 너무 비슷하다. 분명히 다른 각도로 바꿔라.")
            nxt = _next_queries(retry, tried)
            if not nxt:
                return {"results": best, "today_only": today_only, "trail": trail,
                        "found": bool(best)}
        queries = nxt

    return {"results": best, "today_only": today_only, "trail": trail, "found": bool(best)}


def _judge(question, queries, results, today_only, tried, ask_llm, last, nudge="") -> dict:
    brief = "\n".join(
        f"- ({r.get('published') or '시각 모름'}) {r['title']}: {r['snippet'][:120]}"
        for r in results[:MERGED_MAX]) or "(결과 없음)"
    user = (f"원래 찾는 것: {question}\n"
            f"이번에 던진 검색어: {queries}\n지금까지 던진 검색어: {tried}\n"
            f"오늘 자료만 모드: {today_only}\n\n결과:\n{brief}")
    if last:
        user += "\n\n(이번이 마지막 판단이다. 모인 것으로 충분하면 enough, 아니면 giveup.)"
    if nudge:
        user += "\n\n" + nudge
    out = ask_llm([{"role": "system", "content": PLAN}, {"role": "user", "content": user}])
    if out.get("verdict") not in ("enough", "retry", "giveup"):
        out["verdict"] = "enough" if results else "giveup"
    return out


def trail_text(trail: list) -> str:
    """사용자·기록에 보여 줄 탐색 과정 요약."""
    if not trail:
        return ""
    lines = [f"{i}. '{t['query']}' → {t['verdict']}"
             + (f" ({t['reason']})" if t.get("reason") else "") for i, t in enumerate(trail, 1)]
    return "검색 과정:\n" + "\n".join(lines)
