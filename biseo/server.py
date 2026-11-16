"""
biseo/server.py — 비서진 창구 (몰랑이 api.py 처럼 코어는 건드리지 않고 문만 연다).

  GET  /api/config                 브라우저가 로그인할 때 쓸 공개 값
  GET  /api/me                     내 비서 (없으면 만든다, 이름은 비어 있음)
  POST /api/name                   이름 짓기 / 바꾸기
  POST /api/chat                   말 걸기
  GET  /api/actions                확인함 + 최근에 스스로 한 일
  POST /api/actions/{id}/approve   승인
  POST /api/actions/{id}/reject    거절
  POST /api/actions/{id}/undo      되돌리기
  GET  /api/history                최근 대화
  GET  /api/cron/tick              pg_cron 전용 (Bearer CRON_SECRET)

인증: Authorization: Bearer <Supabase 로그인 토큰>
"""
import hmac
import os
import secrets
import sys
from urllib.parse import parse_qsl, urlencode
from datetime import datetime, timedelta, timezone


from fastapi import FastAPI, Header, HTTPException, Request  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

import traceback  # noqa: E402

# Vercel 은 이 파일을 패키지 안의 모듈로 부를 수도, 파일 하나로 부를 수도 있다.
# 파일 하나로 부르면 'from . import' 가 실패해서 함수가 통째로 죽는다.
# 그래서 저장소 맨 위를 경로에 넣고, 절대 경로(biseo.xxx)로 불러온다.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

app = FastAPI(title="biseojin")

# 본체 모듈을 못 불러와도 함수가 죽지 않고, 무엇이 실패했는지 돌려준다
_BOOT_ERROR = None
try:
    from biseo import actions as act  # noqa: E402
    from biseo import brain, db, tick  # noqa: E402
    from biseo import tasks as tk  # noqa: E402
    from biseo import skills as sk  # noqa: E402
    from biseo import apps  # noqa: E402
    from biseo import push  # noqa: E402
    from biseo import shortcuts as sc  # noqa: E402
    from biseo import daily  # noqa: E402
    from biseo import layout as home_layout  # noqa: E402
except Exception:  # noqa: BLE001
    _lines = traceback.format_exc().strip().splitlines()
    _BOOT_ERROR = {"detail": "서버 본체를 불러오지 못했어요",
                   "error": _lines[-1] if _lines else "알 수 없음",
                   "where": [l.strip() for l in _lines if l.strip().startswith("File ")][-3:],
                   "python": sys.version.split()[0]}


class _FixPath:
    """
    Vercel 은 rewrite 한 뒤 함수에 원래 경로를 넘기지 않는다 (함수는 '/api/index.py' 만 받는다).
    그래서 vercel.json 이 원래 경로를 ?__path= 로 실어 보내고, 여기서 되살린다.
    """
    def __init__(self, app):          # Starlette 미들웨어 규약: 인자 이름이 app
        self.inner = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http":
            orig = scope.get("path") or "/"
            q = parse_qsl((scope.get("query_string") or b"").decode(), keep_blank_values=True)
            rest = next((v for k, v in q if k == "__path"), None)
            q = [(k, v) for k, v in q if k not in ("__path", "path")]
            p = orig
            if rest is not None:                       # 예전 rewrite 방식이 남아 있을 때
                p = "/api/" + rest.lstrip("/")
            else:
                for pre in ("/api/index.py", "/api/index"):
                    if p.startswith(pre + "/"):
                        p = "/api/" + p[len(pre) + 1:]
                        break
            # 그 밖의 경로는 건드리지 않는다 (FastAPI 프로젝트 방식에서는 원래 경로가 그대로 온다)
            scope = {**scope, "path": p, "raw_path": p.encode(),
                     "query_string": urlencode(q).encode(), "_orig_path": orig}
        await self.inner(scope, receive, send)


# ---------- 예상 못 한 오류도 이유를 돌려준다 ----------
# 개발 중에만 켠다: 오류 응답에 종류·위치·내용을 그대로 담는다.
# 꺼져 있으면 사용자에게는 짧은 안내와 오류 번호만 가고, 자세한 건 Vercel Logs 에만 남는다.
DEBUG = os.environ.get("DEBUG_ERRORS") == "1"


def _error_id() -> str:
    return secrets.token_hex(3)


@app.exception_handler(Exception)
async def _any_error(request: Request, exc: Exception):
    eid = _error_id()
    tb = traceback.extract_tb(exc.__traceback__)
    mine = [f for f in tb if "/biseo/" in f.filename]
    where = f"{mine[-1].filename.split('/biseo/')[-1]}:{mine[-1].lineno}" if mine else ""
    print("ERROR", eid, request.method, request.url.path, "".join(traceback.format_exception(exc)))
    content = {"detail": "잠깐 문제가 생겼어요. 잠시 뒤 다시 해 주세요.", "error_id": eid}
    if DEBUG:
        content["debug"] = f"{type(exc).__name__}{' · ' + where if where else ''}: " \
                           f"{str(exc).replace(chr(10), ' ')[:200]}"
    return JSONResponse(status_code=500, content=content)


