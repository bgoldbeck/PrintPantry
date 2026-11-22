// Print guide panel on the model page: rendered guide, "Ask AI" box, editor, AI settings.
import { renderMarkdown } from "./markdown.js";

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

async function call(url, opts = {}) {
  const r = await fetch(url, {
    ...opts,
    headers: opts.body ? { "Content-Type": "application/json" } : undefined,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.detail || `HTTP ${r.status}`);
  return data;
}

const BY = { ai: "Written by AI", basic: "Basic (no AI)", edited: "Edited by you" };
const running = new Map(); // model id -> job id, survives re-rendering the page

// opts: {buildsEl, slicers: [{id, name}], slice(build, printerId), hydrate(root)}
export async function guidePanel(el, model, opts = {}) {
  if (!el) return;
  let guide = { markdown: "", builds: [] };
  let ai = { has_key: false, model: "" };
  let editing = false;
  let open = false;  // collapsed until asked for; the build sets above say what to print

  const load = async () => {
    [guide, ai] = await Promise.all([
      call(`/api/models/${model.id}/guide`).catch(() => ({ markdown: "", builds: [] })),
      call("/api/ai/settings").catch(() => ai),
    ]);
    buildsPanel(opts.buildsEl, guide, opts);
  };

  const render = (status = "", error = "") => {
    const has = !!guide.markdown.trim();
    const busy = running.has(model.id);
    el.innerHTML = `
      <div class="guide-head">
        <button type="button" class="guide-toggle" aria-expanded="${open}">
          <span class="chev">${open ? "▾" : "▸"}</span> Print guide
          ${has && guide.by ? `<span class="guide-by ${esc(guide.by)}">${esc(BY[guide.by] || guide.by)}${guide.ai_model ? ` · ${esc(guide.ai_model)}` : ""}</span>` : ""}
        </button>
        <div class="guide-actions">
          ${has && !editing ? `<button type="button" class="small ghost" data-edit>Edit</button>` : ""}
          ${!has && !editing ? `<button type="button" class="small ghost" data-edit>Write my own</button>` : ""}
          <button type="button" class="small ghost" data-ai-settings title="Anthropic API key and model">AI settings</button>
        </div>
      </div>
      <div class="guide-main"${open ? "" : " hidden"}>
        ${editing ? `
          <textarea class="guide-editor" spellcheck="true" aria-label="Guide (Markdown)">${esc(guide.markdown)}</textarea>
          <p class="muted guide-hint">Markdown: <code>## Section</code>, <code>**bold**</code>, <code>- list</code>, <code>| table |</code>,
            callouts <code>&gt; [!TIP]</code> <code>&gt; [!WARNING]</code>. Build sets go in a <code>\`\`\`builds</code> block:
            <code>[{"group": "...", "name": "...", "note": "...", "parts": [["file.stl", 2]]}]</code>. Saved as <code>pantry-guide.md</code> in the model folder.</p>
          <div class="guide-buttons">
            <button type="button" data-save>Save</button>
            <button type="button" class="ghost" data-cancel>Cancel</button>
            ${has ? `<button type="button" class="ghost danger-text" data-clear>Delete guide</button>` : ""}
          </div>` : `
          ${guide.builds_error ? `<div class="notice warn">${esc(guide.builds_error)}</div>` : ""}
          ${has ? `<div class="guide-body">${renderMarkdown(guide.markdown)}</div>` : `<p class="empty">No guide yet. Ask AI to write one from the source page, the makers' comments and the files${model.files.filter((f) => f.kind === "stl").length > 2 ? ". It also sorts the STLs into build sets (which files to print together, and how many)" : ""}${ai.has_key ? "" : " (add an Anthropic API key under AI settings first, or get a basic guide without one)"}.</p>`}
          <form class="guide-ask">
            <label for="guide-request">${has ? "Ask AI to improve or answer something in the guide" : "Ask AI for a guide"}</label>
            <textarea id="guide-request" rows="2" placeholder="${has
              ? "e.g. Which files do I print for one 3/4 in and one 2 in roll side by side?"
              : "Optional: anything you want it to focus on (sizes, which version, what you have on hand...)"}"></textarea>
            <div class="guide-buttons">
              <button type="submit"${busy ? " disabled" : ""}>${busy ? "Working…" : has ? "Ask AI" : ai.has_key ? "Generate guide" : "Make basic guide"}</button>
              ${!ai.has_key ? `<span class="muted">No API key yet: <a href="#" data-ai-settings>add one</a> for a full AI guide.</span>` : ""}
              <span class="guide-status${error ? " error" : ""}" role="status">${esc(error || status)}</span>
            </div>
          </form>`}
      </div>`;

    el.querySelector(".guide-toggle").onclick = () => { open = !open; render(status, error); };
    el.querySelectorAll("[data-ai-settings]").forEach((b) => b.addEventListener("click", (e) => {
      e.preventDefault();
      openAiSettings(ai).then(async (s) => { if (s) { ai = s; render(); } });
    }));
    el.querySelector("[data-edit]")?.addEventListener("click", () => { editing = true; open = true; render(); });
    el.querySelector("[data-cancel]")?.addEventListener("click", () => { editing = false; render(); });
    el.querySelector("[data-save]")?.addEventListener("click", async () => {
      try {
        guide = await call(`/api/models/${model.id}/guide`, { method: "PUT", body: { markdown: el.querySelector(".guide-editor").value } });
        editing = false;
        render("Saved");
      } catch (err) { render("", err.message); }
    });
    el.querySelector("[data-clear]")?.addEventListener("click", async () => {
      await call(`/api/models/${model.id}/guide`, { method: "DELETE" }).catch(() => {});
      guide = { markdown: "" };
      editing = false;
      render();
    });
    el.querySelector(".guide-ask")?.addEventListener("submit", async (e) => {
      e.preventDefault();
      const request = el.querySelector("#guide-request").value;
      try {
        const job = await call(`/api/models/${model.id}/guide/generate`, { method: "POST", body: { request } });
        running.set(model.id, job.id);
        follow(job.id, request);
      } catch (err) { render("", err.message); }
    });
  };

  const follow = async (jobId, request = "") => {
    render(ai.has_key ? "Claude is reading the source page and comments and writing the guide. This takes about a minute." : "Making a basic guide…");
    if (request) { const t = el.querySelector("#guide-request"); if (t) t.value = request; }
    for (;;) {
      await new Promise((r) => setTimeout(r, 2000));
      if (!el.isConnected) return; // left the page; the job keeps running on the server
      let job;
      try { job = await call(`/api/guide-jobs/${jobId}`); } catch { running.delete(model.id); render(); return; }
      if (job.state === "running") continue;
      running.delete(model.id);
      if (job.state === "done") {
        await load();
        open = true;
        render("Guide updated");
      } else {
        render("", job.error || "Failed");
        const t = el.querySelector("#guide-request");
        if (t) t.value = request;
      }
      return;
    }
  };

  await load();
  render();
  if (running.has(model.id)) follow(running.get(model.id));
}

