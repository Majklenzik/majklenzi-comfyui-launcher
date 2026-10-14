"""Background jobs (downloads, git clones, pip installs) with progress, log and cancel."""
from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
import uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

from . import config
from .security import ValidationError, redact

QUEUED, RUNNING, DONE, ERROR, CANCELLED = "queued", "running", "done", "error", "cancelled"
FINISHED = {DONE, ERROR, CANCELLED}


class Cancelled(Exception):
    pass


_lock_registry_guard = threading.Lock()
_lock_registry: dict[str, threading.Lock] = {}
PIP_LOCK = "pip"


def _resource_lock(key: str) -> threading.Lock:
    with _lock_registry_guard:
        return _lock_registry.setdefault(key, threading.Lock())


class JobError(Exception):
    """A failure whose message is safe to show to the user."""


class Job:
    def __init__(self, kind: str, title: str, key: str | None):
        self.id = uuid.uuid4().hex[:12]
        self.kind = kind
        self.title = title
        self.key = key
        self.state = QUEUED
        self.created = time.time()
        self.started: float | None = None
        self.finished: float | None = None
        self.bytes_done = 0
        self.bytes_total: int | None = None
        self.message = "Waiting"
        self.error: str | None = None
        self.log: deque[str] = deque(maxlen=500)
        self.cancel_event = threading.Event()
        self.proc: subprocess.Popen | None = None
        self.needs_restart = False   # set when ComfyUI must be restarted to see the result


class JobContext:
    """What a running task is allowed to touch."""

    def __init__(self, job: Job, lock: threading.RLock, secrets: Callable[[], list[str]],
                 on_restart: Callable[[], None] | None = None):
        self._job, self._lock, self._secrets, self._on_restart = job, lock, secrets, on_restart

    # -- reporting
    def log(self, line: str) -> None:
        line = redact(str(line).rstrip("\r\n"), self._secrets())
        with self._lock:
            self._job.log.append(line[:2000])

    def say(self, message: str) -> None:
        with self._lock:
            self._job.message = message
        self.log(message)

    def progress(self, done: int, total: int | None) -> None:
        with self._lock:
            self._job.bytes_done = done
            self._job.bytes_total = total

    def needs_restart(self) -> None:
        with self._lock:
            self._job.needs_restart = True
        if self._on_restart:
            self._on_restart()

    def lock(self, key: str):
        """Context manager: one job at a time per resource (a file, a node folder, pip). Cancel-aware."""
        return _Held(self, _resource_lock(key))

    # -- cancel
    @property
    def cancelled(self) -> bool:
        return self._job.cancel_event.is_set()

    def check(self) -> None:
        if self._job.cancel_event.is_set():
            raise Cancelled()

    # -- subprocess
    def run(self, args: list[str], cwd=None, env: dict | None = None, timeout: int | None = None) -> None:
        """Run a command, stream its output into the log, raise JobError on failure."""
        self.check()
        full_env = config.child_env(env, strip_tokens=True)
        self.log("$ " + " ".join(args))
        proc = subprocess.Popen(
            args, cwd=cwd, env=full_env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, errors="replace", bufsize=1, start_new_session=True,
        )
        with self._lock:
            self._job.proc = proc
        if self._job.cancel_event.is_set():       # cancel arrived between check() and Popen
            _kill_group(proc)
        timed_out = threading.Event()

        def watchdog():
            if timeout and not proc_done.wait(timeout):
                timed_out.set()
                _kill_group(proc)

        proc_done = threading.Event()
        threading.Thread(target=watchdog, daemon=True).start()
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                self.log(line)
            code = proc.wait()
        finally:
            proc_done.set()
            with self._lock:
                self._job.proc = None
            if proc.poll() is None:
                _kill_group(proc)
        if self._job.cancel_event.is_set():
            raise Cancelled()
        if timed_out.is_set():
            raise JobError(f"Command timed out after {timeout // 60} min: {args[0]}")
        if code != 0:
            raise JobError(f"Command failed (exit code {code}): {' '.join(args[:3])}")


class _Held:
    def __init__(self, ctx: JobContext, lock: threading.Lock):
        self._ctx, self._lock = ctx, lock

    def __enter__(self):
        if self._lock.acquire(blocking=False):
            return self
        self._ctx.say("Waiting for another job that uses the same files")
        while not self._lock.acquire(timeout=0.5):
            self._ctx.check()
        return self

    def __exit__(self, *exc):
        self._lock.release()
        return False


def _kill_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        try:
            proc.wait(5)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


