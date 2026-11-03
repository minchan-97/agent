"""
db.py — Supabase 연결 두 가지.

  for_user(token)  사용자 요청용. 그 사람의 로그인 토큰으로 접속하므로
                   RLS 가 그대로 적용된다. 코드에 실수가 있어도
                   남의 행은 DB 가 안 보여준다. 요청 처리는 전부 이것만 쓴다.

  for_cron()       크론 전용 service role. RLS 를 우회하므로
                   모든 쿼리에 user_id 를 반드시 직접 건다.
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
