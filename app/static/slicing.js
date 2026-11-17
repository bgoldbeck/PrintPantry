// "Slice for U1 / P1S" dialog. The slicer runs on the NAS (OrcaSlicer's
// command line in its own container); new files land in the model's
// "sliced" folder and show up under Print files.
import { printerById, stateBadge } from "./printers.js";
import { createPartsViewer } from "./viewer.js";
import { createProcessPanel } from "./process.js";
const esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

async function api(path, opts) {
  const res = await fetch(path, opts);
  if (res.status === 401) { location.href = "/login"; throw new Error("login"); }
  if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText);
  return res.json();
}

let STATUS = null;
export async function loadSlicer() {
  if (STATUS?.available) return STATUS;
  try { STATUS = await api("/api/slicer"); } catch { STATUS = { available: false }; }
  return STATUS;
}
export const slicerPrinters = () => (STATUS?.available ? STATUS.printers || [] : []);

const option = (value, label, selected) =>
  `<option value="${esc(value)}"${selected ? " selected" : ""}>${esc(label ?? value)}</option>`;
// The last choices per printer, kept in this browser.
const LAST_KEY = "pp.slice.last";
function loadLast(printerId) {
  try { return JSON.parse(localStorage.getItem(LAST_KEY) || "{}")[printerId] || {}; } catch { return {}; }
}
function saveLast(printerId, choice) {
  try {
    const all = JSON.parse(localStorage.getItem(LAST_KEY) || "{}");
    const prev = all[printerId] || {};
    all[printerId] = { ...choice, byType: { ...(prev.byType || {}), ...choice.byType } };
    localStorage.setItem(LAST_KEY, JSON.stringify(all));
  } catch { /* private mode: nothing to remember */ }
}

const swatch = (c) => (c ? `<i class="dot" style="--c:${esc(c)}"></i>` : "");

// Preview colours for filament slots nobody has a colour for.
const PALETTE = ["#E9A23B", "#3B82E9", "#E94B3B", "#3BBF6A", "#9B59D0", "#E9D63B", "#3BD0D0", "#E93BA8"];
// Build plate sizes (mm) for the preview.
const BEDS = { u1: [270, 270], p1s: [256, 256] };
const mm = (v) => (v >= 10 ? Math.round(v) : Math.round(v * 10) / 10);
const MAX_COPIES = 200;

// Shelf-pack ``copies`` sets of footprints ({g, w, d} mm) on the bed, the way
// the slicer's arrange would at least manage; null if they don't fit. Returns
// {c, g, x, y} per piece: centres in mm from the middle of the bed (y = back).
export function packCopies(rects, copies, bed, gap = 8, edge = 3) {
  const W = bed[0] - 2 * edge, D = bed[1] - 2 * edge;
  const all = [];
  for (let c = 0; c < copies; c++) rects.forEach((r) => all.push({ c, g: r.g, w: r.w, d: r.d }));
  all.sort((a, b) => b.d - a.d || b.w - a.w || a.c - b.c);
  let x = 0, y = 0, row = 0;
  for (const r of all) {
    if (r.w > W || r.d > D) return null;
    if (x > 0 && x + r.w > W) { y += row + gap; x = 0; row = 0; }
    if (y + r.d > D) return null;
    r.x = x + r.w / 2; r.y = y + r.d / 2;
    x += r.w + gap;
    row = Math.max(row, r.d);
  }
  // Centre the whole layout on the bed.
  const x1 = Math.max(...all.map((r) => r.x + r.w / 2)), y1 = Math.max(...all.map((r) => r.y + r.d / 2));
  all.forEach((r) => { r.x -= x1 / 2; r.y = y1 / 2 - r.y; });
  return all;
}
export function maxCopies(rects, bed) {
  if (!rects.length || !bed || !packCopies(rects, 1, bed)) return 1;
  let n = 1;
  while (n < MAX_COPIES && packCopies(rects, n + 1, bed)) n++;
  return n;
}

