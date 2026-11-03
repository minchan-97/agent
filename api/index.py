"""
api/index.py — 비서진 창구 (몰랑이 api.py 처럼 코어는 건드리지 않고 문만 연다).

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
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI, Header, HTTPException, Request  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

from biseo import actions as act  # noqa: E402
from biseo import brain, db, tick  # noqa: E402

app = FastAPI(title="biseojin")


class _FixPath:
    """
    Vercel 이 함수에 넘기는 경로가 설정·버전에 따라 다르다.
    '/api/config' 로 올 수도, '/config' 로 올 수도, '/api/index/config' 로 올 수도 있다.
    어느 쪽이든 '/api/...' 로 맞춰서 라우트가 찾을 수 있게 한다.
    """
    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http":
            p = scope.get("path") or "/"
            for pre in ("/api/index.py", "/api/index"):
                if p.startswith(pre):
                    p = p[len(pre):] or "/"
                    break
            if not p.startswith("/api/") and p != "/api":
                p = "/api" + (p if p.startswith("/") else "/" + p)
            scope = {**scope, "path": p, "raw_path": p.encode(), "_orig_path": scope.get("path")}
        await self.inner(scope, receive, send)


# ---------- 인증 ----------
def _session(authorization: str | None):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "로그인이 필요해요")
    token = authorization.split(" ", 1)[1]
    uid = db.user_id_from_token(token)
    if not uid:
        raise HTTPException(401, "로그인이 만료됐어요")
    sb = db.for_user(token)
    sb.uid = uid
    return sb


def _agent(sb) -> dict:
    rows = sb.table("agents").select("*").limit(1).execute().data
    if rows:
        return rows[0]
    # 첫 접속: 이름 없는 비서를 만든다. 알림 주제는 길고 뜻 없게 (주제 = 비밀번호)
    topic = "bs-" + secrets.token_urlsafe(18)
    return sb.table("agents").insert({"ntfy_topic": topic}).execute().data[0]


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
                               "updated_at": datetime.now(timezone.utc).isoformat()}
                              ).eq("user_id", sb.uid).execute()
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


# ---------- 크론 ----------
@app.get("/api/cron/tick")
def cron_tick(authorization: str | None = Header(None)):
    secret = os.environ.get("CRON_SECRET", "")
    given = (authorization or "").removeprefix("Bearer ")
    if not secret or not hmac.compare_digest(given, secret):
        raise HTTPException(401, "unauthorized")
    return {"ok": True, "report": tick.run(db.for_cron())}


# ---------- 진단 ----------
@app.api_route("/api/{rest:path}", methods=["GET", "POST"])
def not_found(rest: str, request: Request):
    """맞는 길이 없을 때, 함수가 실제로 받은 경로를 보여 준다 (원인 찾기용)."""
    hints = {k: v for k, v in request.headers.items()
             if any(w in k for w in ("path", "url", "uri", "matched")) and "auth" not in k}
    return JSONResponse(status_code=404, content={
        "detail": "Not Found", "path_seen": request.scope.get("path"),
        "original_path": request.scope.get("_orig_path"), "headers": hints})


_inner_app = app
app = _FixPath(_inner_app)
