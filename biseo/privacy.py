"""
privacy.py — 바깥으로 나가는 글을 거른다.

몰랑이 notify.py 의 민감어 필터를 넓혔다. 알림뿐 아니라 LLM 으로 나가는
모든 요청에도 건다. 교사 업무에는 학생 정보가 섞이기 쉽다.
"""
import re

# 알림으로는 아예 안 보낸다 (ntfy 서버를 거친다)
SENSITIVE = re.compile(r"(비밀번호|주민등록|계좌|카드번호|전화번호|주소는|사는 곳은|상담 내용)")

_MASKS = [
    (re.compile(r"\b\d{6}-?[1-4]\d{6}\b"), "[주민번호]"),
    (re.compile(r"\b01[016789]-?\d{3,4}-?\d{4}\b"), "[전화번호]"),
    (re.compile(r"\b\d{4}-?\d{4}-?\d{4}-?\d{4}\b"), "[카드번호]"),
    (re.compile(r"\b\d{2,6}-\d{2,6}-\d{2,8}\b"), "[계좌번호]"),
]


def mask(text: str) -> str:
    """LLM 으로 보내기 전에 번호류를 가린다. 뜻을 이해하는 데 번호는 필요 없다."""
    out = text or ""
    for pat, rep in _MASKS:
        out = pat.sub(rep, out)
    return out


def ok_to_notify(text: str) -> bool:
    return not SENSITIVE.search(text or "")