// "Slice for ..." on an STL of a model that has other STLs, or on a 3MF with
// several objects: each object (or STL, or separate body inside an STL) can
// print with its own toolhead. ctx.siblings: the model's other STLs.
export async function openSliceDialog(file, printerId, onDone, ctx = {}) {
  const dlg = document.createElement("dialog");
  dlg.className = "slice-dialog";
  dlg.innerHTML = `<h2>Getting slicer settings...</h2>`;
  document.body.appendChild(dlg);
  let timer = null;
  let viewer = null;
  dlg.addEventListener("close", () => { clearTimeout(timer); viewer?.dispose(); dlg.remove(); });
  dlg.showModal();

  let o;
  try {
    o = await api(`/api/slicer/options?file_id=${encodeURIComponent(file.id)}&printer=${encodeURIComponent(printerId)}`);
  } catch (e) {
    dlg.innerHTML = `<h2>Can't slice right now</h2><div class="notice error">${esc(e.message)}</div>
      <div class="dialog-actions"><button class="ghost" data-close>Close</button></div>`;
    dlg.querySelector("[data-close]").onclick = () => dlg.close();
    return;
  }

  const proj = o.project;
  const last = loadLast(printerId);
  const has = (list, v) => v && list.includes(v);
  const machineNames = o.machines.map((m) => m.name);
  const d = { ...o.defaults };
  // "Start from": the author's profile (the 3MF's own settings) or one of our
  // process presets. On another printer the author's settings go on top of
  // that printer's process preset.
  const hasOwn = proj.kind === "3mf" && !!proj.printer_model;
  d.start = hasOwn ? "author" : "presets";
  if (hasOwn && (last.start === "author" || last.start === "presets")) d.start = last.start;
  if (has(machineNames, last.machine)) d.machine = last.machine;
  if (has((o.processes[d.machine] || {}).names || [], last.process)) d.process = last.process;
  // A remembered plate counts only from the time the presets had temperatures for every
  // plate (bedV 2); before that, Smooth PEI was often forced on the U1's textured plate.
  if (last.bedV === 2 && has(o.bed_types.map((b) => b.name), last.bed_type)) d.bed_type = last.bed_type;
  const slots = proj.filaments.length ? proj.filaments.map((f) => ({ ...f })) : [{ type: "PLA", colour: "" }];
  let baseCount = slots.length;
  const tooMany = slots.length > o.printer.max_filaments;
  const listed = slots.length;  // filaments the project lists (before dropping unused ones)
  const isU1 = printerId === "u1";
  const heads = printerById(printerId)?.status?.toolheads || [];
  const head = (i) => heads.find((h) => h.index === i);
  const headColour = (i) => { const h = head(i); return h && h.loaded !== false && h.color ? h.color : ""; };
  const slotColour = (i) => headColour(i) || slots[i]?.colour || PALETTE[i % PALETTE.length];
  const slotTag = (i) => (isU1 ? `T${i + 1}` : `${i + 1}`);
  const siblings = (ctx.siblings || []).filter((s) => s.id !== file.id);
  // Per-object state, filled by loadObjects().
  const objs = { items: [], assign: {}, together: new Set(), keep: null, active: false, brush: 1, overlapping: false,
    compact: false, copies: 1, max: 1 };
  const plates = proj.plates > 1
    ? `<label class="field"><span>Plates</span><select data-plate>${option(0, `All ${proj.plates} plates`, true)}${
      Array.from({ length: proj.plates }, (_, i) => option(i + 1, `Plate ${i + 1} only`)).join("")}</select></label>`
    : "";

  dlg.innerHTML = `
    <h2 class="with-state">Slice for ${esc(o.printer.name)} ${printerById(o.printer.id) ? stateBadge(printerById(o.printer.id)) : ""}</h2>
    <p class="sub" style="margin-bottom:12px"><strong>${esc(file.name)}</strong>${
      proj.printer_model ? ` · set up for ${esc(proj.printer_model)}` : ""}</p>
    <div data-toomany>${tooMany ? `<div class="notice">Checking which of the project's ${slots.length} filaments its objects use...</div>` : ""}</div>
    ${hasOwn ? `<div class="field"><span>Start from</span><div class="modes">
        <label class="check"><input type="radio" name="start" value="author"${d.start === "author" ? " checked" : ""}> Author's profile (from the file)</label>
        <label class="check"><input type="radio" name="start" value="presets"${d.start === "presets" ? " checked" : ""}> Our process preset</label>
      </div></div>` : ""}
    <div data-presets>
      <label class="field"><span>Printer</span><select data-machine>${o.machines.map((m) =>
        option(m.name, m.user ? `${m.name} (yours)` : m.name, m.name === d.machine)).join("")}</select></label>
      <label class="field"><span>Process</span><select data-process></select></label>
      <label class="field"><span>Bed plate</span><select data-bed>${o.bed_types.map((b) =>
        option(b.name, b.label, b.name === d.bed_type)).join("")}</select></label>
      <div class="field"><span>Filaments</span><div class="fil-rows" data-fil-rows></div>${Object.keys(o.calibrated || {}).length
        ? `<small class="hint">✓ = uses the flow ratio you calibrated on the Printers page.</small>` : ""}</div>
    </div>
    <div data-process-panel></div>
    <div data-objects></div>
    <div data-copies></div>
    ${plates}
    <div data-warn></div>
    <div data-progress></div>
    <div class="dialog-actions"><button class="ghost" data-close>Cancel</button><button data-go${tooMany ? " disabled" : ""}>Slice</button></div>`;

  const $ = (sel) => dlg.querySelector(sel);
  const machineSel = $("[data-machine]"), processSel = $("[data-process]"), bedSel = $("[data-bed]");
  const filSels = () => [...dlg.querySelectorAll("[data-fil]")];
  const filRowsBox = $("[data-fil-rows]"), objBox = $("[data-objects]");
  let chosen = [];  // filament preset per row, kept while rows come and go

  function rowLabel(s, i) {
    const many = slots.length > 1 || objs.active;
    const tag = many ? `<b>${esc(slotTag(i))}</b>` : "";
    if (s.extra) {
      const h = head(i);
      return `${tag}${swatch(slotColour(i))}<small>${h && h.loaded !== false && h.type ? `loaded: ${esc(h.type)}` : "any filament"}</small>`;
    }
    const col = objs.active ? slotColour(i) : s.colour;
    return `${tag}${s.type || col ? `${swatch(col)}<small>file had ${esc(s.type || "filament")}</small>` : "<small>any filament</small>"}`;
  }
  function renderFilRows() {
    filRowsBox.innerHTML = slots.map((s, i) => `
        <label class="fil-row"><span class="slot" title="What the project file was set up with. The preset on the right is what gets sliced.">${rowLabel(s, i)}</span>
          <select data-fil="${i}"></select></label>`).join("");
    filSels().forEach((sel) => (sel.onchange = () => { chosen[Number(sel.dataset.fil)] = sel.value; check(); }));
  }
  const presetsBox = $("[data-presets]"), warnBox = $("[data-warn]"), progress = $("[data-progress]");
  const go = $("[data-go]");
  const isStl = proj.kind === "stl";
  const isOff = (it) => objs.assign[it.key] === 0;  // left off the plate
  // Changing what's on the plate of a project needs presets (the slicer gets a re-made file).
  const reshaped = () => !isStl && (objs.together.size > 0 || objs.items.some(isOff) || objs.compact || objs.copies > 1);
  const start = () => dlg.querySelector('input[name="start"]:checked')?.value || "presets";
  // "project": the slicer uses the 3MF as it is. Changing what's on the plate
  // needs presets, with the author's settings carried over (author()).
  const mode = () => (start() === "author" && o.project_matches && !reshaped() ? "project" : "presets");
  const author = () => start() === "author" && mode() === "presets";

  function fillFor(machine, keep) {
    const procs = o.processes[machine] || { names: [], user: [], default: "" };
    const wantProc = keep && procs.names.includes(processSel.value) ? processSel.value : (machine === d.machine ? d.process : procs.default);
    processSel.innerHTML = procs.names.map((n) => option(n, procs.user.includes(n) ? `${n} (yours)` : n, n === wantProc)).join("");
    const fils = o.filaments[machine] || [];
    filSels().forEach((sel, i) => {
      const prev = keep ? (sel.value || chosen[i] || "") : "";
      // Last time's pick for this toolhead if it's the same material, then the
      // last pick for this material, then the slicer's suggestion.
      const typeOf = (n) => fils.find((f) => f.name === n)?.type || "";
      const slotType = (slots[i].type || "").toUpperCase();
      const lastHere = last.filaments?.[i];
      const remembered = lastHere && (!slotType || typeOf(lastHere).toUpperCase() === slotType) ? lastHere : last.byType?.[slotType];
      const want = fils.some((f) => f.name === prev) ? prev
        : fils.some((f) => f.name === remembered) ? remembered : (d.filaments[i] || "");
      const pick = fils.some((f) => f.name === want) ? want
        : (fils.find((f) => f.type === slots[i].type)?.name || fils.find((f) => f.type === "PLA")?.name || fils[0]?.name);
      const cal = o.calibrated || {};
      sel.innerHTML = fils.map((f) => option(f.name,
        `${f.name}${f.user ? " (yours)" : ""}${cal[f.name] ? ` · flow ${cal[f.name]} ✓` : ""}`, f.name === pick)).join("");
      chosen[i] = sel.value;
    });
    if (!procs.names.length || !fils.length) warnBox.innerHTML = `<div class="notice warn">No process or filament presets for ${esc(machine)}.</div>`;
  }

  function check() {
    const usePresets = mode() === "presets";
    presetsBox.hidden = !usePresets;
    processSel.closest(".field").querySelector("span").textContent = author() ? "Base preset" : "Process";
    panel?.refresh();
    if (!usePresets) { warnBox.innerHTML = ""; return; }
    const fils = o.filaments[machineSel.value] || [];
    const cold = filSels().map((s) => fils.find((f) => f.name === s.value)).filter((f) => f && !(f.beds[bedSel.value] > 0));
    warnBox.innerHTML = cold.length
      ? `<div class="notice warn">${cold.map((f) => esc(f.name)).join(", ")} ${cold.length > 1 ? "have" : "has"} no bed temperature for this plate, so the bed won't heat. Pick another plate.</div>`
      : "";
  }

  let panel = null;
  renderFilRows();
  fillFor(machineSel.value, false);
  check();
  panel = createProcessPanel($("[data-process-panel]"), {
    printerId, fileId: file.id,
    base: () => ({ mode: start() === "author" ? "project" : "presets", process: processSel.value, machine: machineSel.value }),
  });
  machineSel.onchange = () => { fillFor(machineSel.value, true); check(); };
  processSel.onchange = check;
  bedSel.onchange = check;
  dlg.querySelectorAll('input[name="start"]').forEach((r) => (r.onchange = () => { check(); objectsChanged(); }));

  // ------------------------------------------------ objects, each with its own toolhead
  // A painted project keeps its own filaments (repainting isn't possible here).
  const choices = () => ((mode() === "project" || objs.items.some((it) => it.painted)) && !isStl ? Math.max(1, baseCount)
    : Math.min(o.printer.max_filaments, Math.max(4, baseCount)));
  const loadedCount = () => Math.max(1, heads.filter((h) => h.loaded !== false && h.index < choices()).length || 1);
  // Filament slot of a piece (1-based); 0 = left off the plate.
  const slotOf = (it) => (objs.assign[it.key] === 0 ? 0 : Math.min(objs.assign[it.key] || it.extruder || 1, choices()));
  function forcePresets() {
    check();  // mode() turns to presets (keeping the author's settings) once the plate changes
  }
  // What's on the plate, remembered per file in this browser.
  const PLATE_KEY = "pp.slice.plate";
  function savePlate() {
    try {
      const all = JSON.parse(localStorage.getItem(PLATE_KEY) || "{}");
      all[file.id] = { together: [...objs.together], off: objs.items.filter(isOff).map((it) => it.key) };
      localStorage.setItem(PLATE_KEY, JSON.stringify(all));
    } catch { /* private mode */ }
  }
  const savedPlate = (() => { try { return JSON.parse(localStorage.getItem(PLATE_KEY) || "{}")[file.id] || null; } catch { return null; } })();
  if (savedPlate) savedPlate.together.filter((id) => siblings.some((x) => x.id === id)).forEach((id) => objs.together.add(id));

  // Filament rows: as many as the highest toolhead in use (a 3MF keeps all its own).
  function syncRows() {
    const want = Math.max(isStl && objs.items.length ? 1 : baseCount, ...objs.items.map(slotOf), 1);
    const before = slots.length;
    while (slots.length < want) {
      const h = head(slots.length);
      slots.push({ type: h && h.loaded !== false ? h.type || "" : "", colour: "", extra: true });
    }
    while (slots.length > want && slots[slots.length - 1].extra) slots.pop();
    if (slots.length !== before || objs.active) {
      chosen = filSels().map((s) => s.value).concat(chosen.slice(filSels().length));
      renderFilRows();
      fillFor(machineSel.value, true);
      check();
    }
    const over = slots.length > o.printer.max_filaments;
    go.disabled = over;
    $("[data-toomany]").innerHTML = over
      ? `<div class="notice error">This project uses ${slots.length} filaments, but the ${esc(o.printer.name)} has ${o.printer.max_filaments} toolheads.</div>`
      : objs.compact ? `<div class="notice">The file lists ${listed} filaments, but its objects only use ${slots.length}, so only ${slots.length > 1 ? "those are" : "that one is"} sliced.</div>` : "";
  }

  function paintObjects() {
    objBox.querySelectorAll("[data-obj]").forEach((sel) => {
      const it = objs.items[Number(sel.dataset.obj)];
      sel.value = String(slotOf(it));
      const row = sel.closest(".obj-row");
      row.classList.toggle("off", isOff(it));
      row.querySelector(".dot").style.setProperty("--c", isOff(it) ? "transparent" : slotColour(slotOf(it) - 1));
    });
    objBox.querySelectorAll("[data-brush]").forEach((b) => b.setAttribute("aria-pressed", String(Number(b.dataset.brush) === objs.brush)));
    viewer?.setColours(objs.items.map((it) => (isOff(it) ? null : slotColour(slotOf(it) - 1))));
    // The slicer lays out separate objects itself when it gets a plain file
    // (STLs, a plain 3MF, or a project with pieces added or left off).
    const arranged = isStl || reshaped() || !proj.printer_model;
    const groups = groupsOf();
    viewer?.layout(arranged || objs.copies > 1 ? groups : null, objs.copies > 1 ? packCopies(footprints(groups), objs.copies, BEDS[printerId]) : null);
  }
  // Which pieces stay together as one object on the plate (as the slicer gets them).
  function groupsOf() {
    const keepStls = (objs.keep ?? objs.overlapping) && (isStl ? 1 : 0) + objs.together.size > 1;
    return objs.items.map((it) => (isOff(it) ? "off" : keepStls && it.group.startsWith("f") ? "stl" : it.group));
  }
  function footprints(groups) {
    const boxes = {};
    objs.items.forEach((it, i) => {
      const g = groups[i];
      if (g === "off" || !it.box) return;
      const b = boxes[g] || (boxes[g] = [Infinity, Infinity, -Infinity, -Infinity]);
      b[0] = Math.min(b[0], it.box[0]); b[1] = Math.min(b[1], it.box[1]);
      b[2] = Math.max(b[2], it.box[2]); b[3] = Math.max(b[3], it.box[3]);
    });
    return Object.entries(boxes).map(([g, b]) => ({ g, w: b[2] - b[0], d: b[3] - b[1] }));
  }
  // "Copies": 1 up to as many as fit on this bed.
  function renderCopies() {
    const box = $("[data-copies]");
    const bed = BEDS[printerId];
    const painted = !isStl && objs.items.some((it) => it.painted);
    if (!objs.items.length || !bed || painted) { box.innerHTML = ""; return; }
    objs.max = maxCopies(footprints(groupsOf()), bed);
    if (objs.copies > objs.max) objs.copies = objs.max;
    box.innerHTML = `<label class="field"><span>Copies</span><div class="copies">
      <input type="number" data-copy min="1" max="${objs.max}" value="${objs.copies}" style="width:5em">
      <button type="button" class="ghost" data-copy-max${objs.max < 2 ? " disabled" : ""}>Max (${objs.max})</button>
      <small class="hint">${objs.max > 1 ? `Up to ${objs.max} fit on the ${bed.join(" × ")} mm bed; the slicer arranges them.` : "Only one fits on the bed."}</small></div></label>`;
    const input = box.querySelector("[data-copy]");
    const set = (n) => {
      objs.copies = Math.max(1, Math.min(objs.max, Math.round(Number(n)) || 1));
      input.value = objs.copies;
      forcePresets();
      if (objs.copies > 1 && !objs.active) { objs.active = true; showObjects(); return; }
      paintObjects();
    };
    input.onchange = () => set(input.value);
    box.querySelector("[data-copy-max]").onclick = () => set(objs.max);
  }
  function objectsChanged() {
    if (!objs.active) return;
    renderObjects(false);
    syncRows();
    paintObjects();
  }

  function renderObjects(withPreview) {
    const n = choices();
    const opts = Array.from({ length: n }, (_, i) => {
      const h = head(i);
      const what = !h ? "" : h.loaded === false ? " · empty" : h.type ? ` · ${h.type}` : "";
      return option(i + 1, `${slotTag(i)}${what}`);
    }).join("") + option(0, "Don't print");
    const stlCount = (isStl ? 1 : 0) + objs.together.size;
    const together = siblings.length ? `<div class="together"><small>Also put on the plate</small>${siblings.map((s) =>
      `<label class="check"><input type="checkbox" data-with="${esc(s.id)}"${objs.together.has(s.id) ? " checked" : ""}> ${esc(s.name)}</label>`).join("")}
      ${stlCount > 1 ? `<label class="check"><input type="checkbox" data-keep${(objs.keep ?? objs.overlapping) ? " checked" : ""}> Keep their positions (they fit together as one model)</label>` : ""}</div>` : "";
    const many = objs.items.length > 1;
    const painted = objs.items.some((it) => it.painted);
    const box = objBox.querySelector("[data-obj-dynamic]");
    const dynamic = `
      ${many ? `<div class="brush"><small>Click a piece to give it</small>${Array.from({ length: n }, (_, i) =>
        `<button type="button" class="tool" data-brush="${i + 1}"><i style="--c:${esc(slotColour(i))}"></i>${esc(slotTag(i))}</button>`).join("")}<button type="button" class="tool" data-brush="0" title="Leave it off the plate">Off</button></div>` : ""}
      <div class="obj-rows">${objs.items.map((it, i) => `
        <div class="obj-row" data-row="${i}"><i class="dot"></i>
          <span class="obj-name">${esc(it.name)}<small>${it.size.map(mm).join(" × ")} mm${proj.plates > 1 && it.plate ? ` · plate ${it.plate}` : ""}${it.painted ? " · painted" : ""}</small></span>
          <select data-obj="${i}" aria-label="Toolhead for ${esc(it.name)}">${opts}</select></div>`).join("")}</div>
      ${painted ? `<small class="hint">Painted pieces keep their painted colours; the toolhead here is for the unpainted rest.</small>` : ""}`;
    if (!withPreview) renderCopies();
    if (box && !withPreview) {
      box.innerHTML = dynamic;
    } else {
      objBox.innerHTML = `<div class="field obj-field"><span>Colours</span><div>
        ${together}
        ${many || objs.together.size || objs.copies > 1 ? `<div class="obj-preview" data-preview><small class="muted">Loading preview...</small></div>
          <small class="hint">Build plate preview${BEDS[printerId] ? ` (${BEDS[printerId].join(" × ")} mm)` : ""}. The slicer centres the model, or lays pieces out when they print separately or as copies.</small>` : ""}
        <div data-obj-dynamic>${dynamic}</div></div></div>`;
      objBox.querySelectorAll("[data-with]").forEach((c) => (c.onchange = () => {
        if (c.checked) objs.together.add(c.dataset.with); else objs.together.delete(c.dataset.with);
        forcePresets();
        savePlate();
        loadObjects();
      }));
      const keep = objBox.querySelector("[data-keep]");
      if (keep) keep.onchange = () => { objs.keep = keep.checked; paintObjects(); };
    }
    const dyn = objBox.querySelector("[data-obj-dynamic]");
    dyn.onchange = (e) => {
      const sel = e.target.closest("[data-obj]");
      if (!sel) return;
      objs.assign[objs.items[Number(sel.dataset.obj)].key] = Number(sel.value);
      forcePresets();
      savePlate();
      syncRows();
      paintObjects();
    };
    dyn.onclick = (e) => {
      const b = e.target.closest("[data-brush]");
      if (b) { objs.brush = Number(b.dataset.brush); paintObjects(); }
    };
    dyn.onmouseover = (e) => { const r = e.target.closest("[data-row]"); viewer?.highlight(r ? Number(r.dataset.row) : null); };
    dyn.onmouseleave = () => viewer?.highlight(null);
  }

  let loadSeq = 0;
  async function loadObjects() {
    const seq = ++loadSeq;
    const extra = [...objs.together].join(",");
    const q = `file_id=${encodeURIComponent(file.id)}&extra=${encodeURIComponent(extra)}`;
    let data;
    try {
      data = await api(`/api/slicer/objects?${q}`);
    } catch (e) {
      if (siblings.length) objBox.innerHTML = `<div class="notice warn">Couldn't list the pieces: ${esc(e.message)}</div>`;
      syncRows();
      return;
    }
    if (seq !== loadSeq) return;
    objs.items = data.items;
    objs.overlapping = data.overlapping;
    // STL pieces start on different loaded toolheads; a 3MF keeps what the file says.
    // Added STLs too.
    const loaded = loadedCount();
    for (const it of objs.items) {
      if (it.key.startsWith("f") && !(it.key in objs.assign)) objs.assign[it.key] = ((it.extruder - 1) % loaded) + 1;
    }
    if (savedPlate && !objs.restored) {
      objs.restored = true;
      for (const it of objs.items) if (savedPlate.off.includes(it.key)) objs.assign[it.key] = 0;
    }
    compactFilaments();
    forcePresets();
    objs.active = objs.items.length > 1 || objs.together.size > 0 || siblings.length > 0 || objs.compact || objs.copies > 1;
    objs.q = q;
    objs.mesh = null;
    showObjects();
  }

  // A project that lists more filaments than the printer has toolheads (a
  // Bambu project lists every AMS slot) but whose objects use few enough:
  // keep only the used ones, each object moved to its new number.
  function compactFilaments() {
    if (isStl || objs.compact || listed <= o.printer.max_filaments || objs.items.some((it) => it.painted)) return;
    const used = [...new Set(objs.items.map((it) => it.extruder))].sort((a, b) => a - b);
    if (!used.length || used.length > o.printer.max_filaments || used[used.length - 1] > listed) return;
    const keep = used.map((u) => slots[u - 1]);
    d.filaments = used.map((u) => d.filaments[u - 1]);
    slots.splice(0, slots.length, ...keep);
    baseCount = slots.length;
    objs.items.forEach((it) => { if (objs.assign[it.key] !== 0) objs.assign[it.key] = used.indexOf(it.extruder) + 1; });
    objs.compact = true;
    chosen = [];
    renderFilRows();
    fillFor(machineSel.value, false);
    check();
  }

  async function showObjects() {
    const seq = loadSeq, q = objs.q;
    viewer?.dispose();
    viewer = null;
    if (!objs.active) { objBox.innerHTML = ""; renderCopies(); syncRows(); return; }
    renderObjects(true);
    renderCopies();
    syncRows();
    paintObjects();
    const prev = objBox.querySelector("[data-preview]");
    if (!prev) return;
    try {
      if (!objs.mesh) {
        const res = await fetch(`/api/slicer/objects/mesh?${q}`);
        if (!res.ok) throw new Error(res.statusText);
        objs.mesh = await res.arrayBuffer();
      }
      const buf = objs.mesh.slice(0);
      if (seq !== loadSeq || !prev.isConnected) return;
      prev.innerHTML = "";
      viewer = createPartsViewer(prev, buf, {
        bed: BEDS[printerId],
        onPick: (i) => {
          const it = objs.items[i];
          if (!it) return;
          objs.assign[it.key] = objs.brush;
          forcePresets();
          savePlate();
          syncRows();
          paintObjects();
          objBox.querySelector(`[data-row="${i}"]`)?.scrollIntoView({ block: "nearest" });
        },
      });
      paintObjects();
    } catch (e) {
      prev.innerHTML = `<small class="muted">No preview (${esc(e.message)})</small>`;
    }
  }
  loadObjects();
  $("[data-close]").onclick = () => dlg.close();

  go.onclick = async () => {
    let overrides;
    try { overrides = panel.overrides(); } catch (e) {
      progress.innerHTML = `<div class="notice error">${esc(e.message)}</div>`;
      return;
    }
    go.disabled = true;
    go.textContent = "Slicing...";
    dlg.querySelectorAll("select, input").forEach((x) => (x.disabled = true));
    const body = {
      file_id: file.id, printer: printerId, mode: mode(),
      machine: machineSel.value, process: processSel.value, bed_type: bedSel.value,
      filaments: filSels().map((s) => s.value), plate: Number($("[data-plate]")?.value || 0),
      overrides, author_profile: author(),
    };
    if (objs.active) {
      const assign = {};
      objs.items.forEach((it) => {
        const slot = slotOf(it);
        // A 3MF only gets the changes; STLs are rebuilt, so every piece counts
        // (and every object of a project whose unused filaments were dropped).
        if (isStl || objs.compact || it.key.startsWith("f") || slot !== it.extruder) assign[it.key] = slot;
      });
      body.assign = assign;
      body.extra_files = [...objs.together];
      body.keep_positions = objs.keep ?? objs.overlapping;
      body.colours = slots.map((s, i) => (headColour(i) || (objs.compact ? s.colour || "" : "")).slice(0, 7));
    }
    if (objs.copies > 1) body.copies = objs.copies;
    const byType = {};
    body.filaments.forEach((n, i) => { if (slots[i]?.type) byType[slots[i].type.toUpperCase()] = n; });
    saveLast(printerId, { start: hasOwn ? start() : last.start, machine: body.machine,
      process: body.process, bed_type: body.bed_type, bedV: 2, filaments: body.filaments, byType });
    progress.innerHTML = `<div class="notice">Sending to the slicer...</div>`;
    let job;
    try {
      job = await api("/api/slicer/jobs", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    } catch (e) {
      progress.innerHTML = `<div class="notice error">${esc(e.message)}</div>`;
      go.disabled = false;
      go.textContent = "Slice";
      dlg.querySelectorAll("select, input, button").forEach((x) => (x.disabled = false));
      return;
    }
    const started = Date.now();
    const poll = async () => {
      try { job = await api(`/api/slicer/jobs/${job.id}`); } catch (e) { job = { ...job, message: e.message }; }
      const secs = Math.round((Date.now() - started) / 1000);
      if (job.state === "done") {
        const warn = job.warnings?.length ? `<div class="notice warn"><ul>${job.warnings.map((w) => `<li>${esc(w)}</li>`).join("")}</ul></div>` : "";
        const used = (job.usage || []).map((g, i) => (g > 0 ? `<span class="used">${swatch(slotColour(i))} ${esc(slotTag(i))} ${g < 10 ? g.toFixed(1) : Math.round(g)} g</span>` : "")).join("");
        progress.innerHTML = `<div class="notice success"><span class="notice-icon">✓</span><div class="notice-body"><strong>Done in ${secs} s!</strong> Saved ${job.outputs.map((p) => `<code>${esc(p.split("/").pop())}</code>`).join(", ")}.${used ? `<div class="usage">${used}</div>` : ""}</div></div>${warn}`;
        go.textContent = "Done";
        $("[data-close]").textContent = "Close";
        dlg.addEventListener("close", () => onDone?.(job), { once: true });
        return;
      }
      if (job.state === "failed") {
        progress.innerHTML = `<div class="notice error">${esc(job.message)}${job.log ? `<details><summary>Slicer log</summary><pre>${esc(job.log.slice(-2000))}</pre></details>` : ""}</div>`;
        go.textContent = "Slice";
        go.disabled = false;
        dlg.querySelectorAll("select, input").forEach((x) => (x.disabled = false));
        return;
      }
      progress.innerHTML = `<div class="notice">${esc(job.state === "queued" ? "Waiting for another slice to finish..." : job.message || "Slicing...")} ${secs} s</div>`;
      timer = setTimeout(poll, 1500);
    };
    poll();
  };
}