class JobManager:
    def __init__(self, secrets: Callable[[], list[str]] = lambda: [],
                 on_restart: Callable[[], None] | None = None):
        self._lock = threading.RLock()
        self._on_restart = on_restart
        self._jobs: dict[str, Job] = {}
        self._pool = ThreadPoolExecutor(max_workers=config.MAX_PARALLEL_JOBS, thread_name_prefix="job")
        self._secrets = secrets

    # -- submit
    def submit(self, kind: str, title: str, fn: Callable[[JobContext], None], key: str | None = None) -> Job:
        with self._lock:
            if key:
                for j in self._jobs.values():
                    if j.key == key and j.state not in FINISHED:
                        return j          # double click: hand back the running job
            queued = sum(1 for j in self._jobs.values() if j.state not in FINISHED)
            if queued >= config.MAX_QUEUED_JOBS:
                raise ValidationError("Too many unfinished jobs. Cancel some or wait for them to finish.")
            job = Job(kind, title, key)
            self._jobs[job.id] = job
            self._prune()
        ctx = JobContext(job, self._lock, self._secrets, self._on_restart)
        self._pool.submit(self._run, job, ctx, fn)
        return job

    def _run(self, job: Job, ctx: JobContext, fn: Callable[[JobContext], None]) -> None:
        with self._lock:
            if job.cancel_event.is_set():
                job.state, job.finished, job.message = CANCELLED, time.time(), "Cancelled"
                return
            job.state, job.started, job.message = RUNNING, time.time(), "Starting"
        try:
            fn(ctx)
        except Cancelled:
            self._finish(job, CANCELLED, "Cancelled")
        except (JobError, ValidationError) as e:
            ctx.log(f"ERROR: {e}")
            self._finish(job, ERROR, str(e), error=str(e))
        except Exception as e:  # noqa: BLE001 - last line of defence, keep the pool alive
            msg = f"{type(e).__name__}: {e}"
            ctx.log(f"ERROR: {msg}")
            self._finish(job, ERROR, msg, error=msg)
        else:
            with self._lock:
                if job.state == RUNNING:
                    if job.message in ("Starting", "Waiting"):
                        job.message = "Done"
                    job.finished = time.time()
                    job.state = DONE

    def _finish(self, job: Job, state: str, message: str, error: str | None = None) -> None:
        with self._lock:
            # state last: lock-free readers that see a finished state also see its message
            job.message = redact(message, self._secrets())
            job.error = redact(error, self._secrets()) if error else None
            job.finished = time.time()
            job.state = state

    # -- control
    def cancel(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job or job.state in FINISHED:
                return False
            job.cancel_event.set()
            proc = job.proc
            if job.state == QUEUED:                 # never started: finish it right now
                job.message, job.finished, job.state = "Cancelled", time.time(), CANCELLED
        if proc is not None:
            _kill_group(proc)
        return True

    def clear_finished(self) -> int:
        with self._lock:
            gone = [i for i, j in self._jobs.items() if j.state in FINISHED]
            for i in gone:
                del self._jobs[i]
            return len(gone)

    def _prune(self) -> None:
        done = sorted((j for j in self._jobs.values() if j.state in FINISHED), key=lambda j: j.finished or 0)
        for j in done[: max(0, len(done) - config.KEEP_FINISHED_JOBS)]:
            self._jobs.pop(j.id, None)

    # -- read
    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def active_count(self) -> int:
        with self._lock:
            return sum(1 for j in self._jobs.values() if j.state not in FINISHED)

    def snapshot(self, job: Job, with_log: bool = False) -> dict:
        with self._lock:
            total = job.bytes_total
            frac = (job.bytes_done / total) if total else None
            out = {
                "id": job.id, "kind": job.kind, "title": job.title, "state": job.state,
                "message": job.message, "error": job.error, "created": job.created,
                "started": job.started, "finished": job.finished,
                "bytes_done": job.bytes_done, "bytes_total": total,
                "progress": None if frac is None else min(1.0, frac),
                "needs_restart": job.needs_restart,
            }
            if with_log:
                out["log"] = list(job.log)
            return out

    def list(self) -> list[dict]:
        with self._lock:
            jobs = sorted(self._jobs.values(), key=lambda j: j.created, reverse=True)
            return [self.snapshot(j) for j in jobs]

    def shutdown(self) -> None:
        with self._lock:
            ids = [j.id for j in self._jobs.values() if j.state not in FINISHED]
        for i in ids:
            self.cancel(i)
        self._pool.shutdown(wait=False, cancel_futures=True)
