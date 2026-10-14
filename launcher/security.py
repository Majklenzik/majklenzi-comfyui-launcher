"""Input validation: URLs (SSRF), file names, paths, GitHub repos, passwords."""
from __future__ import annotations

import base64
import binascii
import hmac
import ipaddress
import re
import socket
from pathlib import Path
from typing import NamedTuple
from urllib.parse import SplitResult, urlsplit

from . import config


class ValidationError(ValueError):
    """Raised for bad user input. The message is safe to show in the UI."""


# --------------------------------------------------------------------------------------------- URLs
_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_SIX_TO_FOUR = ipaddress.ip_network("2002::/16")
_BLOCKED_V6 = [ipaddress.ip_network(n) for n in ("fec0::/10", "2001::/32", "100::/64", "64:ff9b:1::/48")]


def _is_public(ip: str) -> bool:
    addr = ipaddress.ip_address(ip.split("%", 1)[0])
    if isinstance(addr, ipaddress.IPv6Address):
        if addr.ipv4_mapped:
            addr = addr.ipv4_mapped
        elif addr in _NAT64:                      # 64:ff9b::a.b.c.d carries an IPv4 address
            addr = ipaddress.IPv4Address(int(addr) & 0xFFFFFFFF)
        elif addr in _SIX_TO_FOUR:                # 2002:aabb:ccdd:: carries a.b.c.d too
            addr = ipaddress.IPv4Address((int(addr) >> 80) & 0xFFFFFFFF)
        elif any(addr in net for net in _BLOCKED_V6):
            return False
    return addr.is_global


def check_url(url: str) -> SplitResult:
    """Validate a download URL. Raises ValidationError. Call again for every redirect hop."""
    if not isinstance(url, str) or not url.strip():
        raise ValidationError("URL is empty")
    url = url.strip()
    if len(url) > 4096 or any(c in url for c in "\r\n\t\x00"):
        raise ValidationError("URL contains forbidden characters")
    parts = urlsplit(url)
    allowed_schemes = {"https", "http"} if config.ALLOW_PRIVATE_URLS else {"https"}
    if parts.scheme not in allowed_schemes:
        raise ValidationError("Only https:// links are allowed")
    if parts.username or parts.password:
        raise ValidationError("Links with a user name or password are not allowed")
    host = parts.hostname
    if not host:
        raise ValidationError("URL has no host name")
    try:
        port = parts.port
    except ValueError:
        raise ValidationError("URL has an invalid port") from None
    if not config.ALLOW_PRIVATE_URLS and port not in (None, 443):
        raise ValidationError("Only the default https port is allowed")
    if not config.ALLOW_PRIVATE_URLS:
        try:
            infos = socket.getaddrinfo(host, port or 443, type=socket.SOCK_STREAM)
        except socket.gaierror:
            raise ValidationError(f"Cannot resolve host {host}") from None
        addrs = {i[4][0] for i in infos}
        if not addrs or not all(_is_public(a) for a in addrs):
            raise ValidationError("This host points to a private or local address")
    return parts


# -------------------------------------------------------------------------------------------- names
_BAD_EXT = {
    ".py", ".pyc", ".sh", ".bash", ".zsh", ".exe", ".dll", ".so", ".dylib", ".bat", ".cmd", ".ps1",
    ".js", ".mjs", ".html", ".htm", ".php", ".pl", ".rb", ".jar", ".msi", ".apk", ".desktop",
}
_FILENAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+()\[\] -]{0,199}$")


def clean_filename(name: str) -> str:
    """Return a safe single file name or raise. No directories, no hidden files, no scripts."""
    if not isinstance(name, str):
        raise ValidationError("File name must be text")
    name = name.strip()
    if "/" in name or "\\" in name or "\x00" in name:
        raise ValidationError("File name must not contain slashes")
    if not _FILENAME_RE.match(name) or ".." in name:
        raise ValidationError("File name may only use letters, digits and . _ - + ( ) [ ] space")
    if Path(name).suffix.lower() in _BAD_EXT:
        raise ValidationError("This file type is not allowed")
    return name


