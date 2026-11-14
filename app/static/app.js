// Print Pantry single-page UI. Routes:
//   #/                 category tiles (+ recently changed)
//   #/c/<category>     models in a category
//   #/m/<model id>     model page with 3D viewer and print files
//   #/search/<query>   search results
//   #/printers         live printer status and cameras
//   #/history          every print, on every printer
//   #/spools           spool inventory and cost settings
import { createViewer, renderThumbnail } from "./viewer.js";
import { canPrintOn, loadPrinters, openPrintDialog, printerById, printersPage, startPrinterDock, startPrinterStrip, stateBadge } from "./printers.js";
import { loadSlicer, openSliceDialog, slicerPrinters } from "./slicing.js";
import { openCustomizeDialog } from "./parametric.js";
import { swatchHtml } from "./filament.js";
import { historyPage, historyPanel } from "./history.js";
import { costHtml, loadPricing, spoolsPage } from "./spools.js";

const view = document.getElementById("view");
const statusEl = document.getElementById("status");
const searchInput = document.getElementById("search");
let ICONS = {};
let LIB = null;
let cleanup = null;

// ---------------------------------------------------------------- helpers
const esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

function icon(name, cls = "") {
  const inner = ICONS[name] || ICONS["shapes"] || "";
  return `<svg class="${cls}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${inner}</svg>`;
}

async function api(path, opts) {
  const res = await fetch(path, opts);
  if (res.status === 401) { location.href = "/login"; throw new Error("login"); }
  if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText);
  return res.json();
}

function fmtTime(sec) {
  if (sec == null) return "";
  const h = Math.floor(sec / 3600), m = Math.round((sec % 3600) / 60);
  return h ? `${h}h ${String(m).padStart(2, "0")}m` : `${m}m`;
}
const fmtGrams = (g) => (g == null ? "" : `${g >= 100 ? Math.round(g) : g.toFixed(1)} g`);
function fmtSize(b) {
  if (b < 1024) return `${b} B`;
  if (b < 1048576) return `${(b / 1024).toFixed(0)} KB`;
  return `${(b / 1048576).toFixed(1)} MB`;
}
function fmtAgo(ts) {
  const s = Date.now() / 1000 - ts;
  if (s < 90) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return new Date(ts * 1000).toLocaleDateString();
}
const plural = (n, one, many = one + "s") => `${n} ${n === 1 ? one : many}`;
const printer = (id) => LIB?.printers.find((p) => p.id === id);
function printerBadge(id) {
  const p = printer(id);
  return p ? `<span class="badge printer" style="--c:${p.color}">${esc(p.name)}</span>` : "";
}
const fileUrl = (id, download) => `/api/files/${id}/raw${download ? "?download=1" : ""}`;

function setTitle(t) { document.title = t ? `${t} · Print Pantry` : "Print Pantry"; }

async function refreshLibrary() {
  LIB = await api("/api/library");
  const when = LIB.scanned_at ? `scanned ${fmtAgo(LIB.scanned_at)}` : "first scan running";
  statusEl.textContent = `${plural(LIB.model_count, "model")} · ${plural(LIB.file_count, "file")} · ${when} · v${LIB.version}`;
  return LIB;
}

// ---------------------------------------------------------------- cards
function coverHtml(m) {
  const c = m.cover;
  if (c && (c.type === "image" || c.type === "plate")) {
    const q = c.type === "plate" ? `?plate=${c.plate}` : "";
    return `<img loading="lazy" alt="" src="/api/files/${c.file}/thumb${q}">`;
  }
  if (c && c.type === "stl") {
    return `<img loading="lazy" alt="" data-stl="${c.file}" src="/api/files/${c.file}/thumb">`;
  }
  return `<span class="ph">${icon("cuboid")}</span>`;
}

function modelCard(m, showCategory = false) {
  const bits = [];
  if (showCategory) bits.push(esc(m.category));
  if (m.counts.stl) bits.push(plural(m.counts.stl, "STL"));
  if (m.counts.sliced) bits.push(plural(m.counts.sliced, "print file"));
  return `<a class="card" href="#/m/${m.id}">
    <div class="cover">${coverHtml(m)}</div>
    <div class="body">
      <div class="name">${esc(m.name)}</div>
      <div class="meta">${bits.join(" · ")} ${m.printers.map(printerBadge).join("")}</div>
      ${(m.tags || []).length ? `<div class="card-tags">${m.tags.slice(0, 4).map((t) => `<span class="tag">${esc(t)}</span>`).join("")}</div>` : ""}
    </div></a>`;
}

// STLs without a stored preview: render once in the browser, store on the server.
function hydrateStlCovers(root) {
  root.querySelectorAll("img[data-stl]").forEach((img) => {
    img.addEventListener("error", async () => {
      const id = img.dataset.stl;
      img.removeAttribute("data-stl");
      const holder = img.parentElement;
      holder.innerHTML = `<span class="ph">${icon("cuboid")}</span>`;
      try {
        const blob = await renderThumbnail(fileUrl(id));
        if (!blob) return;
        const url = URL.createObjectURL(blob);
        holder.innerHTML = `<img alt="" src="${url}">`;
        fetch(`/api/files/${id}/thumb`, { method: "PUT", body: blob, headers: { "Content-Type": "image/png" } });
      } catch (e) {
        console.warn("thumbnail failed", id, e);
      }
    }, { once: true });
  });
}

// ---------------------------------------------------------------- prompt dialog
function promptDialog({ title, label, value = "", placeholder = "", actionLabel = "Save" }) {
  return new Promise((resolve) => {
    const dlg = document.createElement("dialog");
    dlg.className = "prompt-dialog";
    dlg.innerHTML = `
      <h2>${esc(title)}</h2>
      <form method="dialog">
        <label for="prompt-input" style="display:block;margin-bottom:6px;font-weight:600">${esc(label)}</label>
        <input id="prompt-input" type="text" style="width:100%;margin-bottom:16px;padding:8px 12px;border-radius:8px;border:1px solid var(--line);background:var(--bg)" value="${esc(value)}" placeholder="${esc(placeholder)}" required>
        <div class="dialog-actions">
          <button type="button" class="ghost" data-cancel>Cancel</button>
          <button type="submit">${esc(actionLabel)}</button>
        </div>
      </form>`;
    document.body.appendChild(dlg);
    const input = dlg.querySelector("#prompt-input");
    dlg.querySelector("[data-cancel]").onclick = () => { dlg.close(); resolve(null); };
    dlg.querySelector("form").onsubmit = (e) => {
      e.preventDefault();
      const val = input.value.trim();
      dlg.close();
      resolve(val || null);
    };
    dlg.addEventListener("close", () => dlg.remove());
    dlg.showModal();
    input.focus();
    input.select();
  });
}

