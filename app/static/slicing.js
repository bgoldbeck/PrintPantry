// "Slice for U1 / P1S" dialog. The slicer runs on the NAS (OrcaSlicer's
// command line in its own container); new files land in the model's
// "sliced" folder and show up under Print files.
import { loadPrinters, printerById, stateBadge, watchToolheads } from "./printers.js";
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
// Dark or light text on a colour swatch.
export function inkOn(hex) {
  const m = /^#?([0-9a-f]{6})/i.exec(hex || "");
  if (!m) return "#1b1712";
  const n = parseInt(m[1], 16), lin = (v) => { v /= 255; return v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; };
  const L = 0.2126 * lin(n >> 16) + 0.7152 * lin((n >> 8) & 255) + 0.0722 * lin(n & 255);
  return L > 0.3 ? "#1b1712" : "#ffffff";
}

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
// A slice left running when its dialog was closed: keep checking, then refresh
// the page so the new print file shows up (unless you've moved on).
function watchInBackground(job, onDone) {
  const page = location.hash;
  const poll = async () => {
    try { job = await api(`/api/slicer/jobs/${job.id}`); } catch { /* try again */ }
    if (job.state === "failed") return;
    if (job.state !== "done") { setTimeout(poll, 3000); return; }
    if (location.hash === page && !document.querySelector("dialog[open]")) onDone?.(job);
  };
  setTimeout(poll, 1500);
}

