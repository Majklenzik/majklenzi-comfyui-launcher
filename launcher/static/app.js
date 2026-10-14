"use strict";

/* ====================================================================================== helpers */
const $ = (sel, root = document) => root.querySelector(sel);

/** Build DOM safely: text always goes through textContent / text nodes, never innerHTML. */
function h(tag, props, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (k === "spellcheck") { el.spellcheck = Boolean(v); continue; }
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
    else if (k === "for") el.htmlFor = v;
    else if (k in el && !k.startsWith("aria-")) el[k] = v;
    else el.setAttribute(k, v === true ? "" : String(v));
  }
  for (const kid of kids.flat(Infinity)) {
    if (kid === null || kid === undefined || kid === false) continue;
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return el;
}

async function api(path, method = "GET", body) {
  const opt = { method, headers: {}, cache: "no-store" };
  if (method !== "GET") {
    opt.headers["Content-Type"] = "application/json";
    opt.body = JSON.stringify(body === undefined ? {} : body);
  }
  let res;
  try {
    res = await fetch(path, opt);
  } catch (e) {
    throw new Error("Cannot reach the launcher. Is the pod still running?");
  }
  let data = null;
  try { data = await res.json(); } catch (e) { /* not JSON */ }
  if (!res.ok) throw new Error((data && data.detail) || `Request failed (${res.status})`);
  return data;
}

function fmtBytes(n) {
  if (n === null || n === undefined) return "?";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0, v = n;
  while (v >= 1000 && i < units.length - 1) { v /= 1000; i++; }
  return `${v >= 100 || i === 0 ? v.toFixed(0) : v.toFixed(1)} ${units[i]}`;
}

function fmtDuration(sec) {
  if (!isFinite(sec) || sec < 0) return "";
  sec = Math.round(sec);
  if (sec < 60) return `${sec}s`;
  const m = Math.floor(sec / 60);
  if (m < 60) return `${m}m ${sec % 60}s`;
  return `${Math.floor(m / 60)}h ${m % 60}m`;
}

let toastTimer = null;
function toast(message, good = false) {
  const box = $("#toast");
  box.textContent = message;
  box.className = good ? "toast good" : "toast";
  box.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { box.hidden = true; }, good ? 5000 : 12000);
}

/** Two-step button: first click arms it, second click within 4 s runs the action. */
function armed(label, armedLabel, action, cls = "btn btn-small btn-danger") {
  const btn = h("button", { type: "button", class: cls }, label);
  let timer = null;
  btn.addEventListener("click", async () => {
    if (!btn.classList.contains("armed")) {
      btn.classList.add("armed");
      btn.textContent = armedLabel;
      timer = setTimeout(() => { btn.classList.remove("armed"); btn.textContent = label; }, 4000);
      return;
    }
    clearTimeout(timer);
    btn.disabled = true;
    try { await action(); } catch (e) { toast(e.message); }
    btn.disabled = false;
    btn.classList.remove("armed");
    btn.textContent = label;
  });
  return btn;
}

async function guarded(btn, fn) {
  btn.disabled = true;
  try { await fn(); } catch (e) { toast(e.message); } finally { btn.disabled = false; }
}

/** RunPod publishes every port as https://<podid>-<port>.proxy.runpod.net/. */
function podLink(port, override) {
  if (override) return override;
  const m = location.hostname.match(/^(.+)-(\d+)\.proxy\.runpod\.net$/);
  if (m) return `${location.protocol}//${m[1]}-${port}.proxy.runpod.net/`;
  return `${location.protocol}//${location.hostname}:${port}/`;
}

const isGithub = (url) => typeof url === "string" && /^https:\/\/github\.com\/[\w.-]+\/[\w.-]+/.test(url);

function store(key, value) {
  try {
    if (value === undefined) return localStorage.getItem(key);
    localStorage.setItem(key, value);
  } catch (e) { /* storage may be blocked */ }
  return null;
}

/* ========================================================================================= state */
const S = {
  state: null,
  jobs: [],
  page: "workflows",
  renderId: 0,
  jobNodes: new Map(),   // job id -> {root, refs}
  openLogs: new Set(),
  speed: new Map(),      // job id -> {t, b, rate}
  seenStates: new Map(),
  bannerSig: "",
};

const PAGES = { workflows: pageWorkflows, models: pageModels, nodes: pageNodes, settings: pageSettings };

