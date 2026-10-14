import sys
import time
from pathlib import Path

from launcher import config
from launcher.comfy import ComfyManager

from tests.conftest import free_port

FAKE = [sys.executable, str(Path(__file__).parent / "fake_comfy.py")]


def wait_state(c, wanted, timeout=15):
    end = time.time() + timeout
    st = c.status()
    while time.time() < end:
        st = c.status()
        if st["state"] in wanted:
            return st
        time.sleep(0.1)
    raise AssertionError(f"state stayed {st} (wanted {wanted})\n" + "\n".join(c.log_tail(20)))


def setup(monkeypatch, extra=()):
    monkeypatch.setattr(config, "COMFY_PORT", free_port())
    monkeypatch.setattr(config, "COMFY_CMD_OVERRIDE", FAKE + list(extra))
    return ComfyManager()


def test_start_becomes_running_then_stops(monkeypatch):
    c = setup(monkeypatch)
    assert c.status()["state"] == "stopped"
    assert c.start() and c.start()                      # second call is a no-op
    st = wait_state(c, {"running"})
    assert st["pid"] and st["uptime"] is not None
    assert any("fake comfy ready" in l for l in c.log_tail())
    c.stop()
    st = c.status()
    assert st["state"] == "stopped" and st["pid"] is None   # a user stop is not a crash


def test_starting_state_until_http_answers(monkeypatch):
    c = setup(monkeypatch, ["--delay", "1.5"])
    c.start()
    assert wait_state(c, {"starting", "running"})["state"] == "starting"
    wait_state(c, {"running"})
    c.stop()


def test_crash_is_reported(monkeypatch):
    c = setup(monkeypatch, ["--exit", "3"])
    c.start()
    st = wait_state(c, {"crashed"})
    assert st["exit_code"] == 3
    assert any("exited with code 3" in l for l in c.log_tail())


def test_restart_new_process_and_clears_flag(monkeypatch):
    c = setup(monkeypatch)
    c.start()
    first = wait_state(c, {"running"})["pid"]
    c.restart_needed = True
    assert c.status()["restart_needed"]
    assert c.restart()
    st = wait_state(c, {"running"})
    assert st["pid"] != first and not st["restart_needed"]
    c.stop()


def test_missing_install(monkeypatch):
    monkeypatch.setattr(config, "COMFY_CMD_OVERRIDE", [])
    monkeypatch.setattr(config, "COMFY_DIR", config.COMFY_DIR / "nope")
    c = ComfyManager()
    assert c.status()["state"] == "missing"
    assert c.start() is False
    assert any("not found" in l for l in c.log_tail())


def test_log_tail_bounds(monkeypatch):
    c = setup(monkeypatch)
    assert c.log_tail(0) == [] or len(c.log_tail(0)) <= 1
    assert len(c.log_tail(10**9)) <= 3000
