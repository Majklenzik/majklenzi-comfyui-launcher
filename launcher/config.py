"""Runtime configuration. Everything is read from environment variables once, at import time."""
from __future__ import annotations

import os
import shlex
import sys
from pathlib import Path

PKG_DIR = Path(__file__).resolve().parent


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _flag(name: str, default: bool = False) -> bool:
    raw = _env(name)
    if not raw:
        return default
    return raw.lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


VERSION = "1.0.0"
TITLE = _env("LAUNCHER_TITLE", "ComfyUI Launcher")

# --- ports -----------------------------------------------------------------------------------------
LAUNCHER_HOST = _env("LAUNCHER_HOST", "0.0.0.0")
LAUNCHER_PORT = _int("LAUNCHER_PORT", 3000)
COMFY_PORT = _int("COMFY_PORT", 8188)
JUPYTER_PORT = _int("JUPYTER_PORT", 8888)

# --- paths -----------------------------------------------------------------------------------------
WORKSPACE = Path(_env("WORKSPACE", "/workspace"))
COMFY_DIR = Path(_env("COMFY_DIR", str(WORKSPACE / "ComfyUI")))
MODELS_DIR = COMFY_DIR / "models"
CUSTOM_NODES_DIR = COMFY_DIR / "custom_nodes"
WORKFLOWS_DIR = COMFY_DIR / "user" / "default" / "workflows"
DATA_DIR = Path(_env("LAUNCHER_DATA", str(WORKSPACE / ".launcher")))
SETTINGS_FILE = DATA_DIR / "settings.json"

# Built-in catalog ships inside the image; users can drop their own *.json next to it on the volume.
CATALOG_DIRS = [PKG_DIR / "catalog", DATA_DIR / "catalog"]
STATIC_DIR = PKG_DIR / "static"

# --- ComfyUI process -------------------------------------------------------------------------------
# COMFY_ARGS is appended to `python main.py --listen 0.0.0.0 --port <COMFY_PORT>`.
# ComfyUI-Manager (--enable-manager) is opt-in: port 8188 has no login, and Manager can install code.
COMFY_ARGS = shlex.split(_env("COMFY_ARGS", ""))
# Tests replace the whole command (e.g. a fake server); normal use leaves it empty.
COMFY_CMD_OVERRIDE = shlex.split(_env("COMFY_CMD"))
AUTO_START_COMFY = _flag("AUTO_START_COMFY", True)
PYTHON = _env("LAUNCHER_PYTHON", sys.executable)

# --- security --------------------------------------------------------------------------------------
LAUNCHER_PASSWORD = os.environ.get("LAUNCHER_PASSWORD", "")  # not stripped on purpose
# "env" (set by the operator), "generated" (random, see settings.ensure_password) or "none" (explicitly open)
PASSWORD_SOURCE = "env" if LAUNCHER_PASSWORD else "none"
# Only with LAUNCHER_ALLOW_NO_AUTH=1 may the launcher run without any password.
ALLOW_NO_AUTH = _flag("LAUNCHER_ALLOW_NO_AUTH", False)
# Tests only: allow loopback / private addresses and plain http in download URLs.
ALLOW_PRIVATE_URLS = _flag("LAUNCHER_ALLOW_PRIVATE_URLS", False)
# git may only speak these protocols (also for submodules). Tests widen it to reach a local repo.
GIT_PROTOCOLS = _env("LAUNCHER_GIT_PROTOCOLS", "https")
# Hosts that may be cloned from. github.com only unless the operator extends the list.
GIT_HOSTS = {"github.com"} | {h.strip().lower() for h in _env("LAUNCHER_GIT_HOSTS").split(",") if h.strip()}

# Tokens are only ever sent to these hosts (exact match, https only).
HF_HOSTS = {"huggingface.co", "www.huggingface.co", "hf.co"}
CIVITAI_HOSTS = {"civitai.com", "www.civitai.com"}

# --- limits ----------------------------------------------------------------------------------------
MAX_PARALLEL_JOBS = _int("LAUNCHER_PARALLEL_JOBS", 2)
MAX_WORKFLOW_BYTES = 8 * 1024 * 1024       # workflow JSON (import and catalog downloads)
MAX_BODY_BYTES = 256 * 1024                 # any other JSON request body
MAX_IMPORT_BODY_BYTES = MAX_WORKFLOW_BYTES + 512 * 1024
MAX_QUEUED_JOBS = 20
MIN_DOWNLOAD_RATE = 1024 * 1024             # bytes that must arrive in every window below, else "too slow"
RATE_WINDOW = 300                           # seconds
MAX_REDIRECTS = 8
DOWNLOAD_CHUNK = 1024 * 1024       # file write buffer
DOWNLOAD_BLOCK = 64 * 1024         # network read size (progress and cancel react at this granularity)
CONNECT_TIMEOUT = 20
READ_TIMEOUT = 60
PIP_TIMEOUT = 20 * 60
GIT_TIMEOUT = 15 * 60
KEEP_FINISHED_JOBS = 60

# Sub-folders of ComfyUI/models offered in the UI. Anything else under models/ is still accepted
# if it is a safe relative path (ComfyUI plugins add their own folders).
MODEL_DIRS = [
    "checkpoints", "diffusion_models", "unet", "loras", "vae", "text_encoders", "clip", "clip_vision",
    "controlnet", "upscale_models", "embeddings", "ipadapter", "style_models", "gligen", "hypernetworks",
    "vae_approx", "model_patches", "audio_encoders", "ultralytics/bbox", "ultralytics/segm", "sams",
    "LLM", "configs",
]


_SECRET_ENV = ("LAUNCHER_PASSWORD", "JUPYTER_PASSWORD")
_TOKEN_ENV = ("HF_TOKEN", "HUGGING_FACE_HUB_TOKEN", "CIVITAI_TOKEN")


def child_env(extra: dict | None = None, strip_tokens: bool = False) -> dict:
    """Environment for subprocesses: never hands the launcher/Jupyter password to code we did not write."""
    env = os.environ.copy()
    for k in _SECRET_ENV + (_TOKEN_ENV if strip_tokens else ()):
        env.pop(k, None)
    env.update(extra or {})
    return env


def ensure_dirs() -> None:
    for p in (DATA_DIR, CUSTOM_NODES_DIR, WORKFLOWS_DIR):
        p.mkdir(parents=True, exist_ok=True)
