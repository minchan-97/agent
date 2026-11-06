"""
api/index.py — Vercel 진입점. 본체는 biseo/server.py 에 있다.

본체를 불러오다 실패하면(라이브러리 버전, 문법, 환경 문제 등) 함수가 통째로 죽어서
FUNCTION_INVOCATION_FAILED 만 보이고 원인을 알 수 없다.
그래서 실패하면 대신 "무엇이 실패했는지"를 돌려주는 작은 앱을 띄운다.
"""
import json
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from biseo.server import app  # noqa: F401
except Exception:  # noqa: BLE001
    _lines = traceback.format_exc().strip().splitlines()
    _err = {"detail": "서버 본체를 불러오지 못했어요",
            "error": _lines[-1] if _lines else "알 수 없음",
            "where": [l.strip() for l in _lines if l.strip().startswith("File ")][-3:],
            "python": sys.version.split()[0]}
    try:
        from fastapi import FastAPI
        from fastapi.responses import JSONResponse

        app = FastAPI()

        @app.api_route("/{rest:path}", methods=["GET", "POST"])
        def _broken(rest: str = ""):
            return JSONResponse(status_code=500, content=_err)
    except Exception:  # fastapi 조차 없으면 순수 ASGI 로
        async def app(scope, receive, send):  # type: ignore[no-redef]
            if scope.get("type") != "http":
                return
            body = json.dumps(_err, ensure_ascii=False).encode()
            await send({"type": "http.response.start", "status": 500,
                        "headers": [(b"content-type", b"application/json; charset=utf-8")]})
            await send({"type": "http.response.body", "body": body})
