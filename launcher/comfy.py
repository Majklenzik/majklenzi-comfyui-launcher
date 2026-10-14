"""Starts, stops and watches the ComfyUI process."""
from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from collections import deque

import requests

from . import config


class ComfyManager:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._proc: subprocess.Popen | None = None
        self._log: deque[str] = deque(maxlen=3000)
        self._started_at: float | None = None
        self._exit_code: int | None = None
        self._stopped_by_user = False
        self.restart_needed = False
        self._probe_at = 0.0
        self._probe_ok = False
        self._http = requests.Session()
        self._http.trust_env = False        # always talk to localhost directly

    # ------------------------------------------------------------------------------------------ command
    def _command(self) -> list[str] | None:
        if config.COMFY_CMD_OVERRIDE:
            return list(config.COMFY_CMD_OVERRIDE) + ["--port", str(config.COMFY_PORT)]
        if not (config.COMFY_DIR / "main.py").is_file():
            return None
        return [config.PYTHON, "main.py", "--listen", "0.0.0.0", "--port", str(config.COMFY_PORT),
                *config.COMFY_ARGS]

    def installed(self) -> bool:
        return self._command() is not None

    # --------------------------------------------------------------------------------------- lifecycle
    def start(self) -> bool:
        with self._lock:
            if self._proc and self._proc.poll() is None:
                return True
            cmd = self._command()
            if cmd is None:
                self._log.append(f"[launcher] ComfyUI not found in {config.COMFY_DIR}")
                return False
            env = config.child_env({"PYTHONUNBUFFERED": "1"})
            cwd = config.COMFY_DIR if config.COMFY_DIR.is_dir() else None
            self._log.append(f"[launcher] starting: {' '.join(cmd)}")
            self._proc = subprocess.Popen(
                cmd, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, errors="replace", bufsize=1, start_new_session=True)
            self._started_at = time.time()
            self._exit_code = None
            self._stopped_by_user = False
            self.restart_needed = False
            self._probe_ok, self._probe_at = False, 0.0
            threading.Thread(target=self._pump, args=(self._proc,), daemon=True).start()
            return True

    def _pump(self, proc: subprocess.Popen) -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            with self._lock:
                self._log.append(line.rstrip("\r\n")[:2000])
        code = proc.wait()
        with self._lock:
            if self._proc is proc:
                self._exit_code = code
            self._log.append(f"[launcher] ComfyUI exited with code {code}")

    def stop(self, timeout: float = 25.0) -> None:
        with self._lock:
            proc = self._proc
        if proc is None or proc.poll() is not None:
            return
        with self._lock:
            self._stopped_by_user = True
        try:
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait(5)
        except (ProcessLookupError, PermissionError):
            pass
        with self._lock:
            self._probe_ok = False

    def restart(self) -> bool:
        self.stop()
        return self.start()

    # ------------------------------------------------------------------------------------------ status
    def _alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def _probe(self) -> bool:
        now = time.time()
        if now - self._probe_at < 2.0:
            return self._probe_ok
        self._probe_at = now
        try:
            r = self._http.get(f"http://127.0.0.1:{config.COMFY_PORT}/system_stats", timeout=1.5)
            self._probe_ok = r.status_code == 200
        except requests.RequestException:
            self._probe_ok = False
        return self._probe_ok

    def status(self) -> dict:
        with self._lock:
            if not self.installed():
                state = "missing"
            elif not self._alive():
                crashed = (self._started_at is not None and not self._stopped_by_user
                           and self._exit_code not in (None, 0))
                state = "crashed" if crashed else "stopped"
            else:
                state = "running" if self._probe() else "starting"
            return {
                "state": state,
                "pid": self._proc.pid if self._alive() and self._proc else None,
                "uptime": (time.time() - self._started_at) if state in ("running", "starting") and self._started_at
                else None,
                "exit_code": self._exit_code,
                "restart_needed": self.restart_needed,
            }

    def log_tail(self, n: int = 200) -> list[str]:
        n = max(1, min(int(n), 3000))
        with self._lock:
            return list(self._log)[-n:]
