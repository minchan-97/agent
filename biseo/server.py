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

from . import actions as act  # noqa: E402
from . import brain, db, tick  # noqa: E402
from . import tasks as tk  # noqa: E402
from . import skills as sk  # noqa: E402
from . import apps  # noqa: E402

app = FastAPI(title="biseojin")


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


# ---------- 인증 ----------
def _session(authorization: str | None):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "로그인이 필요해요")
    token = authorization.split(" ", 1)[1]
    uid = db.user_id_from_token(token)
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


# ---------- 비서 ----------
@app.get("/api/me")
def me(authorization: str | None = Header(None)):
    sb = _session(authorization)
    a = _agent(sb)
    return {"name": a.get("name"), "ntfy_topic": a.get("ntfy_topic")}


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
    return brain.respond(sb, a, body.text)


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
@app.get("/api/tasks")
def list_tasks(authorization: str | None = Header(None)):
    sb = _session(authorization)
    cols = "id,goal,status,next_step,question,result,steps,max_steps,log,updated_at"
    live = (sb.table("tasks").select(cols)
            .in_("status", ["open", "waiting_user", "waiting_approval"])
            .order("id", desc=True).limit(10).execute().data) or []
    since = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    ended = (sb.table("tasks").select(cols).in_("status", ["done", "stopped"])
             .gte("updated_at", since).order("id", desc=True).limit(5).execute().data) or []
    for t in live + ended:          # 화면에는 마지막 생각 하나만
        log = t.pop("log") or []
        last = next((e for e in reversed(log) if e.get("by") != "user"), {})
        t["last_thought"] = last.get("thought", "")
    return {"live": live, "ended": ended}


class GoalIn(BaseModel):
    goal: str = Field(min_length=2, max_length=500)


@app.post("/api/tasks")
def new_task(body: GoalIn, authorization: str | None = Header(None)):
    sb = _session(authorization)
    return tk.create(sb, body.goal)


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


@app.post("/api/tasks/{task_id}/step")
def task_step(task_id: int, authorization: str | None = Header(None)):
    """크론을 기다리지 않고 지금 한 걸음. 등급 규칙은 똑같이 적용된다."""
    sb = _session(authorization)
    res = tk.step(sb, _agent(sb), task_id, brain._ask)
    if not res["task"]:
        raise HTTPException(404, "없는 과제")
    return res["task"]


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
    return apps.today(_session(authorization))


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


_CRUD = {
    "events":    (apps.create_event, apps.update_event, apps.delete_event),
    "reminders": (apps.create_reminder, apps.update_reminder, apps.delete_reminder),
    "notes":     (apps.create_note, apps.update_memory, apps.delete_memory),
    "drafts":    (None, apps.update_memory, apps.delete_memory),
}


@app.post("/api/{kind}/new")
async def item_new(kind: str, request: Request, authorization: str | None = Header(None)):
    if kind not in _CRUD or not _CRUD[kind][0]:
        raise HTTPException(404, "없는 앱")
    sb = _session(authorization)
    return _run(_CRUD[kind][0], sb, await request.json())


@app.post("/api/{kind}/{item_id}/update")
async def item_update(kind: str, item_id: int, request: Request,
                      authorization: str | None = Header(None)):
    if kind not in _CRUD:
        raise HTTPException(404, "없는 앱")
    sb = _session(authorization)
    return _run(_CRUD[kind][1], sb, item_id, await request.json())


@app.post("/api/{kind}/{item_id}/delete")
def item_delete(kind: str, item_id: int, authorization: str | None = Header(None)):
    if kind not in _CRUD:
        raise HTTPException(404, "없는 앱")
    return _run(_CRUD[kind][2], _session(authorization), item_id)


@app.post("/api/events/{item_id}/remind")
async def event_remind(item_id: int, request: Request, authorization: str | None = Header(None)):
    """일정에 '몇 분 전' 알림 붙이기."""
    sb = _session(authorization)
    body = await request.json()
    ev = _run(apps.get_event, sb, item_id)
    return _run(apps.add_event_reminder, sb, ev, int(body.get("minutes", 30)))


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
    return tk.create(sb, goal, skill_id=skill_id)


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


# ---------- 진단 ----------
@app.api_route("/api/{rest:path}", methods=["GET", "POST"])
def not_found(rest: str, request: Request):
    """맞는 길이 없을 때, 함수가 실제로 받은 경로를 보여 준다 (원인 찾기용)."""
    hints = {k: v for k, v in request.headers.items()
             if any(w in k for w in ("path", "url", "uri", "matched")) and "auth" not in k}
    return JSONResponse(status_code=404, content={
        "detail": "Not Found", "path_seen": request.scope.get("path"),
        "original_path": request.scope.get("_orig_path"), "headers": hints})


# app 은 FastAPI 그대로 두고, 경로 보정은 미들웨어로 끼운다.
# (Vercel 이 FastAPI 프로젝트로 인식하면 app 이 FastAPI 객체이길 기대한다)
app.add_middleware(_FixPath)
