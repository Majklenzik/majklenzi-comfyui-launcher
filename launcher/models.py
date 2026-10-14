"""Model downloads (resumable, token only for Hugging Face / CivitAI) and the installed-model listing."""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import shutil
import time
from email.message import Message
from pathlib import PurePosixPath
from urllib.parse import SplitResult, unquote, urljoin, urlsplit

import requests

from . import config, settings
from .jobs import Cancelled, JobContext, JobError
from .security import ValidationError, check_url, clean_filename, clean_subdir, safe_join

USER_AGENT = f"ComfyUI-Launcher/{config.VERSION}"
KNOWN_EXT = {
    ".safetensors", ".sft", ".ckpt", ".pt", ".pth", ".bin", ".gguf", ".onnx", ".pkl", ".json", ".yaml",
    ".yml", ".txt", ".model", ".engine", ".npz",
}
REDIRECT_CODES = {301, 302, 303, 307, 308}
PART_RE = re.compile(r"^\.dl-[0-9a-f]{12}\.part$")
RETRIES = 5
SKIP_DIRS = {"__pycache__", ".git", ".cache"}


# -------------------------------------------------------------------------------------- URL helpers
def normalize_url(url: str) -> str:
    """Turn a Hugging Face 'blob' page link into the direct 'resolve' download link."""
    url = url.strip()
    parts = urlsplit(url)
    if (parts.hostname or "").lower() in config.HF_HOSTS:
        m = re.match(r"^/([^/]+/[^/]+)/blob/(.+)$", parts.path)
        if m:
            path = f"/{m.group(1)}/resolve/{m.group(2)}"
            query = "" if parts.query == "download=true" else parts.query
            return parts._replace(path=path, query=query).geturl()
    return url


def _token_for(parts: SplitResult) -> str:
    """Token for this exact hop, or ''. Only https, only the provider's own hosts."""
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" and not config.ALLOW_PRIVATE_URLS:
        return ""
    if host in config.HF_HOSTS:
        return settings.get("hf_token")
    if host in config.CIVITAI_HOSTS:
        return settings.get("civitai_token")
    return ""


def _name_from_url(parts: SplitResult) -> str | None:
    base = unquote(PurePosixPath(parts.path).name)
    try:
        name = clean_filename(base)
    except ValidationError:
        return None
    return name if os.path.splitext(name)[1].lower() in KNOWN_EXT else None


def _name_from_headers(resp: requests.Response) -> str | None:
    value = resp.headers.get("Content-Disposition")
    if not value:
        return None
    msg = Message()
    msg["content-disposition"] = value
    raw = msg.get_filename()
    if not raw:
        return None
    try:
        return clean_filename(os.path.basename(raw))
    except ValidationError:
        return None


def _open(ctx: JobContext, session: requests.Session, url: str, start: int) -> tuple[requests.Response, str]:
    """GET with manual redirects: every hop is re-validated and gets only its own token."""
    cur = url
    for _ in range(config.MAX_REDIRECTS + 1):
        parts = check_url(cur)
        headers = {"User-Agent": USER_AGENT, "Accept-Encoding": "identity"}
        if start:
            headers["Range"] = f"bytes={start}-"
        token = _token_for(parts)
        if token:
            headers["Authorization"] = f"Bearer {token}"
        resp = session.get(cur, headers=headers, stream=True, allow_redirects=False,
                           timeout=(config.CONNECT_TIMEOUT, config.READ_TIMEOUT))
        if resp.status_code in REDIRECT_CODES:
            loc = resp.headers.get("Location")
            resp.close()
            if not loc:
                raise JobError("Server sent a redirect without a target")
            cur = urljoin(cur, loc)
            ctx.log(f"redirect -> {urlsplit(cur).hostname}")
            continue
        return resp, cur
    raise JobError("Too many redirects")


def _http_error(resp: requests.Response, parts: SplitResult) -> JobError:
    code = resp.status_code
    host = (parts.hostname or "").lower()
    if code in (401, 403) and host in config.HF_HOSTS:
        have = "a token is set" if settings.get("hf_token") else "no Hugging Face token is set"
        return JobError(f"Access denied (HTTP {code}); {have}. Gated models need a token (Settings) and you "
                        "must accept the model licence on its Hugging Face page first.")
    if code in (401, 403) and host in config.CIVITAI_HOSTS:
        return JobError(f"Access denied (HTTP {code}). This CivitAI file needs a CivitAI API key (Settings).")
    if code in (401, 403):
        return JobError(f"Access denied (HTTP {code}). The link needs a login, which is not supported.")
    if code == 404:
        return JobError("File not found (HTTP 404). Check the link.")
    if code == 429:
        return JobError("Too many requests (HTTP 429). Try again in a few minutes.")
    return JobError(f"Server answered HTTP {code}")