/* ====================================================================================== header */
function renderHeader() {
  const st = S.state;
  if (!st) return;
  document.title = st.title;
  $("#title").textContent = st.title;

  const pill = $("#status");
  const map = {
    running: ["ComfyUI running", "pill pill-on"],
    starting: ["ComfyUI starting…", "pill pill-wait"],
    stopped: ["ComfyUI stopped", "pill pill-off"],
    crashed: ["ComfyUI crashed", "pill pill-bad"],
    missing: ["ComfyUI not installed", "pill pill-bad"],
  };
  const [text, cls] = map[st.comfy.state] || ["Unknown", "pill pill-off"];
  pill.textContent = text;
  pill.className = cls;

  const comfy = $("#open-comfy");
  comfy.href = podLink(st.ports.comfy, st.links.comfy);
  comfy.setAttribute("aria-disabled", st.comfy.state === "running" ? "false" : "true");
  $("#open-jupyter").href = podLink(st.ports.jupyter, st.links.jupyter);

  const bits = [];
  if (st.gpu) bits.push(`GPU: ${st.gpu.name} (${(st.gpu.memory_used_mb / 1024).toFixed(1)} / ${(st.gpu.memory_total_mb / 1024).toFixed(0)} GB VRAM used)`);
  if (st.disk && st.disk.total) bits.push(`Disk: ${fmtBytes(st.disk.free)} free of ${fmtBytes(st.disk.total)}`);
  $("#sysline").textContent = bits.join("   ·   ");

  renderBanners();
}

function renderBanners() {
  const st = S.state;
  const box = $("#banners");
  const sig = JSON.stringify([st.comfy.restart_needed, st.password_protected, store("launcher.hidePwBanner"),
    st.disk && st.disk.total ? st.disk.free < 5e9 : false]);
  if (sig === S.bannerSig) return;
  S.bannerSig = sig;
  const items = [];
  if (st.comfy.restart_needed) {
    const btn = h("button", { type: "button", class: "btn btn-small btn-primary" }, "Restart now");
    btn.addEventListener("click", () => guarded(btn, restartComfy));
    items.push(h("div", { class: "banner banner-info" },
      h("span", {}, "Custom nodes changed. Restart ComfyUI so it loads them."), btn));
  }
  if (!st.password_protected && store("launcher.hidePwBanner") !== "1") {
    const btn = h("button", { type: "button", class: "btn btn-small" }, "Hide");
    btn.addEventListener("click", () => { store("launcher.hidePwBanner", "1"); renderBanners(); });
    items.push(h("div", { class: "banner banner-warn" },
      h("span", {}, "Password protection is switched off (LAUNCHER_ALLOW_NO_AUTH=1). Anyone who has this link can " +
        "install code on this pod and download files. Remove that variable to turn the password back on."), btn));
  }
  if (st.disk && st.disk.total && st.disk.free < 5e9) {
    items.push(h("div", { class: "banner banner-warn" },
      h("span", {}, `Only ${fmtBytes(st.disk.free)} of disk left. Downloads may fail. Delete unused models or enlarge the volume.`)));
  }
  box.replaceChildren(...items);
}

async function restartComfy() {
  await api("/api/comfy/restart", "POST");
  toast("Restarting ComfyUI. It takes about a minute.", true);
  await refreshState();
}

/* ======================================================================================== tokens */
function tokenRow(field, label, helpText, placeholder) {
  const key = field === "hf_token" ? "hf" : "civitai";
  const source = S.state ? S.state.tokens[key] : "none";
  const input = h("input", {
    type: "password", id: `tok-${field}`, autocomplete: "off", spellcheck: false, placeholder,
    "aria-describedby": `tok-${field}-help`,
  });
  const save = h("button", { type: "button", class: "btn btn-primary" }, "Save token");
  save.addEventListener("click", () => guarded(save, async () => {
    const value = input.value.trim();
    if (!value) { toast("Paste a token first."); return; }
    await api("/api/settings", "POST", { [field]: value });
    input.value = "";
    toast("Token saved.", true);
    await refreshState();
    rerenderTokens();
  }));
  const statusText = { ui: "Saved on this pod", env: "Set through the pod environment", none: "Not set" }[source];
  const status = h("span", { class: source === "none" ? "hint" : "pill pill-on" }, statusText);
  const clear = source === "ui"
    ? armed("Remove", "Really remove?", async () => {
      await api("/api/settings", "POST", { [field]: "" });
      toast("Token removed.", true);
      await refreshState();
      rerenderTokens();
    }) : null;
  return h("div", { class: "token-row" },
    h("label", { for: `tok-${field}` }, label),
    h("div", { class: "row row-2" }, input, h("div", { class: "row-actions" }, save, clear)),
    h("div", { class: "status-line" }, status, h("span", { id: `tok-${field}-help`, class: "hint" }, helpText)));
}