// ---------------------------------------------------------------- tags
const tagHref = (t) => `#/search/${encodeURIComponent(`tag:${t.replaceAll(" ", "_")}`)}`;
function tagCloud(list, active = []) {
  return `<div class="tag-cloud">${list.map(({ tag, count }) =>
    `<a class="tag${active.includes(tag) ? " on" : ""}" href="${tagHref(tag)}">${esc(tag)}${count != null ? ` <span class="n">${count}</span>` : ""}</a>`).join("")}</div>`;
}

function tagEditor(m, holder) {
  let tags = [...(m.tags || [])];
  let known = [];
  api("/api/tags").then((r) => { known = r.tags.map((t) => t.tag); draw(); }).catch(() => {});
  async function save(next) {
    try {
      const r = await api(`/api/models/${m.id}/tags`, {
        method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ tags: next }),
      });
      tags = r.tags;
    } catch (e) { flash(`Couldn't save tags: ${esc(e.message || e)}`); }
    draw(true);
  }
  function draw(focus) {
    const suggestions = known.filter((t) => !tags.includes(t));
    const selectHtml = suggestions.length
      ? `<select class="tag-select" aria-label="Add existing tag">
          <option value="">+ Add existing tag...</option>
          ${suggestions.map((t) => `<option value="${esc(t)}">${esc(t)}</option>`).join("")}
         </select>`
      : "";
    holder.innerHTML = `${tags.map((t) => `<span class="tag">
        <a href="${tagHref(t)}">${esc(t)}</a><button type="button" class="x" data-untag="${esc(t)}" aria-label="Remove tag ${esc(t)}">×</button></span>`).join("")}
      ${selectHtml}
      <form class="tag-add"><input list="tag-list" placeholder="${tags.length ? "New tag..." : "New tag, like kitchen or gift"}" aria-label="Add a tag" maxlength="40">
        <datalist id="tag-list">${suggestions.map((t) => `<option value="${esc(t)}">`).join("")}</datalist></form>`;
    const sel = holder.querySelector(".tag-select");
    if (sel) {
      sel.onchange = () => {
        if (sel.value) save([...tags, sel.value]);
      };
    }
    const input = holder.querySelector("input");
    holder.querySelector("form").onsubmit = (e) => {
      e.preventDefault();
      const add = input.value.split(",").map((x) => x.trim()).filter(Boolean);
      if (add.length) save([...tags, ...add]);
    };
    holder.querySelectorAll("[data-untag]").forEach((b) =>
      b.addEventListener("click", () => save(tags.filter((t) => t !== b.dataset.untag))));
    if (focus) input.focus();
  }
  draw();
}

// ---------------------------------------------------------------- pages
async function homePage() {
  setTitle("");
  await refreshLibrary();
  if (LIB.error) {
    view.innerHTML = `<div class="notice error"><strong>Can't read the library.</strong><br>${esc(LIB.error)}</div>`;
    return;
  }
  if (!LIB.categories.length) {
    view.innerHTML = `<h1>Your pantry is empty</h1>
      <div class="notice">${LIB.scanning || !LIB.scanned_at ? "Scanning the library folder..." :
        `Add folders to the library share, one per category (for example <code>Tools</code>), with one folder per model inside.
         Then press <strong>Rescan</strong>.`}</div>`;
    if (LIB.scanning || !LIB.scanned_at) setTimeout(() => route(), 2000);
    return;
  }
  const tiles = LIB.categories.map((c) => `
    <a class="tile" href="#/c/${encodeURIComponent(c.id)}" style="--c:${c.color}">
      <div class="icon">${icon(c.icon)}</div>
      <div>
        <div class="label">${esc(c.name)}</div>
        <div class="count">${plural(c.model_count, "model")}${c.sliced_count ? ` · ${plural(c.sliced_count, "print file")}` : ""}</div>
      </div>
      <button class="edit" data-edit="${esc(c.id)}" title="Change icon and colour" aria-label="Change icon for ${esc(c.name)}">${icon("pencil")}</button>
    </a>`).join("");
  view.innerHTML = `<div class="tiles">${tiles}</div>
    <div id="home-tags"></div>
    <div class="section-head"><h2>Recently changed</h2></div>
    <div class="cards" id="recent"></div>`;
  api("/api/tags").then(({ tags }) => {
    const el = document.getElementById("home-tags");
    if (el && tags.length) el.innerHTML = `<div class="section-head"><h2>Tags</h2></div>${tagCloud(tags.slice(0, 40))}`;
  }).catch(() => {});
  view.querySelectorAll("[data-edit]").forEach((b) =>
    b.addEventListener("click", (e) => { e.preventDefault(); editCategory(b.dataset.edit); }));
  const recent = await api("/api/models?sort=recent&limit=8");
  const el = document.getElementById("recent");
  if (el) { el.innerHTML = recent.models.map((m) => modelCard(m, true)).join(""); hydrateStlCovers(el); }
}

async function categoryPage(name) {
  if (!LIB) await refreshLibrary();
  const cat = await api(`/api/categories/${encodeURIComponent(name)}`);
  setTitle(cat.name);
  view.innerHTML = `
    <div class="crumbs"><a href="#/">All categories</a></div>
    <div class="section-head">
      <h1 style="display:flex;gap:12px;align-items:center"><span style="color:${cat.color};display:inline-flex">${icon(cat.icon)}</span>${esc(cat.name)}</h1>
      <button class="ghost small" id="edit-cat">Change icon</button>
    </div>
    <p class="sub">${plural(cat.model_count, "model")}</p>
    ${cat.models.length ? `<div class="cards">${cat.models.map((m) => modelCard(m)).join("")}</div>`
      : `<div class="notice">No models here yet. Put a folder per model inside <code>${esc(cat.name)}</code> and press Rescan.</div>`}`;
  view.querySelector("h1 svg").style.cssText = "width:36px;height:36px";
  document.getElementById("edit-cat").onclick = () => editCategory(cat.id);
  hydrateStlCovers(view);
}

