"""Workflow catalog: JSON files that describe a workflow plus the nodes and models it needs."""
from __future__ import annotations

import json
import math
import re
from pathlib import Path
from urllib.parse import urlsplit

import requests

from . import config, models, nodes
from .jobs import Cancelled, JobContext, JobError
from .security import (ValidationError, check_url, clean_filename, clean_subdir, parse_repo, safe_join)

ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


# ---------------------------------------------------------------------------------------------- catalog
def _normalize(raw: dict) -> dict:
    """Validate one catalog entry. Raises ValidationError with a readable reason."""
    if not isinstance(raw, dict):
        raise ValidationError("entry is not an object")
    eid = str(raw.get("id", ""))
    if not ID_RE.match(eid):
        raise ValidationError(f"bad id {eid!r} (use lowercase letters, digits and dashes)")
    title = str(raw.get("title") or eid)[:120]
    entry = {
        "id": eid,
        "title": title,
        "description": str(raw.get("description", ""))[:1200],
        "tags": [str(t)[:30] for t in (raw.get("tags") or [])][:10],
        "note": str(raw.get("note", ""))[:600],
        "workflow_file": None,
        "workflow_url": None,
        "workflow_name": None,
        "nodes": [],
        "models": [],
    }
    wf_file, wf_url = raw.get("workflow_file"), raw.get("workflow_url")
    if wf_file and wf_url:
        raise ValidationError("use either workflow_file or workflow_url, not both")
    if wf_file:
        if not isinstance(wf_file, str) or not wf_file.endswith(".json"):
            raise ValidationError("workflow_file must be a .json path")
        entry["workflow_file"] = wf_file
    if wf_url:
        entry["workflow_url"] = str(wf_url)
        if urlsplit(entry["workflow_url"]).scheme != "https" and not config.ALLOW_PRIVATE_URLS:
            raise ValidationError("workflow_url must be https")
    if wf_file or wf_url:
        name = raw.get("workflow_name") or f"{eid}.json"
        name = clean_filename(str(name))
        if not name.lower().endswith(".json"):
            name += ".json"
        entry["workflow_name"] = name

    for n in raw.get("nodes") or []:
        item = {"url": n} if isinstance(n, str) else dict(n or {})
        repo = parse_repo(str(item.get("url", "")), item.get("branch"))
        entry["nodes"].append({"url": f"https://github.com/{repo.owner}/{repo.name}", "name": repo.name,
                               "branch": repo.branch, "note": str(item.get("note", ""))[:200]})

    for m in raw.get("models") or []:
        if not isinstance(m, dict):
            raise ValidationError("model entry is not an object")
        url = models.normalize_url(str(m.get("url", "")))
        if urlsplit(url).scheme != "https" and not config.ALLOW_PRIVATE_URLS:
            raise ValidationError("model url must be https")
        sub = clean_subdir(str(m.get("dir", "")))
        fname = m.get("filename")
        fname = clean_filename(str(fname)) if fname else models._name_from_url(urlsplit(url))
        sha = m.get("sha256")
        if sha and not re.fullmatch(r"[0-9a-fA-F]{64}", str(sha)):
            raise ValidationError("sha256 must be 64 hex characters")
        size = m.get("size_gb")
        if isinstance(size, bool) or (isinstance(size, (int, float)) and not (math.isfinite(size) and 0 <= size < 10000)):
            raise ValidationError("size_gb must be a number between 0 and 10000")
        entry["models"].append({
            "url": url, "dir": sub, "filename": fname, "sha256": sha or None,
            "size_gb": float(size) if isinstance(size, (int, float)) else None,
            "label": str(m.get("label") or fname or url.rsplit("/", 1)[-1])[:120],
            "gated": bool(m.get("gated", False)),
        })
    if not (entry["workflow_name"] or entry["nodes"] or entry["models"]):
        raise ValidationError("entry installs nothing")
    return entry


def load_catalog() -> tuple[list[dict], list[str]]:
    """Returns (entries, problems). Later folders override earlier ones with the same id."""
    entries: dict[str, dict] = {}
    problems: list[str] = []
    for base in config.CATALOG_DIRS:
        if not base.is_dir():
            continue
        for f in sorted(base.glob("*.json")):
            if f.name.startswith("_"):
                continue
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as e:
                problems.append(f"{f.name}: cannot read ({e})")
                continue
            for raw in data if isinstance(data, list) else [data]:
                try:
                    e = _normalize(raw)
                    e["_base"] = str(base)
                    entries[e["id"]] = e
                except (ValidationError, ValueError, TypeError, OverflowError) as err:
                    problems.append(f"{f.name}: {err}")
    return sorted(entries.values(), key=lambda e: e["title"].lower()), problems


def find_entry(entry_id: str) -> dict | None:
    return next((e for e in load_catalog()[0] if e["id"] == entry_id), None)