function openAiSettings(current) {
  return new Promise((resolve) => {
    const dlg = document.createElement("dialog");
    dlg.className = "prompt-dialog ai-settings";
    dlg.innerHTML = `
      <h2>AI settings</h2>
      <form method="dialog">
        <p class="muted" style="margin-top:0">Print guides are written by Claude through the Anthropic API.
          Create a key at <a href="https://console.anthropic.com/settings/keys" target="_blank" rel="noopener">console.anthropic.com</a>
          (pay as you go; a guide costs roughly 5-15 cents with Opus). The key stays in Print Pantry's database on the NAS.</p>
        <label for="ai-key">API key ${current.has_key ? `<span class="muted">(saved: ${esc(current.key_hint)})</span>` : ""}</label>
        <input id="ai-key" type="password" autocomplete="off" placeholder="${current.has_key ? "Leave empty to keep the saved key" : "sk-ant-..."}">
        <label for="ai-model">Model</label>
        <select id="ai-model">${(current.models || ["claude-opus-5-5"]).map((m) =>
          `<option value="${esc(m)}"${m === current.model ? " selected" : ""}>${esc(m)}${m.includes("opus") ? " (best, default)" : m.includes("haiku") ? " (cheapest)" : ""}</option>`).join("")}</select>
        <p class="guide-status error" data-err></p>
        <div class="dialog-actions">
          ${current.has_key ? `<button type="button" class="ghost danger-text" data-remove>Remove key</button>` : ""}
          <button type="button" class="ghost" data-cancel>Cancel</button>
          <button type="submit">Save</button>
        </div>
      </form>`;
    document.body.appendChild(dlg);
    let result = null;
    const save = async (body) => {
      try {
        result = await call("/api/ai/settings", { method: "PUT", body });
        dlg.close();
      } catch (err) { dlg.querySelector("[data-err]").textContent = err.message; }
    };
    dlg.querySelector("[data-cancel]").onclick = () => dlg.close();
    dlg.querySelector("[data-remove]")?.addEventListener("click", () => save({ api_key: "" }));
    dlg.querySelector("form").onsubmit = (e) => {
      e.preventDefault();
      const key = dlg.querySelector("#ai-key").value.trim();
      save({ model: dlg.querySelector("#ai-model").value, ...(key ? { api_key: key } : {}) });
    };
    dlg.addEventListener("close", () => { dlg.remove(); resolve(result); });
    dlg.showModal();
  });
}