async function searchPage(q) {
  if (!LIB) await refreshLibrary();
  setTitle(`Search: ${q}`);
  searchInput.value = q;
  const [res, all] = await Promise.all([api(`/api/models?q=${encodeURIComponent(q)}`), api("/api/tags").catch(() => ({ tags: [] }))]);
  const active = q.toLowerCase().split(/\s+/).filter((w) => w.startsWith("tag:")).map((w) => w.slice(4).replaceAll("_", " "));
  const words = q.toLowerCase().split(/\s+/).filter((w) => w && !w.startsWith("tag:"));
  const cats = LIB.categories.filter((c) => words.length && words.every((w) => c.name.toLowerCase().includes(w)));
  // Tags in the results, to narrow down further.
  const inResults = new Map();
  res.models.forEach((m) => (m.tags || []).forEach((t) => inResults.set(t, (inResults.get(t) || 0) + 1)));
  const narrow = [...inResults].filter(([t]) => !active.includes(t)).sort((a, b) => b[1] - a[1]).slice(0, 20)
    .map(([tag, count]) => ({ tag, count }));
  const matchingTags = all.tags.filter((t) => words.length && words.every((w) => t.tag.includes(w)) && !active.includes(t.tag));
  const title = active.length && !words.length ? `Tagged ${active.map((t) => `"${esc(t)}"`).join(" and ")}` : `Results for "${esc(q)}"`;
  view.innerHTML = `<div class="crumbs"><a href="#/">All categories</a></div>
    <h1>${title}</h1><p class="sub">${plural(res.total, "model")}${active.length ? ` · <a href="#/search/${encodeURIComponent(words.join(" "))}">clear tag filter</a>` : ""}</p>
    ${cats.length ? `<div class="tiles small">${cats.map((c) => `<a class="tile" href="#/c/${encodeURIComponent(c.id)}" style="--c:${c.color}">
      <div class="icon">${icon(c.icon)}</div><div><div class="label">${esc(c.name)}</div><div class="count">${plural(c.model_count, "model")}</div></div></a>`).join("")}</div>` : ""}
    ${matchingTags.length ? `<p class="sub">Tags: ${tagCloud(matchingTags)}</p>` : ""}
    ${narrow.length > 1 ? `<div class="narrow"><span class="muted">Narrow down:</span>${narrow.map(({ tag, count }) =>
      `<a class="tag" href="#/search/${encodeURIComponent(`${q} tag:${tag.replaceAll(" ", "_")}`)}">${esc(tag)} <span class="n">${count}</span></a>`).join("")}</div>` : ""}
    ${res.models.length ? `<div class="cards">${res.models.map((m) => modelCard(m, true)).join("")}</div>` : `<div class="notice">Nothing matched.</div>`}`;
  hydrateStlCovers(view);
}

// "Pot Ladle Clip_U1_plate_2.gcode" -> "Pot Ladle Clip"
const sourceName = (name) => name.replace(/(\.gcode)?(\.3mf)?$/i, "").replace(/_(U1|P1S)(_plate_\d+)?$/i, "");

function plateHtml(f, p, count, fallback) {
  // Headless slicing writes no thumbnail, so use the model's own picture.
  const thumb = p.has_thumbnail
    ? `<img loading="lazy" alt="" src="/api/files/${f.id}/thumb?plate=${p.index}">`
    : fallback;
  const facts = [fmtTime(p.seconds), fmtGrams(p.grams), costHtml(f.printer, p)].filter(Boolean).map((t) => `<span>${t}</span>`).join("");
  const sw = p.filaments.map((fl) => swatchHtml(fl.type, fl.color, fl.grams ? fmtGrams(fl.grams) : "", `Toolhead ${fl.slot}`)).join("");
  const title = [sourceName(f.name), printer(f.printer)?.name, count > 1 ? `Plate ${p.index} of ${count}` : ""].filter(Boolean).join(" · ");
  return `<div class="plate${thumb ? "" : " no-thumb"}">${thumb}<div>
      <div class="title">${esc(title)}</div>
      <div class="facts">${facts || "<span>No time or weight info</span>"}</div>
      ${sw ? `<div class="swatches">${sw}</div>` : ""}
    </div></div>`;
}

function slicedFileHtml(f, fallback = "") {
  const count = (f.plates || []).length;
  const sub = [f.printer_model || "Unknown printer", f.slicer, fmtSize(f.size)].filter(Boolean).map(esc).join(" · ");
  return `<div class="file">
    <div class="file-head">
      <span class="file-name">${esc(f.name)}</span>
      <button type="button" class="ghost icon-btn rename-btn" data-rename-file="${f.id}" data-file-name="${esc(f.name)}" title="Rename file">✎</button>
    </div>
    <div class="file-sub">${sub}${f.error ? ` · <span style="color:#c0392b">${esc(f.error)}</span>` : ""}</div>
    ${(f.plates || []).length ? `<div class="plates">${f.plates.map((p) => plateHtml(f, p, count, fallback)).join("")}</div>` : ""}
    <div class="file-actions">
      ${canPrintOn(f.printer) ? `<button class="small" data-print="${f.id}" data-printer="${f.printer}">Print on ${esc(printer(f.printer)?.name || f.printer)}</button>
        <span class="print-state" data-print-state="${esc(f.printer)}">${stateBadge(printerById(f.printer))}</span>` : ""}
      <a class="button small ghost" href="${fileUrl(f.id, true)}">Download</a></div>
  </div>`;
}

// ---------------------------------------------------------------- desktop slicers
// A web page can't start programs, so we hand the slicer a URL through its link
// handler. Snapmaker Orca downloads any http(s) link it is given. Bambu Studio only
// accepts MakerWorld links, so for it we go through the small "printpantry://"
// helper from scripts\install-slicer-links.cmd, which downloads the file and
// starts Bambu Studio with it.
const SLICERS = {
  snapmaker: { name: "Snapmaker Orca", link: (u) => `snapmaker-orca://open?file=${encodeURIComponent(u)}` },
  bambu: { name: "Bambu Studio", link: (u) => `printpantry://open?slicer=bambu&file=${encodeURIComponent(u)}` },
};
let PREFS = null;
async function loadPrefs() {
  if (!PREFS) PREFS = await api("/api/prefs").catch(() => ({ slicer: "snapmaker" }));
  return PREFS;
}
const slicerName = () => SLICERS[PREFS?.slicer || "snapmaker"].name;

async function openInSlicer(fileId, button) {
  const s = SLICERS[PREFS?.slicer] || SLICERS.snapmaker;
  if (button) button.disabled = true;
  try {
    const { path } = await api(`/api/files/${fileId}/link`, { method: "POST" });
    location.href = s.link(new URL(path, location.href).href);
    const tip = PREFS?.slicer === "bambu"
      ? `Opening in Bambu Studio. If nothing happens, run <code>scripts\\install-slicer-links.cmd</code> once on this PC, or use Download.`
      : `Opening in Snapmaker Orca. If nothing happens, check Snapmaker Orca is installed, or use Download.`;
    flash(tip);
  } catch (e) {
    flash(`Couldn't make a link: ${esc(e.message || e)}`);
  } finally {
    if (button) setTimeout(() => (button.disabled = false), 1500);
  }
}

function flash(html) {
  let el = document.getElementById("flash");
  if (!el) {
    el = document.createElement("div");
    el.id = "flash";
    el.className = "flash";
    el.setAttribute("role", "status");
    document.body.appendChild(el);
  }
  el.innerHTML = html;
  el.classList.add("show");
  clearTimeout(el._t);
  el._t = setTimeout(() => el.classList.remove("show"), 8000);
}

