"""
contra.py — 판단 사이의 모순 검사 (arcogit 순환검증).

원칙 (몰랑이에서 온 것)
  LLM 의 말은 '그럴듯함'에 맞춰져 있지 참에 맞춰져 있지 않다.
  그래서 윗단은 말이 참인지 판정하려 하지 않는다. 대신 이미 확정된 판단들과 서로 어긋나는지만 본다.

확정된 판단 (기준이 되는 쪽)
  self    자기 모형 — 할 수 있는 일(CAPABILITIES) · 못 하는 일(CANNOT)      selfknow.py
  acted   이번 턴에 실제로 일어난 일 — 행동마다 상태(done·scheduled·proposed·blocked)

답에서 뽑는 판단 (검사받는 쪽)
  did     "알림 맞춰 뒀어요" — 했다는 말
  cannot  "알림은 못 해요" — 못 한다는 말
  can     "카톡으로 보내 드릴게요" — 한다는 말
  vague   자기 질문에 기능 이름 없이 빈말 (selfknow.is_vague)

순환
  1) 검사 → 2) 모순을 짚어 다시 쓰게 함 → 3) 다시 검사 → 4) 남은 모순은 그 문장을 빼고 바로잡는 한 줄을 붙임
  모순이 났던 판단 경로는 약하게 배운다 (judge) — 다음 판단이 그 길로 덜 간다.
"""
import re

from . import selfknow

# 한 문장 안에서 '무엇을' 했다/못 한다고 말하는지
_NOUN = [
    ("event", r"(일정|약속|회의|캘린더에)"),
    ("reminder", r"(알림|리마인더|알려 ?드릴게)"),
    ("note", r"(메모|기록해)"),
    ("draft", r"(초안)"),
    ("doc", r"(문서|파일|양식|워드)"),
]
_DID = re.compile(r"(넣었|넣어 ?(?:뒀|두었|놨|놓았|드렸)|등록했|등록해 ?(?:뒀|드렸)|추가했|추가해 ?(?:뒀|드렸)|저장했|저장해 ?(?:뒀|드렸)|"
                  r"맞춰 ?(?:뒀|두었|놨|놓았|드렸)|설정했|설정해 ?(?:뒀|드렸)|만들었|만들어 ?(?:뒀|드렸)|고쳤|고쳐 ?(?:뒀|드렸)|"
                  r"적어 ?(?:뒀|두었|놨|드렸)|기록했|기록해 ?(?:뒀|드렸)|잡아 ?(?:뒀|드렸)|잡았)")
_SENT = re.compile(r"(보냈|보내 ?드렸|전송했|발송했)")
_SEND_PROMISE = re.compile(r"(보내 ?드릴게|보내 ?드리겠|전송해 ?드릴|발송해 ?드릴|전화 ?(?:걸어|해) ?드릴|결제해 ?드릴|"
                           r"(?:구글|아이폰) ?캘린더에 ?(?:넣|추가|등록)|카톡으로 ?보내|로그인해 ?(?:서|드릴))")
_REFUSE = re.compile(r"(못 ?해|못 ?하|할 수 없|할 수가 없|지원하지 않|어려워|안 돼요|드릴 수 없|불가능)")
_NEG = re.compile(r"(않|못|없|안 |말고|대신)")
_PAST = re.compile(r"(지난|예전|어제|이미|았던|었던|했던|둔 |뒀던)")
# 못 하는 쪽 낱말 — 이게 있으면 '못 한다'는 말은 자기 모형과 맞다
_CANNOT_WORDS = re.compile(r"(보내|전송|메일함|카톡|문자를|전화|결제|로그인|구글|아이폰 캘린더|연동|연결|업무 ?시스템|그림|이미지|"
                           r"다른 사람|공유|앱을 열|대신)")
_DONE = ("done", "scheduled")
_KIND_OF = {"event": ("event",), "reminder": ("reminder",), "note": ("note",), "draft": ("draft",),
            "doc": ("doc_edit", "doc_new", "doc_make")}
_LABEL = {"event": "일정", "reminder": "알림", "note": "메모", "draft": "초안", "doc": "문서"}


def sentences(text: str) -> list:
    parts = re.split(r"(?<=[.!?。\n])\s*", text or "")
    return [p.strip() for p in parts if p and p.strip()]


