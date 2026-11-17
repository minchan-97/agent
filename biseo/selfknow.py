"""
selfknow.py — 비서가 '나는 무엇을 할 수 있고, 실제로 어떻게 쓰이고 있는가'를 안다.

왜 필요한가
  자기 기능 목록이 없으면, "무슨 기능을 더하면 좋을까?"에 LLM 이 상상으로 답한다
  (이미 있는 일정·알림을 새 기능이라고 하고, 보안 강화 같은 만드는 사람 몫을 사용자 기능이라고 한다).
  arcogit 으로 말하면 '생각하는 나'가 자기를 모르는 상태다.

무엇을 주는가
  CAPABILITIES   실제로 있는 기능 (이름 · 하는 일 · 부르는 법) — 코드와 같이 고친다
  CANNOT         지금 못 하는 일 — 새 기능 후보는 여기서 나온다
  usage(sb)      최근 30일 실제 쓰임: 기능별 횟수, 받아들인/넘긴 제안, 못 해 드린 부탁
  for_prompt     자기에 대한 질문(기능·사용법·개선)일 때만 길게, 평소엔 한 줄
                 쓰임 줄마다 K번호를 달아, 제안의 근거로 인용하게 한다 ('왜' 화면에 그대로 보인다)
"""
import re
from collections import Counter
from datetime import datetime, timedelta, timezone

CAPABILITIES = [
    ("schedule", "일정", "말로 일정을 넣고 고친다. 매일·평일·매주·매월 반복, '몇 분 전 알림'을 일정에 붙인다", "\"내일 3시 협의회 넣어 줘, 30분 전에 알려 줘\""),
    ("reminder", "알림", "정한 시각에 휴대폰 알림. 반복 알림, 문서에 붙은 알림(누르면 그 문서가 열림)", "\"금요일 9시에 동의서 걷기 알려 줘\""),
    ("note", "메모", "기록해 두고, 일정에 붙인다", "\"이거 메모해 줘\""),
    ("draft", "초안", "문자·메일·공지 글 초안. 초안을 워드 파일·내 양식·기존 문서로 바꾼다", "\"학부모님께 보낼 안내 문자 써 줘\""),
    ("task", "맡은 일", "여러 단계가 필요한 일을 혼자 진행(찾고·정리하고·쓰기). 물을 게 있거나 확인이 필요하면 멈추고 알린다. 끝나면 스스로 한계를 적는다", "\"체험학습 장소 후보 조사해서 정리해 줘\""),
    ("search", "검색", "최신 정보(날씨·뉴스·일정·가격)는 검색해서 출처와 기준 시각을 붙여 답한다", "\"내일 울산 날씨 어때?\""),
    ("daily", "오늘의 제안", "매일 정한 시각에 일정·맡은 일·최근 이야기를 보고 '오늘 하면 좋을 것'을 먼저 건넨다", "설정 → 오늘의 생각 시각"),
    ("doc", "문서", "엑셀·워드·PDF·한글·사진을 읽고, 물으면 근거와 함께 답하고, 고친 새 버전을 만든다. 올리면 마감·일정을 찾아 제안한다", "문서 → 올리기, 또는 \"통신문 날짜 6월 3일로 고쳐 줘\""),
    ("template", "양식", "자주 쓰는 문서를 양식으로 저장해, 내용만 말하면 같은 모양의 새 문서를 만든다", "\"체험학습 양식으로 6월 3일 박물관 써 줘\""),
    ("why", "왜", "제안·행동마다 이유 한 줄과 근거(일정·메모·문서…), 판단 경로를 보여 준다", "제안 아래 '왜' 줄을 누르기"),
    ("memory", "기억", "사용자가 말한 자기 사실을 기억하되, 확인한 것만 확실하게 쓴다. 보고·고치고·지울 수 있다", "기억 앱"),
    ("skill", "기능·버튼 만들기", "반복되는 부탁을 기능이나 홈 버튼으로 만든다 (기본 도구 범위 안에서만)", "\"회의 준비하는 기능 만들어 줘\""),
    ("voice", "음성 입력", "채팅 입력창의 마이크로 말로 입력한다", "채팅 → 마이크"),
]
CANNOT = [
    "메일·문자·카톡을 직접 보내기 (초안까지만, 보내기는 휴대폰 공유로 사람이 직접)",
    "메일함·외부 캘린더(구글·아이폰 캘린더)·학교 업무 시스템과 연결",
    "전화 걸기, 결제, 다른 앱 조작, 웹사이트 로그인",
    "사진·그림 새로 만들기",
    "다른 사람과 일정·문서 함께 쓰기",
]
LABEL = {k: n for k, n, _, _ in CAPABILITIES}
ACTION_TO_CAP = {"event": "schedule", "reminder": "reminder", "note": "note", "draft": "draft",
                 "start_task": "task", "self_task": "task", "search": "search", "doc_edit": "doc",
                 "doc_new": "doc", "doc_make": "template", "create_skill": "skill",
                 "suggest_shortcut": "skill", "pin_shortcut": "skill"}

