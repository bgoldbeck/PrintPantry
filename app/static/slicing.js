// "Slice for U1 / P1S" dialog. The slicer runs on the NAS (OrcaSlicer's
// command line in its own container); new files land in the model's
// "sliced" folder and show up under Print files.
import { printerById, stateBadge } from "./printers.js";
import { createPartsViewer } from "./viewer.js";
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
  if (o.project_matches && (last.mode === "project" || last.mode === "presets")) d.mode = last.mode;
  if (has(machineNames, last.machine)) d.machine = last.machine;
  if (has((o.processes[d.machine] || {}).names || [], last.process)) d.process = last.process;
  // A remembered plate counts only from the time the presets had temperatures for every
  // plate (bedV 2); before that, Smooth PEI was often forced on the U1's textured plate.
  if (last.bedV === 2 && has(o.bed_types.map((b) => b.name), last.bed_type)) d.bed_type = last.bed_type;
  const slots = proj.filaments.length ? proj.filaments.map((f) => ({ ...f })) : [{ type: "PLA", colour: "" }];
  const baseCount = slots.length;
  const tooMany = slots.length > o.printer.max_filaments;
  const isU1 = printerId === "u1";
  const heads = printerById(printerId)?.status?.toolheads || [];
  const head = (i) => heads.find((h) => h.index === i);
  const headColour = (i) => { const h = head(i); return h && h.loaded !== false && h.color ? h.color : ""; };
  const slotColour = (i) => headColour(i) || slots[i]?.colour || PALETTE[i % PALETTE.length];
  const slotTag = (i) => (isU1 ? `T${i + 1}` : `${i + 1}`);
  const siblings = (ctx.siblings || []).filter((s) => s.id !== file.id);
  // Per-object state, filled by loadObjects().
  const objs = { items: [], assign: {}, together: new Set(), keep: null, active: false, brush: 1, overlapping: false };
  const plates = proj.plates > 1
    ? `<label class="field"><span>Plates</span><select data-plate>${option(0, `All ${proj.plates} plates`, true)}${
      Array.from({ length: proj.plates }, (_, i) => option(i + 1, `Plate ${i + 1} only`)).join("")}</select></label>`
    : "";

  dlg.innerHTML = `
    <h2 class="with-state">Slice for ${esc(o.printer.name)} ${printerById(o.printer.id) ? stateBadge(printerById(o.printer.id)) : ""}</h2>
    <p class="sub" style="margin-bottom:12px"><strong>${esc(file.name)}</strong>${
      proj.printer_model ? ` · set up for ${esc(proj.printer_model)}` : ""}</p>
    ${tooMany ? `<div class="notice error">This project uses ${slots.length} filaments, but the ${esc(o.printer.name)} has ${o.printer.max_filaments} toolheads.</div>` : ""}
    ${o.project_matches ? `<div class="modes">
        <label class="check"><input type="radio" name="mode" value="project"${d.mode === "project" ? " checked" : ""}> Use the project's own settings</label>
        <label class="check"><input type="radio" name="mode" value="presets"${d.mode === "presets" ? " checked" : ""}> Pick presets</label>
      </div>` : ""}
    <div data-presets>
      <label class="field"><span>Printer</span><select data-machine>${o.machines.map((m) =>
        option(m.name, m.user ? `${m.name} (yours)` : m.name, m.name === d.machine)).join("")}</select></label>
      <label class="field"><span>Process</span><select data-process></select></label>
      <label class="field"><span>Bed plate</span><select data-bed>${o.bed_types.map((b) =>
        option(b.name, b.label, b.name === d.bed_type)).join("")}</select></label>
      <div class="field"><span>Filaments</span><div class="fil-rows" data-fil-rows></div>${Object.keys(o.calibrated || {}).length
        ? `<small class="hint">✓ = uses the flow ratio you calibrated on the Printers page.</small>` : ""}</div>
    </div>
    <div data-objects></div>
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
  const mode = () => dlg.querySelector('input[name="mode"]:checked')?.value || "presets";

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
    if (!usePresets) { warnBox.innerHTML = ""; return; }
    const fils = o.filaments[machineSel.value] || [];
    const cold = filSels().map((s) => fils.find((f) => f.name === s.value)).filter((f) => f && !(f.beds[bedSel.value] > 0));
    warnBox.innerHTML = cold.length
      ? `<div class="notice warn">${cold.map((f) => esc(f.name)).join(", ")} ${cold.length > 1 ? "have" : "has"} no bed temperature for this plate, so the bed won't heat. Pick another plate.</div>`
      : "";
  }

  renderFilRows();
  fillFor(machineSel.value, false);
  check();
  machineSel.onchange = () => { fillFor(machineSel.value, true); check(); };
  bedSel.onchange = check;
  dlg.querySelectorAll('input[name="mode"]').forEach((r) => (r.onchange = () => { check(); objectsChanged(); }));

  // ------------------------------------------------ objects, each with its own toolhead
  const isStl = proj.kind === "stl";
  // A painted project keeps its own filaments (repainting isn't possible here).
  const choices = () => ((mode() === "project" || objs.items.some((it) => it.painted)) && !isStl ? Math.max(1, baseCount)
    : Math.min(o.printer.max_filaments, Math.max(4, baseCount)));
  const loadedCount = () => Math.max(1, heads.filter((h) => h.loaded !== false && h.index < choices()).length || 1);
  const slotOf = (it) => Math.min(objs.assign[it.key] || it.extruder || 1, choices());

  // Filament rows: as many as the highest toolhead in use (a 3MF keeps all its own).
  function syncRows() {
    const want = Math.max(baseCount, ...objs.items.map(slotOf), 1);
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
  }

  function paintObjects() {
    objBox.querySelectorAll("[data-obj]").forEach((sel) => {
      const it = objs.items[Number(sel.dataset.obj)];
      sel.value = String(slotOf(it));
      sel.closest(".obj-row").querySelector(".dot").style.setProperty("--c", slotColour(slotOf(it) - 1));
    });
    objBox.querySelectorAll("[data-brush]").forEach((b) => b.setAttribute("aria-pressed", String(Number(b.dataset.brush) === objs.brush)));
    viewer?.setColours(objs.items.map((it) => slotColour(slotOf(it) - 1)));
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
    }).join("");
    const together = siblings.length ? `<div class="together"><small>Slice together with</small>${siblings.map((s) =>
      `<label class="check"><input type="checkbox" data-with="${esc(s.id)}"${objs.together.has(s.id) ? " checked" : ""}> ${esc(s.name)}</label>`).join("")}
      ${objs.together.size ? `<label class="check"><input type="checkbox" data-keep${(objs.keep ?? objs.overlapping) ? " checked" : ""}> Keep their positions (they fit together as one model)</label>` : ""}</div>` : "";
    const many = objs.items.length > 1;
    const painted = objs.items.some((it) => it.painted);
    const box = objBox.querySelector("[data-obj-dynamic]");
    const dynamic = `
      ${many ? `<div class="brush"><small>Click a piece to give it</small>${Array.from({ length: n }, (_, i) =>
        `<button type="button" class="tool" data-brush="${i + 1}"><i style="--c:${esc(slotColour(i))}"></i>${esc(slotTag(i))}</button>`).join("")}</div>` : ""}
      <div class="obj-rows">${objs.items.map((it, i) => `
        <div class="obj-row" data-row="${i}"><i class="dot"></i>
          <span class="obj-name">${esc(it.name)}<small>${it.size.map(mm).join(" × ")} mm${proj.plates > 1 && it.plate ? ` · plate ${it.plate}` : ""}${it.painted ? " · painted" : ""}</small></span>
          <select data-obj="${i}" aria-label="Toolhead for ${esc(it.name)}">${opts}</select></div>`).join("")}</div>
      ${painted ? `<small class="hint">Painted pieces keep their painted colours; the toolhead here is for the unpainted rest.</small>` : ""}`;
    if (box && !withPreview) {
      box.innerHTML = dynamic;
    } else {
      objBox.innerHTML = `<div class="field obj-field"><span>Colours</span><div>
        ${together}
        ${many || objs.together.size ? `<div class="obj-preview" data-preview><small class="muted">Loading preview...</small></div>
          <small class="hint">Build plate preview${BEDS[printerId] ? ` (${BEDS[printerId].join(" × ")} mm)` : ""}. The slicer centres the model, or lays pieces out when they print separately.</small>` : ""}
        <div data-obj-dynamic>${dynamic}</div></div></div>`;
      objBox.querySelectorAll("[data-with]").forEach((c) => (c.onchange = () => {
        if (c.checked) objs.together.add(c.dataset.with); else objs.together.delete(c.dataset.with);
        loadObjects();
      }));
      const keep = objBox.querySelector("[data-keep]");
      if (keep) keep.onchange = () => { objs.keep = keep.checked; };
    }
    const dyn = objBox.querySelector("[data-obj-dynamic]");
    dyn.onchange = (e) => {
      const sel = e.target.closest("[data-obj]");
      if (!sel) return;
      objs.assign[objs.items[Number(sel.dataset.obj)].key] = Number(sel.value);
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
      return;
    }
    if (seq !== loadSeq) return;
    objs.items = data.items;
    objs.overlapping = data.overlapping;
    // STL pieces start on different loaded toolheads; a 3MF keeps what the file says.
    if (isStl) {
      const loaded = loadedCount();
      objs.assign = Object.fromEntries(objs.items.map((it, i) => [it.key, objs.assign[it.key] || ((it.extruder - 1) % loaded) + 1]));
    }
    objs.active = objs.items.length > 1 || objs.together.size > 0 || siblings.length > 0;
    viewer?.dispose();
    viewer = null;
    if (!objs.active) { objBox.innerHTML = ""; return; }
    renderObjects(true);
    syncRows();
    paintObjects();
    const prev = objBox.querySelector("[data-preview]");
    if (!prev) return;
    try {
      const res = await fetch(`/api/slicer/objects/mesh?${q}`);
      if (!res.ok) throw new Error(res.statusText);
      const buf = await res.arrayBuffer();
      if (seq !== loadSeq || !prev.isConnected) return;
      prev.innerHTML = "";
      viewer = createPartsViewer(prev, buf, {
        bed: BEDS[printerId],
        onPick: (i) => {
          const it = objs.items[i];
          if (!it) return;
          objs.assign[it.key] = objs.brush;
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
    go.disabled = true;
    go.textContent = "Slicing...";
    dlg.querySelectorAll("select, input").forEach((x) => (x.disabled = true));
    const body = {
      file_id: file.id, printer: printerId, mode: mode(),
      machine: machineSel.value, process: processSel.value, bed_type: bedSel.value,
      filaments: filSels().map((s) => s.value), plate: Number($("[data-plate]")?.value || 0),
    };
    if (objs.active) {
      const assign = {};
      objs.items.forEach((it) => {
        const slot = slotOf(it);
        // A 3MF only gets the changes; STLs are rebuilt, so every piece counts.
        if (isStl || slot !== it.extruder) assign[it.key] = slot;
      });
      body.assign = assign;
      body.extra_files = [...objs.together];
      body.keep_positions = objs.keep ?? objs.overlapping;
      body.colours = slots.map((_, i) => headColour(i).slice(0, 7));
    }
    const byType = {};
    body.filaments.forEach((n, i) => { if (slots[i]?.type) byType[slots[i].type.toUpperCase()] = n; });
    saveLast(printerId, { mode: o.project_matches ? body.mode : last.mode, machine: body.machine,
      process: body.process, bed_type: body.bed_type, bedV: 2, filaments: body.filaments, byType });
    progress.innerHTML = `<div class="notice">Sending to the slicer...</div>`;
    let job;
    try {
      job = await api("/api/slicer/jobs", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    } catch (e) {
      progress.innerHTML = `<div class="notice error">${esc(e.message)}</div>`;
      go.disabled = false;
      go.textContent = "Slice";
      dlg.querySelectorAll("select, input").forEach((x) => (x.disabled = false));
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
