"""
storage.py — Supabase Storage(파일 저장소)를 REST 로 직접 부른다.

왜 supabase-py 의 storage 를 안 쓰나
  사용자 토큰(RLS)을 저장소 요청에 확실히 싣기 위해. 라이브러리 버전에 따라
  postgrest.auth(token) 이 storage 쪽에는 안 실리는 경우가 있어, 남의 권한(anon)으로
  나가거나 실패한다. 여기서는 모든 요청에 사용자 토큰을 직접 붙인다.

파일 경로 규칙: docs/{user_id}/{무작위}.{확장자}
  저장소 정책이 첫 폴더 = auth.uid() 인 파일만 읽고 쓰게 막는다 (00_schema.sql).
"""
import os
from urllib.parse import quote

import requests

BUCKET = "docs"
TIMEOUT = 30


class StorageError(RuntimeError):
    pass


def _base() -> str:
    return os.environ["SUPABASE_URL"].rstrip("/") + "/storage/v1"


def _h(token: str, key: str | None = None) -> dict:
    return {"Authorization": f"Bearer {token}", "apikey": key or os.environ["SUPABASE_ANON_KEY"]}


def _q(path: str) -> str:
    return "/".join(quote(p, safe="") for p in path.split("/"))


def _check(r, what):
    if r.status_code >= 300:
        raise StorageError(f"{what} 실패 ({r.status_code})")
    return r


def download(token: str, path: str, max_bytes: int) -> bytes:
    r = requests.get(f"{_base()}/object/authenticated/{BUCKET}/{_q(path)}",
                     headers=_h(token), timeout=TIMEOUT, stream=True)
    if r.status_code == 404 or r.status_code == 400:
        raise FileNotFoundError("저장소에 파일이 없어요")
    _check(r, "파일 받기")
    buf = bytearray()
    for chunk in r.iter_content(256 * 1024):
        buf += chunk
        if len(buf) > max_bytes:
            r.close()
            raise ValueError("파일이 너무 커요")
    return bytes(buf)


def upload(token: str, path: str, data: bytes, content_type: str):
    _check(requests.post(f"{_base()}/object/{BUCKET}/{_q(path)}", data=data, timeout=TIMEOUT,
                         headers={**_h(token), "Content-Type": content_type or "application/octet-stream",
                                  "x-upsert": "false"}), "파일 올리기")


def remove(token: str, paths: list):
    if paths:
        requests.delete(f"{_base()}/object/{BUCKET}", json={"prefixes": paths},
                        headers=_h(token), timeout=TIMEOUT)


def signed_url(token: str, path: str, download_name: str | None = None, seconds: int = 300) -> str:
    r = _check(requests.post(f"{_base()}/object/sign/{BUCKET}/{_q(path)}", json={"expiresIn": seconds},
                             headers=_h(token), timeout=TIMEOUT), "주소 만들기")
    url = os.environ["SUPABASE_URL"].rstrip("/") + "/storage/v1" + r.json()["signedURL"]
    if download_name:
        url += ("&" if "?" in url else "?") + "download=" + quote(download_name)
    return url


def purge_user(uid: str):
    """계정 삭제 때: 그 사람 폴더의 파일을 모두 지운다 (service role)."""
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not key:
        return
    h = _h(key, key)
    for _ in range(50):                                   # 한 번에 1000개씩
        r = requests.post(f"{_base()}/object/list/{BUCKET}", headers=h, timeout=TIMEOUT,
                          json={"prefix": uid, "limit": 1000, "offset": 0})
        names = [f"{uid}/{o['name']}" for o in (r.json() if r.ok else []) if o.get("name")]
        if not names:
            return
        requests.delete(f"{_base()}/object/{BUCKET}", json={"prefixes": names}, headers=h, timeout=TIMEOUT)