function tokenBox() {
  return h("section", { class: "panel", id: "token-box" },
    h("h2", {}, "Access tokens"),
    h("p", { class: "hint" }, "Tokens are stored only on this pod's volume and are never shown again. " +
      "Each one is sent only to its own website."),
    tokenRow("hf_token", "Hugging Face token",
      "Needed for gated or private models and LoRAs. Create a read token at huggingface.co/settings/tokens.", "hf_…"),
    tokenRow("civitai_token", "CivitAI API key",
      "Needed for CivitAI downloads that require a login.", "API key"));
}

function rerenderTokens() {
  const old = $("#token-box");
  if (old) old.replaceWith(tokenBox());
}

/* ===================================================================================== workflows */
async function pageWorkflows(root, rid) {
  const data = await api("/api/workflows");
  if (rid !== S.renderId) return;
  const out = [];

  out.push(h("p", { class: "hint" },
    "Pick a workflow. The launcher adds the workflow file, installs its custom nodes and downloads its models. " +
    "Afterwards open ComfyUI and load it from the Workflows panel."));

  if (data.problems.length) {
    out.push(h("div", { class: "banner banner-warn" },
      h("span", {}, "Some catalog files were skipped: " + data.problems.join(" | "))));
  }

  if (!data.catalog.length) {
    out.push(h("div", { class: "panel empty" },
      "The catalog is empty. Add *.json files to the catalog folder (see the README), or import your own workflow below."));
  } else {
    out.push(h("div", { class: "grid" }, data.catalog.map(workflowCard)));
  }
  out.push(importPanel(), installedWorkflows(data.installed));
  root.replaceChildren(...out);
}

function workflowCard(entry) {
  const hfSet = S.state && S.state.tokens.hf !== "none";
  const checks = [];   // {kind, key, box}

  const nodeRows = entry.nodes.map((n) => {
    const box = h("input", { type: "checkbox", checked: !n.installed, disabled: n.installed, id: `n-${entry.id}-${n.name}` });
    checks.push({ kind: "node", key: n.name, box, installed: n.installed });
    return h("div", { class: "part" }, box,
      h("label", { for: box.id }, h("span", {}, n.name, " "),
        h("span", { class: n.installed ? "ok" : "meta" }, n.installed ? "✓ installed" : "custom node")));
  });
  const modelRows = entry.models.map((m, i) => {
    const box = h("input", { type: "checkbox", checked: !m.installed, disabled: m.installed, id: `m-${entry.id}-${i}` });
    checks.push({ kind: "model", key: i, box, installed: m.installed, size: m.size_gb });
    return h("div", { class: "part" }, box,
      h("label", { for: box.id }, h("span", {}, m.label, " "),
        h("span", { class: m.installed ? "ok" : "meta" },
          m.installed ? "✓ installed" : `${m.dir}${m.size_gb ? ` · ${m.size_gb} GB` : ""}${m.gated && !hfSet ? " · needs Hugging Face token" : ""}`)));
  });

  const summary = h("span", { class: "hint" });
  const button = h("button", { type: "button", class: "btn btn-primary" }, "Install");
  const update = () => {
    const todo = checks.filter((c) => c.box.checked && !c.installed);
    const gb = todo.filter((c) => c.kind === "model").reduce((a, c) => a + (c.size || 0), 0);
    const needsWorkflow = entry.has_workflow && !entry.workflow_installed;
    button.disabled = !todo.length && !needsWorkflow;
    button.textContent = entry.complete ? "Installed" : "Install";
    const nNodes = todo.filter((c) => c.kind === "node").length;
    const nModels = todo.filter((c) => c.kind === "model").length;
    const plural = (n, w) => `${n} ${w}${n === 1 ? "" : "s"}`;
    const size = gb >= 1 ? ` · about ${gb.toFixed(1)} GB` : gb > 0 ? ` · about ${Math.max(1, Math.round(gb * 1000))} MB` : "";
    summary.textContent = todo.length || needsWorkflow
      ? `${plural(nNodes, "node")}, ${plural(nModels, "model")}${size}`
      : "Everything is installed";
  };
  checks.forEach((c) => c.box.addEventListener("change", update));
  update();

  button.addEventListener("click", () => guarded(button, async () => {
    const skip_nodes = checks.filter((c) => c.kind === "node" && (!c.box.checked || c.installed)).map((c) => c.key);
    const skip_models = checks.filter((c) => c.kind === "model" && (!c.box.checked || c.installed)).map((c) => c.key);
    await api("/api/workflows/install", "POST", { id: entry.id, skip_nodes, skip_models });
    toast(`Installing ${entry.title}. Progress is shown below.`, true);
    await refreshJobs();
  }));

  const badge = entry.complete
    ? h("span", { class: "pill pill-on" }, "Installed")
    : (entry.workflow_installed || entry.nodes.some((n) => n.installed) || entry.models.some((m) => m.installed))
      ? h("span", { class: "pill pill-wait" }, "Partly installed") : null;

  return h("article", { class: "card" },
    h("h3", {}, h("span", {}, entry.title), badge),
    h("p", { class: "desc grow" }, entry.description),
    entry.tags.length ? h("div", { class: "tags" }, entry.tags.map((t) => h("span", { class: "tag" }, t))) : null,
    entry.note ? h("p", { class: "hint" }, entry.note) : null,
    (nodeRows.length || modelRows.length || entry.has_workflow) ? h("details", { class: "parts" },
      h("summary", {}, "What gets installed"),
      entry.has_workflow ? h("div", { class: "part" }, h("span", {}, "Workflow file ",
        h("span", { class: entry.workflow_installed ? "ok" : "meta" }, entry.workflow_installed ? "✓ added" : "will be added"))) : null,
      nodeRows, modelRows) : null,
    h("div", { class: "row-actions" }, button, summary));
}

