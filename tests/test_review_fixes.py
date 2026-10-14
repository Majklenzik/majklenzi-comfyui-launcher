"""Regression tests for the problems an independent code review found."""
import json
import os
import subprocess
import sys
import threading
import time

import pytest
from fastapi.testclient import TestClient

from launcher import config, jobs as jobs_mod, models, nodes, security, settings, workflows
from launcher.app import create_app
from launcher.jobs import CANCELLED, DONE, ERROR, JobManager
from launcher.security import ValidationError

from tests.conftest import wait_job
from tests.test_models import DATA
from tests.test_nodes import git, upstream, run as run_node  # noqa: F401  (upstream is a fixture)


# ------------------------------------------------------------------ H1: protected by default
def test_password_is_generated_once_and_reused(monkeypatch):
    monkeypatch.setattr(config, "LAUNCHER_PASSWORD", "")
    monkeypatch.setattr(config, "ALLOW_NO_AUTH", False)
    first = settings.ensure_password()
    assert first and len(first) >= 16 and config.LAUNCHER_PASSWORD == first and config.PASSWORD_SOURCE == "generated"
    path = config.DATA_DIR / settings.PASSWORD_FILE_NAME
    assert oct(path.stat().st_mode & 0o777) == "0o600" and path.read_text().strip() == first
    monkeypatch.setattr(config, "LAUNCHER_PASSWORD", "")
    assert settings.ensure_password() == first                      # same password after a restart


def test_env_password_wins_and_opt_out_is_explicit(monkeypatch):
    monkeypatch.setattr(config, "LAUNCHER_PASSWORD", "mine-mine-mine")
    assert settings.ensure_password() is None and config.PASSWORD_SOURCE == "env"
    assert not (config.DATA_DIR / settings.PASSWORD_FILE_NAME).exists()
    monkeypatch.setattr(config, "LAUNCHER_PASSWORD", "")
    monkeypatch.setattr(config, "ALLOW_NO_AUTH", True)
    assert settings.ensure_password() is None and config.LAUNCHER_PASSWORD == "" and config.PASSWORD_SOURCE == "none"


def test_generated_password_protects_the_api(monkeypatch):
    monkeypatch.setattr(config, "LAUNCHER_PASSWORD", "")
    monkeypatch.setattr(config, "ALLOW_NO_AUTH", False)
    pw = settings.ensure_password()
    with TestClient(create_app(autostart=False)) as c:
        assert c.get("/api/state").status_code == 401
        r = c.post("/api/nodes/install", content=json.dumps({"url": "https://github.com/a/b"}),
                   headers={"Content-Type": "application/json"})
        assert r.status_code == 401                                  # the anonymous code-execution path is closed
        assert c.get("/api/state", auth=("x", pw)).json()["password_source"] == "generated"


def test_child_processes_never_get_the_passwords(jm, monkeypatch):
    monkeypatch.setenv("LAUNCHER_PASSWORD", "launcher-pw-123")
    monkeypatch.setenv("JUPYTER_PASSWORD", "jupyter-pw-123")
    monkeypatch.setenv("HF_TOKEN", "hf_envtoken1234")
    code = "import os; print([os.environ.get(k) for k in ('LAUNCHER_PASSWORD','JUPYTER_PASSWORD','HF_TOKEN')])"
    job = jm.submit("t", "t", lambda ctx: ctx.run([sys.executable, "-c", code]))
    wait_job(jm, job)
    assert job.state == DONE and "[None, None, None]" in "\n".join(job.log)
    assert "launcher-pw-123" not in config.child_env()["PATH"] and "LAUNCHER_PASSWORD" not in config.child_env()
    assert "HF_TOKEN" in config.child_env()                         # ComfyUI keeps the hub token


# ------------------------------------------------------------------ M2: request body limits
@pytest.fixture
def client(env):
    with TestClient(create_app(autostart=False)) as c:
        yield c


def test_body_size_limits(client):
    big = json.dumps({"url": "x" * 300_000, "dir": "loras"})
    r = client.post("/api/models/download", content=big, headers={"Content-Type": "application/json"})
    assert r.status_code == 413
    wf = {"nodes": [{"id": i, "type": "A"} for i in range(20000)], "links": []}     # about 0.5 MB
    r = client.post("/api/workflows/import", content=json.dumps({"name": "ok", "workflow": wf}),
                    headers={"Content-Type": "application/json"})
    assert r.status_code == 200
    huge = json.dumps({"name": "huge", "workflow": {"nodes": ["x" * (config.MAX_WORKFLOW_BYTES + 1_000_000)]}})
    r = client.post("/api/workflows/import", content=huge, headers={"Content-Type": "application/json"})
    assert r.status_code == 413
    assert not (config.WORKFLOWS_DIR / "huge.json").exists()


def test_chunked_bodies_without_length_are_refused(client):
    r = client.post("/api/jobs/clear", content=iter([b"{}"]), headers={"Content-Type": "application/json"})
    assert r.status_code == 411


def test_origin_null_is_blocked(client):
    r = client.post("/api/jobs/clear", content="{}", headers={"Content-Type": "application/json", "Origin": "null"})
    assert r.status_code == 403


