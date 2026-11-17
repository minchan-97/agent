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
_STOP = {"오늘", "내일", "어제", "이번", "지금", "현재", "최근", "알려", "알려줘", "어때", "뭐야", "있어", "정보",
         "관련", "검색", "찾아", "찾아줘", "요즘", "최신", "기준", "년", "월", "일"}
LABEL = {k: n for k, n, _, _ in CAPABILITIES}
ACTION_TO_CAP = {"event": "schedule", "reminder": "reminder", "note": "note", "draft": "draft",
                 "start_task": "task", "self_task": "task", "search": "search", "doc_edit": "doc",
                 "doc_new": "doc", "doc_make": "template", "create_skill": "skill",
                 "suggest_shortcut": "skill", "pin_shortcut": "skill"}

ABOUT_ME = re.compile(r"(기능|할 수 있|할수있|뭘 할|무엇을 할|뭐 할 수|뭐가 돼|뭐 돼|사용법|어떻게 써|어떻게 쓰|개선|추가하면|"
                      r"더하면|업데이트|필요한 기능|부족한|아쉬운|너는 뭐|넌 뭐|자기소개|소개해|도움말|할 줄)")
REFUSAL = re.compile(r"(할 수 없|할 수가 없|못 해요|못해요|못 해 드|못 합니다|지원하지 않|아직 못|아직 할 수 없|불가능|"
                     r"드릴 수 없|드릴 수는 없|볼 수 없|접근할 수 없|연결돼 있지 않|연결되어 있지 않|직접은 어려|직접 하기는 어려)")


def asks_about_me(text: str) -> bool:
    return bool(ABOUT_ME.search(text or ""))


