"""Persistent settings (access tokens). Stored on the volume, mode 0600, never returned by the API."""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading

from . import config
from .security import ValidationError

_TOKEN_RE = re.compile(r"^[A-Za-z0-9_.\-]{8,512}$")
_FIELDS = ("hf_token", "civitai_token")
_lock = threading.Lock()


def _load() -> dict:
    try:
        with open(config.SETTINGS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _env_fallback(field: str) -> str:
    names = {
        "hf_token": ("HF_TOKEN", "HUGGING_FACE_HUB_TOKEN"),
        "civitai_token": ("CIVITAI_TOKEN",),
    }[field]
    for n in names:
        v = os.environ.get(n, "").strip()
        if v:
            return v
    return ""


def get(field: str) -> str:
    """Token entered in the UI wins; otherwise the environment variable (RunPod secret)."""
    if field not in _FIELDS:
        raise KeyError(field)
    with _lock:
        value = str(_load().get(field, "") or "")
    return value or _env_fallback(field)


def source(field: str) -> str:
    with _lock:
        if _load().get(field):
            return "ui"
    return "env" if _env_fallback(field) else "none"


def secrets() -> list[str]:
    return [t for t in (get(f) for f in _FIELDS) if t]


def update(changes: dict) -> None:
    """changes: {'hf_token': 'hf_xxx' | '' (clear)}. Unknown keys are rejected."""
    clean: dict[str, str] = {}
    for k, v in changes.items():
        if k not in _FIELDS:
            raise ValidationError(f"Unknown setting: {k}")
        if v is None:
            continue
        if not isinstance(v, str):
            raise ValidationError("Token must be text")
        v = v.strip()
        if v and not _TOKEN_RE.match(v):
            raise ValidationError("That does not look like a valid token (letters, digits, - _ . only)")
        clean[k] = v
    if not clean:
        return
    with _lock:
        data = _load()
        for k, v in clean.items():
            if v:
                data[k] = v
            else:
                data.pop(k, None)
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=config.DATA_DIR, prefix=".settings-")
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f)
            os.replace(tmp, config.SETTINGS_FILE)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


# --------------------------------------------------------------------------------- launcher password
PASSWORD_FILE_NAME = "launcher_password.txt"


def ensure_password() -> str | None:
    """Make sure the launcher is protected. Returns the password when one was generated, else None.

    LAUNCHER_PASSWORD from the environment always wins. Without it a random password is generated once, kept
    on the volume (mode 0600) and reused on later boots. LAUNCHER_ALLOW_NO_AUTH=1 opts out."""
    import secrets as _secrets
    if config.LAUNCHER_PASSWORD:
        config.PASSWORD_SOURCE = "env"
        return None
    if config.ALLOW_NO_AUTH:
        config.PASSWORD_SOURCE = "none"
        return None
    path = config.DATA_DIR / PASSWORD_FILE_NAME
    try:
        existing = path.read_text(encoding="utf-8").strip()
    except OSError:
        existing = ""
    if len(existing) < 12:
        existing = _secrets.token_urlsafe(14)
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(existing + "\n")
    config.LAUNCHER_PASSWORD = existing
    config.PASSWORD_SOURCE = "generated"
    return existing