def _total_from(resp: requests.Response, start: int) -> int | None:
    if resp.status_code == 206:
        m = re.match(r"^bytes (\d+)-(\d+)/(\d+|\*)$", resp.headers.get("Content-Range", ""))
        if m and int(m.group(1)) == start and m.group(3) != "*":
            return int(m.group(3))
        return None
    cl = resp.headers.get("Content-Length")
    return int(cl) if cl and cl.isascii() and cl.isdigit() else None


def _sha256(path, ctx: JobContext) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(8 * 1024 * 1024):
            ctx.check()
            h.update(chunk)
    return h.hexdigest()


# ------------------------------------------------------------------------------------------ download
_DONE, _RESTART, _EXISTS = "done", "restart", "exists"


def new_session() -> requests.Session:
    """No proxy variables, no ~/.netrc: the address we validated is the address we talk to."""
    s = requests.Session()
    s.trust_env = False
    return s


def download_model(ctx: JobContext, url: str, subdir: str, filename: str | None = None,
                   overwrite: bool = False, sha256: str | None = None) -> str:
    """Download one file into models/<subdir>. Returns the final file name."""
    subdir = clean_subdir(subdir)
    url = normalize_url(url)
    first = check_url(url)
    wanted = clean_filename(filename) if filename and filename.strip() else None
    dest_dir = safe_join(config.MODELS_DIR, subdir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    part = dest_dir / f".dl-{hashlib.sha1(url.encode()).hexdigest()[:12]}.part"

    with ctx.lock(f"part:{part}"):                  # the same link from two places must not share a .part file
        session = new_session()
        try:
            failures = 0
            restarts = 0
            while True:
                ctx.check()
                start = part.stat().st_size if part.exists() else 0
                try:
                    status, name = _fetch(ctx, session, url, first, dest_dir, part, start, wanted, overwrite)
                except (requests.ConnectionError, requests.Timeout, requests.exceptions.ChunkedEncodingError) as e:
                    if part.exists() and part.stat().st_size > start:
                        failures = 0                # it made progress, so this is not a hopeless link
                    failures += 1
                    if failures > RETRIES:
                        raise JobError("Connection lost and retries ran out. Start the download again to "
                                       "resume where it stopped.") from e
                    wait = min(60, 2 ** failures)
                    ctx.say(f"Connection problem, retrying in {wait}s ({failures}/{RETRIES})")
                    for _ in range(wait):
                        ctx.check()
                        time.sleep(1)
                    continue
                if status == _EXISTS:
                    ctx.say(f"Already installed: {subdir}/{name}")
                    return name
                if status == _RESTART:
                    restarts += 1
                    if restarts > 2:
                        raise JobError("Server keeps refusing to resume this download.")
                    continue
                if sha256:
                    ctx.say("Verifying checksum")
                    if not hmac.compare_digest(_sha256(part, ctx), sha256.lower()):
                        part.unlink(missing_ok=True)
                        raise JobError("Checksum does not match; the file was discarded.")
                if (dest_dir / name).exists() and not overwrite:
                    part.unlink(missing_ok=True)
                    ctx.say(f"Already installed: {subdir}/{name}")
                    return name
                os.replace(part, dest_dir / name)
                ctx.say(f"Saved {subdir}/{name}")
                return name
        finally:
            session.close()


def _fetch(ctx: JobContext, session: requests.Session, url: str, first: SplitResult, dest_dir, part,
           start: int, wanted: str | None, overwrite: bool) -> tuple[str, str | None]:
    """One HTTP attempt. Returns (_DONE, name) when `part` holds the whole file, (_EXISTS, name) when the
    target already exists, or (_RESTART, None) when the partial file was thrown away."""
    ctx.say("Connecting" if not start else f"Resuming at {start / 1e6:.0f} MB")
    resp, final_url = _open(ctx, session, url, start)
    try:
        code = resp.status_code
        final_parts = urlsplit(final_url)
        if code == 416:
            # Range not satisfiable: the part file is either already complete or stale.
            m = re.match(r"^bytes \*/(\d+)$", resp.headers.get("Content-Range", ""))
            name = wanted or _name_from_url(first) or _name_from_url(final_parts)
            if m and int(m.group(1)) == start and start > 0 and name:
                if (dest_dir / name).exists() and not overwrite:
                    part.unlink(missing_ok=True)
                    return _EXISTS, name
                return _DONE, name
            part.unlink(missing_ok=True)
            return _RESTART, None
        if code not in (200, 206):
            raise _http_error(resp, final_parts)
        if code == 206 and _range_start(resp) != start:     # not the bytes we asked for: do not append them
            part.unlink(missing_ok=True)
            return _RESTART, None
        if start and code == 200:        # server ignored Range: begin again from zero
            start = 0
            part.unlink(missing_ok=True)
        total = _total_from(resp, start)
        name = wanted or _name_from_url(first) or _name_from_url(final_parts) or _name_from_headers(resp)
        if not name:
            raise JobError("Could not work out the file name. Fill in the 'File name' field.")
        if (dest_dir / name).exists() and not overwrite:
            return _EXISTS, name
        free = shutil.disk_usage(dest_dir).free
        budget = free - 256 * 1024 * 1024                 # bytes this download may still write
        if total is not None and total - start > budget:
            raise JobError(f"Not enough disk space: need {(total - start) / 1e9:.1f} GB, "
                           f"{free / 1e9:.1f} GB free. Increase the volume size or delete files.")
        done = start
        ctx.progress(done, total)
        ctx.say(f"Downloading {name}")
        window_t, window_b = time.time(), 0
        with open(part, "ab" if start else "wb", buffering=config.DOWNLOAD_CHUNK) as f:
            for chunk in resp.iter_content(config.DOWNLOAD_BLOCK):
                ctx.check()
                if not chunk:
                    continue
                f.write(chunk)
                done += len(chunk)
                window_b += len(chunk)
                ctx.progress(done, total)
                if done - start > budget:           # also guards servers that never announce a size
                    raise JobError("The disk is full. Free some space and start the download again to resume.")
                if total is not None and done > total:
                    raise JobError("Server sent more data than it announced.")
                now = time.time()
                if now - window_t >= config.RATE_WINDOW:
                    if window_b < config.MIN_DOWNLOAD_RATE:
                        raise JobError("Download is far too slow. Start it again later to resume.")
                    window_t, window_b = now, 0
        if total is not None and done != total:
            raise requests.exceptions.ChunkedEncodingError("short read")
        return _DONE, name
    finally:
        resp.close()


def _range_start(resp: requests.Response) -> int | None:
    m = re.match(r"^bytes (\d+)-(\d+)/(\d+|\*)$", resp.headers.get("Content-Range", ""))
    return int(m.group(1)) if m else None


# --------------------------------------------------------------------------------------------- listing
def list_models(limit: int = 3000) -> list[dict]:
    out: list[dict] = []
    root = config.MODELS_DIR
    if not root.is_dir():
        return out
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.startswith("."))
        rel = os.path.relpath(dirpath, root)
        if rel == ".":
            rel = ""
        if rel.count(os.sep) >= 3:
            dirnames[:] = []
        for fn in sorted(filenames):
            partial = bool(PART_RE.match(fn))
            hidden = fn.startswith(".") and not partial
            if hidden or fn.startswith("put_") or (fn.endswith(".part") and not partial):
                continue
            try:
                size = os.path.getsize(os.path.join(dirpath, fn))
            except OSError:
                continue
            d = rel.replace(os.sep, "/")
            out.append({"dir": d, "name": fn, "size": size, "partial": partial, "deletable": _deletable(d, fn, partial)})
            if len(out) >= limit:
                return out
    return out


def _deletable(subdir: str, name: str, partial: bool) -> bool:
    try:
        clean_subdir(subdir)
        if not partial:
            clean_filename(name)
        return True
    except ValidationError:
        return False


def delete_model(subdir: str, filename: str) -> None:
    """Delete one file. A symlink is removed itself, never the file it points to."""
    subdir = clean_subdir(subdir)
    if not PART_RE.match(filename or ""):
        filename = clean_filename(filename)
    parent = safe_join(config.MODELS_DIR, subdir)
    target = parent / filename                 # `filename` has no separators, so this stays inside `parent`
    if target.is_symlink() or target.is_file():
        target.unlink()
        return
    raise ValidationError("File not found")