def _since(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def usage(sb, days: int = 30) -> dict:
    """최근 days 일의 실제 쓰임. 표가 없거나 실패한 부분은 비워 둔다."""
    since = _since(days)
    out = {"days": days, "by_cap": {}, "decided": {}, "unmet": [], "talks": 0}
    try:
        acts = sb.table("actions").select("kind,status,args").gte("created_at", since).limit(2000).execute().data or []
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
    words = Counter()
    for a in acts:
        if a.get("kind") == "search":
            q = str((a.get("args") or {}).get("query") or "")
            for w in set(re.findall(r"[가-힣A-Za-z]{2,}", q)):
                if w not in _STOP:
                    words[w] += 1
    out["top_search"] = [(w, c) for w, c in words.most_common(3) if c >= 3]
    # 못 해 드린 부탁: 비서가 '할 수 없다'고 답한 직전의 사용자 말 + 막힌 행동
    try:
        eps = (sb.table("episodes").select("role,text").gte("created_at", since)
               .order("id", desc=True).limit(800).execute().data or [])[::-1]
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


# ── 개선 질문에 대한 '확실한' 답 ─────────────────────────────────────────────
# 프롬프트 규칙만으로는 LLM 이 "소통을 원활하게, 맞춤형 강화" 같은 빈말로 답할 때가 있다.
# 그래서 개선 질문이면 답을 검사하고, 빈말이면 실제 쓰임 기록으로 서버가 직접 답을 짠다.
ASKS_IMPROVE = re.compile(r"(개선|더하면|추가하면|추가했으면|필요한 기능|부족한|아쉬운|업데이트|뭘 더|뭐를 더|뭐 더|"
                          r"고치면|바꾸면 좋|발전)")
VAGUE = re.compile(r"(원활|맞춤형|강화|향상|편리하게|더 쉽게|다양한|효율적|최적화|사용자 경험|발전할 수|"
                   r"인터페이스|직관적|접근성)")
_PREFER = ["template", "daily", "task", "doc", "note", "draft", "reminder", "schedule", "skill"]
# '왜'·기억·음성은 쓰임이 기록되지 않는 기능이라 '안 써 본 기능'으로 권하지 않는다
_TIP = {
    "template": "자주 쓰는 문서를 양식으로 저장해 두면, 내용만 말해도 같은 모양 문서가 나와요. 예: \"체험학습 양식으로 6월 3일 박물관 써 줘\"",
    "daily": "매일 정한 시각에 오늘 할 만한 일을 먼저 건네 드려요. 설정 → 오늘의 생각 시각에서 켜요",
    "task": "여러 단계 일을 맡기면 혼자 찾고 정리해서 결과를 드려요. 예: \"체험학습 장소 후보 조사해서 정리해 줘\"",
    "doc": "엑셀·한글·PDF를 올리면 읽고 답하고, 고친 새 버전도 만들어요. 문서 → 올리기",
    "note": "\"이거 메모해 줘\" 한마디로 기록하고, 일정에 붙여 둘 수 있어요",
    "draft": "문자·메일·공지 초안을 써 드려요. 예: \"학부모님께 보낼 안내 문자 써 줘\"",
    "reminder": "정한 시각에 휴대폰 알림을 보내요. 예: \"금요일 9시에 동의서 걷기 알려 줘\"",
    "schedule": "말로 일정을 넣고 반복도 걸 수 있어요. 예: \"매주 월요일 9시 회의 넣어 줘\"",
    "skill": "반복되는 부탁을 홈 버튼으로 만들어요. 예: \"회의 준비 버튼 만들어 줘\"",
}
_CANNOT_HINT = [
    (re.compile(r"(보내|전송|카톡|문자 해|메일 해|발송)"), 0),
    (re.compile(r"(메일함|받은 메일|구글|아이폰 캘린더|캘린더 연동|나이스|업무포털|k-?에듀|에듀파인)", re.I), 1),
    (re.compile(r"(전화|결제|로그인|앱 열어|다른 앱)"), 2),
    (re.compile(r"(그림|이미지 만들|사진 만들|그려)"), 3),
    (re.compile(r"(같이|공유해서|함께 쓰|다른 사람이)"), 4),
]


def asks_improvement(text: str) -> bool:
    return bool(ASKS_IMPROVE.search(text or ""))


def is_vague(reply: str) -> bool:
    """기능 이름을 거의 안 대고 빈말만 하는 답인가."""
    r = reply or ""
    named = sum(1 for _, n, _, _ in CAPABILITIES if n in r)
    return named < 2 or (bool(VAGUE.search(r)) and named < 3)


def _cannot_for(ask: str):
    for rx, i in _CANNOT_HINT:
        if rx.search(ask):
            return CANNOT[i]
    return None


def improve_answer(sb, u: dict | None = None) -> str:
    """실제 쓰임으로만 짠 개선 답: ① 자주 하는 일을 더 빠르게 ② 안 써 본 기능 ③ 못 해 드린 부탁."""
    u = u or usage(sb)
    used = u.get("by_cap") or {}
    if u.get("talks", 0) < 5 and not any(used.values()):
        return ("아직 쓰신 기록이 거의 없어서, 무엇을 더하면 좋을지 판단하기 일러요.\n"
                f"우선 이렇게 써 보세요.\n• {_TIP['doc']}\n• {_TIP['reminder']}\n"
                "며칠 쓰신 뒤 다시 물어보시면, 쓰신 기록을 보고 말씀드릴게요.")
    parts = []
    # ① 자주 하는 일 → 더 빠르게 (기능 이름이 아니라 실제로 자주 한 부탁을 버튼으로)
    top_search = u.get("top_search") or []
    if top_search:
        w, c = top_search[0]
        parts.append(f"• '{w}'를 {c}번 찾으셨어요. 홈에 '{w}' 버튼을 두면 누르기만 해도 바로 찾아 드려요. "
                     f"\"{w} 버튼 만들어 줘\"라고 하시면 돼요.")
    elif used.get("note", 0) >= 20:
        parts.append(f"• 메모가 {used['note']}개 쌓였어요. \"이번 주 메모 정리해 줘\"라고 맡기시면 묶어서 정리해 드려요.")
    # ② 안 써 본 기능 중 하나 (문서는 쓰는데 양식은 안 쓰면 양식)
    order = list(_PREFER)
    if used.get("doc") and not used.get("template"):
        order.remove("template"); order.insert(0, "template")
    unused = [k for k in order if not used.get(k)]
    if unused:
        parts.append(f"• 아직 안 써 보신 {LABEL[unused[0]]} — {_TIP[unused[0]]}")
    # ③ 새 기능 후보: 실제로 못 해 드린 부탁에서만
    wants = []
    for ask in u.get("unmet") or []:
        c = _cannot_for(ask)
        if c and c not in [w[0] for w in wants]:
            wants.append((c, ask))
    for c, ask in wants[:2]:
        said = ask if ask.startswith("메시지를 직접") else f"\"{ask[:40]}\" 같은 부탁"
        parts.append(f"• 새로 생기면 좋을 것: {c.split(' (')[0]} — 실제로 {said}을 못 해 드렸어요.")
    d = u.get("decided") or {}
    if d.get("rejected", 0) >= 5 and d.get("rejected", 0) > d.get("accepted", 0):
        parts.append(f"• 제가 하자고 한 일 중 넘기신 게 더 많아요(받아들임 {d.get('accepted', 0)} · 넘김 {d['rejected']}). "
                     "제안이 과하면 말씀해 주세요.")
    head = f"최근 {u.get('days', 30)}일 쓰신 걸 보면,"
    tail = ("" if wants else
            "\n\n새 기능은 아직 근거가 없어요. 제가 못 해 드린 부탁이 기록에 없거든요. "
            "쓰시다 '이건 왜 안 돼?' 싶으면 그대로 말씀해 주세요. 그게 다음 기능의 근거가 돼요.")
    return head + "\n" + "\n".join(parts) + tail
