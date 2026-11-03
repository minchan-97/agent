"""
norm.py — 정규화·중복·에코 판정 (몰랑이 molang_norm 에서 물려받음).

바뀐 점
  · '찬기' 고정 치환을 없앴다. 계정마다 사용자가 다르다.
  · 에코 표지에 비서 이름을 넣는다. 이름은 사용자가 정하니까.
"""
import re
from datetime import timedelta

_PARTICLES = r'(은|는|이|가|을|를|의|에|에서|로|으로|와|과|도|만|랑|이랑)'

STATE_PATTERNS = ['현재', '지금', '방금', '오늘', '에 있다', '중이다', '중이야',
                  '중인', '계획이다', '할 계획', '까지 해야']
BELIEF_SUFFIXES = ['고 생각한다', '고 여긴다', '고 믿는다', '고 본다']
PREF_PATTERNS = ['좋아한다', '싫어한다', '선호', '중요하게 생각']

TTL = {'state': timedelta(hours=12), 'fact': None, 'preference': None}


def normalize(text: str) -> str:
    t = text.strip()
    t = re.sub(r'^(사용자는|사용자가|사용자)', '', t)
    t = re.sub(r'[^\w가-힣 ]', '', t)
    t = re.sub(r'\s+', ' ', t)
    t = re.sub(_PARTICLES + r'\b', '', t)
    return t.strip()


def tokens(text: str) -> set:
    return set(normalize(text).split())


def jaccard(a: str, b: str) -> float:
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def is_agent_echo(text: str, agent_name: str = '') -> bool:
    """비서 자기 말이 사용자 사실로 굳으려는 것인지."""
    markers = ['제가 도와', '도와드릴', '알려드릴', '해 드릴게', '말씀하신']
    if agent_name:
        markers.append(agent_name)
    if any(m in text for m in markers):
        return True
    if len(text) > 55 and not text.rstrip().endswith(('다', '다.')):
        return True
    return False


def classify_kind(text: str) -> str:
    t = text.rstrip('. ')
    is_belief = any(b in t for b in BELIEF_SUFFIXES)
    if not is_belief and any(p in text for p in STATE_PATTERNS):
        return 'state'
    if any(p in text for p in PREF_PATTERNS) or is_belief:
        return 'preference'
    return 'fact'
