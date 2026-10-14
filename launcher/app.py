"""HTTP API + static UI."""
from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import time
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import config, models, nodes, settings, workflows
from .comfy import ComfyManager
from .jobs import JobManager
from .security import ValidationError, check_basic_auth, check_url, clean_filename, clean_subdir, parse_repo

CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; "
       "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
MUTATING = {"POST", "PUT", "PATCH", "DELETE"}


# ---------------------------------------------------------------------------------------- request bodies
class DownloadReq(BaseModel):
    url: str = Field(max_length=4096)
    dir: str = Field(max_length=120)
    filename: str | None = Field(default=None, max_length=200)
    overwrite: bool = False


class ModelDeleteReq(BaseModel):
    dir: str = Field(max_length=120)
    filename: str = Field(max_length=220)


class NodeReq(BaseModel):
    url: str = Field(max_length=300)
    branch: str | None = Field(default=None, max_length=100)


class NodeNameReq(BaseModel):
    name: str = Field(max_length=100)


class WorkflowInstallReq(BaseModel):
    id: str = Field(max_length=64)
    skip_nodes: list[str] = Field(default_factory=list, max_length=100)
    skip_models: list[int] = Field(default_factory=list, max_length=100)


class WorkflowImportReq(BaseModel):
    name: str = Field(max_length=200)
    workflow: dict
    overwrite: bool = False


class SettingsReq(BaseModel):
    hf_token: str | None = Field(default=None, max_length=600)
    civitai_token: str | None = Field(default=None, max_length=600)


# ------------------------------------------------------------------------------------------------ helpers
_gpu_cache: tuple[float, dict | None] = (0.0, None)


