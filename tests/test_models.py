import hashlib
import os

import pytest

from launcher import config, models, settings
from launcher.jobs import ERROR, DONE, CANCELLED
from launcher.security import ValidationError

from tests.conftest import wait_job

DATA = bytes(range(256)) * 4000        # ~1 MB, easy to verify


def run(jm, server, path, subdir="loras", **kw):
    url = server.url(path)
    job = jm.submit("model", "t", lambda ctx: models.download_model(ctx, url, subdir, **kw))
    return wait_job(jm, job)


def test_plain_download(jm, server):
    server.files["a.safetensors"] = DATA
    job = run(jm, server, "/files/a.safetensors")
    assert job.state == DONE, job.message
    assert (config.MODELS_DIR / "loras" / "a.safetensors").read_bytes() == DATA
    assert not list((config.MODELS_DIR / "loras").glob(".dl-*"))            # no leftovers
    assert job.bytes_done == job.bytes_total == len(DATA)


def test_resume_from_partial(jm, server):
    server.files["big.safetensors"] = DATA
    url = server.url("/files/big.safetensors")
    part = config.MODELS_DIR / "loras" / models.hashlib.sha1(url.encode()).hexdigest()[:12]
    part = config.MODELS_DIR / "loras" / f".dl-{part.name}.part"
    part.parent.mkdir(parents=True, exist_ok=True)
    part.write_bytes(DATA[:300_000])
    job = run(jm, server, "/files/big.safetensors")
    assert job.state == DONE, job.message
    assert (config.MODELS_DIR / "loras" / "big.safetensors").read_bytes() == DATA
    ranges = [h.get("range") for p, h in server.log if p == "/files/big.safetensors"]
    assert ranges == ["bytes=300000-"]


def test_server_ignores_range_restarts(jm, server):
    server.files["n.safetensors"] = DATA
    url = server.url("/norange/n.safetensors")
    pname = f".dl-{hashlib.sha1(url.encode()).hexdigest()[:12]}.part"
    d = config.MODELS_DIR / "loras"
    d.mkdir(parents=True, exist_ok=True)
    (d / pname).write_bytes(b"garbage-from-earlier")
    job = run(jm, server, "/norange/n.safetensors")
    assert job.state == DONE, job.message
    assert (d / "n.safetensors").read_bytes() == DATA


def test_complete_partial_gets_416_and_finishes(jm, server):
    server.files["c.safetensors"] = DATA
    url = server.url("/files/c.safetensors")
    d = config.MODELS_DIR / "loras"
    d.mkdir(parents=True, exist_ok=True)
    (d / f".dl-{hashlib.sha1(url.encode()).hexdigest()[:12]}.part").write_bytes(DATA)
    job = run(jm, server, "/files/c.safetensors")
    assert job.state == DONE, job.message
    assert (d / "c.safetensors").read_bytes() == DATA


def test_connection_drop_retries_and_resumes(jm, server):
    server.files["f.safetensors"] = DATA
    server.flaky_once.add("f.safetensors")
    job = run(jm, server, "/files/f.safetensors")
    assert job.state == DONE, job.message
    assert (config.MODELS_DIR / "loras" / "f.safetensors").read_bytes() == DATA
    assert any("retrying" in line for line in job.log)


def test_existing_file_is_kept(jm, server):
    server.files["e.safetensors"] = DATA
    d = config.MODELS_DIR / "loras"
    (d).mkdir(parents=True, exist_ok=True)
    (d / "e.safetensors").write_bytes(b"mine")
    job = run(jm, server, "/files/e.safetensors")
    assert job.state == DONE
    assert (d / "e.safetensors").read_bytes() == b"mine"
    assert "Already installed" in job.message
    job = run(jm, server, "/files/e.safetensors", overwrite=True)
    assert (d / "e.safetensors").read_bytes() == DATA


def test_filename_from_content_disposition(jm, server):
    server.files["12345"] = DATA
    server.disp_names["12345"] = "real-name.safetensors"
    job = run(jm, server, "/disp/12345")
    assert job.state == DONE, job.message
    assert (config.MODELS_DIR / "loras" / "real-name.safetensors").read_bytes() == DATA


def test_unknown_name_asks_for_one(jm, server):
    server.files["12345"] = DATA
    job = run(jm, server, "/files/12345")
    assert job.state == ERROR and "File name" in job.message
    job = run(jm, server, "/files/12345", filename="given.safetensors")
    assert job.state == DONE
    assert (config.MODELS_DIR / "loras" / "given.safetensors").exists()


