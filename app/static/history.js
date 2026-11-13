// Print history: the #/history page, the panel on each model page, and the
// dialog to change a print's outcome, add a note or a picture.
import { swatchHtml } from "./filament.js";

const esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

async function api(path, opts) {
  const res = await fetch(path, opts);
  if (res.status === 401) { location.href = "/login"; throw new Error("login"); }
  if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText);
  return res.json();
}

const OUTCOME = { printing: "Printing", success: "Success", failed: "Failed", cancelled: "Cancelled" };

function fmtDur(sec) {
  if (sec == null) return "";
  sec = Math.max(0, Math.round(sec));
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60);
  return h ? `${h}h ${String(m).padStart(2, "0")}m` : m ? `${m}m` : `${sec}s`;
}
function fmtWhen(ts) {
  if (!ts) return "";
  const d = new Date(ts * 1000), now = new Date();
  const time = d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  if (d.toDateString() === now.toDateString()) return `Today ${time}`;
  const y = new Date(now); y.setDate(now.getDate() - 1);
  if (d.toDateString() === y.toDateString()) return `Yesterday ${time}`;
  const opts = { month: "short", day: "numeric" };
  if (d.getFullYear() !== now.getFullYear()) opts.year = "numeric";
  return `${d.toLocaleDateString([], opts)} ${time}`;
}
const fmtGrams = (g) => (g == null ? "" : `${g >= 100 ? Math.round(g) : g.toFixed(1)} g`);
const fmtCost = (c) => (c == null ? "" : `$${c.toFixed(2)}`);
const stem = (f) => String(f || "").replace(/\.(gcode|bgcode|gcode\.3mf|3mf)$/i, "");

function picture(p, ctx) {
  if (p.photo_url) return `<img loading="lazy" alt="" src="${p.photo_url}">`;
  if (p.file_id) return `<img loading="lazy" alt="" src="/api/files/${p.file_id}/thumb" onerror="this.replaceWith(Object.assign(document.createElement('span'),{className:'ph'}))">`;
  const m = p.model_id && ctx.model?.(p.model_id);
  if (m?.cover && m.cover.type !== "stl") {
    const q = m.cover.plate ? `?plate=${m.cover.plate}` : "";
    return `<img loading="lazy" alt="" src="/api/files/${m.cover.file}/thumb${q}">`;
  }
  return `<span class="ph"></span>`;
}

function filamentHtml(p) {
  const by = new Map((p.usage?.filament || []).map((u) => [u.toolhead, u]));
  return p.filament.map((f) => {
    const g = by.get(f.toolhead)?.grams ?? (f.grams != null && p.progress != null ? f.grams * p.progress : f.grams);
    return swatchHtml(f.type, f.color, `T${f.toolhead + 1}${g ? ` · ${fmtGrams(g)}` : ""}`);
  }).join("");
}

export function printRowHtml(p, ctx = {}) {
  const m = p.model_id && ctx.model?.(p.model_id);
  const title = ctx.showModel && m ? `<a href="#/m/${m.id}">${esc(m.name)}</a>` : esc(stem(p.file) || "Unknown file");
  const sub = [];
  if (ctx.showModel && m) sub.push(esc(stem(p.file)));
  const facts = [
    fmtWhen(p.started),
    p.status === "printing" ? "in progress" : fmtDur(p.duration),
    p.status !== "success" && p.status !== "printing" && p.progress != null && p.progress < 1 ? `${Math.round(p.progress * 100)}% done` : "",
    p.grams != null ? fmtGrams(p.grams) : "",
    fmtCost(p.cost),
  ].filter(Boolean);
  const empty = p.status === "cancelled" && !(p.duration > 0);
  return `<button type="button" class="print-row${empty ? " empty-cancel" : ""}" data-print-id="${p.id}">
    <span class="print-pic">${picture(p, ctx)}</span>
    <span class="print-main">
      <span class="print-title">${title}</span>
      ${sub.length ? `<span class="print-sub">${sub.join(" · ")}</span>` : ""}
      <span class="print-facts">${ctx.printerBadge ? ctx.printerBadge(p.printer) : esc(p.printer)}${facts.map((f) => `<span>${esc(f)}</span>`).join("")}</span>
      ${p.filament.length ? `<span class="swatches">${filamentHtml(p)}</span>` : ""}
      ${p.note ? `<span class="print-note">${esc(p.note)}</span>` : ""}
    </span>
    <span class="outcome" data-status="${p.status}">${OUTCOME[p.status] || esc(p.status)}${p.status_by_user ? " ✎" : ""}</span>
  </button>`;
}

