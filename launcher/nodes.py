"""Custom nodes: install / update / remove from GitHub, plus the installed-node listing."""
from __future__ import annotations

import re
import shutil
from importlib import metadata

from . import config
from .jobs import PIP_LOCK, JobContext, JobError
from .security import ValidationError, parse_repo, safe_join

NODE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ -]{0,99}$")
_PROTECTED = {"__pycache__"}


def _git_env() -> dict:
    return {
        "GIT_TERMINAL_PROMPT": "0",          # fail instead of asking for a password (private repos)
        "GIT_ALLOW_PROTOCOL": config.GIT_PROTOCOLS,
        "GIT_ASKPASS": "true",
        "GCM_INTERACTIVE": "never",
    }


def _constraints_file() -> str:
    """Pin the packages a node must never replace: torch stack (CUDA build) and numpy's major version."""
    lines = []
    for pkg in ("torch", "torchvision", "torchaudio"):
        try:
            lines.append(f"{pkg}=={metadata.version(pkg).split('+')[0]}")
        except metadata.PackageNotFoundError:
            pass
    try:
        major = int(metadata.version("numpy").split(".")[0])
        lines.append(f"numpy>={major},<{major + 1}")
    except (metadata.PackageNotFoundError, ValueError):
        pass
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = config.DATA_DIR / "constraints.txt"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(path)


def _install_deps(ctx: JobContext, node_dir) -> None:
    req = node_dir / "requirements.txt"
    if req.is_file():
        ctx.say("Installing Python requirements")
        with ctx.lock(PIP_LOCK):                       # concurrent `pip install --user` runs corrupt each other
            ctx.run([config.PYTHON, "-m", "pip", "install", "--disable-pip-version-check", "--no-input",
                     "-r", str(req), "-c", _constraints_file()], cwd=node_dir, timeout=config.PIP_TIMEOUT)
    script = node_dir / "install.py"
    if script.is_file():
        ctx.say("Running install.py (same as ComfyUI-Manager does)")
        with ctx.lock(PIP_LOCK):
            ctx.run([config.PYTHON, str(script)], cwd=node_dir, timeout=config.PIP_TIMEOUT)


def _check_name(name: str) -> str:
    if not isinstance(name, str) or not NODE_NAME_RE.match(name) or ".." in name or name in _PROTECTED:
        raise ValidationError("Invalid node folder name")
    return name


def install_node(ctx: JobContext, repo_text: str, branch: str | None = None) -> str:
    """Clone a GitHub repo into custom_nodes (or update it when it is already there)."""
    repo = parse_repo(repo_text, branch)
    name = _check_name(repo.name)
    with ctx.lock(f"node:{name.lower()}"):          # same node from two places: second job waits, then updates
        return _install_node_locked(ctx, repo, name)


def _install_node_locked(ctx: JobContext, repo, name: str) -> str:
    config.CUSTOM_NODES_DIR.mkdir(parents=True, exist_ok=True)
    dest = safe_join(config.CUSTOM_NODES_DIR, name)

    if dest.exists():
        if not (dest / ".git").exists():
            raise JobError(f"custom_nodes/{name} already exists and is not a git checkout; remove it first.")
        have = _branch(dest)
        if repo.branch and repo.branch != have:
            ctx.log(f"Note: {name} is installed on {have or 'a fixed commit'}; to switch to {repo.branch} remove it "
                    "first and install again.")
        ctx.say(f"{name} is already installed, updating")
        return _update_locked(ctx, name)

    ctx.say(f"Cloning {repo.owner}/{repo.name}")
    tmp = safe_join(config.CUSTOM_NODES_DIR, f".cloning-{name}")
    if tmp.exists():
        shutil.rmtree(tmp, ignore_errors=True)
    cmd = ["git", "clone", "--depth", "1", "--recurse-submodules", "--shallow-submodules"]
    if repo.branch:
        cmd += ["--branch", repo.branch]
    cmd += ["--", repo.url, str(tmp)]
    try:
        ctx.run(cmd, env=_git_env(), timeout=config.GIT_TIMEOUT)
        ctx.check()
        tmp.rename(dest)       # only now does ComfyUI see it, never a half-cloned folder
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    try:
        _install_deps(ctx, dest)
    except BaseException:
        ctx.log("Dependencies failed. The node folder was kept; fix the error above and press Update, "
                "or remove the node.")
        raise
    ctx.needs_restart()
    ctx.say(f"Installed {name}. Restart ComfyUI to load it.")
    return name


def update_node(ctx: JobContext, name: str) -> str:
    name = _check_name(name)
    with ctx.lock(f"node:{name.lower()}"):
        return _update_locked(ctx, name)


def _update_locked(ctx: JobContext, name: str) -> str:
    dest = safe_join(config.CUSTOM_NODES_DIR, name)
    if not (dest / ".git").exists():
        raise JobError(f"{name} is not a git checkout, cannot update.")
    if _branch(dest) is None:
        raise JobError(f"{name} is on a fixed commit or tag, so it cannot be updated. Remove it and install it again.")
    ctx.say(f"Updating {name}")
    ctx.run(["git", "-C", str(dest), "pull", "--ff-only", "--recurse-submodules"], env=_git_env(),
            timeout=config.GIT_TIMEOUT)
    _install_deps(ctx, dest)
    ctx.needs_restart()
    ctx.say(f"Updated {name}. Restart ComfyUI to load it.")
    return name


def remove_node(name: str) -> None:
    name = _check_name(name)
    raw = config.CUSTOM_NODES_DIR / name
    if raw.is_symlink():          # unlink the link itself, never what it points to
        raw.unlink()
        return
    if raw.is_file() and raw.suffix == ".py":     # single-file node
        raw.unlink()
        return
    dest = safe_join(config.CUSTOM_NODES_DIR, name)
    if not dest.is_dir():
        raise ValidationError("Node not found")
    shutil.rmtree(dest)


# --------------------------------------------------------------------------------------------- listing
_URL_RE = re.compile(r"^\s*url\s*=\s*(\S+)\s*$", re.M)
_HEAD_RE = re.compile(r"^ref:\s*refs/heads/(\S+)")


def _remote_url(node_dir) -> str | None:
    try:
        text = (node_dir / ".git" / "config").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    m = _URL_RE.search(text)
    if not m:
        return None
    url = m.group(1)
    # never show credentials that may sit in a remote URL
    return re.sub(r"//[^/@]+@", "//", url)


def _branch(node_dir) -> str | None:
    try:
        m = _HEAD_RE.match((node_dir / ".git" / "HEAD").read_text(encoding="utf-8").strip())
        return m.group(1) if m else None
    except OSError:
        return None


def list_nodes() -> list[dict]:
    root = config.CUSTOM_NODES_DIR
    if not root.is_dir():
        return []
    out = []
    for p in sorted(root.iterdir(), key=lambda x: x.name.lower()):
        if p.name.startswith(".") or p.name in _PROTECTED:
            continue
        if p.is_dir():
            out.append({
                "name": p.name,
                "kind": "git" if (p / ".git").exists() else "folder",
                "url": _remote_url(p),
                "branch": _branch(p),
                "disabled": p.name.endswith(".disabled"),
                "has_requirements": (p / "requirements.txt").is_file(),
            })
        elif p.suffix == ".py":
            out.append({"name": p.name, "kind": "file", "url": None, "branch": None, "disabled": False,
                        "has_requirements": False})
    return out


def installed_repo_names() -> set[str]:
    return {n["name"].lower() for n in list_nodes()}