// "What to print": one card per build set, with part pictures and Slice buttons.
function buildsPanel(el, guide, opts) {
  if (!el) return;
  const builds = guide.builds || [];
  el.hidden = !builds.length;
  if (!builds.length) { el.innerHTML = ""; return; }
  const groups = [...new Set(builds.map((b) => b.group || ""))];
  let group = groups.includes(el.dataset.group) ? el.dataset.group : groups[0];
  const slicers = opts.slicers || [];
  const render = () => {
    el.dataset.group = group;
    const shown = builds.filter((b) => (b.group || "") === group);
    el.innerHTML = `
      <h2>What to print <span class="n">${builds.length} build set${builds.length === 1 ? "" : "s"}</span></h2>
      <p class="muted builds-lead">Pick one build. Each card lists exactly which files to print and how many.</p>
      ${groups.length > 1 ? `<div class="build-groups" role="tablist">${groups.map((g) =>
        `<button type="button" class="small${g === group ? "" : " ghost"}" role="tab" aria-selected="${g === group}" data-group="${esc(g)}">${esc(g || "Other")}</button>`).join("")}</div>` : ""}
      <div class="build-cards">${shown.map((b) => {
        const i = builds.indexOf(b);
        const sliceable = b.parts.some((p) => p.id && p.kind === "stl");
        return `<article class="build-card">
          <header><h3>${esc(b.name)}</h3><span class="pieces">${b.pieces} piece${b.pieces === 1 ? "" : "s"} · ${b.parts.length} file${b.parts.length === 1 ? "" : "s"}</span></header>
          ${b.note ? `<p class="build-note">${esc(b.note)}</p>` : ""}
          <ul class="build-parts">${b.parts.map((p) => `<li class="${p.id ? "" : "missing"}">
            <span class="thumb">${p.id ? `<img loading="lazy" alt="" data-stl="${esc(p.id)}" src="/api/files/${esc(p.id)}/thumb">` : "?"}</span>
            <span class="qty">×${p.qty}</span><span class="pname">${esc(p.name)}${p.id ? "" : " <small>(not in this folder)</small>"}</span></li>`).join("")}</ul>
          <div class="build-actions">${sliceable ? slicers.map((pr, k) =>
            `<button type="button" class="small${k ? " ghost" : ""}" data-build="${i}" data-printer="${esc(pr.id)}">Slice this set for ${esc(pr.name)}</button>`).join("")
            : ""}${!slicers.length && sliceable ? `<span class="muted">Slicer not available right now.</span>` : ""}</div>
        </article>`;
      }).join("")}</div>`;
    el.querySelectorAll("[data-group]").forEach((b) => (b.onclick = () => { group = b.dataset.group; render(); }));
    el.querySelectorAll("[data-build]").forEach((b) => (b.onclick = () => opts.slice?.(builds[Number(b.dataset.build)], b.dataset.printer)));
    opts.hydrate?.(el);
  };
  render();
}