function slicerPicker() {
  return `<label class="slicer-pick">Open in
    <select id="slicer-pick">${Object.entries(SLICERS).map(([id, s]) =>
      `<option value="${id}" ${PREFS?.slicer === id ? "selected" : ""}>${s.name}</option>`).join("")}</select></label>`;
}

// "Slice for U1" (first, it's the main printer) and "Slice for P1S" buttons.
function sliceButtons(f) {
  return slicerPrinters().map((p, i) =>
    `<button class="small${i ? " ghost" : ""}" data-slice="${f.id}" data-printer="${p.id}">Slice for ${esc(p.name)}</button>`).join("");
}

function projectFileHtml(f) {
  return `<div class="file">
    <div class="file-head">
      <span class="file-name">${esc(f.name)}</span>
      <button type="button" class="ghost icon-btn rename-btn" data-rename-file="${f.id}" data-file-name="${esc(f.name)}" title="Rename 3MF file">✎</button>
    </div>
    <div class="file-sub">${[f.slicer, plural((f.plates || []).length || 1, "plate"), fmtSize(f.size)].filter(Boolean).map(esc).join(" · ")}</div>
    <div class="file-actions">
      ${sliceButtons(f)}
      <button class="small${slicerPrinters().length ? " ghost" : ""}" data-open="${f.id}">Open in<span class="slicer-name">${esc(slicerName())}</span></button>
      <a class="button small ghost" href="${fileUrl(f.id, true)}">Download</a>
    </div></div>`;
}

// OpenSCAD / FreeCAD sources: the Customize dialog renders STLs on the NAS.
let SHOW_FILE = null;
function parametricFileHtml(f) {
  const tool = f.kind === "scad" ? "OpenSCAD" : "FreeCAD";
  return `<div class="file">
    <div class="file-head"><span class="file-name">${esc(f.name)}</span></div>
    <div class="file-sub">${tool} · ${fmtSize(f.size)}</div>
    <div class="file-actions">
      <button class="small" data-customize="${f.id}" title="Change the parameters and make an STL on the NAS">Customize</button>
      <a class="button small ghost" href="${fileUrl(f.id, true)}">Download</a>
    </div></div>`;
}

// What history rows need from the rest of the UI.
async function historyCtx(models) {
  const printers = await loadPrinters();
  const byId = new Map((models || []).map((m) => [m.id, m]));
  return {
    printers: LIB?.printers || [],
    printerBadge,
    model: (id) => byId.get(id),
    canCamera: (id) => !!printers.find((p) => p.id === id && p.can_camera),
  };
}

