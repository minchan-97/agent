"""
search.py — Brave 검색 (몰랑이 molang_search 에서 물려받음).

격리 원칙 그대로: 검색 결과는 답변 문맥으로만 쓰인다.
facts 에는 절대 들어가지 않는다 (DB 제약 only_user_absorbs 가 한 번 더 막는다).

시점 원칙 (코스피 사건 이후 추가)
  "11월 6일 코스피"로 찾으면 2020·2024·2025년 11월 6일 기사가 섞여 나오고,
  어제 장중 기사가 오늘 것처럼 보이기도 한다. 그래서
  · 결과마다 작성 시점을 붙여서 넘긴다 (모르면 "알 수 없음")
  · "오늘·지금" 질문이면 검색어에 오늘 날짜를 붙이고, 최근 하루 결과만 요청하고,
    작성 날짜가 오늘이 아닌 결과는 아예 버린다 (규칙이 먼저, 모델 판단은 그다음)
"""
import os
import re
from datetime import datetime, timedelta, timezone

import requests

BRAVE_ENDPOINT = 'https://api.search.brave.com/res/v1/web/search'
KST = timezone(timedelta(hours=9))

_TODAY_WORDS = re.compile(r"(오늘|지금|현재|방금|실시간|금일|장중|이 시각)")
_HAS_DATE = re.compile(r"(\d{1,2}\s*월\s*\d{1,2}\s*일|\d{4}[-./]\d{1,2}|\d{4}\s*년)")


def available() -> bool:
    return bool(os.environ.get('BRAVE_API_KEY'))


def now_kst() -> datetime:
    return datetime.now(KST)


def wants_today(text: str) -> bool:
    return bool(_TODAY_WORDS.search(text or ""))


def dated_query(query: str) -> str:
    """오늘을 묻는데 날짜가 없으면 '2026년 11월 6일'을 붙인다 (해가 다른 같은 날짜 기사를 피한다)."""
    if _HAS_DATE.search(query):
        return query
    d = now_kst()
    return f"{query} {d.year}년 {d.month}월 {d.day}일"


def _published(r: dict):
    """Brave 의 page_age(ISO 시각)를 한국 시각으로. 없으면 None."""
    raw = r.get('page_age')
    if not raw:
        return None
    try:
        d = datetime.fromisoformat(str(raw).replace('Z', '+00:00'))
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d.astimezone(KST)
    except ValueError:
        return None


def brave(query: str, count: int = 5, freshness: str | None = None) -> list:
    params = {'q': query, 'count': count, 'country': 'KR', 'search_lang': 'ko'}
    if freshness:
        params['freshness'] = freshness          # pd = 최근 하루
    r = requests.get(
        BRAVE_ENDPOINT,
        headers={'Accept': 'application/json',
                 'X-Subscription-Token': os.environ['BRAVE_API_KEY']},
        params=params, timeout=10)
    r.raise_for_status()
    out = []
    for x in r.json().get('web', {}).get('results', []):
        item = {'title': x.get('title', ''), 'url': x.get('url', ''),
                'snippet': x.get('description', ''), 'age': x.get('age', ''),
                'page_age': x.get('page_age')}
        pub = _published(item)
        item['published'] = pub.strftime('%Y-%m-%d %H:%M') if pub else None
        item['published_date'] = pub.date().isoformat() if pub else None
        out.append(item)
    return out


def find(query: str, today_only: bool, count: int = 6) -> dict:
    """
    시점 원칙을 지키는 검색.
    돌려주는 값: {"query": 실제 검색어, "results": 쓸 결과, "dropped": 버린 개수}
    """
    q = dated_query(query) if today_only else query
    raw = brave(q, count=count + 4 if today_only else count,
                freshness='pd' if today_only else None)
    if not today_only:
        return {"query": q, "results": raw[:count], "dropped": 0}
    today = now_kst().date().isoformat()
    keep = [r for r in raw if r.get('published_date') == today]   # 날짜를 모르는 것도 버린다
    return {"query": q, "results": keep[:count], "dropped": len(raw) - len(keep)}


def to_context(results: list, today_only: bool = False) -> str:
    if not results:
        return ''
    now = now_kst().strftime('%Y-%m-%d %H:%M')
    lines = []
    for i, r in enumerate(results, 1):
        when = r.get('published') or (r.get('age') and f"{r['age']} (정확한 시각 모름)") or "알 수 없음"
        lines.append(f"[{i}] {r['title']}\n작성: {when}\n{r['snippet']}\n출처: {r['url']}")
    head = (f"[외부 검색 결과 — 이것은 네 기억도, 사용자에 대한 사실도 아니다]\n"
            f"지금 시각(한국): {now}"
            + ("\n이 결과들은 오늘 작성된 것만 남긴 것이다." if today_only else ""))
    return head + "\n\n" + "\n\n".join(lines)