function importPanel() {
  const file = h("input", { type: "file", accept: ".json,application/json", id: "wf-file" });
  const name = h("input", { type: "text", id: "wf-name", placeholder: "my-workflow.json", autocomplete: "off" });
  const button = h("button", { type: "button", class: "btn" }, "Add workflow");
  file.addEventListener("change", () => { if (file.files[0] && !name.value) name.value = file.files[0].name; });
  button.addEventListener("click", () => guarded(button, async () => {
    const f = file.files[0];
    if (!f) { toast("Choose a workflow .json file first."); return; }
    if (f.size > 8 * 1024 * 1024) { toast("That file is too large (limit 8 MB)."); return; }
    let workflow;
    try { workflow = JSON.parse(await f.text()); } catch (e) { toast("That file is not valid JSON."); return; }
    const res = await api("/api/workflows/import", "POST", { name: name.value.trim() || f.name, workflow });
    toast(`Added ${res.name}.`, true);
    file.value = ""; name.value = "";
    if (S.page === "workflows") renderPage();
  }));
  return h("section", { class: "panel" },
    h("h2", {}, "Add your own workflow"),
    h("p", { class: "hint" }, "Upload a workflow .json exported from ComfyUI. It then shows up in ComfyUI's Workflows panel."),
    h("div", { class: "row row-2" },
      h("div", {}, h("label", { for: "wf-file" }, "Workflow file"), file),
      h("div", {}, h("label", { for: "wf-name" }, "Save as"), name)),
    h("div", { class: "row-actions" }, button));
}

function installedWorkflows(list) {
  if (!list.length) return h("div");
  return h("section", { class: "panel" },
    h("h2", {}, "Workflows on this pod"),
    h("table", { class: "table" },
      h("thead", {}, h("tr", {}, h("th", {}, "Name"), h("th", { class: "num" }, "Size"))),
      h("tbody", {}, list.map((w) => h("tr", {}, h("td", {}, w.name), h("td", { class: "num" }, fmtBytes(w.size)))))));
}