function statsHtml(s) {
  if (!s.count) return "";
  const rate = s.success + s.failed ? Math.round((100 * s.success) / (s.success + s.failed)) : null;
  const bits = [
    `${s.count} print${s.count === 1 ? "" : "s"}`,
    rate != null ? `${rate}% successful` : "",
    s.seconds ? `${fmtDur(s.seconds)} printing` : "",
    s.grams ? fmtGrams(s.grams) : "",
    s.cost ? fmtCost(s.cost) : "",
    s.last ? `last ${fmtWhen(s.last)}` : "",
  ].filter(Boolean);
  return `<div class="print-stats">${bits.map((b) => `<span>${esc(b)}</span>`).join("")}</div>`;
}

// ---------------------------------------------------------------- edit dialog
export function editPrint(p, ctx, onSaved) {
  let status = p.status;
  const dlg = document.createElement("dialog");
  const canCam = ctx.canCamera?.(p.printer);
  const render = () => {
    dlg.innerHTML = `<h2>${esc(stem(p.file) || "Print")}</h2>
      <p class="sub" style="margin-bottom:12px">${esc(fmtWhen(p.started))} · ${esc(p.printer.toUpperCase())}
        ${p.duration ? ` · ${esc(fmtDur(p.duration))}` : ""}${p.printer_status ? ` · printer said “${esc(p.printer_status)}”` : ""}</p>
      ${p.photo_url ? `<a href="${p.photo_url}" target="_blank" rel="noopener"><img class="print-photo" alt="Picture of the print" src="${p.photo_url}"></a>` : ""}
      <div class="dialog-row"><button type="button" class="small ghost" data-upload>Add a picture</button>
        ${canCam ? `<button type="button" class="small ghost" data-snap>Take one with the printer camera</button>` : ""}
        <input type="file" accept="image/jpeg,image/png,image/webp" capture="environment" hidden></div>
      <strong>How did it go?</strong>
      <div class="dialog-row outcomes">${["success", "failed", "cancelled"].map((s) =>
        `<button type="button" class="small" data-status="${s}" aria-pressed="${s === status}">${OUTCOME[s]}</button>`).join("")}</div>
      <label for="print-note"><strong>Note</strong></label>
      <textarea id="print-note" rows="3" placeholder="What went wrong, settings to change next time...">${esc(p.note)}</textarea>
      <p class="dialog-msg" role="status"></p>
      <div class="dialog-actions"><button type="button" class="ghost" data-close>Cancel</button><button type="button" data-save>Save</button></div>`;
  };
  render();
  const msg = (t) => { dlg.querySelector(".dialog-msg").textContent = t; };
  const keepNote = () => dlg.querySelector("#print-note").value;
  dlg.addEventListener("click", async (e) => {
    const b = e.target.closest("button");
    if (!b) return;
    if (b.dataset.status) {
      status = b.dataset.status;
      dlg.querySelectorAll(".outcomes button").forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
    } else if ("close" in b.dataset) {
      dlg.close();
    } else if ("upload" in b.dataset) {
      dlg.querySelector("input[type=file]").click();
    } else if ("snap" in b.dataset) {
      b.disabled = true; msg("Asking the camera...");
      try {
        const note = keepNote();
        p = await api(`/api/history/${p.id}/photo`, { method: "POST" });
        p.note = note; render(); onSaved?.();
      } catch (err) { msg(err.message); b.disabled = false; }
    } else if ("save" in b.dataset) {
      b.disabled = true;
      try {
        const body = { note: keepNote() };
        if (status !== p.status && status !== "printing") body.status = status;
        await api(`/api/history/${p.id}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
        dlg.close();
        onSaved?.();
      } catch (err) { msg(err.message); b.disabled = false; }
    }
  });
  dlg.addEventListener("change", async (e) => {
    const file = e.target.files?.[0];
    if (!file) return;
    msg("Uploading...");
    try {
      const note = keepNote();
      p = await api(`/api/history/${p.id}/photo`, { method: "PUT", body: file, headers: { "Content-Type": file.type } });
      p.note = note; render(); onSaved?.();
    } catch (err) { msg(err.message); }
  });
  dlg.addEventListener("close", () => dlg.remove());
  document.body.appendChild(dlg);
  dlg.showModal();
}

function wireRows(root, getPrints, ctx, reload) {
  root.addEventListener("click", (e) => {
    if (e.target.closest("a")) return;
    const row = e.target.closest("[data-print-id]");
    if (!row) return;
    const p = getPrints().find((x) => String(x.id) === row.dataset.printId);
    if (p) editPrint(p, ctx, reload);
  });
}

// ---------------------------------------------------------------- model page panel
export async function historyPanel(el, modelId, ctx) {
  let prints = [];
  async function load() {
    const res = await api(`/api/history?model_id=${encodeURIComponent(modelId)}&limit=50`);
    prints = res.prints;
    if (!res.total) { el.hidden = true; return; }
    el.hidden = false;
    el.innerHTML = `<h2>Print history <span class="n">${res.total}</span></h2>${statsHtml(res.stats)}
      <div class="print-list">${prints.map((p) => printRowHtml(p, ctx)).join("")}</div>
      ${res.total > prints.length ? `<p class="empty"><a href="#/history">See all on the History page</a></p>` : ""}`;
  }
  wireRows(el, () => prints, ctx, load);
  await load().catch(() => { el.hidden = true; });
}

// ---------------------------------------------------------------- #/history
export async function historyPage(view, ctx) {
  const PAGE = 60;
  const state = { printer: "", status: "", hideEmpty: true, limit: PAGE };
  let prints = [];
  let timer = null;
  view.innerHTML = `<div class="crumbs"><a href="#/">All categories</a></div>
    <div class="page-head"><h1>Print history</h1>
      <button class="small ghost" id="h-sync" title="Check the printers for new jobs now">Check printers</button></div>
    <div class="filters">
      <select id="h-printer" aria-label="Printer"><option value="">All printers</option>${(ctx.printers || []).map((p) =>
        `<option value="${esc(p.id)}">${esc(p.name)}</option>`).join("")}</select>
      <select id="h-status" aria-label="Outcome"><option value="">Any outcome</option>${Object.entries(OUTCOME).map(([k, v]) =>
        `<option value="${k}">${v}</option>`).join("")}</select>
      <label class="check"><input type="checkbox" id="h-empty" checked> Hide prints cancelled before they began</label>
    </div>
    <div id="h-stats"></div>
    <section class="panel"><div class="print-list" id="h-list"><p class="empty">Loading...</p></div></section>
    <div id="h-more"></div>`;
  const list = view.querySelector("#h-list");
  async function load() {
    const q = new URLSearchParams({ limit: state.limit, printer: state.printer, status: state.status, hide_empty: state.hideEmpty });
    const res = await api(`/api/history?${q}`);
    prints = res.prints;
    view.querySelector("#h-stats").innerHTML = statsHtml(res.stats);
    list.innerHTML = prints.length ? prints.map((p) => printRowHtml(p, { ...ctx, showModel: true })).join("")
      : `<p class="empty">No prints yet. Prints show up here as soon as a printer reports them, including ones started from the printer's screen or a desktop slicer.</p>`;
    view.querySelector("#h-more").innerHTML = res.total > prints.length
      ? `<p style="text-align:center"><button class="ghost" id="h-load">Show more (${res.total - prints.length} older)</button></p>` : "";
    clearTimeout(timer);
    if (prints.some((p) => p.status === "printing")) timer = setTimeout(() => load().catch(() => {}), 30000);
  }
  wireRows(list, () => prints, ctx, load);
  view.querySelector("#h-printer").onchange = (e) => { state.printer = e.target.value; load(); };
  view.querySelector("#h-status").onchange = (e) => { state.status = e.target.value; load(); };
  view.querySelector("#h-empty").onchange = (e) => { state.hideEmpty = e.target.checked; load(); };
  view.addEventListener("click", async (e) => {
    if (e.target.id === "h-load") { state.limit += PAGE; load(); }
    if (e.target.id === "h-sync") {
      e.target.disabled = true;
      try { await api("/api/history/sync", { method: "POST" }); await load(); } finally { e.target.disabled = false; }
    }
  });
  await load();
  return () => clearTimeout(timer);
}