async function modelPage(id) {
  if (!LIB) await refreshLibrary();
  const [m] = await Promise.all([api(`/api/models/${id}`), loadPrinters(), loadPrefs(), loadSlicer(), loadPricing()]);
  setTitle(m.name);
  const cat = LIB.categories.find((c) => c.id === m.category);
  const stls = m.files.filter((f) => f.kind === "stl");
  const projects = m.files.filter((f) => f.kind === "project3mf");
  const sliced = m.files.filter((f) => f.kind === "sliced3mf" || f.kind === "gcode");
  const images = m.files.filter((f) => f.kind === "image");
  const parametric = m.files.filter((f) => f.kind === "scad" || f.kind === "fcstd");
  // Newest generated STL first after a render, else the first one.
  const wanted = SHOW_FILE && stls.findIndex((f) => f.id === SHOW_FILE);
  if (wanted > 0) stls.unshift(...stls.splice(wanted, 1));
  SHOW_FILE = null;

  // Picture for print files without their own thumbnail.
  const plateFallback = m.cover ? coverHtml(m).replace(/^<span class="ph">.*$/s, "") : "";

  // Group print files by printer, known printers first.
  const groups = new Map();
  for (const p of LIB.printers) groups.set(p.id, []);
  groups.set("", []);
  for (const f of sliced) (groups.get(f.printer) || groups.get("")).push(f);
  const printPanels = [...groups.entries()].filter(([, fs]) => fs.length).map(([pid, fs]) => {
    const p = printer(pid);
    return `<section class="panel"><h2>${p ? `<span class="badge printer" style="--c:${p.color}">${esc(p.name)}</span>` : "Other printers"}
      <span class="n">${plural(fs.length, "file")}</span></h2>${fs.map((f) => slicedFileHtml(f, plateFallback)).join("")}</section>`;
  }).join("");

  const cats = (LIB.categories || []);
  view.innerHTML = `
    <div class="model-head-bar">
      <div class="crumbs"><a href="#/">All categories</a> / <a href="#/c/${encodeURIComponent(m.category)}">${esc(m.category)}</a></div>
      <div class="model-category-picker">
        <label for="cat-select" class="muted">Category:</label>
        <select id="cat-select" aria-label="Change category">
          ${cats.map((c) => `<option value="${esc(c.name)}"${c.name === m.category ? " selected" : ""}>${esc(c.name)}</option>`).join("")}
        </select>
        <button type="button" class="small ghost" id="btn-add-cat" title="Create a new category and move this model to it">+ New Category</button>
      </div>
    </div>
    <div class="model-title-row">
      <h1>${esc(m.name)}</h1>
      ${!m.path.startsWith("#") ? `<button type="button" class="ghost icon-btn rename-btn" id="btn-rename-model" title="Rename model" aria-label="Rename model">
        <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20h9M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z"/></svg>
      </button>` : ""}
    </div>
    <p class="sub">${esc(m.path.replace(/#.*/, ""))}${m.source ? ` · from <a href="${esc(m.source.url)}" target="_blank" rel="noopener">${esc(m.source.site)}</a>${m.source.author ? ` by ${esc(m.source.author)}` : ""}${m.source.license ? ` · ${esc(m.source.license)}` : ""}` : ""}</p>
    <div class="tags-edit" id="tags"></div>
    ${m.source?.summary ? `<details class="source-summary"><summary>Description from ${esc(m.source.site)}</summary><p>${esc(m.source.summary).replaceAll("\n", "<br>")}</p></details>` : ""}
    <div class="model-layout">
      <div class="viewer-wrap">
        <div class="viewer" id="viewer"></div>
        ${stls.length > 1 ? `<div class="parts" id="parts">${stls.map((f, i) =>
          `<button class="small" data-part="${f.id}" aria-pressed="${i === 0}">${esc(f.name)}</button>`).join("")}</div>` : ""}
        ${stls.length ? `<div class="file-actions" id="stl-actions"></div>` : ""}
      </div>
      <div>
        ${parametric.length ? `<section class="panel"><h2>Customizable <span class="n">${parametric.length > 1 ? plural(parametric.length, "source") : parametric[0].kind === "scad" ? "OpenSCAD" : "FreeCAD"}</span></h2>
          ${parametric.map(parametricFileHtml).join("")}</section>` : ""}
        ${stls.length ? `<section class="panel" id="sources"></section>` : ""}
        ${printPanels || `<section class="panel"><h2>Print files</h2><p class="empty">Nothing sliced yet. ${slicerPrinters().length
          ? "Use a Slice button to slice it here, or save"
          : "Slice the model and save"} the <code>.gcode.3mf</code> (P1S) or <code>.gcode</code> (U1) into this model's folder.</p></section>`}
        ${!stls.length && projects.length ? `<section class="panel"><h2>Editable source <span class="n">project 3MF</span></h2>
          <div class="panel-tools">${slicerPicker()}</div>${projects.map(projectFileHtml).join("")}</section>` : ""}
        <section class="panel" id="history" hidden></section>
        ${images.length ? `<section class="panel"><h2>Pictures</h2><div class="gallery">${images.map((f) =>
          `<a href="${fileUrl(f.id)}" target="_blank" rel="noopener"><img loading="lazy" alt="${esc(f.name)}" src="${fileUrl(f.id)}"></a>`).join("")}</div></section>` : ""}
      </div>
    </div>`;

  tagEditor(m, document.getElementById("tags"));
  hydrateStlCovers(view);
  historyCtx([m]).then((ctx) => historyPanel(document.getElementById("history"), m.id, ctx));
  view.querySelectorAll("[data-print]").forEach((b) =>
    b.addEventListener("click", () => openPrintDialog(b.dataset.print, b.dataset.printer)));
  view.querySelector(".model-layout").addEventListener("click", (e) => {
    const c = e.target.closest("[data-customize]");
    if (c) {
      const pf = m.files.find((x) => x.id === c.dataset.customize);
      if (pf) openCustomizeDialog(pf, async (job) => {
        SHOW_FILE = job.output_ids?.[0] || null;
        await refreshLibrary();
        route();
      });
      return;
    }
    const b = e.target.closest("[data-slice]");
    if (!b) return;
    const f = m.files.find((x) => x.id === b.dataset.slice);
    if (f) openSliceDialog(f, b.dataset.printer, async () => { await refreshLibrary(); route(); });
  });

  const catSelect = document.getElementById("cat-select");
  if (catSelect) {
    catSelect.onchange = async () => {
      const newCat = catSelect.value;
      if (!newCat || newCat === m.category) return;
      try {
        const r = await api(`/api/models/${m.id}/category`, {
          method: "PUT", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ category: newCat }),
        });
        await refreshLibrary();
        location.hash = `#/m/${r.id}`;
      } catch (err) {
        alert(err.message);
        catSelect.value = m.category;
      }
    };
  }

  const addCatBtn = document.getElementById("btn-add-cat");
  if (addCatBtn) {
    addCatBtn.onclick = async () => {
      const newCat = await promptDialog({
        title: "New Category", label: "Category name", value: "", placeholder: "e.g. Workshop", actionLabel: "Create & Move",
      });
      if (!newCat) return;
      try {
        const r = await api(`/api/models/${m.id}/category`, {
          method: "PUT", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ category: newCat }),
        });
        await refreshLibrary();
        location.hash = `#/m/${r.id}`;
      } catch (err) {
        alert(err.message);
      }
    };
  }

  const renameModelBtn = document.getElementById("btn-rename-model");
  if (renameModelBtn) {
    renameModelBtn.onclick = async () => {
      const newName = await promptDialog({
        title: "Rename Model", label: "Model name", value: m.name, actionLabel: "Rename",
      });
      if (!newName || newName === m.name) return;
      try {
        const r = await api(`/api/models/${m.id}/rename`, {
          method: "PUT", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name: newName }),
        });
        await refreshLibrary();
        location.hash = `#/m/${r.id}`;
      } catch (err) {
        alert(err.message);
      }
    };
  }

  view.addEventListener("click", async (e) => {
    const btn = e.target.closest("[data-rename-file]");
    if (!btn) return;
    const fileId = btn.dataset.renameFile;
    const oldName = btn.dataset.fileName;
    const newName = await promptDialog({
      title: "Rename File", label: "File name", value: oldName, actionLabel: "Rename",
    });
    if (!newName || newName === oldName) return;
    try {
      await api(`/api/files/${fileId}/rename`, {
        method: "PUT", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: newName }),
      });
      await refreshLibrary();
      route();
    } catch (err) {
      alert(err.message);
    }
  });

  const holder = document.getElementById("viewer");
  if (!stls.length) {
    const pic = m.cover && m.cover.type !== "stl"
      ? `<img class="preview-img" alt="" src="/api/files/${m.cover.file}/thumb${m.cover.plate ? `?plate=${m.cover.plate}` : ""}">`
      : `<div class="loading">${parametric.length ? "Press Customize to make an STL" : "No STL in this model"}</div>`;
    holder.innerHTML = pic;
    return;
  }

  holder.innerHTML = `<div class="loading" id="viewer-loading">Loading 3D view...</div>
    <div class="overlay"><span class="dims" id="dims"></span>
      <span class="tools"><button class="small ghost" id="spin" title="Start or stop spinning">${icon("rotate-3d")}Spin</button>
      <button class="small ghost" id="reset" title="Reset the camera">Reset</button></span></div>`;
  const viewer = createViewer(holder, {
    color: cat?.color || "#e9a23b",
    onInfo: (i) => {
      document.getElementById("dims").textContent =
        `${i.x.toFixed(1)} × ${i.y.toFixed(1)} × ${i.z.toFixed(1)} mm · ${Math.round(i.triangles).toLocaleString()} triangles`;
    },
  });
  cleanup = () => viewer.dispose();
  document.getElementById("spin").onclick = () => viewer.toggleSpin();
  document.getElementById("reset").onclick = () => viewer.resetView();

  const actions = document.getElementById("stl-actions");
  const sources = document.getElementById("sources");
  const links = m.stl_links || {};
  const linked = new Set(Object.values(links).flatMap((l) => l.projects));
  const unlinked = projects.filter((f) => !linked.has(f.id));
  function showSources(f) {
    const mine = (links[f.id]?.projects || []).map((pid) => projects.find((p) => p.id === pid)).filter(Boolean);
    sources.innerHTML = `<h2>Editable source <span class="n">${stls.length > 1 ? `for ${esc(f.name)}${mine.length ? " · " : ""}` : ""}${mine.length ? plural(mine.length, "project 3MF") : ""}</span></h2>
      <div class="panel-tools">${slicerPicker()}</div>
      ${mine.length ? mine.map(projectFileHtml).join("")
        : `<p class="empty">No project 3MF for this STL yet. Press <strong>Create New Source</strong> to open it in your slicer,
           then save the project next to the STL.</p>`}
      ${unlinked.length ? `<h3 class="sub-head">Other project files in this model</h3>${unlinked.map(projectFileHtml).join("")}` : ""}`;
  }
  async function show(f) {
    const loading = document.getElementById("viewer-loading");
    loading.style.display = "grid";
    loading.textContent = `Loading ${f.name} (${fmtSize(f.size)})...`;
    actions.innerHTML = `<button class="small" data-open="${f.id}" title="Open this STL in your slicer to make a new project 3MF">Create New Source
        <span class="muted">in <span class="slicer-name">${esc(slicerName())}</span></span></button>
      <button type="button" class="small ghost" data-rename-file="${f.id}" data-file-name="${esc(f.name)}" title="Rename this STL file">Rename STL</button>
      ${links[f.id]?.projects?.length ? "" : sliceButtons(f).replaceAll('class="small"', 'class="small ghost"')}
      <a class="button small ghost" href="${fileUrl(f.id, true)}">Download ${esc(f.name)}</a>`;
    showSources(f);
    try {
      await viewer.load(fileUrl(f.id));
      loading.style.display = "none";
    } catch (e) {
      loading.textContent = `Couldn't open ${f.name}: ${e.message || e}`;
    }
  }
  document.querySelectorAll("[data-part]").forEach((b) =>
    b.addEventListener("click", () => {
      document.querySelectorAll("[data-part]").forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
      show(stls.find((f) => f.id === b.dataset.part));
    }));
  show(stls[0]);
}