def test_failed_login_is_slowed_down(client, monkeypatch):
    monkeypatch.setattr(config, "LAUNCHER_PASSWORD", "right-password-1")
    t = time.time()
    assert client.get("/api/state", auth=("x", "wrong")).status_code == 401
    assert time.time() - t >= 0.9
    t = time.time()
    assert client.get("/api/state").status_code == 401              # the browser's first, credential-less request
    assert time.time() - t < 0.5


# ------------------------------------------------------------------ M3: races between jobs
def test_same_model_from_two_jobs_is_downloaded_once(jm, server):
    server.files["race.safetensors"] = DATA * 4
    server.slow.add("race.safetensors")
    url = server.url("/files/race.safetensors")
    a = jm.submit("model", "a", lambda ctx: models.download_model(ctx, url, "loras"), key="from-models-page")
    for _ in range(300):
        if a.bytes_done > 0:
            break
        time.sleep(0.02)
    b = jm.submit("model", "b", lambda ctx: models.download_model(ctx, url, "loras"), key="from-workflow-card")
    time.sleep(0.3)
    assert "Waiting for another job" in b.message and b.bytes_done == 0   # B is parked, not racing A
    server.release.set()
    wait_job(jm, a), wait_job(jm, b)
    assert a.state == DONE and b.state == DONE, (a.message, b.message)
    assert (config.MODELS_DIR / "loras" / "race.safetensors").read_bytes() == DATA * 4
    assert not list((config.MODELS_DIR / "loras").glob(".dl-*"))
    assert sorted([a.message, b.message])[0].startswith("Already installed")


def test_same_node_from_two_jobs(jm, upstream):
    a = jm.submit("node", "a", lambda ctx: nodes.install_node(ctx, "https://github.com/test/my-node"), key="a")
    b = jm.submit("node", "b", lambda ctx: nodes.install_node(ctx, "https://github.com/test/my-node"), key="b")
    wait_job(jm, a, 60), wait_job(jm, b, 60)
    assert a.state == DONE and b.state == DONE, (a.message, b.message, list(a.log), list(b.log))
    assert (config.CUSTOM_NODES_DIR / "my-node" / "__init__.py").exists()


def test_pip_runs_are_serialised(jm):
    inside, overlap = [0], [0]
    guard = threading.Lock()

    def job(ctx):
        with ctx.lock(jobs_mod.PIP_LOCK):
            with guard:
                inside[0] += 1
                overlap[0] = max(overlap[0], inside[0])
            time.sleep(0.15)
            with guard:
                inside[0] -= 1

    js = [jm.submit("t", str(i), job) for i in range(2)]
    for j in js:
        wait_job(jm, j)
    assert overlap[0] == 1


# ------------------------------------------------------------------ L1/queue: cancel and caps
def test_cancelled_queued_job_can_be_retried_at_once(monkeypatch):
    monkeypatch.setattr(config, "MAX_PARALLEL_JOBS", 1)
    m = JobManager()
    gate = threading.Event()
    try:
        busy = m.submit("t", "busy", lambda ctx: gate.wait(10))
        queued = m.submit("t", "queued", lambda ctx: None, key="k")
        assert queued.state == jobs_mod.QUEUED
        assert m.cancel(queued.id)
        assert queued.state == CANCELLED                              # immediately, not when a worker frees up
        again = m.submit("t", "queued", lambda ctx: None, key="k")
        assert again.id != queued.id
    finally:
        gate.set()
        m.shutdown()


def test_queue_is_capped(monkeypatch, client):
    monkeypatch.setattr(config, "MAX_QUEUED_JOBS", 2)
    m = JobManager()
    gate = threading.Event()
    try:
        m.submit("t", "1", lambda ctx: gate.wait(10))
        m.submit("t", "2", lambda ctx: gate.wait(10))
        with pytest.raises(ValidationError):
            m.submit("t", "3", lambda ctx: None)
    finally:
        gate.set()
        m.shutdown()


# ------------------------------------------------------------------ M4: IPv6 ranges that carry IPv4
@pytest.mark.parametrize("ip,expected", [
    ("64:ff9b::7f00:1", False), ("64:ff9b::a00:5", False), ("fec0::1", False), ("2002:7f00:1::", False),
    ("2002:0a00:0001::", False), ("2001::1", False), ("::ffff:10.0.0.1", False), ("::1", False),
    ("64:ff9b::808:808", True), ("2002:0808:0808::", True), ("2606:4700:4700::1111", True), ("8.8.8.8", True),
])
def test_ipv6_embedded_addresses(ip, expected):
    assert security._is_public(ip) is expected


# ------------------------------------------------------------------ M5: unbounded downloads
def test_unannounced_size_cannot_fill_the_disk(jm, server, monkeypatch):
    server.files["endless.safetensors"] = DATA * 4
    server.chunked.add("endless.safetensors")
    free = 256 * 1024 * 1024 + 100_000
    monkeypatch.setattr(models.shutil, "disk_usage", lambda p: type("U", (), {"free": free})())
    url = server.url("/files/endless.safetensors")
    job = jm.submit("model", "t", lambda ctx: models.download_model(ctx, url, "loras"))
    wait_job(jm, job)
    assert job.state == ERROR and "disk is full" in job.message
    part = list((config.MODELS_DIR / "loras").glob(".dl-*.part"))[0]
    assert part.stat().st_size < 200_000                              # stopped right after the budget


