import base64
import json
import time

import pytest
from fastapi.testclient import TestClient

from launcher import config, settings
from launcher.app import create_app

DATA = bytes(range(256)) * 2000
WF = {"nodes": [{"id": 1, "type": "KSampler"}], "links": []}


@pytest.fixture
def client(env):
    with TestClient(create_app(autostart=False)) as c:
        yield c


def post(client, path, body=None, **kw):
    return client.post(path, content=json.dumps(body if body is not None else {}),
                       headers={"Content-Type": "application/json", **kw.pop("headers", {})}, **kw)


def wait_done(client, job_id, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        j = client.get(f"/api/jobs/{job_id}").json()
        if j["state"] in ("done", "error", "cancelled"):
            return j
        time.sleep(0.05)
    raise AssertionError(j)


# ----------------------------------------------------------------------------------------------- basics
def test_index_and_static(client):
    r = client.get("/")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]
    assert "default-src 'self'" in r.headers["content-security-policy"]
    assert "script-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["x-content-type-options"] == "nosniff"
    assert "<script>" not in r.text and "style=" not in r.text          # nothing inline, CSP stays strict
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/app.css").status_code == 200
    assert client.get("/static/../app.py").status_code in (404, 400)


def test_state_shape_and_no_secret_leak(client):
    settings.update({"hf_token": "hf_supersecret123"})
    r = client.get("/api/state")
    body = r.text
    assert r.status_code == 200 and "hf_supersecret123" not in body
    st = r.json()
    assert st["tokens"] == {"hf": "ui", "civitai": "none"}
    assert st["comfy"]["state"] in ("stopped", "missing")
    assert st["ports"] == {"comfy": config.COMFY_PORT, "jupyter": config.JUPYTER_PORT}
    assert st["password_protected"] is False
    assert r.headers["cache-control"] == "no-store"


def test_healthz_is_open_even_with_password(client, monkeypatch):
    monkeypatch.setattr(config, "LAUNCHER_PASSWORD", "pw12345")
    assert client.get("/healthz").json() == {"ok": True}


def test_password_protects_everything_else(client, monkeypatch):
    monkeypatch.setattr(config, "LAUNCHER_PASSWORD", "pw12345")
    for path in ("/", "/api/state", "/static/app.js", "/api/nodes"):
        r = client.get(path)
        assert r.status_code == 401 and "Basic" in r.headers["www-authenticate"], path
    bad = {"Authorization": "Basic " + base64.b64encode(b"x:wrong").decode()}
    assert client.get("/api/state", headers=bad).status_code == 401
    good = {"Authorization": "Basic " + base64.b64encode(b"x:pw12345").decode()}
    assert client.get("/api/state", headers=good).status_code == 200
    r = post(client, "/api/settings", {"hf_token": "hf_abcdefgh1"})
    assert r.status_code == 401                                         # writes need the password too
    assert settings.get("hf_token") == ""


