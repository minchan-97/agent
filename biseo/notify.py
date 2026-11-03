"""
notify.py — 앱을 안 열어도 닿게 (몰랑이 notify 에서 물려받음).

바뀐 점: 주제(topic)가 계정마다 다르다. 환경변수가 아니라 agents 행에 있다.
주제 이름이 곧 비밀번호다. 그래서 서버가 길고 뜻 없는 이름을 만들어 준다.
"""
import os
import urllib.request

from .privacy import ok_to_notify

TIMEOUT = 8


def send(topic: str, title: str, body: str, tags: str = "bell") -> dict:
    if not topic:
        return {"ok": False, "why": "알림 주제가 없음"}
    text = (body or "").strip()
    if not text:
        return {"ok": False, "why": "보낼 말이 없음"}
    if not ok_to_notify(text):
        return {"ok": False, "why": "민감한 내용이라 안 보냄"}
    server = (os.environ.get("NTFY_SERVER") or "https://ntfy.sh").rstrip("/")
    headers = {"Title": (title or "비서").encode("utf-8"), "Tags": tags,
               "Content-Type": "text/plain; charset=utf-8", "Cache": "no"}
    try:
        req = urllib.request.Request(f"{server}/{topic}", data=text.encode("utf-8"),
                                     headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return {"ok": 200 <= r.status < 300}
    except Exception as e:
        return {"ok": False, "why": str(e)[:80]}