def public_view(entry: dict) -> dict:
    """Entry plus what is already installed. No server paths leave the process."""
    installed_nodes = nodes.installed_repo_names()
    wf_dir = config.WORKFLOWS_DIR
    out = {k: v for k, v in entry.items() if not k.startswith("_") and k not in ("workflow_file", "workflow_url")}
    out["has_workflow"] = bool(entry["workflow_name"])
    out["workflow_installed"] = bool(entry["workflow_name"]) and (wf_dir / entry["workflow_name"]).is_file()
    out["nodes"] = [dict(n, installed=n["name"].lower() in installed_nodes) for n in entry["nodes"]]
    ms = []
    for m in entry["models"]:
        have = bool(m["filename"]) and (config.MODELS_DIR / m["dir"] / m["filename"]).is_file()
        ms.append({k: v for k, v in m.items() if k not in ("url", "sha256")} | {"installed": have})
    out["models"] = ms
    out["complete"] = (
        (out["workflow_installed"] or not out["has_workflow"])
        and all(n["installed"] for n in out["nodes"]) and all(m["installed"] for m in ms)
    )
    return out


# ---------------------------------------------------------------------------------------------- install
def _write_workflow(name: str, text: str, overwrite: bool) -> str:
    """Validate and store a workflow JSON in ComfyUI's workflow folder."""
    if len(text.encode("utf-8")) > config.MAX_WORKFLOW_BYTES:
        raise ValidationError("Workflow file is too large")
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        raise ValidationError("That is not valid JSON") from None
    ok = isinstance(data, dict) and (isinstance(data.get("nodes"), list) or
                                     any(isinstance(v, dict) and "class_type" in v for v in data.values()))
    if not ok:
        raise ValidationError("That JSON does not look like a ComfyUI workflow")
    name = clean_filename(name)
    if not name.lower().endswith(".json"):
        name += ".json"
    config.WORKFLOWS_DIR.mkdir(parents=True, exist_ok=True)
    target = safe_join(config.WORKFLOWS_DIR, name)
    if target.exists() and not overwrite:
        raise ValidationError(f"A workflow called {name} already exists")
    tmp = target.with_name(f".{name}.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(target)
    return name


def import_workflow(name: str, workflow: object, overwrite: bool = False) -> str:
    return _write_workflow(name, json.dumps(workflow, ensure_ascii=False), overwrite)


def _fetch_text(ctx: JobContext, url: str) -> str:
    check_url(url)
    session = models.new_session()
    try:
        resp, final = models._open(ctx, session, url, 0)
        try:
            if resp.status_code != 200:
                raise models._http_error(resp, urlsplit(final))
            buf = bytearray()
            for chunk in resp.iter_content(64 * 1024):
                ctx.check()
                buf += chunk
                if len(buf) > config.MAX_WORKFLOW_BYTES:
                    raise JobError("Workflow file is too large")
            return bytes(buf).decode("utf-8")
        finally:
            resp.close()
    except UnicodeDecodeError:
        raise JobError("Workflow file is not UTF-8 text") from None
    finally:
        session.close()


def install_entry(ctx: JobContext, entry: dict, skip_nodes: set[str] | None = None,
                  skip_models: set[int] | None = None) -> None:
    """Workflow file first (instant), then nodes, then the big model downloads."""
    skip_nodes, skip_models = skip_nodes or set(), skip_models or set()
    failures: list[str] = []

    if entry["workflow_name"]:
        ctx.say(f"Adding workflow {entry['workflow_name']}")
        try:
            if entry["workflow_file"]:
                src = safe_join(Path(entry["_base"]), entry["workflow_file"])
                text = src.read_text(encoding="utf-8")
            else:
                text = _fetch_text(ctx, entry["workflow_url"])
            _write_workflow(entry["workflow_name"], text, overwrite=True)
        except (ValidationError, OSError, JobError, requests.RequestException) as e:
            failures.append(f"workflow: {e}")
            ctx.log(f"workflow failed: {e}")

    for n in entry["nodes"]:
        if n["name"] in skip_nodes:
            continue
        ctx.check()
        try:
            nodes.install_node(ctx, n["url"], n["branch"])
        except (Cancelled, KeyboardInterrupt):
            raise
        except (JobError, ValidationError) as e:
            failures.append(f"node {n['name']}: {e}")
            ctx.log(f"node {n['name']} failed: {e}")

    for i, m in enumerate(entry["models"]):
        if i in skip_models:
            continue
        ctx.check()
        try:
            ctx.progress(0, None)
            models.download_model(ctx, m["url"], m["dir"], m["filename"], sha256=m["sha256"])
        except (Cancelled, KeyboardInterrupt):
            raise
        except (JobError, ValidationError) as e:
            failures.append(f"model {m['label']}: {e}")
            ctx.log(f"model {m['label']} failed: {e}")

    if failures:
        raise JobError("Some parts failed: " + " | ".join(failures)[:600])
    ctx.say("All done. Restart ComfyUI if new custom nodes were installed." if entry["nodes"] else "All done.")


def list_installed_workflows() -> list[dict]:
    root = config.WORKFLOWS_DIR
    if not root.is_dir():
        return []
    out = []
    for p in sorted(root.rglob("*.json")):
        if p.name.startswith(".") or len(p.relative_to(root).parts) > 3:
            continue
        try:
            out.append({"name": str(p.relative_to(root)).replace("\\", "/"), "size": p.stat().st_size})
        except OSError:
            continue
    return out