# ------------------------------------------------------------------------------------------------ CSRF
def test_mutations_require_json_content_type(client):
    r = client.post("/api/jobs/clear", content="x=1", headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert r.status_code == 415
    r = client.post("/api/jobs/clear", content="{}", headers={"Content-Type": "text/plain"})
    assert r.status_code == 415
    assert client.post("/api/jobs/clear").status_code == 415            # no content type at all


def test_cross_site_requests_are_blocked(client):
    for site in ("cross-site", "same-site"):
        assert post(client, "/api/jobs/clear", headers={"Sec-Fetch-Site": site}).status_code == 403
    assert post(client, "/api/jobs/clear", headers={"Origin": "https://evil.example"}).status_code == 403
    assert post(client, "/api/jobs/clear", headers={"Sec-Fetch-Site": "same-origin"}).status_code == 200
    ok = {"Origin": "http://testserver"}
    assert post(client, "/api/jobs/clear", headers=ok).status_code == 200
    behind_proxy = {"Origin": "https://abc-3000.proxy.runpod.net", "X-Forwarded-Host": "abc-3000.proxy.runpod.net"}
    assert post(client, "/api/jobs/clear", headers=behind_proxy).status_code == 200
    # forged same-origin claim does not help when the Origin is foreign and Sec-Fetch-Site is absent
    assert post(client, "/api/jobs/clear", headers={"Origin": "https://evil.example",
                                                    "X-Forwarded-Host": "other"}).status_code == 403


# ---------------------------------------------------------------------------------------------- settings
def test_settings_roundtrip_and_validation(client):
    r = post(client, "/api/settings", {"hf_token": "hf_abcdefgh123"})
    assert r.status_code == 200 and "hf_abcdefgh123" not in r.text and r.json()["tokens"]["hf"] == "ui"
    assert oct(config.SETTINGS_FILE.stat().st_mode & 0o777) == "0o600"
    assert settings.get("hf_token") == "hf_abcdefgh123"
    r = post(client, "/api/settings", {"hf_token": "bad token with spaces and ;"})
    assert r.status_code == 400 and "bad token" not in r.text                 # the value is never echoed
    r = post(client, "/api/settings", {"hf_token": 12345})
    assert r.status_code == 400 and "12345" not in r.text
    r = post(client, "/api/settings", {"evil": "x"})
    assert r.status_code == 200 and settings.get("hf_token") == "hf_abcdefgh123"   # unknown keys are ignored
    r = post(client, "/api/settings", {"hf_token": ""})
    assert r.json()["tokens"]["hf"] == "none" and settings.get("hf_token") == ""


def test_env_token_is_reported_as_env(client, monkeypatch):
    monkeypatch.setenv("HF_TOKEN", "hf_fromenv12345")
    assert client.get("/api/state").json()["tokens"]["hf"] == "env"
    post(client, "/api/settings", {"hf_token": "hf_fromui123456"})
    assert settings.get("hf_token") == "hf_fromui123456"                       # UI value wins


# ------------------------------------------------------------------------------------------------ models
def test_model_download_flow(client, server):
    server.files["lora.safetensors"] = DATA
    r = post(client, "/api/models/download", {"url": server.url("/files/lora.safetensors"), "dir": "loras"})
    assert r.status_code == 200, r.text
    job = wait_done(client, r.json()["id"])
    assert job["state"] == "done", job
    listed = client.get("/api/models").json()
    assert {"dir": "loras", "name": "lora.safetensors", "size": len(DATA), "partial": False, "deletable": True} in listed["models"]
    assert "loras" in listed["dirs"] and "checkpoints" in listed["dirs"]
    r = post(client, "/api/models/delete", {"dir": "loras", "filename": "lora.safetensors"})
    assert r.status_code == 200
    assert client.get("/api/models").json()["models"] == []


@pytest.mark.parametrize("body", [
    {"url": "not a url", "dir": "loras"},
    {"url": "http://127.0.0.1:1/x.safetensors", "dir": "../../etc"},
    {"url": "http://127.0.0.1:1/x.safetensors", "dir": "loras", "filename": "../x.safetensors"},
    {"url": "http://127.0.0.1:1/x.safetensors", "dir": "loras", "filename": "evil.sh"},
    {"dir": "loras"},
    {"url": "x" * 5000, "dir": "loras"},
])
def test_download_rejects_bad_input(client, body):
    r = post(client, "/api/models/download", body)
    assert r.status_code == 400, r.text


def test_download_url_is_checked_before_a_job_exists(client, monkeypatch):
    monkeypatch.setattr(config, "ALLOW_PRIVATE_URLS", False)
    r = post(client, "/api/models/download", {"url": "https://169.254.169.254/latest/x.safetensors", "dir": "loras"})
    assert r.status_code == 400
    assert client.get("/api/jobs").json()["jobs"] == []


def test_double_click_returns_same_job(client, server):
    server.files["slow.safetensors"] = DATA * 20
    server.slow.add("slow.safetensors")
    body = {"url": server.url("/files/slow.safetensors"), "dir": "loras"}
    a = post(client, "/api/models/download", body).json()
    b = post(client, "/api/models/download", body).json()
    assert a["id"] == b["id"]
    assert post(client, f"/api/jobs/{a['id']}/cancel").json() == {"cancelled": True}
    assert wait_done(client, a["id"])["state"] == "cancelled"
    server.release.set()


def test_delete_model_rejects_traversal(client, tmp_path):
    (tmp_path / "secret.txt").write_text("x")
    for body in ({"dir": "loras", "filename": "../../../secret.txt"}, {"dir": "../..", "filename": "secret.txt"},
                 {"dir": "loras", "filename": "nothere.pt"}):
        assert post(client, "/api/models/delete", body).status_code == 400
    assert (tmp_path / "secret.txt").exists()


# -------------------------------------------------------------------------------------------- workflows
def write_catalog(env, entries):
    (env["catalog"] / "test.json").write_text(json.dumps(entries))


def test_catalog_install_with_skip_and_status(client, server, env):
    server.files["wf.json"] = json.dumps(WF).encode()
    server.files["m1.safetensors"] = DATA
    server.files["m2.safetensors"] = DATA
    write_catalog(env, [{
        "id": "demo", "title": "Demo", "description": "d", "tags": ["image"],
        "workflow_url": server.url("/files/wf.json"), "workflow_name": "demo.json",
        "models": [
            {"label": "one", "url": server.url("/files/m1.safetensors"), "dir": "loras", "size_gb": 0.1},
            {"label": "two", "url": server.url("/files/m2.safetensors"), "dir": "vae"},
        ],
    }, {"id": "broken", "title": "x", "nodes": ["https://gitlab.com/a/b"]}])
    r = client.get("/api/workflows").json()
    assert [c["id"] for c in r["catalog"]] == ["demo"]
    assert any("broken" in p or "github" in p for p in r["problems"])
    demo = r["catalog"][0]
    assert not demo["complete"] and "url" not in json.dumps(demo["models"][0])        # no server URLs leak

    job = post(client, "/api/workflows/install", {"id": "demo", "skip_models": [1]}).json()
    assert wait_done(client, job["id"])["state"] == "done"
    demo = client.get("/api/workflows").json()["catalog"][0]
    assert demo["workflow_installed"] and [m["installed"] for m in demo["models"]] == [True, False]
    assert not demo["complete"]
    assert (config.WORKFLOWS_DIR / "demo.json").exists()
    assert client.get("/api/workflows").json()["installed"][0]["name"] == "demo.json"

    job = post(client, "/api/workflows/install", {"id": "demo"}).json()
    assert wait_done(client, job["id"])["state"] == "done"
    assert client.get("/api/workflows").json()["catalog"][0]["complete"]
    assert post(client, "/api/workflows/install", {"id": "nope"}).status_code == 400


def test_catalog_install_reports_partial_failure(client, server, env):
    server.files["ok.safetensors"] = DATA
    write_catalog(env, {"id": "half", "title": "Half", "models": [
        {"label": "good", "url": server.url("/files/ok.safetensors"), "dir": "loras"},
        {"label": "gone", "url": server.url("/files/missing.safetensors"), "dir": "loras"}]})
    job = post(client, "/api/workflows/install", {"id": "half"}).json()
    j = wait_done(client, job["id"])
    assert j["state"] == "error" and "gone" in j["error"]
    assert (config.MODELS_DIR / "loras" / "ok.safetensors").exists()           # the good part still landed


def test_import_workflow(client):
    r = post(client, "/api/workflows/import", {"name": "mine", "workflow": WF})
    assert r.status_code == 200 and r.json() == {"name": "mine.json"}
    assert json.loads((config.WORKFLOWS_DIR / "mine.json").read_text()) == WF
    assert post(client, "/api/workflows/import", {"name": "mine", "workflow": WF}).status_code == 400   # exists
    assert post(client, "/api/workflows/import", {"name": "mine", "workflow": WF, "overwrite": True}).status_code == 200
    assert post(client, "/api/workflows/import", {"name": "../evil", "workflow": WF}).status_code == 400
    assert post(client, "/api/workflows/import", {"name": "x.py", "workflow": WF}).status_code == 400
    assert post(client, "/api/workflows/import", {"name": "notwf", "workflow": {"hello": "world"}}).status_code == 400
    assert post(client, "/api/workflows/import", {"name": "str", "workflow": "text"}).status_code == 400
    api_fmt = {"3": {"class_type": "KSampler", "inputs": {}}}
    assert post(client, "/api/workflows/import", {"name": "api-format", "workflow": api_fmt}).status_code == 200


def test_workflow_url_must_be_workflow_json(client, server, env):
    server.files["bad.json"] = b'{"not": "a workflow"}'
    write_catalog(env, {"id": "bad", "title": "Bad", "workflow_url": server.url("/files/bad.json")})
    job = post(client, "/api/workflows/install", {"id": "bad"}).json()
    j = wait_done(client, job["id"])
    assert j["state"] == "error" and "workflow" in j["error"]
    assert not (config.WORKFLOWS_DIR / "bad.json").exists()


def test_catalog_workflow_file_cannot_escape(client, env, tmp_path):
    (tmp_path / "outside.json").write_text(json.dumps(WF))
    write_catalog(env, {"id": "esc", "title": "Esc", "workflow_file": "../outside.json"})
    job = post(client, "/api/workflows/install", {"id": "esc"}).json()
    assert wait_done(client, job["id"])["state"] == "error"
    assert not (config.WORKFLOWS_DIR / "esc.json").exists()


# --------------------------------------------------------------------------------------------- nodes
def test_nodes_api_validation(client):
    for body in ({"url": "https://gitlab.com/a/b"}, {"url": "http://github.com/a/b"}, {"url": ""},
                 {"url": "https://github.com/a/b", "branch": "--upload-pack=x"}, {"url": "a"}):
        assert post(client, "/api/nodes/install", body).status_code == 400, body
    assert post(client, "/api/nodes/update", {"name": "../x"}).status_code == 400
    assert post(client, "/api/nodes/remove", {"name": "../x"}).status_code == 400
    assert post(client, "/api/nodes/remove", {"name": "ghost"}).status_code == 400
    assert client.get("/api/nodes").json() == {"nodes": []}


def test_node_remove_flags_restart(client):
    d = config.CUSTOM_NODES_DIR / "old-node"
    d.mkdir()
    (d / "__init__.py").write_text("")
    assert [n["name"] for n in client.get("/api/nodes").json()["nodes"]] == ["old-node"]
    assert post(client, "/api/nodes/remove", {"name": "old-node"}).status_code == 200
    assert client.get("/api/state").json()["comfy"]["restart_needed"] is True


# ----------------------------------------------------------------------------------------------- jobs
def test_unknown_job_and_clear(client, server):
    assert client.get("/api/jobs/nope").status_code == 404
    assert post(client, "/api/jobs/nope/cancel").json() == {"cancelled": False}
    server.files["a.safetensors"] = DATA
    j = post(client, "/api/models/download", {"url": server.url("/files/a.safetensors"), "dir": "loras"}).json()
    wait_done(client, j["id"])
    assert post(client, "/api/jobs/clear").json() == {"removed": 1}
    assert client.get("/api/jobs").json() == {"jobs": []}


def test_job_log_never_contains_tokens(client, server):
    settings.update({"hf_token": "hf_topsecret999"})
    server.files["a.safetensors"] = DATA
    start = server.url("/redir?to=" + server.url("/files/a.safetensors"), host="localhost")
    j = post(client, "/api/models/download", {"url": start, "dir": "loras", "filename": "a.safetensors"}).json()
    done = wait_done(client, j["id"])
    assert done["state"] == "done"
    assert "hf_topsecret999" not in json.dumps(done)


def test_invalid_json_body(client):
    r = client.post("/api/models/download", content="{not json", headers={"Content-Type": "application/json"})
    assert r.status_code == 400
    r = client.post("/api/settings", content=json.dumps(["hf_leaky_token_1"]), headers={"Content-Type": "application/json"})
    assert r.status_code == 400 and "hf_leaky_token_1" not in r.text


# -------------------------------------------------------------------------------------- comfy endpoints
def test_comfy_endpoints_without_install(client):
    assert post(client, "/api/comfy/start").json() == {"started": False}
    assert post(client, "/api/comfy/stop").status_code == 200
    lines = client.get("/api/comfy/log?tail=5").json()["lines"]
    assert any("not found" in l for l in lines)
    assert client.get("/api/comfy/log?tail=abc").status_code == 400