def _what(s: str):
    for k, rx in _NOUN:
        if re.search(rx, s):
            return k
    return None


def check(reply: str, acted: list, user_text: str = "") -> list:
    """답의 문장마다 확정된 판단과 어긋나는지. [{type, sentence, against, fix}]"""
    out = []
    by_kind = {}
    for a in acted or []:
        by_kind.setdefault(a.get("kind"), []).append(a.get("status"))
    for s in sentences(reply):
        neg = bool(_NEG.search(s))
        # did: 했다고 했는데 이번에 그 일이 일어나지 않았거나 확인을 기다리는 중
        if _DID.search(s) and not neg and not _PAST.search(s):
            k = _what(s)
            if k:
                st = [x for kk in _KIND_OF[k] for x in by_kind.get(kk, [])]
                if not any(x in _DONE for x in st):
                    waiting = "proposed" in st
                    out.append({"type": "did", "sentence": s, "cap": k,
                                "against": f"이번에 실제로 한 일: {_LABEL[k]} " + ("— 확인을 기다리는 중" if waiting else "없음"),
                                "fix": (f"{_LABEL[k]}은(는) 확인함에 올려 뒀어요. 확인을 누르시면 그때 들어가요." if waiting
                                        else f"{_LABEL[k]}은(는) 아직 넣지 않았어요. 원하시면 다시 말씀해 주세요.")})
                    continue
        # sent / can: 못 하는 일(직접 보내기·외부 연결)을 했다거나 하겠다고 함
        if (_SENT.search(s) or _SEND_PROMISE.search(s)) and not neg:
            out.append({"type": "can", "sentence": s, "cap": None,
                        "against": "못 하는 일: " + selfknow.CANNOT[0].split(" (")[0],
                        "fix": "보내기는 제가 직접 하지 못해요. 초안을 만들어 드리면 휴대폰 공유로 보내실 수 있어요."})
            continue
        # cannot: 할 수 있는 일을 못 한다고 함
        if _REFUSE.search(s) and not _CANNOT_WORDS.search(s):
            k = _what(s)
            if k:
                cap = next(c for c in selfknow.CAPABILITIES if c[0] in (k, "schedule" if k == "event" else k))
                out.append({"type": "cannot", "sentence": s, "cap": k,
                            "against": f"할 수 있는 일: {cap[1]} — {cap[2].split('. ')[0]}",
                            "fix": f"{cap[1]}은(는) 할 수 있어요. 예: {cap[3]}"})
    if selfknow.asks_about_me(user_text) and (selfknow.asks_compare(user_text) or selfknow.asks_improvement(user_text)) \
            and (selfknow.is_vague_autonomy(reply) if selfknow.asks_autonomy(user_text) else selfknow.is_vague(reply)):
        out.append({"type": "vague", "sentence": "", "cap": None,
                    "against": "자기 모형: 기능 이름과 실제 쓰임으로 말해야 함", "fix": ""})
    return out


def prompt_text(found: list) -> str:
    """다시 쓰게 할 때 넣는 글: 어느 문장이 어느 확정된 판단과 어긋나는지."""
    lines = [f"- \"{f['sentence'][:80]}\" ↔ {f['against']}" for f in found if f["type"] != "vague"]
    if any(f["type"] == "vague" for f in found):
        lines.append("- 자기 소개·비교·개선 답이 기능 이름 없이 빈말이다 ↔ [나에 대해] 목록과 K 쓰임(자율 질문이면 A 상태 숫자)으로만 말할 것")
    return ("[모순 검사 — 네 답이 이미 확정된 판단과 어긋난다]\n" + "\n".join(lines) +
            "\n참·거짓을 다투지 말고, 확정된 쪽에 맞게 reply 만 다시 써라. 행동(actions)은 새로 만들지 마라.")


def repair(reply: str, found: list) -> str:
    """다시 써도 남은 모순: 그 문장을 빼고 바로잡는 말을 붙인다 (빈말은 호출한 쪽이 따로 처리)."""
    bad = {f["sentence"] for f in found if f["sentence"]}
    kept = [s for s in sentences(reply) if s not in bad]
    fixes = list(dict.fromkeys(f["fix"] for f in found if f["fix"]))
    return " ".join(kept + fixes).strip()
