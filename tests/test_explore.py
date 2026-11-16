"""
test_explore.py — 섞는 방식의 검색 탐색.
  · 쉬운 질문: 검색 1번, 판단 1번으로 끝
  · 어려운 질문: 2바퀴부터 각도 여러 개를 '동시에'
  · Brave 가 너무 빠르다(429)고 하면 쉬었다 다시
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from biseo import explore as ex
from biseo import search


def hit(q, n=2):
    return [{"title": f"{q} {i}", "url": f"https://x/{q}/{i}", "snippet": "s", "published": None}
            for i in range(n)]


@pytest.fixture
def calls(monkeypatch):
    log = []

    def fake_find(query, today_only, count=6):
        log.append((query, time.monotonic()))
        time.sleep(0.3)                    # 검색 한 번에 0.3초 걸린다고 치자
        return {"query": query, "results": hit(query), "dropped": 0}

    monkeypatch.setattr(search, "available", lambda: True)
    monkeypatch.setattr(search, "find", fake_find)
    return log


def judge_seq(*answers):
    it, used = iter(answers), []

    def ask(msgs):
        used.append(msgs)
        return next(it)
    ask.used = used
    return ask


def test_easy_question_is_one_search_one_judgement(calls):
    ask = judge_seq({"verdict": "enough", "reason": "충분"})
    out = ex.explore("오늘 날씨", False, ask)
    assert len(calls) == 1 and len(ask.used) == 1 and out["found"]


def test_hard_question_searches_angles_in_parallel(calls):
    ask = judge_seq(
        {"verdict": "retry", "reason": "말이 어긋남", "next_queries": ["정식 명칭", "영어 용어", "공식 사이트"]},
        {"verdict": "enough", "reason": "찾음"},
    )
    t0 = time.monotonic()
    out = ex.explore("어떤 제도", False, ask)
    took = time.monotonic() - t0

    assert [q for q, _ in calls] == ["어떤 제도", "정식 명칭", "영어 용어", "공식 사이트"]
    assert len(ask.used) == 2, "판단은 바퀴당 한 번"
    starts = [t for _, t in calls[1:]]
    assert max(starts) - min(starts) < 0.2, "2바퀴의 세 검색은 동시에 출발해야 한다"
    assert took < 0.3 * 4, "차례로 했으면 1.2초 이상 걸렸을 것"
    assert len(out["results"]) == 6 and out["trail"][1]["parallel"] == 3


def test_merge_dedupes_and_interleaves():
    a = {"results": [{"url": "u1", "title": "a1"}, {"url": "u2", "title": "a2"}]}
    b = {"results": [{"url": "u1", "title": "b1"}, {"url": "u3", "title": "b3"}]}
    assert [r["url"] for r in ex._merge([a, b])] == ["u1", "u2", "u3"]


def test_similar_angles_are_dropped(calls):
    ask = judge_seq(
        {"verdict": "retry", "next_queries": ["어떤 제도", "어떤 제도"]},     # 전부 이전과 같다
        {"verdict": "retry", "next_queries": ["어떤 제도"]},                  # 다시 시켜도 같다
    )
    out = ex.explore("어떤 제도", False, ask)
    assert len(calls) == 1 and out["found"], "새 각도가 없으면 더 검색하지 않고 가진 것으로"


def test_one_failed_angle_does_not_sink_the_round(monkeypatch):
    def flaky(query, today_only, count=6):
        if query == "고장":
            raise RuntimeError("timeout")
        return {"query": query, "results": hit(query), "dropped": 0}
    monkeypatch.setattr(search, "available", lambda: True)
    monkeypatch.setattr(search, "find", flaky)
    ask = judge_seq({"verdict": "retry", "next_queries": ["고장", "정상"]},
                    {"verdict": "enough"})
    out = ex.explore("질문", False, ask)
    assert any("정상" in r["title"] for r in out["results"])


def test_rate_limit_waits_and_retries(monkeypatch):
    class R:
        def __init__(self, code):
            self.status_code, self.headers = code, {"Retry-After": "0.2"}
        def raise_for_status(self):
            if self.status_code >= 400:
                raise RuntimeError(self.status_code)
        def json(self):
            return {"web": {"results": [{"title": "t", "url": "u", "description": "d"}]}}
    codes = iter([429, 200])
    monkeypatch.setenv("BRAVE_API_KEY", "k")
    monkeypatch.setattr(search.requests, "get", lambda *a, **k: R(next(codes)))
    slept = []
    monkeypatch.setattr(search.time, "sleep", lambda s: slept.append(s))
    assert search.brave("q")[0]["title"] == "t" and slept == [0.2]