if not _BOOT_ERROR:
    @app.exception_handler(brain.LLMUnavailable)
    async def _llm_down(request: Request, exc: Exception):
        print("LLM_UNAVAILABLE", request.url.path, str(exc))
        return JSONResponse(status_code=503, headers={"Retry-After": "20"}, content={
            "detail": "지금 생각하는 쪽(AI)이 바빠요. 20초쯤 뒤에 다시 말해 주세요.",
            "retry_after": 20})


# ---------- 인증 ----------
_TOKENS: dict = {}          # 토큰 → (uid, 확인한 시각). 함수가 따뜻한 동안만 산다
TOKEN_TTL = 60              # 초. 이 안에서는 Supabase Auth 에 다시 묻지 않는다


def _uid_cached(token: str):
    """요청마다 Auth 서버에 왕복하지 않게, 확인된 토큰을 잠깐 기억한다.
    (RLS 는 여전히 매 쿼리에서 토큰을 검사하므로, 만료 토큰으로 남의 행을 볼 수는 없다)"""
    import time as _t
    hit = _TOKENS.get(token)
    if hit and _t.monotonic() - hit[1] < TOKEN_TTL:
        return hit[0]
    uid = db.user_id_from_token(token)
    if uid:
        if len(_TOKENS) > 500:
            _TOKENS.clear()
        _TOKENS[token] = (uid, _t.monotonic())
    return uid


def _session(authorization: str | None):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "로그인이 필요해요")
    token = authorization.split(" ", 1)[1]
    uid = _uid_cached(token)
    if not uid:
        raise HTTPException(401, "로그인이 만료됐어요")
    # 사용자 토큰(RLS) + Scoped(user_id 자동) 이중으로 막는다
    return db.Scoped(db.for_user(token), uid)


def _agent(sb) -> dict:
    rows = sb.table("agents").select("*").limit(1).execute().data
    if rows:
        return rows[0]
    # 첫 접속: 이름 없는 비서를 만든다. 알림 주제는 길고 뜻 없게 (주제 = 비밀번호)
    topic = "bs-" + secrets.token_urlsafe(18)
    return sb.table("agents").insert({"ntfy_topic": topic}).execute().data[0]


# ---------- 첫 화면 ----------
@app.get("/", include_in_schema=False)
def root():
    from fastapi.responses import RedirectResponse
    return RedirectResponse("/index.html")


# ---------- 공개 ----------
@app.get("/api/config")
def config():
    return {"supabase_url": os.environ.get("SUPABASE_URL", ""),
            "anon_key": os.environ.get("SUPABASE_ANON_KEY", "")}


# ---------- 몰랑이의 자식 (heir) 토글 — 본인만 ----------
def _heir_allowed(uid: str) -> bool:
    """
    외부 공개 뒤에는 아무나 선착순으로 heir 를 가져가면 안 된다.
    HEIR_OWNER_EMAIL 에 적힌 계정만 켤 수 있다. 비어 있으면 아무도 새로 켤 수 없다
    (이미 heir 인 계정은 그대로 유지·끄기 가능).
    """
    owner = os.environ.get("HEIR_OWNER_EMAIL", "").strip().lower()
    if not owner:
        return False
    try:
        u = db.for_cron().auth.admin.get_user_by_id(uid).user
        return bool(u and (u.email or "").lower() == owner)
    except Exception:
        return False


@app.get("/api/heir")
def heir_get(authorization: str | None = Header(None)):
    sb = _session(authorization)
    a = _agent(sb)
    mine = bool(a.get("heir"))
    # 자리가 이미 찼는지 (남이 heir 면 나는 영영 못 켠다)
    taken = bool(db.for_cron().table("agents").select("user_id")
                 .eq("heir", True).limit(1).execute().data)
    return {"heir": mine, "persona": a.get("persona") or "" if mine else "",
            "available": mine or (not taken and _heir_allowed(sb.uid))}


def a_is_heir(sb) -> bool:
    rows = sb.table("agents").select("heir").limit(1).execute().data
    return bool(rows and rows[0].get("heir"))


class HeirIn(BaseModel):
    on: bool


