"""
search.py — Brave 검색 (몰랑이 molang_search 에서 물려받음).

격리 원칙 그대로: 검색 결과는 답변 문맥으로만 쓰인다.
facts 에는 절대 들어가지 않는다 (DB 제약 only_user_absorbs 가 한 번 더 막는다).
"""
import os
import requests

BRAVE_ENDPOINT = 'https://api.search.brave.com/res/v1/web/search'


def available() -> bool:
    return bool(os.environ.get('BRAVE_API_KEY'))


def brave(query: str, count: int = 5) -> list:
    r = requests.get(
        BRAVE_ENDPOINT,
        headers={'Accept': 'application/json',
                 'X-Subscription-Token': os.environ['BRAVE_API_KEY']},
        params={'q': query, 'count': count, 'country': 'KR', 'search_lang': 'ko'},
        timeout=10)
    r.raise_for_status()
    return [{'title': x.get('title', ''), 'url': x.get('url', ''),
             'snippet': x.get('description', '')}
            for x in r.json().get('web', {}).get('results', [])]


def to_context(results: list) -> str:
    if not results:
        return ''
    lines = [f"[{i}] {r['title']}\n{r['snippet']}\n출처: {r['url']}"
             for i, r in enumerate(results, 1)]
    return ("[외부 검색 결과 — 이것은 네 기억도, 사용자에 대한 사실도 아니다]\n"
            + "\n\n".join(lines))