def test_checksum(jm, server):
    server.files["s.safetensors"] = DATA
    good = hashlib.sha256(DATA).hexdigest()
    job = run(jm, server, "/files/s.safetensors", sha256=good)
    assert job.state == DONE
    (config.MODELS_DIR / "loras" / "s.safetensors").unlink()
    job = run(jm, server, "/files/s.safetensors", sha256="0" * 64)
    assert job.state == ERROR and "Checksum" in job.message
    assert not (config.MODELS_DIR / "loras" / "s.safetensors").exists()


@pytest.mark.parametrize("code,needle", [(404, "not found"), (401, "Access denied"), (403, "Access denied"),
                                         (429, "Too many"), (500, "HTTP 500")])
def test_http_errors(jm, server, code, needle):
    url = server.url(f"/status/{code}/model.safetensors")
    # route /status/<code> ignores the tail, filename comes from the URL tail
    job = jm.submit("model", "t", lambda ctx: models.download_model(ctx, url, "loras", "x.safetensors"))
    wait_job(jm, job)
    assert job.state == ERROR
    assert needle.lower() in job.message.lower()


def test_hf_token_only_on_hf_host_and_dropped_on_redirect(jm, server):
    settings.update({"hf_token": "hf_secrettoken123"})
    server.files["t.safetensors"] = DATA
    # first hop "localhost" counts as the Hugging Face host (config patched), it redirects to 127.0.0.1
    target = server.url("/files/t.safetensors", host="127.0.0.1")
    start = server.url("/redir?to=" + target, host="localhost")
    job = jm.submit("model", "t", lambda ctx: models.download_model(ctx, start, "loras", "t.safetensors"))
    wait_job(jm, job)
    assert job.state == DONE, job.message
    first = [h for p, h in server.log if p == "/redir"][0]
    second = [h for p, h in server.log if p == "/files/t.safetensors"][0]
    assert first.get("authorization") == "Bearer hf_secrettoken123"
    assert "authorization" not in second                                    # CDN hop gets no token
    assert "hf_secrettoken123" not in "\n".join(job.log)


def test_token_never_sent_to_other_hosts(jm, server):
    settings.update({"hf_token": "hf_secrettoken123", "civitai_token": "civ_secrettoken456"})
    server.files["o.safetensors"] = DATA
    job = run(jm, server, "/files/o.safetensors")            # 127.0.0.1 is not an HF/CivitAI host
    assert job.state == DONE
    assert all("authorization" not in h for _, h in server.log)


def test_plain_http_is_refused_in_production(jm, server, monkeypatch):
    settings.update({"hf_token": "hf_secrettoken123"})
    monkeypatch.setattr(config, "ALLOW_PRIVATE_URLS", False)
    with pytest.raises(ValidationError):
        models.download_model(None, server.url("/files/x.safetensors", host="localhost"), "loras")
    assert server.log == []                       # nothing was sent, so no token either


def test_redirect_to_private_address_is_blocked(jm, server, monkeypatch):
    monkeypatch.setattr(config, "ALLOW_PRIVATE_URLS", False)
    class FakeResp:
        status_code = 302
        headers = {"Location": "https://169.254.169.254/latest/meta-data/"}

        def close(self):
            pass

    calls = []

    class FakeSession:
        def get(self, url, **kw):
            calls.append(url)
            if len(calls) == 1:
                return FakeResp()
            raise AssertionError("second hop must never be requested")

    def fake_gai(host, port, *a, **k):
        ip = "169.254.169.254" if host == "169.254.169.254" else "93.184.216.34"
        return [(2, 1, 6, "", (ip, port))]

    monkeypatch.setattr("launcher.security.socket.getaddrinfo", fake_gai)
    job = jm.submit("model", "t", lambda ctx: models._open(ctx, FakeSession(), "https://huggingface.co/a/b.safetensors", 0))
    wait_job(jm, job)
    assert job.state == ERROR
    assert "private or local" in job.message
    assert calls == ["https://huggingface.co/a/b.safetensors"]


def test_redirect_loop_gives_up(jm, server):
    job = run(jm, server, "/loop", filename="x.safetensors")
    assert job.state == ERROR and "redirect" in job.message.lower()
    assert len([p for p, _ in server.log if p == "/loop"]) == config.MAX_REDIRECTS + 1