@app.post("/api/heir")
def heir_set(body: HeirIn, authorization: str | None = Header(None)):
    """켜기: 전체에서 아직 아무도 아닐 때만 선착순 1명. 끄기: 본인 것만 끈다."""
    sb = _session(authorization)
    _agent(sb)
    if not body.on:
        sb.table("agents").update({"heir": False}).eq("user_id", sb.uid).execute()
        return {"heir": False}
    if not a_is_heir(sb) and not _heir_allowed(sb.uid):
        raise HTTPException(403, "이 계정에서는 켤 수 없어요.")
    # DB 함수가 '아무도 아닐 때만' 올려 준다 (동시 요청도 유니크 인덱스가 1명만 통과)
    try:
        got = db.for_cron().rpc("claim_heir", {"uid": sb.uid}).execute().data
    except Exception:
        raise HTTPException(500, "heir 설정 함수가 없어요 (71_heir_unique.sql 실행 필요)")
    if not got:
        raise HTTPException(409, "이미 몰랑이의 자식이 정해졌어요. 한 명만 될 수 있어요.")
    return {"heir": True}


# ---------- 계정 삭제 ----------
@app.post("/api/account/delete")
def account_delete(authorization: str | None = Header(None)):
    """
    본인 계정을 지운다. auth.users 행을 지우면 모든 표가 on delete cascade 로 함께 지워진다.
    (개인정보보호법의 파기·앱스토어의 '앱 안에서 계정 삭제' 요건)
    """
    sb = _session(authorization)
    uid = sb.uid
    svc = db.for_cron()
    try:
        svc.table("push_subscriptions").delete().eq("user_id", uid).execute()
        svc.auth.admin.delete_user(uid)
    except Exception as e:
        print("ACCOUNT_DELETE_ERROR", uid[:8], type(e).__name__, str(e)[:200])
        raise HTTPException(500, "계정을 지우지 못했어요. 잠시 뒤 다시 해 주세요.")
    _TOKENS.clear()
    print("ACCOUNT_DELETED", uid[:8])
    return {"deleted": True}


# ---------- 비서 ----------
@app.get("/api/me")
def me(authorization: str | None = Header(None)):
    sb = _session(authorization)
    a = _agent(sb)
    return {"name": a.get("name"), "ntfy_topic": a.get("ntfy_topic"), "think_at": daily.think_at(a)}


class NameIn(BaseModel):
    name: str = Field(min_length=1, max_length=12)


@app.post("/api/name")
def set_name(body: NameIn, authorization: str | None = Header(None)):
    sb = _session(authorization)
    _agent(sb)
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "이름을 한 글자 이상 적어 주세요")
    sb.table("agents").update({"name": name,
                               "updated_at": datetime.now(timezone.utc).isoformat()}).execute()
    sb.table("audit").insert({"kind": "named", "detail": {"name": name}}).execute()
    return {"name": name}