// ---------------------------------------------------------------- category editor
function editCategory(name) {
  const cat = LIB.categories.find((c) => c.id === name);
  if (!cat) return;
  let chosenIcon = cat.icon, chosenColor = cat.color;
  const colors = ["#E4572E", "#F2A900", "#4CB944", "#2E86AB", "#7B61FF", "#E84A9B", "#17BEBB", "#F17105", "#6A994E", "#3D5A80", "#8D6E63", "#546E7A"];
  const dlg = document.createElement("dialog");
  dlg.innerHTML = `<h2>${esc(cat.name)}</h2>
    <div class="dialog-row"><strong>Colour</strong><div class="colors">${colors.map((c) =>
      `<button type="button" style="--c:${c}" data-color="${c}" aria-label="${c}" aria-pressed="${c.toLowerCase() === chosenColor.toLowerCase()}"></button>`).join("")}</div></div>
    <strong>Icon</strong>
    <div class="icon-grid">${Object.keys(ICONS).sort().map((n) =>
      `<button type="button" data-icon="${n}" title="${n}" aria-label="${n}" aria-pressed="${n === chosenIcon}">${icon(n)}</button>`).join("")}</div>
    <div class="dialog-actions" style="margin-top:16px"><button type="button" class="ghost" data-close>Cancel</button><button type="button" data-save>Save</button></div>`;
  document.body.appendChild(dlg);
  dlg.addEventListener("click", async (e) => {
    const b = e.target.closest("button");
    if (!b) return;
    if (b.dataset.icon) {
      chosenIcon = b.dataset.icon;
      dlg.querySelectorAll("[data-icon]").forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
    } else if (b.dataset.color) {
      chosenColor = b.dataset.color;
      dlg.querySelectorAll("[data-color]").forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
    } else if ("close" in b.dataset) {
      dlg.close();
    } else if ("save" in b.dataset) {
      b.disabled = true;
      try {
        await api(`/api/categories/${encodeURIComponent(name)}`, {
          method: "PUT", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ icon: chosenIcon, color: chosenColor }),
        });
        dlg.close();
        route();
      } catch (err) { alert(err.message); b.disabled = false; }
    }
  });
  dlg.addEventListener("close", () => dlg.remove());
  dlg.showModal();
}