/* ========================================================================================= models */
async function pageModels(root, rid) {
  const data = await api("/api/models");
  if (rid !== S.renderId) return;

  const url = h("input", { type: "text", id: "dl-url", placeholder: "https://huggingface.co/…/model.safetensors", autocomplete: "off", spellcheck: false });
  const dir = h("select", { id: "dl-dir" }, data.dirs.map((d) => h("option", { value: d }, d)));
  dir.value = store("launcher.lastDir") && data.dirs.includes(store("launcher.lastDir")) ? store("launcher.lastDir") : "loras";
  const fname = h("input", { type: "text", id: "dl-name", placeholder: "optional", autocomplete: "off" });
  const button = h("button", { type: "button", class: "btn btn-primary" }, "Download");
  button.addEventListener("click", () => guarded(button, async () => {
    if (!url.value.trim()) { toast("Paste a link first."); return; }
    store("launcher.lastDir", dir.value);
    await api("/api/models/download", "POST", { url: url.value.trim(), dir: dir.value, filename: fname.value.trim() || null });
    toast("Download started. Progress is shown below.", true);
    url.value = ""; fname.value = "";
    await refreshJobs();
  }));
  url.addEventListener("keydown", (ev) => { if (ev.key === "Enter") button.click(); });

  const form = h("section", { class: "panel" },
    h("h2", {}, "Add a model by link"),
    h("p", { class: "hint" }, "Works with Hugging Face and CivitAI links, and any direct https download link. " +
      "Checkpoints, LoRAs, VAEs, text encoders: pick the matching folder."),
    h("div", { class: "row" }, h("div", {}, h("label", { for: "dl-url" }, "Download link"), url)),
    h("div", { class: "row row-2" },
      h("div", {}, h("label", { for: "dl-dir" }, "Folder (ComfyUI/models/…)"), dir),
      h("div", {}, h("label", { for: "dl-name" }, "File name"), fname)),
    h("div", { class: "row-actions" }, button,
      h("span", { class: "hint" }, "New models appear in ComfyUI after you press R (refresh) there. No restart needed.")));

  root.replaceChildren(form, tokenBox(), installedModels(data.models));
}

function installedModels(list) {
  const wrap = h("section", { class: "panel" }, h("h2", {}, "Models on this pod"));
  if (!list.length) { wrap.append(h("div", { class: "empty" }, "No models yet.")); return wrap; }
  const groups = new Map();
  for (const m of list) {
    const k = m.dir || "(root)";
    if (!groups.has(k)) groups.set(k, []);
    groups.get(k).push(m);
  }
  for (const [dir, items] of [...groups].sort((a, b) => a[0].localeCompare(b[0]))) {
    wrap.append(h("div", { class: "group-title" }, dir),
      h("table", { class: "table" }, h("tbody", {}, items.map((m) => h("tr", {},
        h("td", {}, m.name, m.partial ? h("span", { class: "tag" }, " incomplete download") : null),
        h("td", { class: "num" }, fmtBytes(m.size)),
        h("td", { class: "act" }, m.deletable ? armed(m.partial ? "Discard" : "Delete", "Really delete?", async () => {
          await api("/api/models/delete", "POST", { dir: m.dir, filename: m.name });
          toast(`Deleted ${m.name}.`, true);
          if (S.page === "models") renderPage();
        }) : h("span", { class: "hint", title: "Delete this file from Jupyter" }, "—")))))));
  }
  return wrap;
}

