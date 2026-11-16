"""
recur.py — 반복 규칙 (일정·알림 공통).

규칙은 네 가지뿐: 매일 · 평일(월~금) · 매주 · 매월.
계산은 한국 시각의 '벽시계'로 한다: 매주 화요일 15:00 은 언제나 화요일 15:00.
매월 31일처럼 그 달에 없는 날은 그 달의 마지막 날로 (2월 → 28/29일).
"""
import calendar
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))
RULES = ("daily", "weekdays", "weekly", "monthly")
LABEL = {"daily": "매일", "weekdays": "평일", "weekly": "매주", "monthly": "매월"}
MAX_STEPS = 4000           # 아주 오래된 시작점에서 앞으로 셀 때의 안전 한도


def valid(rule) -> bool:
    return rule is None or rule in RULES


def _parse(v) -> datetime:
    d = v if isinstance(v, datetime) else datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    if d.tzinfo is None:
        d = d.replace(tzinfo=KST)
    return d.astimezone(KST)


def _add_month(d: datetime, n: int, day: int) -> datetime:
    y, m = divmod(d.month - 1 + n, 12)
    y, m = d.year + y, m + 1
    return d.replace(year=y, month=m, day=min(day, calendar.monthrange(y, m)[1]))


def step(d: datetime, rule: str, anchor_day: int | None = None) -> datetime:
    """한 회차 뒤. anchor_day 는 매월 규칙에서 원래 날짜(31일 등)를 기억하기 위한 것."""
    if rule == "daily":
        return d + timedelta(days=1)
    if rule == "weekly":
        return d + timedelta(days=7)
    if rule == "weekdays":
        n = d + timedelta(days=1)
        while n.weekday() >= 5:              # 토(5)·일(6) 건너뛰기
            n += timedelta(days=1)
        return n
    if rule == "monthly":
        return _add_month(d, 1, anchor_day or d.day)
    raise ValueError(f"모르는 반복 규칙: {rule}")


def first_on_or_after(start, rule: str, after) -> datetime:
    """시작점 start 에서 규칙대로 셌을 때, after 이후(같거나 뒤) 첫 회차."""
    s, a = _parse(start), _parse(after)
    if rule == "weekdays" and s.weekday() >= 5:     # 주말에 시작했으면 다음 평일부터
        s = step(s, rule)
    if s >= a:
        return s
    anchor = s.day
    # 일·주 단위는 한 번에 건너뛰어 계산을 줄인다
    if rule in ("daily", "weekly"):
        period = 1 if rule == "daily" else 7
        k = ((a - s).days // period)
        s = s + timedelta(days=k * period)
    elif rule == "monthly":
        months = (a.year - s.year) * 12 + (a.month - s.month)
        s = _add_month(s, max(0, months - 1), anchor)
    for _ in range(MAX_STEPS):
        if s >= a:
            return s
        s = step(s, rule, anchor)
    raise ValueError("반복 계산이 너무 길어요")


def next_after(due, rule: str, now, start=None) -> datetime:
    """보낸 알림의 다음 회차: due 다음 회차부터 now 보다 뒤인 첫 회차 (밀린 회차는 건너뛴다)."""
    base = _parse(start) if start is not None else _parse(due)
    after = max(_parse(due) + timedelta(seconds=1), _parse(now) + timedelta(seconds=1))
    return first_on_or_after(base, rule, after)


def occurrences(start, rule: str, frm, to, limit: int = 200) -> list:
    """[frm, to) 안의 회차 시작 시각들."""
    out = []
    t = _parse(to)
    try:
        d = first_on_or_after(start, rule, frm)
    except ValueError:
        return out
    anchor = _parse(start).day
    while d < t and len(out) < limit:
        out.append(d)
        d = step(d, rule, anchor)
    return out
