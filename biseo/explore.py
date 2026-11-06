"""
explore.py — 자율 검색 탐색.

한 번 던지고 끝내지 않는다. 비서가 스스로 결과를 채점하고, 부족하면
'왜 부족한지' 진단해서 다음 검색을 다른 각도로 바꾼다. 충분하면 멈춘다.

자유는 넓게 (각도를 어떻게 바꿀지는 비서가 정한다)
울타리는 분명하게
  · 한 탐색당 검색 MAX_TRIES 회까지
  · 이미 던진 검색어와 거의 같으면(토큰 80% 겹침) 버리고 한 번 더 바꾸게 한다
  · 끝까지 못 찾으면 지어내지 말고 "이런 각도를 시도했지만 못 찾았다"고 보고

이 모듈은 검색을 '하고', 과정을 기록으로 돌려줄 뿐 답을 짓지 않는다.
답은 brain/tasks 가 모인 자료를 근거로 짓는다 (시점·출처 규칙은 그대로 적용).
"""
import json

from . import search
from .norm import jaccard

MAX_TRIES = 5
TOO_SIMILAR = 0.8

PLAN = """
너는 검색으로 사실을 찾는 중이다. JSON 객체 하나로만 답해.
{
  "verdict": "enough" | "retry" | "giveup",
  "reason": "지금 결과를 어떻게 봤는지 한 문장",
  "next_query": "retry 일 때 다음에 던질 검색어 (지금까지와 다른 각도로)",
  "today_only": true | false
}

각도를 바꾸는 법(예시일 뿐, 스스로 판단해라)
- 결과가 너무 넓다 → 더 구체적인 말, 고유명사, 기간을 넣어 좁힌다
- 쓰는 말이 어긋났다 → 동의어·정식 명칭·영어 용어로 바꾼다
- 출처가 부실하다 → 기관·공식 사이트 이름을 넣거나 'site:' 로 범위를 준다
- 너무 오래된 것뿐이다 → 올해·이번 달, 또는 today_only 를 켠다
- 다른 측면이 필요하다 → 원인·반응·비교 등 다른 각을 질문에 넣는다

enough: 질문에 답할 근거가 모였다. giveup: 각도를 여러 번 바꿔도 안 나온다.
같은 검색어를 되풀이하지 마라.
"""


def _similar_to_prior(q: str, tried: list) -> bool:
    return any(jaccard(q, t) >= TOO_SIMILAR for t in tried)


def explore(question: str, today_hint: bool, ask_llm, max_tries: int = MAX_TRIES) -> dict:
    """
    ask_llm(messages) -> dict  (brain._ask)
    돌려주는 값:
      {"results": [...], "today_only": bool, "trail": [{query, verdict, reason}], "found": bool}
    """
    if not search.available():
        return {"results": [], "today_only": today_hint, "trail": [], "found": False,
                "note": "검색을 쓸 수 없음 (BRAVE_API_KEY 없음)"}

    today_only = today_hint
    query = question.strip()[:200]
    tried, trail, best = [], [], []

    for attempt in range(1, max_tries + 1):
        try:
            got = search.find(query, today_only, count=6)
            results = got["results"]
        except Exception as e:
            trail.append({"query": query, "verdict": "error", "reason": str(e)[:80]})
            break
        tried.append(query)
        if len(results) > len(best):
            best = results

        last = attempt == max_tries
        decision = _judge(question, query, results, today_only, tried, ask_llm, last)
        trail.append({"query": query, "verdict": decision["verdict"],
                      "reason": decision.get("reason", ""), "hits": len(results)})

        if decision["verdict"] == "enough" or last:
            return {"results": results or best, "today_only": today_only,
                    "trail": trail, "found": bool(results or best)}
        if decision["verdict"] == "giveup":
            return {"results": best, "today_only": today_only, "trail": trail,
                    "found": bool(best)}

        nxt = str(decision.get("next_query", "")).strip()[:200]
        today_only = bool(decision.get("today_only", today_only))
        if not nxt or _similar_to_prior(nxt, tried):
            # 너무 비슷하면 한 번 더, 분명히 다르게 바꾸라고 되돌린다
            retry = _judge(question, query, results, today_only, tried, ask_llm, last,
                           nudge="방금 제안한 검색어가 이전과 너무 비슷하다. 분명히 다른 각도로 바꿔라.")
            nxt = str(retry.get("next_query", "")).strip()[:200]
            if not nxt or _similar_to_prior(nxt, tried):
                return {"results": best, "today_only": today_only, "trail": trail,
                        "found": bool(best)}
        query = nxt

    return {"results": best, "today_only": today_only, "trail": trail, "found": bool(best)}


def _judge(question, query, results, today_only, tried, ask_llm, last, nudge="") -> dict:
    brief = "\n".join(
        f"- ({r.get('published') or '시각 모름'}) {r['title']}: {r['snippet'][:120]}"
        for r in results[:6]) or "(결과 없음)"
    user = (f"원래 찾는 것: {question}\n"
            f"이번 검색어: {query}\n지금까지 던진 검색어: {tried}\n"
            f"오늘 자료만 모드: {today_only}\n\n결과:\n{brief}")
    if last:
        user += "\n\n(이번이 마지막 시도다. enough 로 가진 말고, 모인 것으로 충분하면 enough, 아니면 giveup.)"
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
