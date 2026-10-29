"""Authenticated LAN entry point for the single-host CAD task fleet."""
from dataclasses import dataclass
from pathlib import Path
import hashlib
import hmac
import logging
import os
import time
from typing import Literal
from urllib.parse import urlsplit

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .store import Conflict, FleetStore
from .templates import TEMPLATES


@dataclass
class FleetSettings:
    data_dir: Path
    token: str
    password: str = ""
    password_hash: str = ""
    lease_seconds: int = 60
    secure_cookie: bool = False

    @classmethod
    def load(cls):
        return cls(Path(os.environ.get("CAD_FLEET_DATA_DIR", ".fleet")), os.environ.get("CAD_FLEET_TOKEN", ""), os.environ.get("CAD_FLEET_PASSWORD", ""), os.environ.get("CAD_FLEET_PASSWORD_HASH", ""), int(os.environ.get("CAD_FLEET_LEASE_SECONDS", "60")), os.environ.get("CAD_FLEET_SECURE_COOKIE") == "1")


def password_hash(password, salt=None):
    salt = salt or os.urandom(16).hex()
    digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1).hex()
    return f"scrypt${salt}${digest}"


def verify_password(password, settings):
    if settings.password_hash:
        try:
            algorithm, salt, expected = settings.password_hash.split("$")
            return algorithm == "scrypt" and hmac.compare_digest(password_hash(password, salt).rsplit("$", 1)[1], expected)
        except (ValueError, TypeError):
            return False
    return bool(settings.password) and hmac.compare_digest(password.encode(), settings.password.encode())


class Login(BaseModel):
    password: str = Field(min_length=1, max_length=1024)


class JobRequest(BaseModel):
    upload_ids: list[str] = Field(min_length=1, max_length=9)
    question: str = Field(default="", max_length=3000)
    template_id: str | None = None
    mode: str = "agent"
    preferred_node: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,64}$")
    dynamic_methods: Literal['off', 'task_verified'] = 'off'


class Claim(BaseModel):
    node_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    model_profile: str | None = None


class Heartbeat(Claim):
    job_id: str
    attempt_id: str
    progress: dict | str | None = None
    snapshot_id: str | None = None
    run_id: str | None = None


class Complete(Claim):
    job_id: str
    attempt_id: str
    result: dict | list | str
    artifacts: dict | list | None = None
    delivery_status: str | None = None


class Fail(Claim):
    job_id: str
    attempt_id: str
    error: dict | str
    retryable: bool = False
    cancelled: bool = False
    artifacts: dict | list | None = None


