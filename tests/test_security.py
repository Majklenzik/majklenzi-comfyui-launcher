import base64
import os
import socket

import pytest

from launcher import config, security
from launcher.security import ValidationError


@pytest.fixture
def strict(monkeypatch):
    """Production URL rules (the autouse env fixture relaxes them for local servers)."""
    monkeypatch.setattr(config, "ALLOW_PRIVATE_URLS", False)

    def fake_gai(host, port, *a, **k):
        table = {
            "huggingface.co": ["93.184.216.34"],
            "rebind.example": ["93.184.216.34", "10.0.0.5"],
            "internal.example": ["192.168.1.10"],
            "meta.example": ["169.254.169.254"],
            "v6.example": ["::ffff:127.0.0.1"],
            "cgnat.example": ["100.64.0.9"],
        }
        if host in table:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port)) for ip in table[host]]
        try:  # literal IPs
            socket.inet_aton(host)
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (host, port))]
        except OSError:
            raise socket.gaierror("nope")

    monkeypatch.setattr(security.socket, "getaddrinfo", fake_gai)


def test_url_accepts_public_https(strict):
    assert security.check_url("https://huggingface.co/a/b/resolve/main/x.safetensors").hostname == "huggingface.co"


@pytest.mark.parametrize("url", [
    "http://huggingface.co/x",                  # plain http
    "ftp://huggingface.co/x",
    "https://user:pw@huggingface.co/x",         # credentials in URL
    "https://huggingface.co:8443/x",            # odd port
    "https://127.0.0.1/x",                      # loopback literal
    "https://10.1.2.3/x",
    "https://169.254.169.254/latest/meta-data",  # cloud metadata
    "https://meta.example/x",                   # name that resolves to metadata IP
    "https://internal.example/x",
    "https://rebind.example/x",                 # one public + one private answer
    "https://v6.example/x",                     # IPv4-mapped IPv6 loopback
    "https://cgnat.example/x",                  # carrier-grade NAT range
    "https://unknown.invalid/x",                # does not resolve
    "https:///nohost",
    "",
    "https://huggingface.co/x\r\nHost: evil",
])
def test_url_rejects(strict, url):
    with pytest.raises(ValidationError):
        security.check_url(url)


@pytest.mark.parametrize("name", ["model.safetensors", "My Model (v2).ckpt", "a-b_c.1.pt", "x.gguf"])
def test_filename_ok(name):
    assert security.clean_filename(name) == name


@pytest.mark.parametrize("name", [
    "", "../x.safetensors", "a/b.pt", "a\\b.pt", ".hidden", "x.py", "x.SH", "evil.exe", "a..b.pt",
    "x" * 300 + ".pt", "bad\x00.pt", "semi;colon.pt", "-rf.pt",
])
def test_filename_bad(name):
    with pytest.raises(ValidationError):
        security.clean_filename(name)


@pytest.mark.parametrize("rel,expected", [("loras", "loras"), ("/ultralytics/bbox/", "ultralytics/bbox"),
                                          ("text_encoders", "text_encoders")])
def test_subdir_ok(rel, expected):
    assert security.clean_subdir(rel) == expected


@pytest.mark.parametrize("rel", ["", ".", "..", "../etc", "a//b", "a/../b", "/", ".hidden", "a b/..", "x" * 200])
def test_subdir_bad(rel):
    with pytest.raises(ValidationError):
        security.clean_subdir(rel)


def test_safe_join_blocks_symlink_escape(tmp_path):
    base = tmp_path / "base"
    base.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    os.symlink(outside, base / "link")
    with pytest.raises(ValidationError):
        security.safe_join(base, "link", "file")
    assert security.safe_join(base, "ok", "file") == (base / "ok" / "file").resolve()
    with pytest.raises(ValidationError):
        security.safe_join(base, "..", "outside")


@pytest.mark.parametrize("text,branch,expected", [
    ("https://github.com/kijai/ComfyUI-KJNodes", None, ("kijai", "ComfyUI-KJNodes", None)),
    ("https://github.com/kijai/ComfyUI-KJNodes.git", None, ("kijai", "ComfyUI-KJNodes", None)),
    ("https://github.com/kijai/ComfyUI-KJNodes/", None, ("kijai", "ComfyUI-KJNodes", None)),
    ("https://www.github.com/a/b", None, ("a", "b", None)),
    ("https://github.com/a/b/tree/dev", None, ("a", "b", "dev")),
    ("https://github.com/a/b/tree/feature/x", None, ("a", "b", "feature/x")),
    ("https://github.com/a/b", "main", ("a", "b", "main")),
    ("git@github.com:a/b.git", None, ("a", "b", None)),
    ("a/b", None, ("a", "b", None)),
])
def test_parse_repo_ok(text, branch, expected):
    r = security.parse_repo(text, branch)
    assert (r.owner, r.name, r.branch) == expected
    assert r.url == f"https://github.com/{r.owner}/{r.name}.git"


@pytest.mark.parametrize("text,branch", [
    ("", None), ("a", None), ("https://gitlab.com/a/b", None), ("http://github.com/a/b", None),
    ("https://user:pw@github.com/a/b", None), ("https://github.com/a/b?x=1", None),
    ("https://github.com/a/..", None), ("https://github.com/-bad/b", None), ("a/b c", None),
    ("https://github.com/a/b", "--upload-pack=evil"), ("https://github.com/a/b", "a..b"),
    ("ssh://github.com/a/b", None), ("https://github.com.evil.com/a/b", None),
    ("git@evil.com:a/b.git", None), ("a/b\nc", None),
])
def test_parse_repo_bad(text, branch):
    with pytest.raises(ValidationError):
        security.parse_repo(text, branch)


def _hdr(user, pw):
    return "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()


def test_basic_auth():
    assert security.check_basic_auth(_hdr("anyone", "secret"), "secret")
    assert security.check_basic_auth(_hdr("", "sec:ret"), "sec:ret")
    assert not security.check_basic_auth(_hdr("a", "wrong"), "secret")
    assert not security.check_basic_auth(None, "secret")
    assert not security.check_basic_auth("Bearer secret", "secret")
    assert not security.check_basic_auth("Basic !!!notbase64", "secret")
    assert not security.check_basic_auth(_hdr("a", ""), "secret")


def test_redact():
    assert security.redact("token=hf_abcdef123 ok", ["hf_abcdef123"]) == "token=*** ok"
    assert security.redact("short", ["abc"]) == "short"     # too short to be a real secret