// ---------------------------------------------------------------- import from Printables / MakerWorld
function openImport(prefill = "", resume = null) {
  const dlg = document.createElement("dialog");
  dlg.className = "import-dialog";
  document.body.appendChild(dlg);
  let plan = null, timer = null;
  dlg.addEventListener("close", () => { clearTimeout(timer); dlg.remove(); });

  function step1(err = "") {
    dlg.innerHTML = `<h2>Import a model</h2>
      <form id="imp-form">
        <label for="imp-url">Printables or MakerWorld link</label>
        <input id="imp-url" type="url" required placeholder="https://www.printables.com/model/..." value="${esc(prefill)}">
        ${err ? `<div class="notice error">${esc(err)}</div>` : ""}
        <div class="dialog-actions" style="margin-top:16px">
          <button type="button" class="ghost" data-mw>Sign in to MakerWorld</button>
          <span style="flex:1"></span>
          <button type="button" class="ghost" data-close>Cancel</button><button type="submit">Look up</button></div>
      </form>`;
    const input = dlg.querySelector("#imp-url");
    input.focus();
    dlg.querySelector("[data-close]").onclick = () => dlg.close();
    dlg.querySelector("[data-mw]").onclick = () => mwLogin();
    dlg.querySelector("form").onsubmit = async (e) => {
      e.preventDefault();
      prefill = input.value.trim();
      dlg.querySelector("button[type=submit]").disabled = true;
      dlg.querySelector("button[type=submit]").textContent = "Looking up...";
      try {
        plan = await api("/api/import/preview", {
          method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ url: prefill }),
        });
        step2();
      } catch (e2) { step1(e2.message || String(e2)); }
    };
  }

  // MakerWorld uses the Bambu Lab account: email + password, then maybe an emailed code.
  async function mwLogin(back = step1) {
    const st = await api("/api/import/settings").catch(() => ({}));
    let pending = null; // {step, message, tfa_key}
    let email0 = st.makerworld_account || "";
    function draw(err = "") {
      dlg.innerHTML = `<h2>Sign in to MakerWorld</h2>
        <p class="muted">MakerWorld only gives files to signed-in accounts. Use your MakerWorld / Bambu Lab account.
        Only the sign-in token is kept (on your NAS), never the password.</p>
        ${st.makerworld_token ? `<div class="notice">Signed in${email0 ? ` as ${esc(email0)}` : ""}. Sign in again if downloads stop working.</div>` : ""}
        <form id="mw-form">
          <label for="mw-email">Email</label>
          <input id="mw-email" type="email" autocomplete="username" required value="${esc(email0)}" ${pending ? "readonly" : ""}>
          ${pending ? `<label for="mw-code">${pending.step === "tfa" ? "Authenticator code" : "Code from the email"}</label>
              <input id="mw-code" inputmode="numeric" autocomplete="one-time-code" required>
              <p class="hint">${esc(pending.message)}</p>`
            : `<label for="mw-pass">Password</label><input id="mw-pass" type="password" autocomplete="current-password" required>`}
          ${err ? `<div class="notice error">${esc(err)}</div>` : ""}
          <div class="dialog-actions" style="margin-top:16px">
            ${st.makerworld_token ? `<button type="button" class="ghost" data-clear>Sign out</button>` : ""}
            <button type="button" class="ghost" data-paste>Paste a token instead</button><span style="flex:1"></span>
            <button type="button" class="ghost" data-back>Back</button><button type="submit">${pending ? "Finish" : "Sign in"}</button></div>
        </form>`;
      (dlg.querySelector("#mw-code") || dlg.querySelector(email0 ? "#mw-pass" : "#mw-email")).focus();
      dlg.querySelector("[data-back]").onclick = () => back();
      dlg.querySelector("[data-paste]").onclick = () => pasteToken(back);
      const clear = dlg.querySelector("[data-clear]");
      if (clear) clear.onclick = async () => {
        await api("/api/import/settings", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ makerworld_token: "" }) });
        flash("Signed out of MakerWorld."); back();
      };
      dlg.querySelector("form").onsubmit = async (e) => {
        e.preventDefault();
        const btn = dlg.querySelector("button[type=submit]");
        btn.disabled = true; btn.textContent = "Signing in...";
        const email = dlg.querySelector("#mw-email").value.trim();
        email0 = email;
        const body = { email };
        if (!pending) body.password = dlg.querySelector("#mw-pass").value;
        else if (pending.step === "tfa") Object.assign(body, { tfa_key: pending.tfa_key, tfa_code: dlg.querySelector("#mw-code").value });
        else body.code = dlg.querySelector("#mw-code").value;
        try {
          const r = await api("/api/import/makerworld-login", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
          if (r.step) { pending = r; draw(); return; }
          flash(`Signed in to MakerWorld as ${esc(email)}.`);
          back();
        } catch (err) { draw(err.message || String(err)); }
      };
    }
    draw();
  }

  function pasteToken(back) {
    dlg.innerHTML = `<h2>Paste a MakerWorld token</h2>
      <p class="muted">Only if signing in doesn't work: log in at makerworld.com, press F12, open Application (Chrome/Edge)
      or Storage (Firefox) &gt; Cookies &gt; https://makerworld.com and copy the cookie named <code>token</code>.</p>
      <input id="mw-token" type="password" autocomplete="off" placeholder="Paste the token">
      <div class="dialog-actions" style="margin-top:16px"><button type="button" class="ghost" data-back>Back</button><button type="button" data-save>Save</button></div>`;
    dlg.querySelector("[data-back]").onclick = () => mwLogin(back);
    dlg.querySelector("[data-save]").onclick = async () => {
      await api("/api/import/settings", { method: "PUT", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ makerworld_token: dlg.querySelector("#mw-token").value.trim() }) });
      flash("MakerWorld token saved."); back();
    };
  }

  // Look the link up again (after signing in) and show the preview.
  async function relook() {
    try {
      plan = await api("/api/import/preview", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ url: prefill }) });
      step2();
    } catch (e) { step1(e.message || String(e)); }
  }

  function step2() {
    const cats = plan.categories || [];
    const pick = plan.suggested_category || "";
    const total = plan.files.reduce((n, f) => n + (f.size || 0), 0);
    dlg.innerHTML = `<h2>Import from ${esc(plan.site)}</h2>
      <div style="display:flex;gap:14px;align-items:flex-start">
        ${plan.pictures[0] ? `<img src="${esc(plan.pictures[0])}" alt="" style="width:110px;height:110px;object-fit:cover;border-radius:12px;flex:none" referrerpolicy="no-referrer">` : ""}
        <div><strong>${esc(plan.title)}</strong>
          <div class="muted">${[plan.author && `by ${esc(plan.author)}`, esc(plan.license), plan.site_categories.length ? esc(plan.site_categories.join(" / ")) : ""].filter(Boolean).join(" · ")}</div>
          <div class="muted">${plan.files.length ? `${plural(plan.files.length, "file")}${total ? `, ${fmtSize(total)}` : ""}` : "No files"} · ${plural(plan.picture_count, "picture")}</div>
          ${plan.tags.length ? `<div class="card-tags">${plan.tags.slice(0, 12).map((t) => `<span class="tag">${esc(t)}</span>`).join("")}</div>` : ""}
        </div>
      </div>
      ${plan.files.length ? `<ul class="files">${plan.files.map((f) => `<li>${esc(f.name)}${f.size ? ` (${fmtSize(f.size)})` : ""}</li>`).join("")}</ul>` : ""}
      ${plan.profile ? `<p class="muted">Print profile: <strong>${esc(plan.profile)}</strong></p>` : ""}
      ${plan.note ? `<div class="notice">${esc(plan.note)}${plan.needs_login ? `<div style="margin-top:8px"><button type="button" class="small" data-signin>Sign in to MakerWorld</button></div>` : ""}</div>` : ""}
      <label for="imp-cat">Category</label>
      <select id="imp-cat">
        ${pick ? "" : `<option value="" selected disabled>Pick a category</option>`}
        ${cats.map((c) => `<option ${c === pick ? "selected" : ""}>${esc(c)}</option>`).join("")}
        <option value="__new">New category...</option></select>
      <input id="imp-newcat" placeholder="New category name" style="display:none;margin-top:6px"
        value="${esc(plan.site_categories[plan.site_categories.length - 1] || "")}">
      <label for="imp-name">Model name <span class="hint">(the folder it goes in)</span></label>
      <input id="imp-name" value="${esc(plan.title)}">
      <div class="dialog-actions" style="margin-top:16px">
        <button type="button" class="ghost" data-back>Back</button><button type="button" data-go>Import</button></div>`;
    const sel = dlg.querySelector("#imp-cat"), newcat = dlg.querySelector("#imp-newcat");
    sel.onchange = () => { newcat.style.display = sel.value === "__new" ? "" : "none"; if (sel.value === "__new") newcat.focus(); };
    dlg.querySelector("[data-back]").onclick = () => step1();
    const signin = dlg.querySelector("[data-signin]");
    if (signin) signin.onclick = () => mwLogin(relook);
    dlg.querySelector("[data-go]").onclick = async (e) => {
      const category = sel.value === "__new" ? newcat.value.trim() : sel.value;
      if (!category) { sel.focus(); return; }
      const btn = e.currentTarget;
      btn.disabled = true;
      btn.textContent = "Starting...";
      try {
        const job = await api("/api/import", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ url: plan.url, category, name: dlg.querySelector("#imp-name").value.trim() }),
        });
        watchImports();
        progress(job);
      } catch (err) {
        // Show it in the dialog: a toast would sit behind the modal.
        btn.disabled = false;
        btn.textContent = "Import";
        let box = dlg.querySelector(".import-error");
        if (!box) {
          box = document.createElement("div");
          box.className = "notice error import-error";
          dlg.querySelector(".dialog-actions").before(box);
        }
        box.textContent = `Couldn't start the import: ${err.message || err}`;
      }
    };
  }

  function progress(job) {
    const pct = Math.round((job.progress ?? 0) * 100);
    const bytes = job.phase === "files" && job.bytes_done
      ? `${fmtSize(job.bytes_done)}${job.bytes_total ? ` of ${fmtSize(job.bytes_total)}` : ""}` : "";
    const secs = Math.round(((job.finished || Date.now() / 1000) - job.started));
    const doneHtml = () => `<p>Saved to <code>${esc(job.folder)}</code>${job.done_files.length ? ` with ${plural(job.done_files.length, "file")}` : ""}${job.pictures_total ? ` and ${plural(job.pictures_total, "picture")}` : ""}${job.skipped.length ? ` (${plural(job.skipped.length, "file")} already there)` : ""} in ${secs}s.</p>
      ${job.note ? `<div class="notice">${esc(job.note)}</div>` : ""}`;
    dlg.innerHTML = `<h2>${job.state === "done" ? "Imported" : job.state === "error" ? "Import failed" : "Importing..."}</h2>
      <p><strong>${esc(job.title || plan?.title || "")}</strong></p>
      ${job.state === "running" ? `<div class="progress" role="progressbar" aria-valuenow="${pct}" aria-valuemin="0" aria-valuemax="100"><span style="width:${pct}%"></span></div>
        <p class="muted progress-text"><span>${esc(job.step)}</span><span>${bytes ? `${bytes} · ` : ""}${pct}% · ${secs}s</span></p>
        <p class="muted">You can close this; the import keeps going and the Import button shows its progress.</p>` : ""}
      ${job.state === "error" ? `<div class="notice error">${esc(job.error)}</div>` : ""}
      ${job.state === "done" ? doneHtml() : ""}
      <div class="dialog-actions" style="margin-top:16px">
        <button type="button" class="ghost" data-close>${job.state === "running" ? "Hide" : "Close"}</button>
        ${job.state === "done" ? `<button type="button" data-another class="ghost">Import another</button><button type="button" data-open-model>Open model</button>` : ""}</div>`;
    dlg.querySelector("[data-close]").onclick = () => dlg.close();
    if (job.state === "running") {
      timer = setTimeout(async () => {
        try { progress(await api(`/api/import/${job.id}`)); } catch { timer = setTimeout(() => progress(job), 2000); }
      }, 700);
      return;
    }
    if (job.state === "done") {
      refreshLibrary().catch(() => {});
      dlg.querySelector("[data-another]").onclick = () => { prefill = ""; step1(); };
      dlg.querySelector("[data-open-model]").onclick = () => { dlg.close(); location.hash = `#/m/${job.model_id}`; };
    }
  }

  if (resume) progress(resume); else step1();
  dlg.showModal();
}

