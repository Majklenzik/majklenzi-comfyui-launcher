import os
import subprocess
from pathlib import Path

import pytest

from launcher import config, nodes
from launcher.jobs import DONE, ERROR
from launcher.security import ValidationError

from tests.conftest import wait_job


def git(*args, cwd):
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *args], cwd=cwd, check=True,
                   capture_output=True)


@pytest.fixture
def upstream(tmp_path, monkeypatch):
    """A local git repo that stands in for https://github.com/test/my-node via git's insteadOf."""
    repo = tmp_path / "upstream" / "my-node"
    repo.mkdir(parents=True)
    git("init", "-b", "main", cwd=repo)
    (repo / "__init__.py").write_text("NODE_CLASS_MAPPINGS = {}\n")
    (repo / "requirements.txt").write_text("# nothing to install\n")
    (repo / "install.py").write_text("import pathlib; pathlib.Path('install_ran.txt').write_text('yes')\n")
    git("add", "-A", cwd=repo)
    git("commit", "-m", "first", cwd=repo)
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", f"url.file://{repo}.insteadOf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "https://github.com/test/my-node.git")
    return repo


def run(jm, fn):
    job = jm.submit("node", "t", fn)
    return wait_job(jm, job, timeout=60)


def test_install_update_remove(jm, upstream):
    job = run(jm, lambda ctx: nodes.install_node(ctx, "https://github.com/test/my-node"))
    assert job.state == DONE, job.message + "\n".join(job.log)
    dest = config.CUSTOM_NODES_DIR / "my-node"
    assert (dest / "__init__.py").exists()
    assert (dest / "install_ran.txt").read_text() == "yes"          # install.py ran in the node folder
    assert job.needs_restart
    assert not [p for p in config.CUSTOM_NODES_DIR.iterdir() if p.name.startswith(".cloning")]

    listed = {n["name"]: n for n in nodes.list_nodes()}
    assert listed["my-node"]["kind"] == "git"
    assert listed["my-node"]["branch"] == "main"
    assert listed["my-node"]["has_requirements"]

    (upstream / "new_file.py").write_text("x = 1\n")
    git("add", "-A", cwd=upstream)
    git("commit", "-m", "second", cwd=upstream)
    job = run(jm, lambda ctx: nodes.install_node(ctx, "https://github.com/test/my-node"))   # already there -> update
    assert job.state == DONE, job.message + "\n".join(job.log)
    assert (dest / "new_file.py").exists()

    nodes.remove_node("my-node")
    assert not dest.exists()
    with pytest.raises(ValidationError):
        nodes.remove_node("my-node")


def test_failed_clone_leaves_nothing(jm, upstream, monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "https://github.com/test/other.git")   # mapping no longer matches
    monkeypatch.setattr(config, "GIT_HOSTS", {"github.com"})
    job = run(jm, lambda ctx: nodes.install_node(ctx, "https://github.com/test/my-node"))
    assert job.state == ERROR
    assert list(config.CUSTOM_NODES_DIR.iterdir()) == []


def test_failing_install_script_keeps_folder_and_reports(jm, upstream):
    (upstream / "install.py").write_text("raise SystemExit(3)\n")
    git("add", "-A", cwd=upstream)
    git("commit", "-m", "break", cwd=upstream)
    job = run(jm, lambda ctx: nodes.install_node(ctx, "https://github.com/test/my-node"))
    assert job.state == ERROR and "exit code 3" in job.message
    assert (config.CUSTOM_NODES_DIR / "my-node").is_dir()
    assert any("fix the error" in line for line in job.log)


def test_branch_option(jm, upstream):
    git("checkout", "-b", "dev", cwd=upstream)
    (upstream / "dev_only.py").write_text("y = 2\n")
    git("add", "-A", cwd=upstream)
    git("commit", "-m", "dev", cwd=upstream)
    git("checkout", "main", cwd=upstream)
    job = run(jm, lambda ctx: nodes.install_node(ctx, "https://github.com/test/my-node", "dev"))
    assert job.state == DONE, job.message + "\n".join(job.log)
    assert (config.CUSTOM_NODES_DIR / "my-node" / "dev_only.py").exists()
    assert nodes.list_nodes()[0]["branch"] == "dev"


def test_existing_non_git_folder_is_not_touched(jm, upstream):
    d = config.CUSTOM_NODES_DIR / "my-node"
    d.mkdir()
    (d / "mine.txt").write_text("keep")
    job = run(jm, lambda ctx: nodes.install_node(ctx, "https://github.com/test/my-node"))
    assert job.state == ERROR and "not a git checkout" in job.message
    assert (d / "mine.txt").read_text() == "keep"


@pytest.mark.parametrize("name", ["", "..", "../x", "a/b", ".hidden", "__pycache__", "x" * 200, "a\x00b"])
def test_bad_node_names(name):
    with pytest.raises(ValidationError):
        nodes.remove_node(name)
    with pytest.raises(ValidationError):
        nodes._check_name(name)


def test_remove_symlink_only_unlinks(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("x")
    os.symlink(outside, config.CUSTOM_NODES_DIR / "linked")
    nodes.remove_node("linked")
    assert not (config.CUSTOM_NODES_DIR / "linked").exists()
    assert (outside / "keep.txt").exists()


def test_constraints_pin_torch_and_numpy_major():
    path = Path(nodes._constraints_file())
    text = path.read_text()
    assert "numpy>=" in text and "<" in text
    for line in text.splitlines():
        assert "+" not in line                 # no local CUDA suffix, so it matches any build


def test_git_runs_with_restricted_protocols_and_no_prompts():
    env = nodes._git_env()
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GIT_ALLOW_PROTOCOL"] == config.GIT_PROTOCOLS


def test_list_nodes_hides_credentials_in_remote(tmp_path):
    d = config.CUSTOM_NODES_DIR / "leaky"
    (d / ".git").mkdir(parents=True)
    (d / ".git" / "config").write_text('[remote "origin"]\n\turl = https://user:ghp_secret@github.com/a/b.git\n')
    (d / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    info = [n for n in nodes.list_nodes() if n["name"] == "leaky"][0]
    assert "ghp_secret" not in str(info) and info["url"] == "https://github.com/a/b.git"