def test_cancel_keeps_partial_for_resume(jm, server):
    big = os.urandom(2_000_000)
    server.files["slow.safetensors"] = big
    server.slow.add("slow.safetensors")
    url = server.url("/files/slow.safetensors")
    job = jm.submit("model", "t", lambda ctx: models.download_model(ctx, url, "loras"))
    for _ in range(200):
        if job.bytes_done > 0:
            break
        import time; time.sleep(0.02)
    assert jm.cancel(job.id)
    wait_job(jm, job)
    assert job.state == CANCELLED
    assert list((config.MODELS_DIR / "loras").glob(".dl-*.part"))
    assert not (config.MODELS_DIR / "loras" / "slow.safetensors").exists()
    # listing shows it as incomplete, delete can discard it
    items = models.list_models()
    part = [m for m in items if m["partial"]][0]
    models.delete_model(part["dir"], part["name"])
    assert not list((config.MODELS_DIR / "loras").glob(".dl-*.part"))
    server.release.set()


def test_disk_space_check(jm, server, monkeypatch):
    server.files["d.safetensors"] = DATA
    monkeypatch.setattr(models.shutil, "disk_usage", lambda p: type("U", (), {"free": 1000})())
    job = run(jm, server, "/files/d.safetensors")
    assert job.state == ERROR and "disk space" in job.message
    assert not (config.MODELS_DIR / "loras" / "d.safetensors").exists()


def test_normalize_hf_blob_url(monkeypatch):
    monkeypatch.setattr(config, "HF_HOSTS", {"huggingface.co"})
    assert models.normalize_url("https://huggingface.co/o/r/blob/main/sub/f.safetensors?download=true") == \
        "https://huggingface.co/o/r/resolve/main/sub/f.safetensors"
    assert models.normalize_url("https://huggingface.co/o/r/resolve/main/f.safetensors") == \
        "https://huggingface.co/o/r/resolve/main/f.safetensors"
    assert models.normalize_url("https://example.com/o/r/blob/main/f.pt") == "https://example.com/o/r/blob/main/f.pt"


def test_traversal_in_inputs_is_rejected(jm, server):
    server.files["a.safetensors"] = DATA
    for subdir in ("../../etc", "abs/../../b", "..", "a/./b"):
        with pytest.raises(ValidationError):
            models.download_model(None, server.url("/files/a.safetensors"), subdir)
    for name in ("../evil.safetensors", "x/y.pt", "run.sh"):
        with pytest.raises(ValidationError):
            models.download_model(None, server.url("/files/a.safetensors"), "loras", name)


def test_list_and_delete(env):
    d = config.MODELS_DIR / "checkpoints"
    d.mkdir(parents=True, exist_ok=True)
    (d / "m.safetensors").write_bytes(b"123")
    (d / "put_checkpoints_here").write_bytes(b"")
    (d / ".hidden").write_bytes(b"x")
    (config.MODELS_DIR / "loras").mkdir(exist_ok=True)
    (config.MODELS_DIR / "loras" / "l.safetensors").write_bytes(b"12")
    items = models.list_models()
    assert {(m["dir"], m["name"]) for m in items} == {("checkpoints", "m.safetensors"), ("loras", "l.safetensors")}
    models.delete_model("checkpoints", "m.safetensors")
    assert not (d / "m.safetensors").exists()
    for bad in (("checkpoints", "../loras/l.safetensors"), ("../loras", "l.safetensors"), ("loras", "missing.pt")):
        with pytest.raises(ValidationError):
            models.delete_model(*bad)
    assert (config.MODELS_DIR / "loras" / "l.safetensors").exists()


def test_delete_symlink_removes_only_the_link(env, tmp_path):
    outside = tmp_path / "secret.safetensors"
    outside.write_bytes(b"keep me")
    d = config.MODELS_DIR / "loras"
    d.mkdir(exist_ok=True)
    os.symlink(outside, d / "link.safetensors")
    models.delete_model("loras", "link.safetensors")
    assert not (d / "link.safetensors").is_symlink()
    assert outside.read_bytes() == b"keep me"             # the target is untouched


def test_delete_alias_inside_models_keeps_the_real_file(env):
    real = config.MODELS_DIR / "diffusion_models"
    real.mkdir(exist_ok=True)
    (real / "real.safetensors").write_bytes(b"data")
    os.symlink(real / "real.safetensors", real / "alias.safetensors")
    models.delete_model("diffusion_models", "alias.safetensors")
    assert (real / "real.safetensors").read_bytes() == b"data"


def test_listing_marks_what_cannot_be_deleted(env):
    (config.MODELS_DIR / "loras").mkdir(exist_ok=True)
    (config.MODELS_DIR / "loras" / "ok.safetensors").write_bytes(b"1")
    (config.MODELS_DIR / "loras" / "weird;name.safetensors").write_bytes(b"1")
    (config.MODELS_DIR / "rootfile.safetensors").write_bytes(b"1")
    by_name = {m["name"]: m["deletable"] for m in models.list_models()}
    assert by_name == {"ok.safetensors": True, "weird;name.safetensors": False, "rootfile.safetensors": False}