def test_download_that_never_speeds_up_is_dropped(jm, server, monkeypatch):
    server.files["slowpoke.safetensors"] = DATA * 4
    server.slow.add("slowpoke.safetensors")
    monkeypatch.setattr(config, "RATE_WINDOW", 0.2)
    monkeypatch.setattr(config, "MIN_DOWNLOAD_RATE", 10 * 1024 * 1024)
    url = server.url("/files/slowpoke.safetensors")
    job = jm.submit("model", "t", lambda ctx: models.download_model(ctx, url, "loras"))
    wait_job(jm, job)
    assert job.state == ERROR and "too slow" in job.message
    server.release.set()


def test_more_data_than_announced_is_an_error(jm, server):
    class Resp:
        headers = {"Content-Length": "10"}
        status_code = 200
    assert models._total_from(Resp(), 0) == 10


# ------------------------------------------------------------------ L4/L5/L6: odd server answers
def test_unicode_digit_content_length_does_not_crash():
    class Resp:
        headers = {"Content-Length": "²"}
        status_code = 200
    assert models._total_from(Resp(), 0) is None


def test_complete_part_does_not_overwrite_existing_file(jm, server):
    server.files["keep.safetensors"] = DATA
    url = server.url("/files/keep.safetensors")
    import hashlib
    d = config.MODELS_DIR / "loras"
    d.mkdir(parents=True, exist_ok=True)
    (d / f".dl-{hashlib.sha1(url.encode()).hexdigest()[:12]}.part").write_bytes(DATA)
    (d / "keep.safetensors").write_bytes(b"mine")
    job = jm.submit("model", "t", lambda ctx: models.download_model(ctx, url, "loras"))
    wait_job(jm, job)
    assert job.state == DONE and (d / "keep.safetensors").read_bytes() == b"mine"
    assert not list(d.glob(".dl-*.part"))


def test_wrong_range_answer_is_not_appended(jm, server):
    server.files["bad.safetensors"] = DATA
    server.bad_range.add("bad.safetensors")
    url = server.url("/files/bad.safetensors")
    import hashlib
    d = config.MODELS_DIR / "loras"
    d.mkdir(parents=True, exist_ok=True)
    (d / f".dl-{hashlib.sha1(url.encode()).hexdigest()[:12]}.part").write_bytes(DATA[:300_000])
    job = jm.submit("model", "t", lambda ctx: models.download_model(ctx, url, "loras"))
    wait_job(jm, job)
    assert job.state == DONE, job.message
    assert (d / "bad.safetensors").read_bytes() == DATA               # exact size, no duplicated prefix


# ------------------------------------------------------------------ L7: bad catalog values
@pytest.mark.parametrize("size", ["1e999", "NaN", "Infinity", "-5", "true", "1e30"])
def test_bad_size_gb_skips_only_that_entry(client, env, size):
    good = {"id": "good", "title": "Good", "nodes": ["https://github.com/a/b"]}
    bad = '{"id": "bad", "title": "Bad", "models": [{"url": "https://huggingface.co/a/b/resolve/main/x.pt", ' \
          f'"dir": "loras", "size_gb": {size}}}]}}'
    (env["catalog"] / "a.json").write_text(json.dumps(good))
    (env["catalog"] / "b.json").write_text(bad)
    r = client.get("/api/workflows")
    assert r.status_code == 200
    assert [c["id"] for c in r.json()["catalog"]] == ["good"] and r.json()["problems"]


# ------------------------------------------------------------------ L8/L11: nodes
def test_remove_single_file_node(env):
    f = config.CUSTOM_NODES_DIR / "one_file_node.py"
    f.write_text("NODE_CLASS_MAPPINGS = {}")
    assert [n["name"] for n in nodes.list_nodes()] == ["one_file_node.py"]
    nodes.remove_node("one_file_node.py")
    assert not f.exists()


def test_node_on_a_tag_explains_why_it_cannot_update(jm, upstream):
    git("tag", "v1", cwd=upstream)
    job = run_node(jm, lambda ctx: nodes.install_node(ctx, "https://github.com/test/my-node", "v1"))
    assert job.state == DONE, job.message
    job = run_node(jm, lambda ctx: nodes.update_node(ctx, "my-node"))
    assert job.state == ERROR and "fixed commit or tag" in job.message


def test_reinstall_with_other_branch_says_so(jm, upstream):
    job = run_node(jm, lambda ctx: nodes.install_node(ctx, "https://github.com/test/my-node"))
    assert job.state == DONE
    git("branch", "dev", cwd=upstream)
    job = run_node(jm, lambda ctx: nodes.install_node(ctx, "https://github.com/test/my-node", "dev"))
    assert any("remove it first" in line for line in job.log)