def create_app(settings=None):
    settings = settings or FleetSettings.load()
    if len(settings.token) < 24 or not (settings.password or settings.password_hash):
        raise RuntimeError("Configure CAD_FLEET_TOKEN (24+ characters) and CAD_FLEET_PASSWORD_HASH or CAD_FLEET_PASSWORD")
    store = FleetStore(settings.data_dir, max(10, settings.lease_seconds))
    app = FastAPI(title="CAD Atlas · 图纸事实任务中心", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.fleet_store = store
    app.state.fleet_settings = settings
    attempts = {}

    def internal(request):
        provided = request.headers.get("authorization", "")
        return hmac.compare_digest(provided.encode(), ("Bearer " + settings.token).encode())

    def require_session(request):
        if not store.authenticated(request.cookies.get("cad_fleet_session")):
            raise HTTPException(401, "请先登录任务中心")

    app.state.require_session = require_session

    @app.middleware("http")
    async def access(request, call_next):
        path = request.url.path
        # Browser mutations require same-origin; workers authenticate independently.
        origin = request.headers.get("origin")
        if request.method not in ("GET", "HEAD", "OPTIONS") and origin and not (path.startswith("/api/fleet/worker/") and internal(request)):
            parsed = urlsplit(origin)
            if parsed.netloc != request.headers.get("host") or parsed.scheme not in ("http", "https"):
                return JSONResponse({"detail": "拒绝跨站操作"}, status_code=403)
        if path.startswith("/api/fleet/") and path not in ("/api/fleet/login", "/api/fleet/session", "/api/fleet/health"):
            if path.startswith("/api/fleet/worker/"):
                if not internal(request):
                    return JSONResponse({"detail": "Worker authentication required"}, status_code=401)
            elif not internal(request) and not store.authenticated(request.cookies.get("cad_fleet_session")):
                return JSONResponse({"detail": "请先登录任务中心"}, status_code=401)
        length = request.headers.get("content-length")
        if length and (not length.isdigit() or int(length) > 70 * 1024 * 1024):
            return JSONResponse({"detail": "请求超过大小限制"}, status_code=413)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; object-src 'none'; frame-ancestors 'self'"
        return response

    @app.exception_handler(KeyError)
    async def missing(request, exc):
        return JSONResponse({"detail": "任务或文件不存在"}, status_code=404)

    @app.exception_handler(Conflict)
    async def conflict(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.exception_handler(ValueError)
    async def invalid(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.get("/api/fleet/health")
    def health():
        return {"status": "ok", "service": "cad-fact-fleet", "schema_version": "1"}

    @app.get("/api/fleet/session")
    def session(request: Request):
        return {"authenticated": store.authenticated(request.cookies.get("cad_fleet_session"))}

    @app.post("/api/fleet/login")
    def login(payload: Login, request: Request):
        host = request.client.host if request.client else "unknown"
        recent = [t for t in attempts.get(host, []) if time.monotonic()-t < 60]
        attempts[host] = recent
        if len(recent) >= 10:
            raise HTTPException(429, "尝试次数过多，请稍后重试")
        if not verify_password(payload.password, settings):
            recent.append(time.monotonic())
            raise HTTPException(401, "登录密码不正确")
        attempts.pop(host, None)
        response = JSONResponse({"authenticated": True})
        response.set_cookie("cad_fleet_session", store.session(), max_age=12*3600, httponly=True, samesite="strict", secure=settings.secure_cookie)
        return response

    @app.post("/api/fleet/logout")
    def logout(request: Request):
        store.logout(request.cookies.get("cad_fleet_session", ""))
        response = JSONResponse({"authenticated": False})
        response.delete_cookie("cad_fleet_session")
        return response

    @app.get("/api/fleet/templates")
    def templates():
        return {"items": TEMPLATES}

    @app.get("/api/fleet/nodes")
    def nodes():
        return {"items": store.nodes()}

    @app.post("/api/fleet/uploads", status_code=201)
    async def upload(file: UploadFile = File(...)):
        content = bytearray()
        while chunk := await file.read(1024 * 1024):
            content.extend(chunk)
            if len(content) > 32 * 1024 * 1024:
                raise HTTPException(413, "图纸超过 32 MB 上传上限")
        from starlette.concurrency import run_in_threadpool
        return await run_in_threadpool(store.add_upload, file.filename or "drawing.dxf", bytes(content))

    @app.get("/api/fleet/uploads/{upload_id}/file")
    def download(upload_id: str):
        record = store.upload(upload_id)
        return FileResponse(store.root / "uploads" / record["id"], filename=record["filename"], media_type="application/octet-stream")

    @app.post("/api/fleet/jobs", status_code=201)
    def create_job(payload: JobRequest):
        data = payload.model_dump()
        if data["mode"] not in ("agent", "facts"):
            raise ValueError("仅支持 agent 或 facts 模式")
        if data["preferred_node"] and data["preferred_node"] not in os.environ.get("CAD_FLEET_NODE_IDS", "a,b").split(","):
            raise ValueError("未知执行节点")
        template = next((t for t in TEMPLATES if t["id"] == payload.template_id), None)
        if payload.template_id and not template:
            raise ValueError("未知任务模板")
        data["question"] = payload.question.strip() or (template["question"] if template else "")
        if len(data["question"]) > 3000:
            raise ValueError("问题超过 agent 的 3000 字符上限")
        if not data["question"]:
            raise ValueError("请填写事实问题或选择任务模板")
        data["template_version"] = template["version"] if template else None
        from types import SimpleNamespace
        from cad_agent.analysis.method_authorization import authorization
        data['original_user_input'] = payload.question
        data['authorization'] = authorization(SimpleNamespace(question=payload.question, dynamic_methods=payload.dynamic_methods))
        data['dynamic_methods'] = data['authorization']['mode']
        return store.create_job(data)

    @app.get("/api/fleet/jobs")
    def jobs():
        return {"items": store.jobs()}

    @app.get("/api/fleet/jobs/{job_id}")
    def job(job_id: str, view: str = 'full'):
        value = store.job(job_id)
        if view == 'answer':
            from .presentation import answer_view
            return answer_view(value)
        if view != 'full':
            raise HTTPException(400, '未知任务视图')
        return value

    @app.post("/api/fleet/jobs/{job_id}/cancel")
    def cancel(job_id: str):
        return store.cancel(job_id)

    @app.post("/api/fleet/jobs/{job_id}/retry", status_code=201)
    def retry(job_id: str):
        if store.job(job_id).get('exploration_run_id'):
            raise Conflict('E0 探索任务不能绕过冻结范围和预算单独重试；请创建新的探索运行。')
        return store.retry(job_id)

    @app.get("/api/fleet/jobs/{job_id}/archive")
    def archive(job_id: str):
        return FileResponse(store.archive(job_id), filename=f"{job_id}.evidence.json", media_type="application/json")

    @app.get("/api/fleet/jobs/{job_id}/attempts")
    def attempts_list(job_id: str):
        return {"items": store.attempts(job_id)}

    @app.get("/api/fleet/jobs/{job_id}/attempts/{attempt_id}/archive")
    def attempt_archive(job_id: str, attempt_id: str):
        return FileResponse(store.archive(job_id, attempt_id), filename=f"{job_id}-{attempt_id}.evidence.json", media_type="application/json")

    @app.post("/api/fleet/worker/claim")
    def claim(payload: Claim):
        if payload.model_profile:
            store.node_metadata(payload.node_id, {"model_profile": payload.model_profile})
        if hasattr(app.state,'operations'):app.state.operations.reconcile_all()
        return {"job": store.claim(payload.node_id), "lease_seconds": store.lease_seconds}

    @app.post("/api/fleet/worker/heartbeat")
    def heartbeat(payload: Heartbeat):
        if payload.model_profile:
            store.node_metadata(payload.node_id, {"model_profile": payload.model_profile})
        result=store.heartbeat(payload.job_id, payload.node_id, payload.attempt_id, payload.progress, payload.snapshot_id, payload.run_id)
        control=app.state.operations.control(payload.job_id)
        return result | {'control':{k:control[k] for k in ('revision','desired_state','state')}}

    @app.post("/api/fleet/worker/complete")
    def complete(payload: Complete):
        return sync_exploration(store.finish(payload.job_id, payload.node_id, payload.attempt_id, result=payload.result, artifacts=payload.artifacts, delivery_status=payload.delivery_status))

    @app.post("/api/fleet/worker/fail")
    def fail(payload: Fail):
        return sync_exploration(store.finish(payload.job_id, payload.node_id, payload.attempt_id, error=payload.error, retryable=payload.retryable, cancelled=payload.cancelled, artifacts=payload.artifacts))

    def sync_exploration(job):
        if job.get('exploration_run_id'):
            try:
                app.state.sync_exploration_run(job['exploration_run_id'])
            except Exception as exc:
                # Fleet completion is already durably archived. A read reconciles
                # E0 again; do not resend the completed model execution.
                logging.getLogger(__name__).exception('E0 archive reconciliation failed')
                return dict(job, exploration_sync={'status': 'pending', 'error_type': type(exc).__name__})
        return job

    from .rule_api import build_router as build_rule_router
    app.include_router(build_rule_router(app))
    from .operations_api import register_operations_api
    register_operations_api(app)
    from .supervisor import register_supervisor_api
    register_supervisor_api(app)

    try:
        from .proxy import build_router
    except ImportError:
        pass
    else:
        app.include_router(build_router(app))
    static = Path(__file__).parent / "static"
    if static.is_dir():
        app.mount("/", StaticFiles(directory=static, html=True), name="fleet-ui")
    return app