export async function openSliceDialog(file, printerId, onDone, ctx = {}) {
  const dlg = document.createElement("dialog");
  dlg.className = "slice-dialog";
  dlg.innerHTML = `<h2>Getting slicer settings...</h2>`;
  document.body.appendChild(dlg);
  let timer = null;
  let viewer = null;
  let stopHeads = null;
  let running = null; // the slice job while it runs; closing the dialog doesn't stop it
  dlg.addEventListener("close", () => {
    clearTimeout(timer); stopHeads?.(); viewer?.dispose(); dlg.remove();
    if (running) watchInBackground(running, onDone);
  });
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
  const printerName = printerById(printerId)?.name || printerId;
  const canPrintAfter = !!printerById(printerId)?.can_print;
  const listed = slots.length;  // filaments the project lists (before dropping unused ones)
  const isU1 = printerId === "u1";
  let heads = printerById(printerId)?.status?.toolheads || [];
  const head = (i) => heads.find((h) => h.index === i);
  // On the U1 filament n prints from toolhead Tn, so the preset follows what's
  // loaded there. (The P1S maps filaments to AMS trays at print time instead.)
  const followsHeads = !printerById(printerId)?.ams_mapping;
  const loadedType = (i) => { const h = followsHeads && head(i); return h && h.loaded !== false ? (h.type || "").trim() : ""; };
  const loadedVendor = (i) => { const h = followsHeads && head(i); return h && h.loaded !== false ? (h.vendor || "").trim() : ""; };
  const headColour = (i) => { const h = head(i); return h && h.loaded !== false && h.color ? h.color : ""; };
  const slotColour = (i) => headColour(i) || slots[i]?.colour || PALETTE[i % PALETTE.length];
  const slotTag = (i) => (isU1 ? `T${i + 1}` : `${i + 1}`);
  const siblings = (ctx.siblings || []).filter((s) => s.id !== file.id);
  // Per-object state, filled by loadObjects().
  const objs = { items: [], assign: {}, together: new Set(), keep: null, active: false, brush: 1, overlapping: false,
    compact: false, copies: 1, max: 1, counts: {}, mainCount: 1 };
  // A build set (ctx.set = {name, parts: [{id, qty}]}): those STLs and counts on the plate.
  const kit = ctx.set || null;
  if (kit) {
    for (const p of kit.parts) {
      if (p.id === file.id) objs.mainCount = Math.max(1, p.qty || 1);
      else if (siblings.some((x) => x.id === p.id)) { objs.together.add(p.id); objs.counts[p.id] = Math.max(1, p.qty || 1); }
    }
    objs.keep = false;  // separate parts: the slicer lays them out
  }
  // Every extra STL as the slicer gets it: repeated once per extra copy.
  const extraList = () => [
    ...Array(objs.mainCount - 1).fill(file.id),
    ...[...objs.together].flatMap((id) => Array(objs.counts[id] || 1).fill(id)),
  ];
  const plates = proj.plates > 1
    ? `<label class="field"><span>Plates</span><select data-plate>${option(0, `All ${proj.plates} plates`, true)}${
      Array.from({ length: proj.plates }, (_, i) => option(i + 1, `Plate ${i + 1} only`)).join("")}</select></label>`
    : "";

  // Laid out as: title, then the plate (preview + colours) beside the
  // settings on wide screens, then a footer with the buttons that's always
  // in view (the body scrolls, not the dialog).
  dlg.innerHTML = `
    <header class="slice-head">
      <h2 class="with-state">Slice for ${esc(o.printer.name)} ${printerById(o.printer.id) ? stateBadge(printerById(o.printer.id)) : ""}</h2>
      <p class="sub"><strong>${esc(file.name)}</strong>${proj.printer_model ? ` · set up for ${esc(proj.printer_model)}` : ""}</p>
    </header>
    <div class="slice-body">
      <div data-toomany>${tooMany ? `<div class="notice">Checking which of the project's ${slots.length} filaments its objects use...</div>` : ""}</div>
      <div class="slice-cols">
        <section class="slice-plate" data-objects></section>
        <section class="slice-settings">
          ${hasOwn ? `<div class="field"><span>Start from</span><div class="seg" role="radiogroup">
              <label><input type="radio" name="start" value="author"${d.start === "author" ? " checked" : ""}><span>Author's profile</span></label>
              <label><input type="radio" name="start" value="presets"${d.start === "presets" ? " checked" : ""}><span>Our preset</span></label>
            </div></div>` : ""}
          <div data-presets>
            <div class="field-pair">
              <label class="field"><span>Printer</span><select data-machine>${o.machines.map((m) =>
                option(m.name, m.user ? `${m.name} (yours)` : m.name, m.name === d.machine)).join("")}</select></label>
              <label class="field"><span>Plate</span><select data-bed>${o.bed_types.map((b) =>
                option(b.name, b.label, b.name === d.bed_type)).join("")}</select></label>
            </div>
            <label class="field"><span>Process</span><select data-process></select></label>
            <div class="field"><span>Filaments</span><div class="fil-rows" data-fil-rows></div></div>
            ${Object.keys(o.calibrated || {}).length ? `<div class="field"><span></span><small class="hint">✓ = uses the flow ratio you calibrated on the Printers page.</small></div>` : ""}
          </div>
          <div data-process-panel></div>
          <div data-copies></div>
          <div data-prime></div>
          <div data-orient></div>
          ${plates}
        </section>
      </div>
    </div>
    <footer class="slice-foot">
      <div data-warn></div>
      <div data-progress></div>
      <div class="dialog-actions">${canPrintAfter ? `<label class="check print-after" title="Sends the sliced file to ${esc(printerName)} as soon as it's ready. You can close this window.">
          <input type="checkbox" data-print-after> Print when sliced <small class="muted">(bed is clear)</small></label>` : ""}
        <button class="ghost" data-close>Cancel</button><button data-go${tooMany ? " disabled" : ""}>Slice</button></div>
    </footer>`;

  const $ = (sel) => dlg.querySelector(sel);
  const machineSel = $("[data-machine]"), processSel = $("[data-process]"), bedSel = $("[data-bed]");
  const filSels = () => [...dlg.querySelectorAll("[data-fil]")];
  const filRowsBox = $("[data-fil-rows]"), objBox = $("[data-objects]");
  let chosen = [];  // filament preset per row, kept while rows come and go
  const byHand = new Set();  // rows whose preset the user picked; the rest follow the loaded spool

  function rowLabel(s, i) {
    const many = slots.length > 1 || objs.active;
    const tag = many ? `<b>${esc(slotTag(i))}</b>` : "";
    const lt = loadedType(i);
    if (s.extra) {
      return `${tag}${swatch(slotColour(i))}<small>${lt ? `loaded: ${esc(lt)}` : "any filament"}</small>`;
    }
    if (lt) {
      const had = s.type && s.type.toUpperCase() !== lt.toUpperCase() ? ` · file had ${esc(s.type)}` : "";
      const v = loadedVendor(i);
      return `${tag}${swatch(headColour(i) || s.colour)}<small>loaded: ${esc(lt)}${v ? ` · ${esc(v)}` : ""}${had}</small>`;
    }
    const col = objs.active ? slotColour(i) : s.colour;
    return `${tag}${s.type || col ? `${swatch(col)}<small>file had ${esc(s.type || "filament")}</small>` : "<small>any filament</small>"}`;
  }
  function renderFilRows() {
    filRowsBox.innerHTML = slots.map((s, i) => `
        <label class="fil-row"><span class="slot">${rowLabel(s, i)}</span>
          <select data-fil="${i}"></select></label>`).join("");
    filRowsBox.querySelectorAll(".slot").forEach((el) => (el.title = `${el.textContent.trim()}. The preset beside it is what gets sliced.`));
    filSels().forEach((sel) => (sel.onchange = () => {
      chosen[Number(sel.dataset.fil)] = sel.value;
      byHand.add(Number(sel.dataset.fil));
      check();
    }));
  }
  const presetsBox = $("[data-presets]"), warnBox = $("[data-warn]"), progress = $("[data-progress]");
  const go = $("[data-go]");
  const isStl = proj.kind === "stl";
  const isOff = (it) => objs.assign[it.key] === 0;  // left off the plate
  // Changing what's on the plate of a project needs presets (the slicer gets a re-made file).
  const reshaped = () => !isStl && (objs.together.size > 0 || objs.items.some(isOff) || objs.compact || objs.copies > 1
    || objs.items.some((it) => slotOf(it) > Math.max(1, baseCount)));
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
      // A preset the user picked stays; otherwise follow the material loaded on
      // the toolhead (or, if the printer doesn't say, the file's): last time's
      // pick for this toolhead if it's that material, then the last pick for
      // that material, then the slicer's suggestion, then any preset of it.
      const prev = keep && byHand.has(i) ? (sel.value || chosen[i] || "") : "";
      const typeOf = (n) => (fils.find((f) => f.name === n)?.type || "").toUpperCase();
      const lt = loadedType(i).toUpperCase();
      const slotType = lt || (slots[i].type || "").toUpperCase();
      const ok = (n) => fils.some((f) => f.name === n) && (!slotType || typeOf(n) === slotType);
      const lastHere = last.filaments?.[i];
      const vendor = loadedVendor(i).toLowerCase();
      const ofType = fils.filter((f) => (f.type || "").toUpperCase() === slotType);
      const pick = (prev && fils.some((f) => f.name === prev) && prev)
        || [lastHere, last.byType?.[slotType], d.filaments[i]].find(ok)
        || (vendor && ofType.find((f) => f.name.toLowerCase().includes(vendor))?.name)
        || ofType[0]?.name || fils.find((f) => f.type === "PLA")?.name || fils[0]?.name;
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
  // Spools loaded or swapped while the dialog is open show up here within seconds.
  stopHeads = watchToolheads(printerId, (now) => {
    heads = now;
    slots.forEach((s, i) => { if (s.extra) s.type = loadedType(i); });
    filRowsBox.querySelectorAll(".fil-row .slot").forEach((el, i) => { if (slots[i]) el.innerHTML = rowLabel(slots[i], i); });
    fillFor(machineSel.value, true);
    check();
    if (objs.active && objBox.querySelector("[data-obj-dynamic]")) objectsChanged();
  }, heads);
  panel = createProcessPanel($("[data-process-panel]"), {
    printerId, fileId: file.id,
    base: () => ({ mode: start() === "author" ? "project" : "presets", process: processSel.value, machine: machineSel.value }),
  });
  // Keep "one object at a time" (by Copies) in step with the panel's Print sequence.
  let lastSeq = null;
  panel.onChange = () => {
    renderPrime();
    const v = panel.value("print_sequence");
    if (v === lastSeq) return;
    lastSeq = v;
    const c = dlg.querySelector("[data-byobj]");
    if (c) c.checked = v === "by object"; else if (v === "by object" && typeof renderCopies === "function") renderCopies();
  };
  machineSel.onchange = () => { fillFor(machineSel.value, true); check(); };
  processSel.onchange = check;
  bedSel.onchange = check;
  dlg.querySelectorAll('input[name="start"]').forEach((r) => (r.onchange = () => { check(); objectsChanged(); }));

  // ------------------------------------------------ objects, each with its own toolhead
  // A painted project keeps its own filaments (repainting isn't possible here).
  // Any toolhead can be picked: a project whose objects move to a toolhead it
  // has no filament for is sliced with presets (the author's settings carried over).
  const choices = () => (objs.items.some((it) => it.painted) && !isStl ? Math.max(1, baseCount)
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
  if (savedPlate && !kit) savedPlate.together.filter((id) => siblings.some((x) => x.id === id)).forEach((id) => objs.together.add(id));

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
    renderPrime();
  }

  function paintObjects() {
    objBox.querySelectorAll("[data-row]").forEach((row) => {
      const it = objs.items[Number(row.dataset.row)];
      const slot = slotOf(it);
      row.classList.toggle("off", isOff(it));
      row.style.setProperty("--c", isOff(it) ? "transparent" : slotColour(slot - 1));
      row.querySelectorAll("[data-set]").forEach((b) => b.setAttribute("aria-pressed", String(Number(b.dataset.set) === slot)));
    });
    objBox.querySelectorAll("[data-brush]").forEach((b) => b.setAttribute("aria-pressed", String(Number(b.dataset.brush) === objs.brush)));
    viewer?.setColours(objs.items.map((it) => (isOff(it) ? null : slotColour(slotOf(it) - 1))));
    // The slicer lays out separate objects itself when it gets a plain file
    // (STLs, a plain 3MF, or a project with pieces added or left off).
    const arranged = isStl || reshaped() || !proj.printer_model;
    const groups = groupsOf();
    viewer?.layout(arranged || objs.copies > 1 ? groups : null, objs.copies > 1 ? packCopies(footprints(groups), objs.copies, BEDS[printerId]) : null);
    renderPrime();
  }
  // Which pieces stay together as one object on the plate (as the slicer gets them).
  function groupsOf() {
    const keepStls = (objs.keep ?? objs.overlapping) && (isStl ? 1 : 0) + extraList().length > 1;
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
    const pieces = objs.items.filter((it) => !isOff(it)).length + (objs.copies > 1 ? 1 : 0);
    const byObj = panel?.value("print_sequence") === "by object";
    objs.max = maxCopies(footprints(groupsOf()), bed);
    if (objs.copies > objs.max) objs.copies = objs.max;
    box.innerHTML = `<div class="field"><span>Copies</span><div class="copies">
      <input type="number" data-copy min="1" max="${objs.max}" value="${objs.copies}" aria-label="Copies">
      <button type="button" class="ghost" data-copy-max${objs.max < 2 ? " disabled" : ""}>Max (${objs.max})</button>
      ${pieces > 1 || byObj ? `<label class="check" title="Each object is printed whole before the next starts, so one can be solid black and another solid white with no colour changes between layers. The slicer re-arranges the plate with room for the toolhead; if they don't fit, slicing says so. Pieces stay separate objects."><input type="checkbox" data-byobj${byObj ? " checked" : ""}> One object at a time</label>` : ""}
      </div></div>`;
    const byBox = box.querySelector("[data-byobj]");
    if (byBox) byBox.onchange = () => panel?.set("print_sequence", byBox.checked ? "by object" : "by layer");
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
  // "Skip prime tower when one filament": OrcaSlicer builds the tower whenever
  // more than one filament is loaded, even if every piece prints from the same
  // toolhead. With one toolhead there are no tool changes, so no tower and no
  // flushing are needed. On by default, remembered in this browser.
  const TOWER_KEY = "pp.slice.skipTower";
  let skipTower = true;
  try { skipTower = localStorage.getItem(TOWER_KEY) !== "0"; } catch { /* private mode */ }
  // Toolheads this slice prints from, or 0 when that can't be told (painted colours).
  function headsUsed() {
    const plate = Number($("[data-plate]")?.value || 0);
    const items = objs.items.filter((it) => !isOff(it) && (!plate || !it.plate || it.plate === plate));
    if (!items.length) return slots.length;
    if (!isStl && items.some((it) => it.painted)) return 0;
    return new Set(items.map(slotOf)).size;
  }
  // The tower is left to the panel when Prime tower > Enable was set there by hand.
  const towerByHand = () => { try { return "enable_prime_tower" in (panel?.overrides() || {}); } catch { return false; } };
  const towerOff = () => skipTower && headsUsed() === 1 && !towerByHand();
  function renderPrime() {
    const box = $("[data-prime]");
    if (!box) return;
    const n = headsUsed();
    const note = !skipTower ? ""
      : towerByHand() ? "Prime tower is set in the settings below, so that wins."
      : n === 1 ? "Only one toolhead is used, so this slice has no prime tower."
      : n > 1 ? `Uses ${n} toolheads, so the prime tower stays.` : "Painted colours: the prime tower stays.";
    box.innerHTML = `<label class="check" title="With one filament there are no colour changes, so the prime tower (and the flushing it holds) only wastes filament and time.">
      <input type="checkbox" data-skip-tower${skipTower ? " checked" : ""}> Skip prime tower when one filament</label>
      ${note ? `<small class="hint prime-note">${esc(note)}</small>` : ""}`;
    box.querySelector("[data-skip-tower]").onchange = (e) => {
      skipTower = e.target.checked;
      try { localStorage.setItem(TOWER_KEY, skipTower ? "1" : "0"); } catch { /* private mode */ }
      renderPrime();
    };
  }
  // "Auto-orient STLs": each STL goes on the bed on the side with the least
  // overhang, unless it has a print orientation saved on the model page
  // (that one wins). On by default, remembered in this browser.
  const ORIENT_KEY = "pp.slice.autoOrient";
  try { objs.auto = localStorage.getItem(ORIENT_KEY) !== "0"; } catch { objs.auto = true; }
  function renderOrient() {
    const box = $("[data-orient]");
    if (!box) return;
    if (!isStl && !extraList().length) { box.innerHTML = ""; return; }
    box.innerHTML = `<label class="check" title="Turns each STL so it prints with the least overhang (the way it lies in the file isn't always the way to print it). A print orientation saved on the model page always wins.">
      <input type="checkbox" data-auto-orient${objs.auto ? " checked" : ""}> Auto-orient STLs</label>
      <small class="hint prime-note">${objs.auto ? "Least overhang, unless the STL has a saved print orientation (model page)."
        : "STLs print the way they lie in the file, or their saved print orientation."}</small>`;
    box.querySelector("[data-auto-orient]").onchange = (e) => {
      objs.auto = e.target.checked;
      try { localStorage.setItem(ORIENT_KEY, objs.auto ? "1" : "0"); } catch { /* private mode */ }
      renderOrient();
      loadObjects(); // the pieces (and the preview) turn
    };
  }
  function objectsChanged() {
    if (!objs.active) return;
    renderObjects(false);
    syncRows();
    paintObjects();
  }

  // What's on toolhead i (0-based): colour, material and the spool's brand.
  function headInfo(i) {
    const h = head(i);
    const colour = slotColour(i);
    if (h && h.loaded === false) return { colour, empty: true, type: "Empty", detail: "nothing loaded" };
    const type = (h?.type || "").trim() || slots[i]?.type || "";
    const detail = (h?.spool || "").trim() || (h?.vendor || "").trim() || (!h && slots[i]?.type ? "as in the file" : "");
    return { colour, type: type || "Not set", detail };
  }
  const headTitle = (i) => { const k = headInfo(i); return [slotTag(i), k.type, k.detail].filter(Boolean).join(" · "); };
  // Pieces on the plate first; with many files the rest fold away.
  function sibRows(qty) {
    const row = (s) => `<label class="check"><input type="checkbox" data-with="${esc(s.id)}"${objs.together.has(s.id) ? " checked" : ""}> ${esc(s.name)}${objs.together.has(s.id) ? ` ${qty(s.id, objs.counts[s.id] || 1)}` : ""}</label>`;
    const on = siblings.filter((s) => objs.together.has(s.id));
    const off = siblings.filter((s) => !objs.together.has(s.id));
    const fold = off.length && on.length && (kit || siblings.length > 10);
    return on.map(row).join("") + (fold
      ? `<details class="others"><summary>Add other files (${off.length})</summary>${off.map(row).join("")}</details>`
      : off.map(row).join(""));
  }
  function renderObjects(withPreview) {
    const n = choices();
    const stlCount = (isStl ? 1 : 0) + extraList().length;
    const qty = (id, n) => `<input type="number" class="qty" min="1" max="20" value="${n}" data-qty="${esc(id)}" aria-label="How many" title="How many">`;
    const together = siblings.length ? `<details class="together"${objs.together.size || kit ? " open" : ""}><summary>${kit ? `Build set: <b>${esc(kit.name)}</b> · ${stlCount} pieces` : `Also put on the plate${
      objs.together.size ? ` (${objs.together.size})` : ""}`}</summary>${isStl ? `<label class="check main">${esc(file.name)} ${qty(file.id, objs.mainCount)}</label>` : ""}${sibRows(qty)}
      ${stlCount > 1 ? `<label class="check"><input type="checkbox" data-keep${(objs.keep ?? objs.overlapping) ? " checked" : ""}> Keep their positions (they fit together as one model)</label>` : ""}</details>` : "";
    const many = objs.items.length > 1;
    const painted = objs.items.some((it) => it.painted);
    const box = objBox.querySelector("[data-obj-dynamic]");
    // Big swatches, one per toolhead: the brush for clicking pieces in the preview.
    const cards = Array.from({ length: n }, (_, i) => {
      const k = headInfo(i);
      return `<button type="button" class="head-card${k.empty ? " empty" : ""}" data-brush="${i + 1}" style="--c:${esc(k.colour)};--on:${inkOn(k.colour)}" title="${esc(headTitle(i))}">
        <span class="chip">${esc(slotTag(i))}</span><span class="head-txt"><b>${esc(k.type)}</b><small>${esc(k.detail) || "&nbsp;"}</small></span></button>`;
    }).join("");
    // Small swatches on each piece's row: click one to put the piece on that toolhead.
    const sws = Array.from({ length: n }, (_, i) => {
      const c = slotColour(i);
      return `<button type="button" class="sw${headInfo(i).empty ? " empty" : ""}" data-set="${i + 1}" style="--c:${esc(c)};--on:${inkOn(c)}" title="${esc(headTitle(i))}">${esc(slotTag(i))}</button>`;
    }).join("") + (many ? `<button type="button" class="sw off" data-set="0" title="Don't print this piece">✕</button>` : "");
    const dynamic = `
      ${many ? `<div class="brush"><div class="brush-hint"><small>Pick a colour, then click pieces in the preview</small>
          <button type="button" class="head-off" data-brush="0" title="Click pieces to leave them off the plate">✕ Don't print</button></div><div class="head-cards">${cards}</div></div>`
        : `<div class="brush"><small>Toolhead</small></div>`}
      <div class="obj-rows">${objs.items.map((it, i) => `
        <div class="obj-row" data-row="${i}">
          <span class="obj-name">${esc(it.name)}<small>${it.size.map(mm).join(" × ")} mm${proj.plates > 1 && it.plate ? ` · plate ${it.plate}` : ""}${it.painted ? " · painted" : ""}</small></span>
          <span class="sws" role="group" aria-label="Toolhead for ${esc(it.name)}">${sws}</span></div>`).join("")}</div>
      ${painted ? `<small class="hint">Painted pieces keep their painted colours; the toolhead here is for the unpainted rest.</small>` : ""}`;
    if (!withPreview) renderCopies();
    if (box && !withPreview) {
      const top = box.querySelector(".obj-rows")?.scrollTop || 0;
      box.innerHTML = dynamic;
      box.querySelector(".obj-rows").scrollTop = top;
    } else {
      objBox.innerHTML = `<div class="plate-head"><h3>Colours</h3>${together}</div>
        <div class="obj-preview" data-preview title="Build plate${BEDS[printerId] ? ` (${BEDS[printerId].join(" × ")} mm)` : ""}. Drag to turn, scroll to zoom${many ? ", click a piece to colour it" : ""}. The slicer centres the model, or lays pieces out when they print separately or as copies."><small class="muted">Loading preview...</small></div>
        <div data-obj-dynamic>${dynamic}</div>`;
      objBox.querySelectorAll("[data-with]").forEach((c) => (c.onchange = () => {
        if (c.checked) objs.together.add(c.dataset.with); else objs.together.delete(c.dataset.with);
        forcePresets();
        savePlate();
        loadObjects();
      }));
      objBox.querySelectorAll("[data-qty]").forEach((q) => {
        q.onclick = (e) => e.stopPropagation();
        q.onchange = () => {
          const n = Math.max(1, Math.min(20, Math.round(Number(q.value)) || 1));
          if (q.dataset.qty === file.id) objs.mainCount = n; else objs.counts[q.dataset.qty] = n;
          forcePresets();
          loadObjects();
        };
      });
      const keep = objBox.querySelector("[data-keep]");
      if (keep) keep.onchange = () => { objs.keep = keep.checked; paintObjects(); };
    }
    const dyn = objBox.querySelector("[data-obj-dynamic]");
    dyn.onclick = (e) => {
      const b = e.target.closest("[data-brush]");
      if (b) { objs.brush = Number(b.dataset.brush); paintObjects(); return; }
      const set = e.target.closest("[data-set]");
      if (!set) return;
      const before = choices();
      objs.assign[objs.items[Number(set.closest("[data-row]").dataset.row)].key] = Number(set.dataset.set);
      forcePresets();
      savePlate();
      syncRows();
      if (choices() !== before) renderObjects(false);
      paintObjects();
    };
    dyn.onmouseover = (e) => { const r = e.target.closest("[data-row]"); viewer?.highlight(r ? Number(r.dataset.row) : null); };
    dyn.onmouseleave = () => viewer?.highlight(null);
  }

  let loadSeq = 0;
  async function loadObjects() {
    const seq = ++loadSeq;
    const extra = extraList().join(",");
    const q = `file_id=${encodeURIComponent(file.id)}&extra=${encodeURIComponent(extra)}&auto=${objs.auto ? 1 : 0}`;
    renderOrient();
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
      if (it.key.startsWith("f") && !(it.key in objs.assign)) objs.assign[it.key] = kit ? 1 : ((it.extruder - 1) % loaded) + 1;
    }
    if (savedPlate && !objs.restored) {
      objs.restored = true;
      for (const it of objs.items) if (savedPlate.off.includes(it.key)) objs.assign[it.key] = 0;
    }
    compactFilaments();
    forcePresets();
    objs.active = objs.items.length > 0 || extraList().length > 0 || objs.copies > 1;
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
          if (!it || objs.items.length < 2) return;
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
  $("[data-plate]")?.addEventListener("change", renderPrime);
  const printAfter = $("[data-print-after]");
  if (printAfter) printAfter.onchange = () => { go.textContent = printAfter.checked ? "Slice & print" : "Slice"; };

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
      overrides, author_profile: author(), auto_orient: !!objs.auto,
    };
    if (towerOff()) body.overrides = { ...overrides, enable_prime_tower: "0" };
    // A lone piece left on its own toolhead slices the file as it is.
    const lone = objs.items.length === 1 && !extraList().length && !siblings.length && !objs.compact && objs.copies === 1
      && slotOf(objs.items[0]) === objs.items[0].extruder;
    if (objs.active && !lone) {
      const assign = {};
      objs.items.forEach((it) => {
        const slot = slotOf(it);
        // A 3MF only gets the changes; STLs are rebuilt, so every piece counts
        // (and every object of a project whose unused filaments were dropped).
        if (isStl || objs.compact || it.key.startsWith("f") || slot !== it.extruder) assign[it.key] = slot;
      });
      body.assign = assign;
      body.extra_files = extraList();
      body.keep_positions = objs.keep ?? objs.overlapping;
      body.colours = slots.map((s, i) => (headColour(i) || (objs.compact ? s.colour || "" : "")).slice(0, 7));
    }
    if (objs.copies > 1) body.copies = objs.copies;
    if (printAfter?.checked) body.print_after = true;
    const byType = {};
    body.filaments.forEach((n, i) => {
      const t = loadedType(i) || slots[i]?.type;
      if (t) byType[t.toUpperCase()] = n;
    });
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
    running = job;
    if (body.print_after) {
      // Nothing left to watch here: the printer card shows the slice and the send.
      progress.innerHTML = `<div class="notice success">Slicing in the background. It goes to ${esc(printerName)} when it's done.</div>`;
      loadPrinters();
      setTimeout(() => dlg.open && dlg.close(), 1200);
      return;
    }
    const started = Date.now();
    const poll = async () => {
      try { job = await api(`/api/slicer/jobs/${job.id}`); } catch (e) { job = { ...job, message: e.message }; }
      const secs = Math.round((Date.now() - started) / 1000);
      if (job.state === "done" || job.state === "failed") running = null;
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
      progress.innerHTML = `<div class="notice">${esc(job.state === "queued" ? "Waiting for another slice to finish..." : job.message || "Slicing...")} ${secs} s
        <small class="muted">· You can close this window; it keeps slicing.</small></div>`;
      timer = setTimeout(poll, 1500);
    };
    poll();
  };
}
