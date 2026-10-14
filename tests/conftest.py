import http.server
import socket
import sys
import threading
import time
import types
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from launcher import config, jobs as jobs_mod, models as models_mod, settings  # noqa: E402


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    ws = tmp_path / "workspace"
    comfy = ws / "ComfyUI"
    (comfy / "models").mkdir(parents=True)
    (comfy / "custom_nodes").mkdir()
    cat = tmp_path / "catalog"
    cat.mkdir()
    patches = {
        "WORKSPACE": ws, "COMFY_DIR": comfy, "MODELS_DIR": comfy / "models",
        "CUSTOM_NODES_DIR": comfy / "custom_nodes", "WORKFLOWS_DIR": comfy / "user" / "default" / "workflows",
        "DATA_DIR": ws / ".launcher", "SETTINGS_FILE": ws / ".launcher" / "settings.json",
        "CATALOG_DIRS": [cat], "ALLOW_PRIVATE_URLS": True, "GIT_PROTOCOLS": "https:file",
        "LAUNCHER_PASSWORD": "", "HF_HOSTS": {"localhost"}, "COMFY_CMD_OVERRIDE": [],
    }
    for k, v in patches.items():
        monkeypatch.setattr(config, k, v)
    for var in ("HF_TOKEN", "HUGGING_FACE_HUB_TOKEN", "CIVITAI_TOKEN", "PIP_USER", "PYTHONUSERBASE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    # No real back-off waits. Replace the name inside `models` only; patching time.sleep itself would be global.
    monkeypatch.setattr(models_mod, "time", types.SimpleNamespace(sleep=lambda s: None, time=time.time))
    config.ensure_dirs()
    return {"ws": ws, "catalog": cat}


# ------------------------------------------------------------------------------------ test HTTP server
class FileServer:
    """Serves in-memory files with Range support plus a few misbehaving endpoints."""

    def __init__(self):
        self.files: dict[str, bytes] = {}
        self.log: list[tuple[str, dict]] = []
        self.disp_names: dict[str, str] = {}
        self.bad_range: set[str] = set()
        self.chunked: set[str] = set()
        self.flaky_once: set[str] = set()
        self.slow: set[str] = set()
        self.release = threading.Event()
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_GET(self):
                parts = urlsplit(self.path)
                outer.log.append((parts.path, {k.lower(): v for k, v in self.headers.items()}))
                if parts.path == "/loop":
                    self.send_response(302)
                    self.send_header("Location", "/loop")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if parts.path.startswith("/redir"):
                    target = parse_qs(parts.query)["to"][0]
                    self.send_response(302)
                    self.send_header("Location", target)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if parts.path.startswith("/status/"):
                    code = int(parts.path.split("/")[2])
                    self.send_response(code)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                name = parts.path.removeprefix("/norange").removeprefix("/files").removeprefix("/disp").lstrip("/")
                data = outer.files.get(name)
                if data is None:
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                ignore_range = parts.path.startswith("/norange")
                rng = self.headers.get("Range")
                start = 0
                status = 200
                if rng and not ignore_range:
                    start = int(rng.split("=")[1].split("-")[0])
                    if start >= len(data):
                        self.send_response(416)
                        self.send_header("Content-Range", f"bytes */{len(data)}")
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    status = 206
                body = data[start:]
                if name in outer.bad_range and status == 206:        # answers with the wrong slice
                    body, start = data, 0
                self.send_response(status)
                if name in outer.chunked:
                    self.send_header("Transfer-Encoding", "chunked")
                else:
                    self.send_header("Content-Length", str(len(body)))
                if status == 206:
                    self.send_header("Content-Range", f"bytes {start}-{len(data) - 1}/{len(data)}")
                if parts.path.startswith("/disp"):
                    self.send_header("Content-Disposition",
                                     f'attachment; filename="{outer.disp_names.get(name, name)}"')
                self.end_headers()
                try:
                    if name in outer.flaky_once and start == 0:
                        outer.flaky_once.discard(name)
                        self.wfile.write(body[: len(body) // 2])
                        self.wfile.flush()
                        self.close_connection = True
                        return
                    if name in outer.slow:
                        step = max(1, len(body) // 50)
                        for i in range(0, len(body), step):
                            self.wfile.write(body[i:i + step])
                            self.wfile.flush()
                            if outer.release.wait(0.05):
                                self.wfile.write(body[i + step:])
                                break
                        return
                    if name in outer.chunked:
                        for i in range(0, len(body), 20000):
                            piece = body[i:i + 20000]
                            self.wfile.write(f"{len(piece):x}\r\n".encode() + piece + b"\r\n")
                        self.wfile.write(b"0\r\n\r\n")
                        return
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    def url(self, path: str, host: str = "127.0.0.1") -> str:
        return f"http://{host}:{self.port}{path}"

    def close(self):
        self.release.set()
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def server():
    s = FileServer()
    yield s
    s.close()


# ------------------------------------------------------------------------------------------- job helper
@pytest.fixture
def jm():
    m = jobs_mod.JobManager(secrets=settings.secrets)
    yield m
    m.shutdown()


def wait_job(manager, job, timeout=20.0):
    end = time.time() + timeout
    while time.time() < end:
        if job.state in jobs_mod.FINISHED:
            return job
        time.sleep(0.02)
    raise AssertionError(f"job still {job.state}: {job.message}")