ABOUT_ME = re.compile(r"(기능|할 수 있|할수있|뭘 할|무엇을 할|뭐 할 수|뭐가 돼|뭐 돼|사용법|어떻게 써|어떻게 쓰|개선|추가하면|"
                      r"더하면|업데이트|필요한 기능|부족한|아쉬운|너는 뭐|넌 뭐|자기소개|소개해|도움말|할 줄)")
REFUSAL = re.compile(r"(할 수 없|못 해요|못해요|못 해 드|못 합니다|지원하지 않|아직 못|아직 할 수 없|불가능)")


def asks_about_me(text: str) -> bool:
    return bool(ABOUT_ME.search(text or ""))


def _since(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def usage(sb, days: int = 30) -> dict:
    """최근 days 일의 실제 쓰임. 표가 없거나 실패한 부분은 비워 둔다."""
    since = _since(days)
    out = {"days": days, "by_cap": {}, "decided": {}, "unmet": [], "talks": 0}
    try:
        acts = sb.table("actions").select("kind,status").gte("created_at", since).limit(2000).execute().data or []
    except Exception:
        acts = []
    used = Counter()
    for a in acts:
        cap = ACTION_TO_CAP.get(a.get("kind"))
        if cap and a.get("status") in ("done", "scheduled"):
            used[cap] += 1
    st = Counter(a.get("status") for a in acts if a.get("status") in ("done", "rejected", "undone"))
    out["decided"] = {"accepted": st["done"], "rejected": st["rejected"], "undone": st["undone"]}
    blocked = Counter(a.get("kind") for a in acts if a.get("status") == "blocked")
    try:      # 올린 문서 수
        used["doc"] += len(sb.table("docs").select("id").gte("created_at", since).limit(1000).execute().data or [])
    except Exception:
        pass
    try:      # 양식으로 만든 문서 수
        used["template"] += sum(r.get("uses") or 0 for r in sb.table("templates").select("uses").limit(200).execute().data or [])
    except Exception:
        pass
    try:
        j = sb.table("judgments").select("tree,feedback").eq("tree", "recommendation").gte("created_at", since) \
              .limit(1000).execute().data or []
        used["daily"] += len(j)
        fb = Counter("ok" if (r.get("feedback") or 0) > 0 else "no" for r in j if r.get("feedback") is not None)
        out["daily_feedback"] = {"accepted": fb["ok"], "passed": fb["no"]}
    except Exception:
        pass
    out["by_cap"] = dict(used)
    # 못 해 드린 부탁: 비서가 '할 수 없다'고 답한 직전의 사용자 말 + 막힌 행동
    try:
        eps = sb.table("episodes").select("role,text").gte("created_at", since).order("id").limit(600).execute().data or []
    except Exception:
        eps = []
    out["talks"] = sum(1 for e in eps if e.get("role") == "user")
    unmet = []
    for prev, cur in zip(eps, eps[1:]):
        if prev.get("role") == "user" and cur.get("role") == "agent" and REFUSAL.search(cur.get("text") or ""):
            unmet.append(str(prev.get("text") or "")[:70])
    if blocked.get("send_message"):
        unmet.append(f"메시지를 직접 보내 달라는 부탁 {blocked['send_message']}번")
    out["unmet"] = list(dict.fromkeys(unmet))[-6:]
    return out


def evidence_lines(u: dict) -> list:
    """쓰임 요약을 '근거 줄'로: [(번호, 글)]. 대화 LLM 이 because 에 인용한다."""
    lines = []
    used = u.get("by_cap") or {}
    if used:
        top = sorted(used.items(), key=lambda x: -x[1])
        lines.append("자주 쓴 기능: " + ", ".join(f"{LABEL.get(k, k)} {v}번" for k, v in top[:5] if v))
    never = [LABEL[k] for k, *_ in CAPABILITIES if not used.get(k) and k not in ("why", "memory", "voice")]
    if never:
        lines.append("최근에 한 번도 안 쓴 기능: " + ", ".join(never))
    d = u.get("decided") or {}
    if any(d.values()):
        lines.append(f"비서가 하자고 한 일: 받아들임 {d.get('accepted', 0)} · 하지 않음 {d.get('rejected', 0)} · 되돌림 {d.get('undone', 0)}")
    df = u.get("daily_feedback") or {}
    if any(df.values()):
        lines.append(f"오늘의 제안: 맡긴 것 {df.get('accepted', 0)} · 넘긴 것 {df.get('passed', 0)}")
    for t in u.get("unmet") or []:
        lines.append(f"못 해 드린 부탁: \"{t}\"")
    return [(f"K{i}", t) for i, t in enumerate(lines[:10], 1)]


def for_prompt(sb, text: str) -> tuple:
    """(프롬프트에 넣을 글, 근거 번호 → evidence). 자기에 대한 질문이 아니면 한 줄만."""
    if not asks_about_me(text):
        return ("\n\n[나에 대해] 할 수 있는 일: " + ", ".join(n for _, n, _, _ in CAPABILITIES)
                + ". 내 기능을 물으면 상상하지 말고 이 목록으로만 답한다."), {}
    caps = "\n".join(f"- {n}: {what} (예: {how})" for _, n, what, how in CAPABILITIES)
    cannot = "\n".join(f"- {c}" for c in CANNOT)
    u = usage(sb)
    ev = evidence_lines(u)
    used_txt = "\n".join(f"[{k}] {t}" for k, t in ev) or "(아직 쓰임 기록이 거의 없음)"
    block = f"""

[나에 대해 — 내가 실제로 할 수 있는 일]
{caps}

[지금은 못 하는 일 — 새 기능 후보는 여기서만 나온다]
{cannot}

[최근 {u['days']}일 이 사람의 실제 쓰임 — 근거 번호 K]
{used_txt}

자기에 대한 질문 규칙
- 할 수 있는 일을 물으면 위 목록으로만, 이 사람에게 맞는 예를 들어 짧게 답한다. 없는 기능을 있다고 하지 않는다.
- 개선·새 기능을 물으면: 이미 있는 기능은 새 기능이라고 하지 말고 '이미 있어요, 이렇게 써 보세요'로 알려 준다.
  새 기능은 [지금은 못 하는 일]에서만 고르고, 반드시 [K번호] 쓰임 근거를 하나 이상 대며 왜 이 사람에게 필요한지 말한다.
  근거가 없으면 '아직 쓰임 기록이 적어 판단하기 이르다'고 솔직히 말한다. 3개 이하로.
- 보안·서버·성능처럼 만드는 사람의 일은 사용자 기능으로 제안하지 않는다.
- 안 쓰는 기능이 있으면 그중 이 사람에게 도움이 될 하나를 골라 써 보는 법을 알려 줘도 좋다."""
    evidence = {k: {"tag": k, "kind": "usage", "id": None, "text": t[:160]} for k, t in ev}
    return block, evidence


def overview(sb) -> dict:
    """설정 화면 '할 수 있는 일': 기능마다 최근 30일 쓰인 횟수."""
    u = usage(sb)
    used = u.get("by_cap") or {}
    return {"capabilities": [{"key": k, "name": n, "what": what, "how": how, "used": used.get(k, 0)}
                             for k, n, what, how in CAPABILITIES],
            "cannot": CANNOT, "days": u["days"], "unmet": u.get("unmet") or []}