/* ========================================================================================== nodes */
async function pageNodes(root, rid) {
  const data = await api("/api/nodes");
  if (rid !== S.renderId) return;

  const url = h("input", { type: "text", id: "node-url", placeholder: "https://github.com/owner/repository", autocomplete: "off", spellcheck: false });
  const branch = h("input", { type: "text", id: "node-branch", placeholder: "default branch", autocomplete: "off" });
  const button = h("button", { type: "button", class: "btn btn-primary" }, "Install");
  button.addEventListener("click", () => guarded(button, async () => {
    if (!url.value.trim()) { toast("Paste a GitHub link first."); return; }
    await api("/api/nodes/install", "POST", { url: url.value.trim(), branch: branch.value.trim() || null });
    toast("Installing. Progress is shown below.", true);
    url.value = ""; branch.value = "";
    await refreshJobs();
  }));
  url.addEventListener("keydown", (ev) => { if (ev.key === "Enter") button.click(); });

  const form = h("section", { class: "panel" },
    h("h2", {}, "Install a custom node from GitHub"),
    h("p", { class: "hint" }, "Clones the repository into custom_nodes, installs its requirements.txt and runs its install.py, " +
      "the same way ComfyUI-Manager does. Only install code you trust. Public repositories only."),
    h("div", { class: "row row-2" },
      h("div", {}, h("label", { for: "node-url" }, "GitHub link"), url),
      h("div", {}, h("label", { for: "node-branch" }, "Branch (optional)"), branch)),
    h("div", { class: "row-actions" }, button));

  const list = h("section", { class: "panel" }, h("h2", {}, "Installed custom nodes"));
  if (!data.nodes.length) {
    list.append(h("div", { class: "empty" }, "No custom nodes installed."));
  } else {
    list.append(h("table", { class: "table" },
      h("thead", {}, h("tr", {}, h("th", {}, "Name"), h("th", {}, "Source"), h("th", {}, ""))),
      h("tbody", {}, data.nodes.map((n) => h("tr", {},
        h("td", {}, n.name, n.disabled ? h("span", { class: "tag" }, " disabled") : null),
        h("td", {}, isGithub(n.url) ? h("a", { href: n.url.replace(/\.git$/, ""), target: "_blank", rel: "noopener noreferrer" }, n.url.replace(/^https:\/\/github\.com\//, "").replace(/\.git$/, "")) : (n.url || n.kind),
          n.branch ? h("span", { class: "hint" }, `  (${n.branch})`) : null),
        h("td", { class: "act" },
          n.kind === "git" ? updateButton(n) : null,
          " ",
          armed("Remove", "Really remove?", async () => {
            await api("/api/nodes/remove", "POST", { name: n.name });
            toast(`Removed ${n.name}. Restart ComfyUI to apply.`, true);
            await refreshState();
            if (S.page === "nodes") renderPage();
          })))))));
  }
  root.replaceChildren(form, list);
}

function updateButton(n) {
  const btn = h("button", { type: "button", class: "btn btn-small" }, "Update");
  btn.addEventListener("click", () => guarded(btn, async () => {
    await api("/api/nodes/update", "POST", { name: n.name });
    toast(`Updating ${n.name}. Progress is shown below.`, true);
    await refreshJobs();
  }));
  return btn;
}

/* ====================================================================================== settings */
let logTimer = null;

async function pageSettings(root, rid) {
  await refreshState();
  if (rid !== S.renderId) return;
  const st = S.state;
  const c = st.comfy;

  const control = (label, path, cls = "btn") => {
    const b = h("button", { type: "button", class: cls }, label);
    b.addEventListener("click", () => guarded(b, async () => { await api(path, "POST"); await refreshState(); renderPage(); }));
    return b;
  };

  const logBox = h("pre", { class: "logbox mono", hidden: true });
  const logBtn = h("button", { type: "button", class: "btn" }, "Show ComfyUI log");
  const loadLog = async () => {
    const res = await api("/api/comfy/log?tail=400");
    const atBottom = logBox.scrollTop + logBox.clientHeight >= logBox.scrollHeight - 20;
    logBox.textContent = res.lines.join("\n") || "(empty)";
    if (atBottom) logBox.scrollTop = logBox.scrollHeight;
  };
  logBtn.addEventListener("click", () => guarded(logBtn, async () => {
    clearInterval(logTimer);
    if (logBox.hidden) {
      logBox.hidden = false;
      logBtn.textContent = "Hide ComfyUI log";
      await loadLog();
      logBox.scrollTop = logBox.scrollHeight;
      logTimer = setInterval(() => { loadLog().catch(() => {}); }, 3000);
    } else {
      logBox.hidden = true;
      logBtn.textContent = "Show ComfyUI log";
    }
  }));

  const kv = (k, v) => [h("dt", {}, k), h("dd", {}, v)];
  const system = h("section", { class: "panel" },
    h("h2", {}, "ComfyUI"),
    h("dl", { class: "kv" },
      kv("State", c.state + (c.uptime ? ` (up ${fmtDuration(c.uptime)})` : "")),
      kv("ComfyUI port", String(st.ports.comfy)),
      kv("Jupyter port", String(st.ports.jupyter)),
      kv("GPU", st.gpu ? `${st.gpu.name}, ${(st.gpu.memory_total_mb / 1024).toFixed(0)} GB VRAM` : "not detected"),
      kv("Disk", st.disk.total ? `${fmtBytes(st.disk.free)} free of ${fmtBytes(st.disk.total)}` : "unknown"),
      kv("Launcher password", !st.password_protected ? "off" : st.password_source === "generated"
        ? "on, generated. Find it in the pod logs or in /workspace/.launcher/launcher_password.txt. Set LAUNCHER_PASSWORD to choose your own."
        : "on (set through LAUNCHER_PASSWORD)"),
      kv("ComfyUI login", "none. Port 8188 has no password, like standard ComfyUI. Only share the link with people you trust."),
      kv("Launcher version", st.version)),
    h("div", { class: "row-actions" },
      control("Start", "/api/comfy/start"),
      control("Stop", "/api/comfy/stop"),
      control("Restart", "/api/comfy/restart", "btn btn-primary"),
      logBtn),
    logBox);

  root.replaceChildren(tokenBox(), system);
}

/* ========================================================================================== jobs */
function jobNode(job) {
  const refs = {
    title: h("span", { class: "job-title" }),
    state: h("span", { class: "state" }),
    msg: h("div", { class: "job-msg" }),
    bar: h("progress", { max: 1 }),
    stats: h("span", { class: "job-stats" }),
    barWrap: null,
    log: h("pre", { class: "job-log mono", hidden: true }),
    cancel: h("button", { type: "button", class: "btn btn-small" }, "Cancel"),
    logBtn: h("button", { type: "button", class: "btn btn-small" }, "Log"),
  };
  refs.barWrap = h("div", { class: "job-bar" }, refs.bar, refs.stats);
  refs.cancel.addEventListener("click", () => guarded(refs.cancel, async () => {
    await api(`/api/jobs/${job.id}/cancel`, "POST");
    await refreshJobs();
  }));
  refs.logBtn.addEventListener("click", () => {
    if (S.openLogs.has(job.id)) { S.openLogs.delete(job.id); refs.log.hidden = true; } else { S.openLogs.add(job.id); refs.log.hidden = false; loadJobLog(job.id); }
  });
  const root = h("div", { class: "job" },
    h("div", { class: "job-top" }, refs.title,
      h("div", { class: "job-actions" }, refs.state, refs.cancel, refs.logBtn)),
    refs.msg, refs.barWrap, refs.log);
  return { root, refs };
}

function updateJobNode(entry, job) {
  const r = entry.refs;
  r.title.textContent = job.title;
  r.state.textContent = job.state;
  r.state.className = `state state-${job.state}`;
  r.msg.textContent = job.error || job.message || "";
  r.msg.className = job.error ? "job-msg err" : "job-msg";
  const active = job.state === "running" || job.state === "queued";
  r.cancel.hidden = !active;

  const showBar = (job.state === "running" && job.bytes_done > 0) || Boolean(job.bytes_total && active);
  r.barWrap.hidden = !showBar;
  if (showBar) {
    if (job.progress === null) r.bar.removeAttribute("value"); else r.bar.value = job.progress;
    const sp = S.speed.get(job.id);
    const now = performance.now() / 1000;
    let rate = sp ? sp.rate : 0;
    if (!sp) S.speed.set(job.id, { t: now, b: job.bytes_done, rate: 0 });
    else if (now - sp.t >= 1) {
      const inst = Math.max(0, (job.bytes_done - sp.b) / (now - sp.t));
      rate = sp.rate ? sp.rate * 0.6 + inst * 0.4 : inst;
      S.speed.set(job.id, { t: now, b: job.bytes_done, rate });
    }
    const parts = [job.bytes_total ? `${fmtBytes(job.bytes_done)} / ${fmtBytes(job.bytes_total)}` : fmtBytes(job.bytes_done)];
    if (job.progress !== null) parts.push(`${Math.floor(job.progress * 100)}%`);
    if (rate > 1000) {
      parts.push(`${fmtBytes(rate)}/s`);
      if (job.bytes_total) parts.push(`${fmtDuration((job.bytes_total - job.bytes_done) / rate)} left`);
    }
    r.stats.textContent = parts.join(" · ");
  }
  r.log.hidden = !S.openLogs.has(job.id);
}

async function loadJobLog(id) {
  const entry = S.jobNodes.get(id);
  if (!entry) return;
  try {
    const job = await api(`/api/jobs/${id}`);
    const log = entry.refs.log;
    const atBottom = log.scrollTop + log.clientHeight >= log.scrollHeight - 20;
    log.textContent = (job.log || []).join("\n") || "(no output yet)";
    if (atBottom) log.scrollTop = log.scrollHeight;
  } catch (e) { /* job gone */ }
}

function renderJobs() {
  const wrap = $("#jobs-wrap");
  wrap.hidden = S.jobs.length === 0;
  const list = $("#jobs");
  const ids = new Set(S.jobs.map((j) => j.id));
  for (const [id, entry] of S.jobNodes) {
    if (!ids.has(id)) { entry.root.remove(); S.jobNodes.delete(id); S.openLogs.delete(id); S.speed.delete(id); }
  }
  S.jobs.forEach((job, i) => {
    let entry = S.jobNodes.get(job.id);
    if (!entry) { entry = jobNode(job); S.jobNodes.set(job.id, entry); }
    updateJobNode(entry, job);
    if (list.children[i] !== entry.root) list.insertBefore(entry.root, list.children[i] || null);
  });
  for (const id of S.openLogs) {
    const j = S.jobs.find((x) => x.id === id);
    if (j && (j.state === "running" || j.state === "queued")) loadJobLog(id);
  }
}

/** Refresh the page that shows the result of a job as soon as the job finishes. */
function onJobTransitions() {
  let refresh = false;
  for (const job of S.jobs) {
    const prev = S.seenStates.get(job.id);
    S.seenStates.set(job.id, job.state);
    const finished = job.state === "done" || job.state === "error" || job.state === "cancelled";
    if (prev && prev !== job.state && finished) {
      if (job.state === "error") toast(`${job.title} failed: ${job.error || job.message}`);
      else if (job.state === "done") toast(`${job.title}: done.`, true);
      if (({ workflow: "workflows", model: "models", node: "nodes" })[job.kind] === S.page) refresh = true;
    }
  }
  if (refresh) renderPage(true);
}

/* ======================================================================================= polling */
async function refreshState() {
  S.state = await api("/api/state");
  renderHeader();
}

async function refreshJobs() {
  const res = await api("/api/jobs");
  S.jobs = res.jobs;
  renderJobs();
  onJobTransitions();
}

async function tick() {
  let delay = 4000;
  try {
    await Promise.all([refreshState(), refreshJobs()]);
    if (S.jobs.some((j) => j.state === "running" || j.state === "queued")) delay = 1500;
  } catch (e) {
    const pill = $("#status");
    pill.textContent = "Launcher unreachable";
    pill.className = "pill pill-bad";
    delay = 5000;
  }
  setTimeout(tick, delay);
}

/* ======================================================================================== routing */
async function renderPage(keepInputs = false) {
  const rid = ++S.renderId;
  clearInterval(logTimer);
  const root = $("#page");
  const typed = {};
  if (keepInputs) root.querySelectorAll("input[id], select[id]").forEach((el) => { if (el.type !== "file" && el.value) typed[el.id] = el.value; });
  document.querySelectorAll(".tab").forEach((t) => {
    if (t.dataset.page === S.page) t.setAttribute("aria-current", "page"); else t.removeAttribute("aria-current");
  });
  try {
    await PAGES[S.page](root, rid);
    for (const [id, value] of Object.entries(typed)) { const el = document.getElementById(id); if (el && !el.value) el.value = value; }
  } catch (e) {
    if (rid === S.renderId) root.replaceChildren(h("div", { class: "toast" }, e.message));
  }
}

function route() {
  const name = location.hash.replace("#", "");
  S.page = PAGES[name] ? name : "workflows";
  renderPage();
}

document.querySelectorAll(".tab").forEach((t) =>
  t.addEventListener("click", () => { location.hash = t.dataset.page; }));
window.addEventListener("hashchange", route);

$("#restart").addEventListener("click", function onRestart() {
  const btn = this;
  if (!btn.classList.contains("armed")) {
    btn.classList.add("armed");
    btn.textContent = "Click again to restart";
    setTimeout(() => { btn.classList.remove("armed"); btn.textContent = "Restart ComfyUI"; }, 4000);
    return;
  }
  btn.classList.remove("armed");
  btn.textContent = "Restart ComfyUI";
  guarded(btn, restartComfy);
});

$("#clear-jobs").addEventListener("click", function () {
  guarded(this, async () => { await api("/api/jobs/clear", "POST"); await refreshJobs(); });
});

$("#open-comfy").addEventListener("click", (ev) => {
  if (ev.currentTarget.getAttribute("aria-disabled") === "true") {
    toast("ComfyUI is not ready yet. Wait until the status says 'running'.");
  }
});

route();
tick();
