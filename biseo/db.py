"""
db.py — Supabase 연결.

  for_user(token)  사용자 요청용. 로그인 토큰으로 접속 → RLS 적용
  for_cron()       크론 전용 service role. RLS 를 우회한다.

  Scoped(sb, uid)  둘 중 무엇이든 감싸서, 모든 읽기·고치기·지우기에
                   user_id 조건을 자동으로 걸고, 쓰기에는 user_id 를 넣는다.

왜 Scoped 인가
  과제가 생기면서 크론(service role)이 LLM 을 부르고 행동까지 제안하게 됐다.
  "코드에서 user_id 를 빠뜨리지 않기"를 사람의 주의에 맡기면 언젠가 샌다.
  그래서 몰랑이처럼 주의가 아니라 구조로 막는다. 코어 모듈은 Scoped 만 받는다.
"""
import os
from supabase import create_client


def _url() -> str:
    return os.environ["SUPABASE_URL"]


def for_user(token: str):
    sb = create_client(_url(), os.environ["SUPABASE_ANON_KEY"])
    sb.postgrest.auth(token)
    return sb


def user_id_from_token(token: str) -> str | None:
    try:
        sb = create_client(_url(), os.environ["SUPABASE_ANON_KEY"])
        res = sb.auth.get_user(token)
        return res.user.id if res and res.user else None
    except Exception:
        return None


def for_cron():
    return create_client(_url(), os.environ["SUPABASE_SERVICE_ROLE_KEY"])


def _own(data, uid):
    if isinstance(data, list):
        return [{**d, "user_id": uid} for d in data]
    return {**data, "user_id": uid}


class _ScopedTable:
    def __init__(self, t, uid: str):
        self._t, self._uid = t, uid

    def select(self, *a, **k):
        return self._t.select(*a, **k).eq("user_id", self._uid)

    def insert(self, data, **k):
        return self._t.insert(_own(data, self._uid), **k)

    def upsert(self, data, **k):
        return self._t.upsert(_own(data, self._uid), **k)

    def update(self, data, **k):
        return self._t.update(data, **k).eq("user_id", self._uid)

    def delete(self, **k):
        return self._t.delete(**k).eq("user_id", self._uid)


class Scoped:
    """한 사람 몫의 DB. uid 가 없으면 만들 수 없다."""

    def __init__(self, sb, uid: str):
        if not uid:
            raise ValueError("user_id 없이 DB 를 열 수 없음")
        self.raw, self.uid = sb, uid

    def table(self, name: str):
        return _ScopedTable(self.raw.table(name), self.uid)
