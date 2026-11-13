// Print Pantry single-page UI. Routes:
//   #/                 category tiles (+ recently changed)
//   #/c/<category>     models in a category
//   #/m/<model id>     model page with 3D viewer and print files
//   #/search/<query>   search results
//   #/printers         live printer status and cameras
import { createViewer, renderThumbnail } from "./viewer.js";
import { canPrintOn, loadPrinters, openPrintDialog, printersPage } from "./printers.js";
import { loadSlicer, openSliceDialog, slicerPrinters } from "./slicing.js";
import { swatchHtml } from "./filament.js";

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
    <div class="section-head"><h2>Recently changed</h2></div>
    <div class="cards" id="recent"></div>`;
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
  const res = await api(`/api/models?q=${encodeURIComponent(q)}`);
  view.innerHTML = `<div class="crumbs"><a href="#/">All categories</a></div>
    <h1>Results for "${esc(q)}"</h1><p class="sub">${plural(res.total, "model")}</p>
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
  const facts = [fmtTime(p.seconds), fmtGrams(p.grams)].filter(Boolean).map((t) => `<span>${t}</span>`).join("");
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
    <div class="file-head"><span class="file-name">${esc(f.name)}</span></div>
    <div class="file-sub">${sub}${f.error ? ` · <span style="color:#c0392b">${esc(f.error)}</span>` : ""}</div>
    ${(f.plates || []).length ? `<div class="plates">${f.plates.map((p) => plateHtml(f, p, count, fallback)).join("")}</div>` : ""}
    <div class="file-actions">
      ${canPrintOn(f.printer) ? `<button class="small" data-print="${f.id}" data-printer="${f.printer}">Print on ${esc(printer(f.printer)?.name || f.printer)}</button>` : ""}
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
    <div class="file-head"><span class="file-name">${esc(f.name)}</span></div>
    <div class="file-sub">${[f.slicer, plural((f.plates || []).length || 1, "plate"), fmtSize(f.size)].filter(Boolean).map(esc).join(" · ")}</div>
    <div class="file-actions">
      ${sliceButtons(f)}
      <button class="small${slicerPrinters().length ? " ghost" : ""}" data-open="${f.id}">Open in<span class="slicer-name">${esc(slicerName())}</span></button>
      <a class="button small ghost" href="${fileUrl(f.id, true)}">Download</a>
    </div></div>`;
}

async function modelPage(id) {
  if (!LIB) await refreshLibrary();
  const [m] = await Promise.all([api(`/api/models/${id}`), loadPrinters(), loadPrefs(), loadSlicer()]);
  setTitle(m.name);
  const cat = LIB.categories.find((c) => c.id === m.category);
  const stls = m.files.filter((f) => f.kind === "stl");
  const projects = m.files.filter((f) => f.kind === "project3mf");
  const sliced = m.files.filter((f) => f.kind === "sliced3mf" || f.kind === "gcode");
  const images = m.files.filter((f) => f.kind === "image");

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

  view.innerHTML = `
    <div class="crumbs"><a href="#/">All categories</a> / <a href="#/c/${encodeURIComponent(m.category)}">${esc(m.category)}</a></div>
    <h1>${esc(m.name)}</h1>
    <p class="sub">${esc(m.path.replace(/#.*/, ""))}</p>
    <div class="model-layout">
      <div class="viewer-wrap">
        <div class="viewer" id="viewer"></div>
        ${stls.length > 1 ? `<div class="parts" id="parts">${stls.map((f, i) =>
          `<button class="small" data-part="${f.id}" aria-pressed="${i === 0}">${esc(f.name)}</button>`).join("")}</div>` : ""}
        ${stls.length ? `<div class="file-actions" id="stl-actions"></div>` : ""}
      </div>
      <div>
        ${stls.length ? `<section class="panel" id="sources"></section>` : ""}
        ${printPanels || `<section class="panel"><h2>Print files</h2><p class="empty">Nothing sliced yet. ${slicerPrinters().length
          ? "Use a Slice button to slice it here, or save"
          : "Slice the model and save"} the <code>.gcode.3mf</code> (P1S) or <code>.gcode</code> (U1) into this model's folder.</p></section>`}
        ${!stls.length && projects.length ? `<section class="panel"><h2>Editable source <span class="n">project 3MF</span></h2>
          <div class="panel-tools">${slicerPicker()}</div>${projects.map(projectFileHtml).join("")}</section>` : ""}
        ${images.length ? `<section class="panel"><h2>Pictures</h2><div class="gallery">${images.map((f) =>
          `<a href="${fileUrl(f.id)}" target="_blank" rel="noopener"><img loading="lazy" alt="${esc(f.name)}" src="${fileUrl(f.id)}"></a>`).join("")}</div></section>` : ""}
      </div>
    </div>`;

  hydrateStlCovers(view);
  view.querySelectorAll("[data-print]").forEach((b) =>
    b.addEventListener("click", () => openPrintDialog(b.dataset.print, b.dataset.printer)));
  view.querySelector(".model-layout").addEventListener("click", (e) => {
    const b = e.target.closest("[data-slice]");
    if (!b) return;
    const f = m.files.find((x) => x.id === b.dataset.slice);
    if (f) openSliceDialog(f, b.dataset.printer, async () => { await refreshLibrary(); route(); });
  });

  const holder = document.getElementById("viewer");
  if (!stls.length) {
    const pic = m.cover && m.cover.type !== "stl"
      ? `<img class="preview-img" alt="" src="/api/files/${m.cover.file}/thumb${m.cover.plate ? `?plate=${m.cover.plate}` : ""}">`
      : `<div class="loading">No STL in this model</div>`;
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

// ---------------------------------------------------------------- router
async function route() {
  if (cleanup) { cleanup(); cleanup = null; }
  const hash = decodeURIComponent(location.hash.slice(1) || "/");
  const [, kind, ...rest] = hash.split("/");
  const arg = rest.join("/");
  try {
    if (kind === "c" && arg) await categoryPage(arg);
    else if (kind === "m" && arg) await modelPage(arg);
    else if (kind === "search" && arg) await searchPage(arg);
    else if (kind === "printers") cleanup = await printersPage(view);
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

(async () => {
  ICONS = await fetch("/static/icons.json").then((r) => r.json()).catch(() => ({}));
  route();
})();