// The Import button shows a running import's progress, and reopens it when clicked.
const importBtn = document.getElementById("import");
let importWatch = null, runningImport = null;
async function watchImports() {
  clearTimeout(importWatch);
  try {
    const { jobs } = await api("/api/import");
    runningImport = jobs.find((j) => j.state === "running") || null;
  } catch { runningImport = null; }
  importBtn.textContent = runningImport ? `Importing ${Math.round(runningImport.progress * 100)}%` : "Import";
  importBtn.classList.toggle("busy", !!runningImport);
  if (runningImport) importWatch = setTimeout(watchImports, 1500);
}

importBtn.addEventListener("click", () => openImport("", runningImport));
watchImports();

// ---------------------------------------------------------------- router
async function route() {
  if (cleanup) { cleanup(); cleanup = null; }
  const hash = decodeURIComponent(location.hash.slice(1) || "/");
  const [, kind, ...rest] = hash.split("/");
  const arg = rest.join("/");
  document.querySelectorAll(".top-nav a").forEach((a) => a.classList.remove("active"));
  if (kind === "printers") document.getElementById("nav-printers")?.classList.add("active");
  else if (kind === "history") document.getElementById("nav-history")?.classList.add("active");
  else if (kind === "spools") document.getElementById("nav-spools")?.classList.add("active");
  else if (!kind || kind === "c" || kind === "m") document.getElementById("nav-library")?.classList.add("active");
  try {
    if (kind === "c" && arg) await categoryPage(arg);
    else if (kind === "m" && arg) await modelPage(arg);
    else if (kind === "search" && arg) await searchPage(arg);
    else if (kind === "printers") cleanup = await printersPage(view);
    else if (kind === "history") {
      setTitle("History");
      if (!LIB) await refreshLibrary();
      const models = (await api("/api/models?limit=1000")).models;
      cleanup = await historyPage(view, await historyCtx(models));
    }
    else if (kind === "spools") await spoolsPage(view, await loadPrinters());
    else await homePage();
  } catch (e) {
    if (e.message === "login") return;
    view.innerHTML = `<div class="notice error">${esc(e.message || e)} · <a href="#/">Back to all categories</a></div>`;
  }
  if (!location.hash.startsWith("#/search")) searchInput.value = "";
  view.focus({ preventScroll: true });
}

document.getElementById("search-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const q = searchInput.value.trim();
  location.hash = q ? `#/search/${encodeURIComponent(q)}` : "#/";
});

document.getElementById("rescan").addEventListener("click", async (e) => {
  const b = e.currentTarget;
  b.disabled = true;
  b.textContent = "Scanning...";
  try {
    await api("/api/rescan", { method: "POST" });
    const before = LIB?.scanned_at || 0;
    for (let i = 0; i < 120; i++) {
      await new Promise((r) => setTimeout(r, 1000));
      await refreshLibrary();
      if (!LIB.scanning && LIB.scanned_at > before) break;
    }
    await route();
  } finally {
    b.disabled = false;
    b.textContent = "Rescan";
  }
});

// Slicer buttons and the slicer picker live in panels that get re-rendered.
view.addEventListener("click", (e) => {
  const b = e.target.closest("[data-open]");
  if (b) openInSlicer(b.dataset.open, b);
});
view.addEventListener("change", async (e) => {
  if (e.target.id !== "slicer-pick") return;
  PREFS = await api("/api/prefs", {
    method: "PUT", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ slicer: e.target.value }),
  }).catch(() => ({ ...PREFS, slicer: e.target.value }));
  view.querySelectorAll(".slicer-name").forEach((el) => (el.textContent = slicerName()));
});

window.addEventListener("hashchange", () => { route(); window.scrollTo(0, 0); });

// Keep the "Printing 42%" badges next to Print buttons current.
window.addEventListener("printers:update", () => {
  document.querySelectorAll("[data-print-state]").forEach((el) => {
    el.innerHTML = stateBadge(printerById(el.dataset.printState));
  });
});

(async () => {
  ICONS = await fetch("/static/icons.json").then((r) => r.json()).catch(() => ({}));
  route();
  startPrinterDock(document.getElementById("printer-dock"));
})();