def _gpu() -> dict | None:
    global _gpu_cache
    if time.time() - _gpu_cache[0] < 15:
        return _gpu_cache[1]
    info = None
    if shutil.which("nvidia-smi"):
        try:
            out = subprocess.run(
                ["nvidia-smi", "--query-gpu=name,memory.total,memory.used", "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=4, check=True).stdout.strip().splitlines()[0]
            name, total, used = [x.strip() for x in out.split(",")]
            info = {"name": name, "memory_total_mb": int(total), "memory_used_mb": int(used)}
        except (subprocess.SubprocessError, OSError, ValueError, IndexError):
            info = None
    _gpu_cache = (time.time(), info)
    return info


def _disk() -> dict:
    path = config.WORKSPACE if config.WORKSPACE.exists() else config.WORKSPACE.parent
    try:
        u = shutil.disk_usage(path)
        return {"total": u.total, "used": u.used, "free": u.free}
    except OSError:
        return {"total": 0, "used": 0, "free": 0}


def _model_dirs() -> list[str]:
    found = set(config.MODEL_DIRS)
    root = config.MODELS_DIR
    if root.is_dir():
        for p in root.iterdir():
            if p.is_dir() and not p.name.startswith((".", "_")) and p.name != "__pycache__":
                found.add(p.name)
    return sorted(found, key=str.lower)


def _basename(url: str) -> str:
    return os.path.basename(urlsplit(url).path) or "file"


# ----------------------------------------------------------------------------------------------- the app
def create_app(autostart: bool | None = None) -> FastAPI:
    comfy = ComfyManager()

    def on_restart() -> None:
        comfy.restart_needed = True

    jobs = JobManager(secrets=settings.secrets, on_restart=on_restart)
    do_autostart = config.AUTO_START_COMFY if autostart is None else autostart

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        config.ensure_dirs()
        if do_autostart:
            comfy.start()
        yield
        jobs.shutdown()
        comfy.stop()

    app = FastAPI(title=config.TITLE, version=config.VERSION, lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.state.comfy, app.state.jobs = comfy, jobs

    # ---- middleware: password, CSRF, headers
    @app.middleware("http")
    async def guard(request: Request, call_next):
        path = request.url.path
        if path == "/healthz":
            return JSONResponse({"ok": True})
        if config.LAUNCHER_PASSWORD and not check_basic_auth(request.headers.get("authorization"),
                                                               config.LAUNCHER_PASSWORD):
            if request.headers.get("authorization"):
                await asyncio.sleep(1.0)            # slows down password guessing
            return Response("Password required", status_code=401,
                            headers={"WWW-Authenticate": 'Basic realm="Launcher", charset="UTF-8"'})
        if request.method in MUTATING:
            ctype = request.headers.get("content-type", "").split(";")[0].strip().lower()
            if ctype != "application/json":
                return JSONResponse({"detail": "JSON requests only"}, status_code=415)
            length = request.headers.get("content-length", "")
            if not (length.isascii() and length.isdigit()):
                return JSONResponse({"detail": "Content-Length required"}, status_code=411)
            limit = config.MAX_IMPORT_BODY_BYTES if path == "/api/workflows/import" else config.MAX_BODY_BYTES
            if int(length) > limit:
                return JSONResponse({"detail": "Request body too large"}, status_code=413)
            site = request.headers.get("sec-fetch-site", "")
            if site in ("cross-site", "same-site"):
                return JSONResponse({"detail": "Cross-site request blocked"}, status_code=403)
            origin = request.headers.get("origin")
            if origin and site != "same-origin":
                allowed = {h for h in (request.headers.get("host", ""), request.headers.get("x-forwarded-host", "")) if h}
                if origin == "null" or urlsplit(origin).netloc not in allowed:
                    return JSONResponse({"detail": "Cross-site request blocked"}, status_code=403)
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = CSP
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        if path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(ValidationError)
    async def _bad_input(_: Request, exc: ValidationError):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.exception_handler(RequestValidationError)
    async def _bad_body(_: Request, exc: RequestValidationError):
        fields = sorted({str(e["loc"][-1]) for e in exc.errors() if e.get("loc")})
        return JSONResponse({"detail": "Invalid request: " + ", ".join(fields)}, status_code=400)

    # ---- state
    @app.get("/api/state")
    def state():
        return {
            "title": config.TITLE, "version": config.VERSION,
            "comfy": comfy.status(),
            "ports": {"comfy": config.COMFY_PORT, "jupyter": config.JUPYTER_PORT},
            "links": {"comfy": os.environ.get("COMFY_URL") or None, "jupyter": os.environ.get("JUPYTER_URL") or None},
            "tokens": {"hf": settings.source("hf_token"), "civitai": settings.source("civitai_token")},
            "password_protected": bool(config.LAUNCHER_PASSWORD),
            "password_source": config.PASSWORD_SOURCE,
            "disk": _disk(), "gpu": _gpu(), "jobs_active": jobs.active_count(),
        }

    # ---- workflows
    @app.get("/api/workflows")
    def list_workflows():
        entries, problems = workflows.load_catalog()
        return {"catalog": [workflows.public_view(e) for e in entries], "problems": problems,
                "installed": workflows.list_installed_workflows()}

    @app.post("/api/workflows/install")
    def install_workflow(req: WorkflowInstallReq):
        entry = workflows.find_entry(req.id)
        if not entry:
            raise ValidationError("Unknown workflow")
        skip_nodes, skip_models = set(req.skip_nodes), set(req.skip_models)
        job = jobs.submit("workflow", f"Install {entry['title']}",
                          lambda ctx: workflows.install_entry(ctx, entry, skip_nodes, skip_models),
                          key=f"wf:{entry['id']}")
        return jobs.snapshot(job)

    @app.post("/api/workflows/import")
    def import_workflow(req: WorkflowImportReq):
        name = workflows.import_workflow(req.name, req.workflow, req.overwrite)
        return {"name": name}

    # ---- models
    @app.get("/api/models")
    def list_models():
        return {"models": models.list_models(), "dirs": _model_dirs()}

    @app.post("/api/models/download")
    def download_model(req: DownloadReq):
        subdir = clean_subdir(req.dir)
        url = models.normalize_url(req.url)
        check_url(url)                         # fail fast in the request, not in the background
        fname = clean_filename(req.filename) if req.filename and req.filename.strip() else None
        title = f"Download {fname or _basename(url)}"
        job = jobs.submit("model", title,
                          lambda ctx: models.download_model(ctx, url, subdir, fname, req.overwrite),
                          key=f"dl:{subdir}:{url}")
        return jobs.snapshot(job)

    @app.post("/api/models/delete")
    def delete_model(req: ModelDeleteReq):
        models.delete_model(req.dir, req.filename)
        return {"ok": True}

    # ---- custom nodes
    @app.get("/api/nodes")
    def list_nodes():
        return {"nodes": nodes.list_nodes()}

    @app.post("/api/nodes/install")
    def install_node(req: NodeReq):
        repo = parse_repo(req.url, req.branch)
        job = jobs.submit("node", f"Install {repo.name}",
                          lambda ctx: nodes.install_node(ctx, req.url, req.branch), key=f"node:{repo.name.lower()}")
        return jobs.snapshot(job)

    @app.post("/api/nodes/update")
    def update_node(req: NodeNameReq):
        nodes._check_name(req.name)
        job = jobs.submit("node", f"Update {req.name}", lambda ctx: nodes.update_node(ctx, req.name),
                          key=f"node:{req.name.lower()}")
        return jobs.snapshot(job)

    @app.post("/api/nodes/remove")
    def remove_node(req: NodeNameReq):
        nodes.remove_node(req.name)
        comfy.restart_needed = True
        return {"ok": True}

    # ---- jobs
    @app.get("/api/jobs")
    def list_jobs():
        return {"jobs": jobs.list()}

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str):
        job = jobs.get(job_id)
        if not job:
            return JSONResponse({"detail": "Job not found"}, status_code=404)
        return jobs.snapshot(job, with_log=True)

    @app.post("/api/jobs/{job_id}/cancel")
    def cancel_job(job_id: str):
        return {"cancelled": jobs.cancel(job_id)}

    @app.post("/api/jobs/clear")
    def clear_jobs():
        return {"removed": jobs.clear_finished()}

    # ---- settings
    @app.post("/api/settings")
    def save_settings(req: SettingsReq):
        settings.update(req.model_dump(exclude_unset=True))
        return {"tokens": {"hf": settings.source("hf_token"), "civitai": settings.source("civitai_token")}}

    # ---- ComfyUI process
    @app.post("/api/comfy/start")
    def comfy_start():
        return {"started": comfy.start()}

    @app.post("/api/comfy/stop")
    def comfy_stop():
        comfy.stop()
        return {"ok": True}

    @app.post("/api/comfy/restart")
    def comfy_restart():
        return {"started": comfy.restart()}

    @app.get("/api/comfy/log")
    def comfy_log(tail: int = 300):
        return {"lines": comfy.log_tail(tail)}

    # ---- UI
    @app.get("/")
    def index():
        return FileResponse(config.STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    app.mount("/static", StaticFiles(directory=config.STATIC_DIR, check_dir=False), name="static")
    return app