def clean_subdir(rel: str) -> str:
    """Validate a relative directory such as 'loras' or 'ultralytics/bbox'. Returns normalised text."""
    if not isinstance(rel, str):
        raise ValidationError("Folder must be text")
    rel = rel.strip().replace("\\", "/").strip("/")
    if not rel:
        raise ValidationError("Choose a folder")
    if len(rel) > 120 or "\x00" in rel:
        raise ValidationError("Invalid folder")
    parts = rel.split("/")
    for p in parts:
        if p in ("", ".", "..") or not re.match(r"^[A-Za-z0-9][A-Za-z0-9._ -]{0,63}$", p):
            raise ValidationError("Folder may only contain letters, digits and . _ - space")
    return "/".join(parts)


def safe_join(base: Path, *rel: str) -> Path:
    """base / rel, guaranteed to stay inside base (symlinks resolved)."""
    base_r = base.resolve()
    target = base_r.joinpath(*rel).resolve()
    if target != base_r and base_r not in target.parents:
        raise ValidationError("Path escapes the allowed folder")
    return target


# -------------------------------------------------------------------------------------------- GitHub
class Repo(NamedTuple):
    owner: str
    name: str
    url: str          # https clone URL
    branch: str | None


_OWNER_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
_REPO_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
_BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/+-]{0,99}$")


def clean_branch(branch: str | None) -> str | None:
    if branch is None or not str(branch).strip():
        return None
    branch = str(branch).strip()
    if not _BRANCH_RE.match(branch) or ".." in branch or branch.endswith("/") or "//" in branch:
        raise ValidationError("Invalid branch name")
    return branch


def parse_repo(text: str, branch: str | None = None) -> Repo:
    """Accepts https://github.com/o/r[.git][/tree/<branch>], git@github.com:o/r.git or o/r."""
    if not isinstance(text, str) or not text.strip():
        raise ValidationError("Enter a GitHub link")
    raw = text.strip()
    if len(raw) > 300 or any(c in raw for c in "\r\n\t\x00 "):
        raise ValidationError("Invalid GitHub link")
    host = "github.com"
    path = raw
    if raw.startswith("git@"):
        m = re.match(r"^git@([^:]+):(.+)$", raw)
        if not m:
            raise ValidationError("Invalid GitHub link")
        host, path = m.group(1).lower(), m.group(2)
    elif "://" in raw:
        parts = urlsplit(raw)
        if parts.scheme != "https":
            raise ValidationError("Only https:// GitHub links are allowed")
        if parts.username or parts.password or parts.query or parts.fragment:
            raise ValidationError("Invalid GitHub link")
        host = (parts.hostname or "").lower()
        path = parts.path
    if host.startswith("www."):
        host = host[4:]
    if host not in config.GIT_HOSTS:
        raise ValidationError("Only github.com repositories are supported")
    segs = [s for s in path.strip("/").split("/") if s]
    if len(segs) < 2:
        raise ValidationError("Link must look like https://github.com/owner/repository")
    owner, name = segs[0], segs[1]
    url_branch = None
    if len(segs) >= 4 and segs[2] == "tree":
        url_branch = "/".join(segs[3:])
    if name.endswith(".git"):
        name = name[:-4]
    if not _OWNER_RE.match(owner) or not _REPO_RE.match(name) or name in (".", ".."):
        raise ValidationError("Invalid repository name")
    chosen = clean_branch(branch) or clean_branch(url_branch)
    return Repo(owner, name, f"https://{host}/{owner}/{name}.git", chosen)


# ----------------------------------------------------------------------------------------- passwords
def check_basic_auth(header: str | None, password: str) -> bool:
    """True if the Authorization header carries `password` (any user name)."""
    if not header or not header.lower().startswith("basic "):
        return False
    try:
        decoded = base64.b64decode(header[6:].strip(), validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return False
    _, _, supplied = decoded.partition(":")
    return hmac.compare_digest(supplied.encode(), password.encode())


def redact(text: str, secrets: list[str]) -> str:
    for s in secrets:
        if s and len(s) >= 6:
            text = text.replace(s, "***")
    return text
