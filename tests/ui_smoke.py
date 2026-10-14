"""Browser walk-through of the launcher UI against a fake ComfyUI (not collected by pytest).

    python3 tests/ui_smoke.py [screenshot_dir]
Needs: playwright + chromium. Everything stays on localhost.
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tests.conftest import FileServer, free_port  # noqa: E402

from playwright.sync_api import sync_playwright  # noqa: E402

shots = Path(sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp(prefix="ui-shots-"))
shots.mkdir(parents=True, exist_ok=True)
work = Path(tempfile.mkdtemp(prefix="ui-smoke-"))
ws = work / "workspace"
(ws / "ComfyUI" / "models").mkdir(parents=True)
(ws / ".launcher" / "catalog").mkdir(parents=True)

files = FileServer()
files.files["demo-lora.safetensors"] = os.urandom(3_000_000)
files.files["big-model.safetensors"] = os.urandom(6_000_000)
files.slow.add("big-model.safetensors")
files.files["demo-wf.json"] = json.dumps({"nodes": [{"id": 1, "type": "KSampler"}], "links": []}).encode()

(ws / ".launcher" / "catalog" / "demo.json").write_text(json.dumps([{
    "id": "demo-image", "title": "Demo image workflow", "description": "Text to image with one LoRA.",
    "tags": ["image", "demo"], "note": "Needs about 12 GB VRAM.",
    "workflow_url": files.url("/files/demo-wf.json"), "workflow_name": "demo-image.json",
    "nodes": ["https://github.com/test/my-node"],
    "models": [
        {"label": "Demo LoRA", "url": files.url("/files/demo-lora.safetensors"), "dir": "loras", "size_gb": 0.003},
        {"label": "Gated base model", "url": files.url("/files/big-model.safetensors"), "dir": "diffusion_models",
         "size_gb": 0.006, "gated": True},
    ]}]))

# a local git repo that stands in for github.com/test/my-node
repo = work / "upstream" / "my-node"
repo.mkdir(parents=True)
def git(*a):
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *a], cwd=repo, check=True, capture_output=True)
git("init", "-b", "main")
(repo / "__init__.py").write_text("NODE_CLASS_MAPPINGS = {}\n")
git("add", "-A"); git("commit", "-m", "init")

port, comfy_port = free_port(), free_port()
env = dict(os.environ, WORKSPACE=str(ws), LAUNCHER_PORT=str(port), LAUNCHER_HOST="127.0.0.1", COMFY_PORT=str(comfy_port),
           COMFY_CMD=f"{sys.executable} {ROOT}/tests/fake_comfy.py", LAUNCHER_ALLOW_PRIVATE_URLS="1",
           LAUNCHER_GIT_PROTOCOLS="https:file", GIT_CONFIG_COUNT="1", NO_PROXY="127.0.0.1,localhost", no_proxy="127.0.0.1,localhost",
           GIT_CONFIG_KEY_0=f"url.file://{repo}.insteadOf", GIT_CONFIG_VALUE_0="https://github.com/test/my-node.git")
env["LAUNCHER_PASSWORD"] = "smoke-test-password"
env.pop("LAUNCHER_ALLOW_NO_AUTH", None)
proc = subprocess.Popen([sys.executable, "-m", "launcher"], cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

problems = []
def check(cond, msg):
    print(("ok   " if cond else "FAIL ") + msg)
    if not cond:
        problems.append(msg)

try:
    base = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            urllib.request.urlopen(base + "/healthz", timeout=1); break
        except Exception:
            time.sleep(0.1)
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox"])
        ctx = browser.new_context(viewport={"width": 1200, "height": 900},
                                  http_credentials={"username": "anyone", "password": "smoke-test-password"})
        anon = browser.new_context().new_page()
        resp = anon.goto(base + "/")
        check(resp.status == 401, "page without the password is refused (401)")
        anon.context.close()
        page = ctx.new_page()
        console = []
        page.on("console", lambda m: console.append((m.type, m.text)) if m.type in ("error", "warning") else None)
        page.on("pageerror", lambda e: console.append(("pageerror", str(e))))

        page.goto(base + "/")
        page.wait_for_selector("#status.pill-on", timeout=20000)
        check(page.inner_text("#status") == "ComfyUI running", "header shows ComfyUI running")
        check(page.get_attribute("#open-comfy", "href") == f"http://127.0.0.1:{comfy_port}/", "Open ComfyUI link points at the ComfyUI port")
        check("switched off" not in page.inner_text("#banners"), "no open-access warning when the password is on")

        # --- workflows
        page.wait_for_selector("text=Demo image workflow")
        page.click("summary:has-text('What gets installed')")
        check(page.is_visible("text=Gated base model"), "model list visible in card")
        check("needs Hugging Face token" in page.inner_text(".card"), "gated hint shown without token")
        page.screenshot(path=str(shots / "1-workflows.png"), full_page=True)

        # --- models page: token, download, list
        page.click(".tab[data-page=models]")
        page.wait_for_selector("#dl-url")
        page.fill("#tok-hf_token", "hf_testtoken_123456")
        page.click("#token-box .btn-primary >> nth=0")
        page.wait_for_selector("text=Saved on this pod")
        check(page.input_value("#tok-hf_token") == "", "token field cleared after saving")
        check("hf_testtoken_123456" not in page.content(), "token is not echoed into the page")
        page.fill("#dl-url", files.url("/files/demo-lora.safetensors"))
        page.select_option("#dl-dir", "loras")
        page.click("text=Download >> nth=-1") if False else page.click("button:has-text('Download')")
        page.wait_for_selector(".job .state-done", timeout=20000)
        page.wait_for_selector("td:has-text('demo-lora.safetensors')")
        check(True, "download finished and appears in the list")
        page.screenshot(path=str(shots / "2-models.png"), full_page=True)

        # --- slow download: progress bar visible, then cancel
        page.fill("#dl-url", files.url("/files/big-model.safetensors"))
        page.select_option("#dl-dir", "diffusion_models")
        page.click("button:has-text('Download')")
        page.wait_for_selector(".job progress:visible", timeout=10000)
        time.sleep(1.2)
        page.screenshot(path=str(shots / "3-progress.png"), full_page=True)
        job = page.locator(".job", has_text="big-model").first
        job.locator("button:has-text('Cancel')").click()
        page.wait_for_selector(".job:has-text('big-model') .state-cancelled", timeout=10000)
        check(True, "running download can be cancelled")
        files.release.set()

        # --- nodes
        page.click(".tab[data-page=nodes]")
        page.fill("#node-url", "https://github.com/test/my-node")
        page.click("button:has-text('Install')")
        page.wait_for_selector(".job:has-text('my-node') .state-done", timeout=60000)
        page.wait_for_selector("td:has-text('my-node')", timeout=10000)
        page.wait_for_selector(".banner:has-text('Restart ComfyUI')", timeout=10000)
        check(True, "node installed, listed, and restart banner shown")
        page.screenshot(path=str(shots / "4-nodes.png"), full_page=True)
        page.click(".banner button:has-text('Restart now')")
        page.wait_for_selector(".banner:has-text('Custom nodes changed')", state="detached", timeout=30000)
        check(True, "restart banner disappears after restart")

        # --- workflows: install catalog entry (nodes already installed, models skip), import file
        page.click(".tab[data-page=workflows]")
        page.wait_for_selector("text=Demo image workflow")
        wf = work / "mine.json"
        wf.write_text(json.dumps({"nodes": [], "links": []}))
        page.set_input_files("#wf-file", str(wf))
        check(page.input_value("#wf-name") == "mine.json", "file name prefilled")
        page.click("button:has-text('Add workflow')")
        page.wait_for_selector("td:has-text('mine.json')", timeout=10000)
        check(True, "imported workflow listed")
        page.locator("article.card", has_text="Demo image workflow").locator("button:has-text('Install')").click()
        page.wait_for_selector(".job:has-text('Demo image workflow') .state-done", timeout=40000)
        page.wait_for_selector("article.card .pill-on:has-text('Installed')", timeout=10000)
        check(True, "catalog workflow installed completely")
        page.screenshot(path=str(shots / "5-workflows-done.png"), full_page=True)

        # --- settings
        page.click(".tab[data-page=settings]")
        page.wait_for_selector("text=ComfyUI port")
        page.click("button:has-text('Show ComfyUI log')")
        page.wait_for_selector(".logbox:visible")
        page.wait_for_function("document.querySelector('.logbox').textContent.includes('fake comfy ready')", timeout=10000)
        check(True, "ComfyUI log viewer works")
        page.screenshot(path=str(shots / "6-settings.png"), full_page=True)

        # --- phone width
        page.set_viewport_size({"width": 390, "height": 800})
        page.click(".tab[data-page=models]")
        page.wait_for_selector("#dl-url")
        no_hscroll = page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1")
        check(no_hscroll, "no horizontal scroll at phone width")
        page.screenshot(path=str(shots / "7-phone.png"), full_page=True)

        errs = [c for c in console if c[0] in ("error", "pageerror")]
        csp = [c for c in console if "Content Security Policy" in c[1]]
        check(not errs, f"no console errors {errs[:3]}")
        check(not csp, "no CSP violations")
        browser.close()
finally:
    proc.terminate()
    try:
        proc.wait(10)
    except subprocess.TimeoutExpired:
        proc.kill()
    files.close()
    out = proc.stdout.read() if proc.stdout else ""
    if problems:
        print("\n--- launcher output ---\n" + out[-3000:])

print("\nscreenshots:", shots)
sys.exit(1 if problems else 0)
