// "Slice for U1 / P1S" dialog. The slicer runs on the NAS (OrcaSlicer's
// command line in its own container); new files land in the model's
// "sliced" folder and show up under Print files.
import { printerById, stateBadge } from "./printers.js";
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

export async function openSliceDialog(file, printerId, onDone) {
  const dlg = document.createElement("dialog");
  dlg.className = "slice-dialog";
  dlg.innerHTML = `<h2>Getting slicer settings...</h2>`;
  document.body.appendChild(dlg);
  let timer = null;
  dlg.addEventListener("close", () => { clearTimeout(timer); dlg.remove(); });
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
  const slots = proj.filaments.length ? proj.filaments : [{ type: "PLA", colour: "" }];
  const tooMany = slots.length > o.printer.max_filaments;
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
      <div class="field"><span>Filaments</span><div class="fil-rows">${slots.map((s, i) => `
        <label class="fil-row"><span class="slot" title="What the project file was set up with. The preset on the right is what gets sliced.">${slots.length > 1 ? `<b>${i + 1}</b>` : ""}${s.type || s.colour ? `${swatch(s.colour)}<small>file had ${esc(s.type || "filament")}</small>` : "<small>any filament</small>"}</span>
          <select data-fil="${i}"></select></label>`).join("")}</div>${Object.keys(o.calibrated || {}).length
        ? `<small class="hint">✓ = uses the flow ratio you calibrated on the Printers page.</small>` : ""}</div>
    </div>
    ${plates}
    <div data-warn></div>
    <div data-progress></div>
    <div class="dialog-actions"><button class="ghost" data-close>Cancel</button><button data-go${tooMany ? " disabled" : ""}>Slice</button></div>`;

  const $ = (sel) => dlg.querySelector(sel);
  const machineSel = $("[data-machine]"), processSel = $("[data-process]"), bedSel = $("[data-bed]");
  const filSels = [...dlg.querySelectorAll("[data-fil]")];
  const presetsBox = $("[data-presets]"), warnBox = $("[data-warn]"), progress = $("[data-progress]");
  const go = $("[data-go]");
  const mode = () => dlg.querySelector('input[name="mode"]:checked')?.value || "presets";

  function fillFor(machine, keep) {
    const procs = o.processes[machine] || { names: [], user: [], default: "" };
    const wantProc = keep && procs.names.includes(processSel.value) ? processSel.value : (machine === d.machine ? d.process : procs.default);
    processSel.innerHTML = procs.names.map((n) => option(n, procs.user.includes(n) ? `${n} (yours)` : n, n === wantProc)).join("");
    const fils = o.filaments[machine] || [];
    filSels.forEach((sel, i) => {
      const prev = keep ? sel.value : "";
      // Last time's pick for this toolhead if it's the same material, then the
      // last pick for this material, then the slicer's suggestion.
      const typeOf = (n) => fils.find((f) => f.name === n)?.type || "";
      const slotType = (slots[i].type || "").toUpperCase();
      const lastHere = last.filaments?.[i];
      const remembered = lastHere && (!slotType || typeOf(lastHere).toUpperCase() === slotType) ? lastHere : last.byType?.[slotType];
      const want = fils.some((f) => f.name === prev) ? prev
        : fils.some((f) => f.name === remembered) ? remembered : (d.filaments[i] || "");
      const pick = fils.some((f) => f.name === want) ? want : (fils.find((f) => f.type === slots[i].type)?.name || fils[0]?.name);
      const cal = o.calibrated || {};
      sel.innerHTML = fils.map((f) => option(f.name,
        `${f.name}${f.user ? " (yours)" : ""}${cal[f.name] ? ` · flow ${cal[f.name]} ✓` : ""}`, f.name === pick)).join("");
    });
    if (!procs.names.length || !fils.length) warnBox.innerHTML = `<div class="notice warn">No process or filament presets for ${esc(machine)}.</div>`;
  }

  function check() {
    const usePresets = mode() === "presets";
    presetsBox.hidden = !usePresets;
    if (!usePresets) { warnBox.innerHTML = ""; return; }
    const fils = o.filaments[machineSel.value] || [];
    const cold = filSels.map((s) => fils.find((f) => f.name === s.value)).filter((f) => f && !(f.beds[bedSel.value] > 0));
    warnBox.innerHTML = cold.length
      ? `<div class="notice warn">${cold.map((f) => esc(f.name)).join(", ")} ${cold.length > 1 ? "have" : "has"} no bed temperature for this plate, so the bed won't heat. Pick another plate.</div>`
      : "";
  }

  fillFor(machineSel.value, false);
  check();
  machineSel.onchange = () => { fillFor(machineSel.value, true); check(); };
  bedSel.onchange = check;
  filSels.forEach((s) => (s.onchange = check));
  dlg.querySelectorAll('input[name="mode"]').forEach((r) => (r.onchange = check));
  $("[data-close]").onclick = () => dlg.close();

  go.onclick = async () => {
    go.disabled = true;
    go.textContent = "Slicing...";
    dlg.querySelectorAll("select, input").forEach((x) => (x.disabled = true));
    const body = {
      file_id: file.id, printer: printerId, mode: mode(),
      machine: machineSel.value, process: processSel.value, bed_type: bedSel.value,
      filaments: filSels.map((s) => s.value), plate: Number($("[data-plate]")?.value || 0),
    };
    const byType = {};
    body.filaments.forEach((n, i) => { if (slots[i].type) byType[slots[i].type.toUpperCase()] = n; });
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
        progress.innerHTML = `<div class="notice success"><span class="notice-icon">✓</span><div class="notice-body"><strong>Done in ${secs} s!</strong> Saved ${job.outputs.map((p) => `<code>${esc(p.split("/").pop())}</code>`).join(", ")}.</div></div>${warn}`;
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