class ChatIn(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


@app.post("/api/chat")
def chat(body: ChatIn, authorization: str | None = Header(None)):
    sb = _session(authorization)
    a = _agent(sb)
    if not a.get("name"):
        raise HTTPException(409, "먼저 비서 이름을 정해 주세요")
    _rate_limit(sb)
    return brain.respond(sb, a, body.text)


CHAT_PER_MIN = int(os.environ.get("CHAT_PER_MIN", "10"))
CHAT_PER_DAY = int(os.environ.get("CHAT_PER_DAY", "300"))


def _rate_limit(sb):
    """비용 폭주 방지. 서버리스는 메모리가 인스턴스마다 따로라 DB(대화 기록)로 센다."""
    now = datetime.now(timezone.utc)
    def count(since):
        return (sb.table("episodes").select("id", count="exact").eq("role", "user")
                .gte("created_at", since.isoformat()).execute().count or 0)
    if count(now - timedelta(minutes=1)) >= CHAT_PER_MIN:
        raise HTTPException(429, "말이 너무 빨라요. 1분쯤 뒤에 다시 해 주세요.")
    if count(now - timedelta(days=1)) >= CHAT_PER_DAY:
        raise HTTPException(429, "오늘 대화 한도에 닿았어요. 내일 다시 이야기해요.")


class SuggestIn(BaseModel):
    kind: str
    label: str = ""
    args: dict = {}


@app.post("/api/suggest/accept")
def suggest_accept(body: SuggestIn, authorization: str | None = Header(None)):
    """채팅 답변 아래 버튼으로 제안을 수락하면, 그 행동을 바로 실행(등급 규칙 적용)."""
    sb = _session(authorization)
    _agent(sb)
    g = body.args or {}
    # 과제로 맡기는 제안: start_task, 또는 모델이 멋대로 붙인 '만들기/쓰기'류 이름
    if body.kind == "start_task" or body.kind not in ("note", "reminder", "draft", "event"):
        goal = str(g.get("goal") or g.get("text") or g.get("title") or body.label or "").strip()
        if len(goal) < 2:
            raise HTTPException(400, "맡길 내용이 비어 있어요")
        return _kick(sb, _run(tk.create, sb, goal))
    return act.propose(sb, body.kind, g, "채팅 제안 수락", origin="user")


@app.get("/api/history")
def history(authorization: str | None = Header(None)):
    sb = _session(authorization)
    rows = (sb.table("episodes").select("role,text,created_at")
            .order("id", desc=True).limit(30).execute().data) or []
    return {"items": rows[::-1]}


# ---------- 행동 ----------
@app.get("/api/actions")
def list_actions(authorization: str | None = Header(None)):
    sb = _session(authorization)
    pending = (sb.table("actions").select("*").eq("status", "proposed")
               .order("id", desc=True).limit(20).execute().data) or []
    since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    recent = (sb.table("actions").select("*").eq("auto_executed", True)
              .in_("status", ["done", "scheduled"]).neq("kind", "search")
              .gte("created_at", since).order("id", desc=True).limit(10).execute().data) or []
    for r in pending + recent:
        r["label"] = act.KINDS.get(r["kind"], {}).get("label", r["kind"])
    return {"pending": pending, "recent": recent}


@app.post("/api/actions/{action_id}/{verb}")
def decide(action_id: int, verb: str, authorization: str | None = Header(None)):
    sb = _session(authorization)
    fn = {"approve": act.approve, "reject": act.reject, "undo": act.undo}.get(verb)
    if not fn:
        raise HTTPException(404, "없는 동작")
    res = fn(sb, action_id)
    if "error" in res:
        raise HTTPException(409, res["error"])
    return res


# ---------- 과제 ----------
def _kick(sb, task):
    """
    과제를 만들면 바로 돌려준다 (화면이 기다리지 않게).
    진행은 화면이 곧바로 /api/tasks/{id}/run 을 따로 불러서 시작하고,
    화면을 닫아도 크론(2분)이 이어 간다.
    """
    return task

@app.get("/api/tasks")
def list_tasks(authorization: str | None = Header(None)):
    sb = _session(authorization)
    cols = ("id,goal,status,next_step,question,result,steps,max_steps,log,updated_at,icon,tint,"
            "reflection,quality,lease_until")
    live = (sb.table("tasks").select(cols)
            .in_("status", ["open", "waiting_user", "waiting_approval"])
            .order("id", desc=True).limit(10).execute().data) or []
    since = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    ended = (sb.table("tasks").select(cols).in_("status", ["done", "stopped"])
             .gte("updated_at", since).order("id", desc=True).limit(5).execute().data) or []
    now_iso = datetime.now(timezone.utc).isoformat()
    for t in live + ended:          # 화면에는 마지막 생각 하나만
        t["running"] = str(t.pop("lease_until", "") or "") > now_iso   # 지금 누가 진행 중인가
        log = t.pop("log") or []
        last = next((e for e in reversed(log) if e.get("by") != "user"), {})
        t["last_thought"] = last.get("thought", "")
    return {"live": live, "ended": ended}


class GoalIn(BaseModel):
    goal: str = Field(min_length=2, max_length=500)
    icon: str | None = None
    tint: str | None = None


@app.post("/api/tasks")
def new_task(body: GoalIn, authorization: str | None = Header(None)):
    sb = _session(authorization)
    return _kick(sb, tk.create(sb, body.goal, icon=body.icon, tint=body.tint))


class AnswerIn(BaseModel):
    text: str = Field(min_length=1, max_length=1000)


@app.post("/api/tasks/{task_id}/answer")
def task_answer(task_id: int, body: AnswerIn, authorization: str | None = Header(None)):
    sb = _session(authorization)
    try:
        return tk.answer(sb, task_id, body.text)
    except ValueError as e:
        raise HTTPException(409, str(e))


@app.post("/api/tasks/{task_id}/cancel")
def task_cancel(task_id: int, authorization: str | None = Header(None)):
    sb = _session(authorization)
    try:
        return tk.cancel(sb, task_id)
    except ValueError as e:
        raise HTTPException(409, str(e))


class SearchIn(BaseModel):
    query: str = Field(min_length=1, max_length=200)


@app.post("/api/search/run")
def search_run(body: SearchIn, authorization: str | None = Header(None)):
    """검색 앱의 '찾기' 버튼: 채팅 없이 바로 탐색 과제를 만든다."""
    sb = _session(authorization)
    from biseo import observe
    observe.note(sb, "search", body.query.strip())
    return _kick(sb, tk.create(sb, f"{body.query.strip()} — 최신 정보를 찾아 핵심만 정리", icon="search", tint="indigo"))


@app.get("/api/tasks/{task_id}")
def task_one(task_id: int, authorization: str | None = Header(None)):
    sb = _session(authorization)
    rows = sb.table("tasks").select("*").eq("id", task_id).limit(1).execute().data
    if not rows:
        raise HTTPException(404, "없는 과제")
    return rows[0]


@app.post("/api/tasks/{task_id}/resume")
def task_resume(task_id: int, authorization: str | None = Header(None)):
    sb = _session(authorization)
    try:
        return tk.resume(sb, task_id)
    except ValueError as e:
        raise HTTPException(409, str(e))


RUN_BUDGET = float(os.environ.get("RUN_BUDGET", "22"))   # 초. 한 요청에서 이어 달릴 시간
RUN_MAX_STEPS = 8


@app.post("/api/tasks/{task_id}/run")
@app.post("/api/tasks/{task_id}/step")          # 예전 화면 호환
def task_run(task_id: int, authorization: str | None = Header(None)):
    """
    크론을 기다리지 않고 지금 진행한다. 한 걸음에서 멈추지 않고, 묻거나·확인이 필요하거나·
    끝나거나·시간(RUN_BUDGET)이 될 때까지 이어 달린다. 등급 규칙은 똑같이 적용된다.
    남은 건 크론이 이어 간다.
    """
    sb = _session(authorization)
    if not tk.get(sb, task_id):
        raise HTTPException(404, "없는 과제")
    res = tick.run_task_steps(sb, _agent(sb), task_id, brain._ask,
                              max_steps=RUN_MAX_STEPS, budget=RUN_BUDGET)
    if res.get("status") == "busy":
        raise HTTPException(409, "이미 진행하는 중이에요.")
    return {**(tk.get(sb, task_id) or {}), "ran_steps": res.get("steps", 0)}


# ---------- 홈 화면 앱 ----------
def _run(fn, *a):
    """앱 항목 처리 공통: 잘못된 값은 400, 없는 항목은 404 로."""
    try:
        return fn(*a)
    except LookupError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/home")
def home(authorization: str | None = Header(None)):
    sb = _session(authorization)
    data = apps.today(sb)
    # 홈에 고정된 단축 버튼
    pins = []
    for app_name in ("search", "tasks"):
        for s0 in sc.list_for(sb, app_name):
            if s0.get("pinned"):
                pins.append({"id": s0["id"], "label": s0["label"], "icon": s0.get("icon"), "tint": s0.get("tint"),
                             "origin": s0.get("origin") or "user", "uses": s0.get("uses") or 0,
                             "created_at": s0.get("created_at")})
    for sk0 in sk.list_pinned(sb):
        pins.append({"id": sk0["id"], "label": sk0["name"], "icon": sk0.get("icon") or "bolt",
                     "tint": sk0.get("tint") or "indigo", "skill": True,
                     "origin": sk0.get("origin") or "user", "uses": sk0.get("uses") or 0,
                     "created_at": sk0.get("created_at")})
    data["pins"] = pins
    data["daily"] = daily.today_plan(sb)          # 오늘의 제안 (없으면 null)
    # 홈의 '진행 중' 카드: 지금 돌고 있는 과제 하나 (없으면 답을 기다리는 것)
    live = (sb.table("tasks").select("id,goal,status,steps,max_steps")
            .in_("status", ["open", "waiting_user"]).order("updated_at", desc=True).limit(5).execute().data) or []
    data["task"] = next((t for t in live if t["status"] == "open"), live[0] if live else None)
    data["notes"] = (sb.table("task_memory").select("id,title,content,updated_at").eq("kind", "note")
                     .order("updated_at", desc=True).limit(3).execute().data) or []
    data["layout"] = home_layout.load(sb)          # 없으면 null → 화면이 기본 배치를 쓴다
    return data


# ---------- 홈 위젯 배치 ----------
@app.get("/api/home/layout")
def layout_get(authorization: str | None = Header(None)):
    sb = _session(authorization)
    return home_layout.load(sb) or home_layout.DEFAULT


@app.post("/api/home/layout")
async def layout_set(request: Request, authorization: str | None = Header(None)):
    sb = _session(authorization)
    raw = await request.body()
    if len(raw) > 16000:
        raise HTTPException(413, "배치가 너무 커요")
    import json as _json
    try:
        body = _json.loads(raw or b"{}")
    except ValueError:
        raise HTTPException(400, "배치 모양이 아니에요")
    return _run(home_layout.save, sb, body)



# ---------- 오늘의 생각 (비서가 먼저 건네는 제안) ----------
@app.get("/api/daily")
def daily_get(authorization: str | None = Header(None)):
    sb = _session(authorization)
    a = _agent(sb)
    return {"plan": daily.today_plan(sb), "think_at": daily.think_at(a)}


class ThinkIn(BaseModel):
    think_at: str | None = None        # "HH:MM" 또는 null(끄기)


@app.post("/api/daily/settings")
def daily_settings(body: ThinkIn, authorization: str | None = Header(None)):
    sb = _session(authorization)
    _agent(sb)
    v = (body.think_at or "").strip() or None
    if not daily.valid_time(v):
        raise HTTPException(400, "시각은 07:30 처럼 적어 주세요")
    sb.table("agents").update({"think_at": v, "updated_at": datetime.now(timezone.utc).isoformat()}).execute()
    return {"think_at": v}


@app.post("/api/daily/now")
def daily_now(authorization: str | None = Header(None)):
    """'지금 생각해 보기': 오늘의 제안을 바로 다시 만든다. 10분 안에 또 누르면 있던 것을 돌려준다."""
    sb = _session(authorization)
    a = _agent(sb)
    p = daily.today_plan(sb)
    if p and p.get("at"):
        try:
            made = datetime.fromisoformat(p["at"])
            if datetime.now(made.tzinfo) - made < timedelta(minutes=10):
                return {"plan": p, "reused": True}
        except ValueError:
            pass
    return {"plan": daily.make(sb, a, brain._ask)}


@app.post("/api/daily/{item_id}/{verb}")
def daily_decide(item_id: str, verb: str, authorization: str | None = Header(None)):
    if verb not in ("accept", "dismiss"):
        raise HTTPException(404, "없는 동작")
    sb = _session(authorization)
    return _run(daily.decide, sb, item_id, verb == "accept")


@app.get("/api/events")
def events_list(start: str = "", end: str = "", authorization: str | None = Header(None)):
    return {"items": _run(apps.list_events, _session(authorization), start or None, end or None)}


@app.get("/api/events/{item_id}")
def events_get(item_id: int, authorization: str | None = Header(None)):
    return _run(apps.get_event, _session(authorization), item_id)


@app.get("/api/reminders")
def reminders_list(past: int = 0, authorization: str | None = Header(None)):
    return {"items": apps.list_reminders(_session(authorization), include_past=bool(past))}


@app.get("/api/notes")
def notes_list(authorization: str | None = Header(None)):
    return {"items": apps.list_memory(_session(authorization), "note")}


@app.get("/api/drafts")
def drafts_list(authorization: str | None = Header(None)):
    return {"items": apps.list_memory(_session(authorization), "draft")}


def _crud():
    return {
        "events":    (apps.create_event, apps.update_event, apps.delete_event),
        "reminders": (apps.create_reminder, apps.update_reminder, apps.delete_reminder),
        "notes":     (apps.create_note, apps.update_memory, apps.delete_memory),
        "drafts":    (None, apps.update_memory, apps.delete_memory),
    }


@app.post("/api/{kind}/new")
async def item_new(kind: str, request: Request, authorization: str | None = Header(None)):
    if kind not in _crud() or not _crud()[kind][0]:
        raise HTTPException(404, "없는 앱")
    sb = _session(authorization)
    return _run(_crud()[kind][0], sb, await request.json())


@app.post("/api/{kind}/{item_id}/update")
async def item_update(kind: str, item_id: int, request: Request,
                      authorization: str | None = Header(None)):
    if kind not in _crud():
        raise HTTPException(404, "없는 앱")
    sb = _session(authorization)
    return _run(_crud()[kind][1], sb, item_id, await request.json())


@app.post("/api/{kind}/{item_id}/delete")
def item_delete(kind: str, item_id: int, authorization: str | None = Header(None)):
    if kind not in _crud():
        raise HTTPException(404, "없는 앱")
    return _run(_crud()[kind][2], _session(authorization), item_id)


@app.post("/api/events/{item_id}/remind")
async def event_remind(item_id: int, request: Request, authorization: str | None = Header(None)):
    """일정에 '몇 분 전' 알림 붙이기."""
    sb = _session(authorization)
    body = await request.json()
    ev = _run(apps.get_event, sb, item_id)
    return _run(apps.add_event_reminder, sb, ev, int(body.get("minutes", 30)))


# ---------- 앱 자체 알림 ----------
@app.get("/api/push/key")
def push_key():
    """브라우저가 구독할 때 쓰는 공개키. 비밀이 아니다."""
    return {"key": push.keys(db.for_cron())["public"]}


@app.post("/api/push/subscribe")
async def push_subscribe(request: Request, authorization: str | None = Header(None)):
    sb = _session(authorization)
    body = await request.json()
    sub = body.get("subscription") or {}
    k = sub.get("keys") or {}
    if not (str(sub.get("endpoint", "")).startswith("https://") and k.get("p256dh") and k.get("auth")):
        raise HTTPException(400, "구독 정보가 올바르지 않아요")
    svc = db.for_cron()
    # 같은 기기(endpoint)가 다시 켜면 주인을 지금 사용자로 바꿔 덮어쓴다
    svc.table("push_subscriptions").delete().eq("endpoint", sub["endpoint"]).execute()
    svc.table("push_subscriptions").insert({
        "user_id": sb.uid, "endpoint": sub["endpoint"], "p256dh": k["p256dh"], "auth": k["auth"],
        "device": str(body.get("device", ""))[:80]}).execute()
    return {"ok": True}


@app.post("/api/push/unsubscribe")
async def push_unsubscribe(request: Request, authorization: str | None = Header(None)):
    sb = _session(authorization)
    endpoint = (await request.json()).get("endpoint", "")
    sb.table("push_subscriptions").delete().eq("endpoint", endpoint).execute()
    return {"ok": True}


@app.get("/api/push/status")
def push_status(authorization: str | None = Header(None)):
    sb = _session(authorization)
    rows = sb.table("push_subscriptions").select("id,device,created_at").execute().data or []
    return {"devices": rows}


@app.post("/api/push/test")
def push_test(authorization: str | None = Header(None)):
    sb = _session(authorization)
    a = _agent(sb)
    res = push.send(db.for_cron(), sb.uid, a.get("name") or "비서",
                    "알림이 잘 와요. 눌러 보면 확인함이 열려요.", "/?open=inbox")
    if not res["devices"]:
        raise HTTPException(409, "이 계정에 알림을 켠 기기가 없어요")
    # 실패한 기기가 있으면 왜 실패했는지 사람이 읽을 수 있게 풀어 준다
    hints = []
    for f in res.get("failures", []):
        apple = "apple" in f["host"]
        if f["code"] in (404, 410):
            hint = "이 기기의 구독이 만료돼 지웠어요. 그 기기에서 알림을 다시 켜 주세요."
        elif f["code"] == 403 and apple:
            hint = ("애플이 서명을 거절했어요(" + (f["why"] or "403") + "). Vercel 환경변수 PUSH_CONTACT 에 "
                    "실제 이메일을 'mailto:you@example.com' 꼴로 넣고 다시 배포해 주세요.")
        elif f["code"] in (400, 403):
            hint = f"알림 서버가 거절했어요 ({f['code']} {f['why']})."
        else:
            hint = f"보내지 못했어요 ({f['code'] or '연결 실패'})."
        hints.append(f"{f['device']}: {hint}")
    res["hints"] = hints
    return res


# ---------- 내가 만드는 명령 버튼 ----------
@app.get("/api/shortcuts")
def shortcuts_list(app: str = "", authorization: str | None = Header(None)):
    sb = _session(authorization)
    if app not in sc.APPS:
        raise HTTPException(400, "검색 또는 맡은 일 앱만 돼요")
    return {"items": sc.list_for(sb, app)}


class ShortcutIn(BaseModel):
    app: str
    label: str = Field(min_length=1, max_length=20)
    text: str = Field(min_length=2, max_length=300)


@app.post("/api/shortcuts/new")
def shortcuts_new(body: ShortcutIn, authorization: str | None = Header(None)):
    sb = _session(authorization)
    return _run(sc.create, sb, body.app, body.label, body.text)


@app.post("/api/shortcuts/{sid}/run")
def shortcuts_run(sid: int, authorization: str | None = Header(None)):
    sb = _session(authorization)
    t = _run(sc.run, sb, sid)
    return _kick(sb, t) if isinstance(t, dict) and t.get("id") else t


@app.post("/api/shortcuts/{sid}/remove")
def shortcuts_remove(sid: int, authorization: str | None = Header(None)):
    sb = _session(authorization)
    return _run(sc.remove, sb, sid)


# ---------- 앱 안에서 자라는 기능 ----------
@app.get("/api/apps/{app}/skills")
def app_skills(app: str, authorization: str | None = Header(None)):
    sb = _session(authorization)
    if app not in sk.APP_NAMES:
        raise HTTPException(400, "기능을 둘 수 없는 앱")
    items = sk.list_for_app(sb, app)
    for c in items:
        c["tool_labels"] = [sk.TOOL_LABEL.get(t, t) for t in c.get("tools") or []]
    return {"items": items}


class AppSkillIn(BaseModel):
    request: str = Field(min_length=2, max_length=300)


@app.post("/api/apps/{app}/skills/add")
def app_skill_add(app: str, body: AppSkillIn, authorization: str | None = Header(None)):
    """앱 안에서 말로 요구 → AI가 그 앱에 맞는 기능을 설계해 추가 (자가 코드 없음)."""
    sb = _session(authorization)
    if app not in sk.APP_NAMES:
        raise HTTPException(400, "기능을 둘 수 없는 앱")
    try:
        return sk.design_from_request(sb, app, body.request, brain._ask)
    except ValueError as e:
        raise HTTPException(400, str(e))


# ---------- 기능 (사이드바) ----------
@app.get("/api/skills")
def list_skills(authorization: str | None = Header(None)):
    sb = _session(authorization)
    custom = sk.list_custom(sb)
    for c in custom:
        c["tool_labels"] = [sk.TOOL_LABEL.get(t, t) for t in c.get("tools") or []]
    return {"builtin": sk.BUILTIN, "custom": custom}


class RunIn(BaseModel):
    text: str = Field(default="", max_length=500)


@app.post("/api/skills/{skill_id}/run")
def run_skill(skill_id: int, body: RunIn, authorization: str | None = Header(None)):
    """기능 실행 = 그 기능의 방식을 품은 과제를 만든다."""
    sb = _session(authorization)
    s = sk.get(sb, skill_id)
    if not s:
        raise HTTPException(404, "없는 기능이에요")
    goal = f"[{s['name']}] {body.text.strip()}" if body.text.strip() else f"[{s['name']}] 실행"
    return _kick(sb, tk.create(sb, goal, skill_id=skill_id))


@app.post("/api/skills/{skill_id}/pin")
def skill_pin(skill_id: int, authorization: str | None = Header(None)):
    sb = _session(authorization)
    return _run(sk.set_pinned, sb, skill_id, True)


@app.post("/api/skills/{skill_id}/unpin")
def skill_unpin(skill_id: int, authorization: str | None = Header(None)):
    sb = _session(authorization)
    return _run(sk.set_pinned, sb, skill_id, False)


@app.post("/api/skills/{skill_id}/remove")
def remove_skill(skill_id: int, authorization: str | None = Header(None)):
    sb = _session(authorization)
    try:
        return sk.remove(sb, skill_id)
    except ValueError as e:
        raise HTTPException(404, str(e))


# ---------- 크론 ----------
@app.get("/api/cron/tick")
def cron_tick(only: str = "", authorization: str | None = Header(None)):
    secret = os.environ.get("CRON_SECRET", "")
    given = (authorization or "").removeprefix("Bearer ")
    if not secret or not hmac.compare_digest(given, secret):
        raise HTTPException(401, "unauthorized")
    return {"ok": True, "report": tick.run(db.for_cron(), brain._ask, only_due=(only == "due"))}


# ---------- 상태 확인 ----------
REQUIRED_ENV = ("SUPABASE_URL", "SUPABASE_ANON_KEY", "SUPABASE_SERVICE_ROLE_KEY",
                "OPENAI_API_KEY", "CRON_SECRET")


@app.get("/api/health")
def health():
    """
    배포가 살아 있는지 확인용 (모니터링 서비스가 1~5분마다 부르면 된다).
    값은 절대 돌려주지 않고, 있는지·연결되는지만 알려 준다.
    """
    import time as _t
    missing = [k for k in REQUIRED_ENV if not os.environ.get(k)]
    db_ok, db_ms = False, None
    if "SUPABASE_URL" not in missing and "SUPABASE_SERVICE_ROLE_KEY" not in missing:
        t0 = _t.monotonic()
        try:
            db.for_cron().table("agents").select("user_id").limit(1).execute()
            db_ok = True
        except Exception as e:
            print("HEALTH_DB_ERROR", type(e).__name__, str(e)[:200])
        db_ms = round((_t.monotonic() - t0) * 1000)
    ok = db_ok and not missing
    body = {"ok": ok, "db": db_ok, "db_ms": db_ms, "missing_env": missing,
            "search": bool(os.environ.get("BRAVE_API_KEY"))}
    return JSONResponse(status_code=200 if ok else 503, content=body)


# ---------- 없는 길 ----------
@app.api_route("/api/{rest:path}", methods=["GET", "POST"])
def not_found(rest: str, request: Request):
    content = {"detail": "없는 주소예요."}
    if DEBUG:      # 배포 경로 문제를 찾을 때만: 함수가 실제로 받은 경로를 보여 준다
        content.update(path_seen=request.scope.get("path"),
                       original_path=request.scope.get("_orig_path"))
    return JSONResponse(status_code=404, content=content)


# app 은 FastAPI 그대로 두고, 경로 보정은 미들웨어로 끼운다.
# (Vercel 이 FastAPI 프로젝트로 인식하면 app 이 FastAPI 객체이길 기대한다)
app.add_middleware(_FixPath)


class _BootGuard:
    """본체를 못 불러왔으면 모든 요청에 그 이유를 돌려준다."""
    def __init__(self, app):
        self.inner = app

    async def __call__(self, scope, receive, send):
        if _BOOT_ERROR and scope.get("type") == "http":
            import json
            print("BOOT_ERROR", _BOOT_ERROR)
            shown = _BOOT_ERROR if DEBUG else {"detail": "서버를 준비하지 못했어요. 잠시 뒤 다시 해 주세요."}
            body = json.dumps(shown, ensure_ascii=False).encode()
            await send({"type": "http.response.start", "status": 500,
                        "headers": [(b"content-type", b"application/json; charset=utf-8")]})
            await send({"type": "http.response.body", "body": body})
            return
        await self.inner(scope, receive, send)


app.add_middleware(_BootGuard)
