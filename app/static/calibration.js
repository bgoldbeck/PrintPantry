// Calibration on the Printers page: the printer's own calibrations (bed mesh,
// input shaper, ...) and the filament flow ratio wizard.
import { cameraLoop, fmtAge } from "./printers.js";

const esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

async function api(path, opts) {
  const res = await fetch(path, opts);
  if (res.status === 401) { location.href = "/login"; throw new Error("login"); }
  if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText);
  return res.json();
}
const post = (path, body, method = "POST") =>
  api(path, { method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body ?? {}) });
const option = (value, label, selected) =>
  `<option value="${esc(value)}"${selected ? " selected" : ""}>${esc(label ?? value)}</option>`;
const dot = (c) => `<i class="dot" style="--c:${esc(c || "#999")}"></i>`;
const ratio = (r) => (r == null ? "–" : Number(r).toFixed(3).replace(/0$/, ""));

function openDialog(cls) {
  const dlg = document.createElement("dialog");
  dlg.className = cls;
  document.body.appendChild(dlg);
  const stops = [];
  dlg.addEventListener("close", () => { stops.forEach((f) => f()); dlg.remove(); });
  dlg.showModal();
  return { dlg, stops, $: (sel) => dlg.querySelector(sel) };
}
function errorDialog(title, msg) {
  const { dlg } = openDialog("print-dialog");
  dlg.innerHTML = `<h2>${esc(title)}</h2><div class="notice error">${esc(msg)}</div>
    <div class="dialog-actions"><button class="ghost" data-close>Close</button></div>`;
  dlg.querySelector("[data-close]").onclick = () => dlg.close();
}
function camHtml(p) {
  return p.can_camera ? `<div class="cam"><img alt="Camera" class="nocam"><span class="cam-age" data-age>Getting a fresh picture...</span></div>` : "";
}
function startCam(d, p) {
  if (!p.can_camera) return;
  const age = d.$("[data-age]");
  d.stops.push(cameraLoop(d.$(".cam img"), p.id, 3000, (t, e) => {
    age.textContent = e || (t ? `Picture from ${fmtAge(t)}` : "Live picture");
  }));
}

// ---------------------------------------------------------------- printer's own calibrations
// Shown on each printer card.
export function calibrationHtml(p) {
  const st = p.status;
  const c = p.calibrating;
  let out = "";
  if (c && c.state === "running") {
    const mins = Math.round((Date.now() / 1000 - c.started) / 60);
    out += `<div class="notice calib-run"><strong>${esc(c.name)}</strong> running for ${mins} min
      (takes about ${c.minutes} min). Leave the printer alone until it finishes.
      <button class="small danger" data-act="estop">Emergency stop</button></div>`;
  } else if (c && c.finished && Date.now() / 1000 - c.finished < 3600) {
    out += `<div class="notice ${c.state === "failed" ? "error" : ""} small-notice">${esc(c.name)}: ${
      c.state === "failed" ? esc(c.message || "failed") : "finished"} ${fmtAge(c.finished)}.</div>`;
  }
  if (p.calibrations?.length && st.online) {
    const busy = (c && c.state === "running") || !["idle", "complete", "cancelled"].includes(st.state);
    out += `<div class="calib-buttons"><span>Calibrate</span>${p.calibrations.map((x) =>
      `<button class="small ghost" data-calib="${esc(x.id)}"${busy ? " disabled" : ""} title="${esc(x.about)}">${esc(x.name)}</button>`).join("")}
      <button class="small ghost" data-flow="${esc(p.id)}"${busy ? " disabled" : ""} title="Print a flow ratio test for a filament">Flow ratio</button></div>`;
  }
  return out;
}

export function calibrationDialog(p, cid, onStarted) {
  const cal = p.calibrations.find((x) => x.id === cid);
  if (!cal) return;
  const d = openDialog("print-dialog");
  const st = p.status;
  const busy = !["idle", "complete", "cancelled"].includes(st.state);
  d.dlg.innerHTML = `
    <h2>${esc(cal.name)} on ${esc(p.name)}</h2>
    <p class="sub" style="margin-bottom:12px">${esc(cal.about)} About ${cal.minutes} minutes.</p>
    ${camHtml(p)}
    ${busy ? `<div class="notice error">${esc(p.name)} is ${esc(st.state)}; wait until it's ready.</div>` : ""}
    ${cal.params.map((x) => `<label class="field"><span>${esc(x.label)}${x.unit ? ` (${esc(x.unit)})` : ""}</span>
      <input type="number" class="num" data-param="${esc(x.id)}" value="${x.default}" min="${x.min}" max="${x.max}" step="1"></label>`).join("")}
    <label class="check"><input type="checkbox" data-ok> ${esc(cal.check)}</label>
    <div data-msg></div>
    <div class="dialog-actions"><button class="ghost" data-close>Cancel</button><button data-go disabled>Start</button></div>`;
  startCam(d, p);
  const go = d.$("[data-go]"), ok = d.$("[data-ok]");
  ok.onchange = () => { go.disabled = busy || !ok.checked; };
  d.$("[data-close]").onclick = () => d.dlg.close();
  go.onclick = async () => {
    go.disabled = true;
    go.textContent = "Starting...";
    const params = {};
    d.dlg.querySelectorAll("[data-param]").forEach((i) => { params[i.dataset.param] = Number(i.value); });
    try {
      const res = await post(`/api/printers/${p.id}/calibrations/${cid}`, { confirmed: ok.checked, params });
      d.dlg.close();
      onStarted?.(res);
    } catch (e) {
      d.$("[data-msg]").innerHTML = `<div class="notice error">${esc(e.message)}</div>`;
      go.textContent = "Start";
      go.disabled = !ok.checked;
    }
  };
}

// ---------------------------------------------------------------- flow ratio: overview panel
let STATE = null;
async function loadState() {
  try { STATE = await api("/api/calibration"); } catch { STATE = STATE || { flow: [], tests: [], flow_tests: [], slicer: false }; }
  return STATE;
}

export async function flowSection(box, getPrinters) {
  const st = await loadState();
  const printers = getPrinters();
  const name = (id) => printers.find((p) => p.id === id)?.name || id;
  const open = st.tests.filter((t) => !t.picked && Date.now() / 1000 - t.created < 14 * 86400);
  box.innerHTML = `
    <div class="flow-head"><h2>Filament flow ratio</h2>
      ${st.slicer ? `<button data-new-flow>New flow test</button>` : ""}</div>
    <p class="sub">Print a plate of blocks at different flow ratios, pick the one with the smoothest top,
      and Print Pantry uses that ratio whenever you slice with that filament preset.</p>
    ${st.slicer ? "" : `<div class="notice warn">The slicer on the NAS isn't reachable, so flow tests can't be made right now.</div>`}
    ${open.length ? `<h3>Waiting for your pick</h3><ul class="flow-tests">${open.map((t) => `
      <li data-test="${esc(t.id)}"><div><strong>${esc(t.filament)}</strong> · ${esc(name(t.printer))} T${t.toolhead} · ${esc(t.label)}
        <small>${t.printed ? `printed ${fmtAge(t.printed)}` : `made ${fmtAge(t.created)}, not printed yet`}</small></div>
        <div class="file-actions">${t.printed ? "" : `<button class="small ghost" data-print-test>Print</button>`}
          <button class="small" data-pick>Pick best block</button>
          <button class="small ghost" data-drop title="Remove from this list">✕</button></div></li>`).join("")}</ul>` : ""}
    ${st.flow.length ? `<h3>Saved flow ratios</h3><table class="flow-table"><thead><tr><th>Filament preset</th><th>Printer</th>
      <th>Flow ratio</th><th>From</th><th></th></tr></thead><tbody>${st.flow.map((f) => `
      <tr data-printer="${esc(f.printer)}" data-filament="${esc(f.filament)}">
        <td>${esc(f.filament)}</td><td>${esc(name(f.printer))}</td><td><b>${ratio(f.ratio)}</b>${
          f.history?.length ? ` <small title="Earlier: ${esc(f.history.map((h) => ratio(h.ratio)).join(", "))}">was ${ratio(f.history[0].ratio)}</small>` : ""}</td>
        <td><small>${esc(f.test || "")}${f.block ? `, block ${esc(f.block)}` : ""}${f.toolhead ? ` on T${f.toolhead}` : ""} · ${fmtAge(f.at)}</small></td>
        <td class="row-actions">${st.slicer ? `<button class="small ghost" data-retest>Test again</button>` : ""}
          <a class="button small ghost" href="/api/calibration/flow/preset?printer=${encodeURIComponent(f.printer)}&filament=${encodeURIComponent(f.filament)}"
            title="Download a filament preset with this flow ratio, to import into OrcaSlicer">Preset</a>
          <button class="small ghost" data-edit>Edit</button>
          <button class="small ghost" data-forget title="Forget this flow ratio">✕</button></td></tr>`).join("")}</tbody></table>`
      : `<p class="empty">No flow ratios saved yet.</p>`}`;

  const refresh = () => flowSection(box, getPrinters);
  box.onclick = async (e) => {
    const b = e.target.closest("button");
    if (!b) return;
    const li = b.closest("[data-test]"), tr = b.closest("tr[data-filament]");
    const test = li && st.tests.find((t) => t.id === li.dataset.test);
    try {
      if ("newFlow" in b.dataset) flowWizard(getPrinters(), {}, refresh);
      else if ("printTest" in b.dataset) printTestDialog(test, refresh);
      else if ("pick" in b.dataset) pickDialog(test, refresh);
      else if ("drop" in b.dataset) { await api(`/api/calibration/flow/tests/${test.id}`, { method: "DELETE" }); refresh(); }
      else if ("retest" in b.dataset) flowWizard(getPrinters(), { printer: tr.dataset.printer, filament: tr.dataset.filament }, refresh);
      else if ("forget" in b.dataset) {
        if (!confirm(`Forget the flow ratio for ${tr.dataset.filament}? Slicing goes back to the preset's own value.`)) return;
        await api(`/api/calibration/flow/ratio?printer=${encodeURIComponent(tr.dataset.printer)}&filament=${encodeURIComponent(tr.dataset.filament)}`, { method: "DELETE" });
        refresh();
      } else if ("edit" in b.dataset) {
        const cur = st.flow.find((f) => f.printer === tr.dataset.printer && f.filament === tr.dataset.filament);
        const v = prompt(`Flow ratio for ${tr.dataset.filament}`, ratio(cur?.ratio));
        if (v == null) return;
        await post("/api/calibration/flow/ratio", { printer: tr.dataset.printer, filament: tr.dataset.filament, ratio: Number(v) }, "PUT");
        refresh();
      }
    } catch (err) { alert(err.message); }
  };
  return refresh;
}

// ---------------------------------------------------------------- flow ratio: wizard
const LAST_KEY = "pp.flow.last";
function loadLast() { try { return JSON.parse(localStorage.getItem(LAST_KEY) || "{}"); } catch { return {}; } }
function saveLast(v) { try { localStorage.setItem(LAST_KEY, JSON.stringify(v)); } catch { /* private mode */ } }

export async function flowWizard(printers, want, onChange) {
  const d = openDialog("slice-dialog flow-dialog");
  d.dlg.innerHTML = `<h2>Getting slicer settings...</h2>`;
  const st = await loadState();
  const slicerPrinters = printers.filter((p) => ["u1", "p1s"].includes(p.id));
  const last = loadLast();
  let printerId = want.printer || last.printer || slicerPrinters.find((p) => p.can_print)?.id || "u1";
  let o;
  async function load() {
    try { o = await api(`/api/calibration/flow/options?printer=${encodeURIComponent(printerId)}`); }
    catch (e) { d.dlg.innerHTML = `<h2>Can't make a flow test</h2><div class="notice error">${esc(e.message)}</div>
      <div class="dialog-actions"><button class="ghost" data-close>Close</button></div>`;
      d.$("[data-close]").onclick = () => d.dlg.close(); return false; }
    return true;
  }
  if (!(await load())) return;

  function render() {
    const p = printers.find((x) => x.id === printerId) || { status: { toolheads: [] } };
    const heads = p.status?.toolheads?.length ? p.status.toolheads
      : Array.from({ length: Math.min(4, o.printer.max_filaments) }, (_, i) => ({ index: i, type: "", color: "" }));
    const lastHere = last[printerId] || {};
    const machine = o.machines.some((m) => m.name === lastHere.machine) ? lastHere.machine : o.defaults.machine;
    const tests = st.flow_tests.filter((t) => t.available);
    const testId = tests.some((t) => t.id === lastHere.test) ? lastHere.test : (tests[0]?.id || "yolo");
    d.dlg.innerHTML = `
      <h2>Flow ratio test</h2>
      <p class="sub" style="margin-bottom:12px">Slices a plate of blocks, each at a different flow ratio, for one filament
        on one toolhead. After it prints, you pick the block with the smoothest top.</p>
      <label class="field"><span>Printer</span><select data-printer>${slicerPrinters.map((x) =>
        option(x.id, x.name + (x.can_print ? "" : " (slice only, not connected)"), x.id === printerId)).join("")}</select></label>
      <div class="field"><span>${printerId === "u1" ? "Toolhead" : "Slot"}</span><div class="tools pick-tools">${heads.map((t) =>
        `<button type="button" class="tool" data-head="${t.index + 1}" aria-pressed="false">${dot(t.color)}${esc(t.label || `T${t.index + 1}`)} ${esc(t.loaded === false ? "empty" : t.type || "")}</button>`).join("")}</div></div>
      <label class="field"><span>Printer preset</span><select data-machine>${o.machines.map((m) =>
        option(m.name, m.user ? `${m.name} (yours)` : m.name, m.name === machine)).join("")}</select></label>
      <label class="field"><span>Process</span><select data-process></select></label>
      <label class="field"><span>Filament</span><select data-fil></select></label>
      <label class="field"><span>Bed plate</span><select data-bed>${o.bed_types.map((b) =>
        option(b.name, b.label, b.name === (lastHere.bed_type || o.defaults.bed_type))).join("")}</select></label>
      <div class="field"><span>Test</span><div class="test-kinds">${tests.map((t) => `
        <label class="check"><input type="radio" name="kind" value="${esc(t.id)}"${t.id === testId ? " checked" : ""}>
          <span>${esc(t.label)}<small>${esc(t.hint)}</small></span></label>`).join("")}</div></div>
      <div data-start></div>
      <div data-warn></div>
      <div data-progress></div>
      <div class="dialog-actions"><button class="ghost" data-close>Cancel</button><button data-go>Slice test print</button></div>`;

    const $ = d.$;
    const machineSel = $("[data-machine]"), processSel = $("[data-process]"), filSel = $("[data-fil]"), bedSel = $("[data-bed]");
    let head = Number(want.toolhead || lastHere.toolhead || 1);
    if (!heads.some((t) => t.index + 1 === head)) head = 1;
    const headInfo = () => heads.find((t) => t.index + 1 === head) || {};
    const pickHead = (n) => {
      head = n;
      d.dlg.querySelectorAll("[data-head]").forEach((b) => b.setAttribute("aria-pressed", String(Number(b.dataset.head) === n)));
    };
    pickHead(head);

    function fill(keep) {
      const procs = o.processes[machineSel.value] || { names: [], user: [], default: "" };
      const wantProc = keep && procs.names.includes(processSel.value) ? processSel.value
        : procs.names.includes(lastHere.process) ? lastHere.process : procs.default;
      processSel.innerHTML = procs.names.map((n) => option(n, procs.user.includes(n) ? `${n} (yours)` : n, n === wantProc)).join("");
      const fils = o.filaments[machineSel.value] || [];
      const type = (headInfo().type || "").toUpperCase();
      const byType = lastHere.byType?.[type];
      // Best guess for what's loaded: same material, the spool's brand, else the printer maker's or a generic preset.
      const vendor = (headInfo().vendor || "").toLowerCase();
      const score = (f) => (f.type.toUpperCase() === type ? 4 : 0)
        + (vendor && f.name.toLowerCase().startsWith(vendor) ? 2 : 0)
        + (new RegExp(`^(snapmaker|generic|bambu) ${type.toLowerCase()}( basic)? @`, "i").test(f.name) ? 1 : 0);
      const guess = type ? [...fils].sort((x, y) => score(y) - score(x))[0]?.name : "";
      const pick = [keep && filSel.value, want.filament, byType, guess, o.defaults.filaments?.[0]]
        .find((n) => n && fils.some((f) => f.name === n));
      filSel.innerHTML = fils.map((f) => option(f.name, `${f.name}${f.user ? " (yours)" : ""}`, f.name === pick)).join("");
    }
    function update() {
      const fils = o.filaments[machineSel.value] || [];
      const f = fils.find((x) => x.name === filSel.value);
      const saved = st.flow.find((x) => x.printer === printerId && x.filament === filSel.value);
      $("[data-start]").innerHTML = f ? `<div class="field"><span>Start from</span><div class="test-kinds">
          ${saved ? `<label class="check"><input type="radio" name="from" value="saved" checked><span>Your saved flow ratio ${ratio(saved.ratio)}</span></label>` : ""}
          <label class="check"><input type="radio" name="from" value="preset"${saved ? "" : " checked"}><span>The preset's flow ratio ${ratio(f.flow)}</span></label>
        </div></div>` : "";
      const warn = [];
      const h = headInfo();
      if (h.loaded === false) warn.push(`T${head} looks empty. Load the filament before printing the test.`);
      else if (f && h.type && !sameMaterial(f.type, h.type)) warn.push(`T${head} has ${h.type} loaded, but ${f.name} is ${f.type}.`);
      if (f && !(f.beds?.[bedSel.value] > 0)) warn.push(`${f.name} has no bed temperature for this plate, so the bed won't heat. Pick another plate.`);
      $("[data-warn]").innerHTML = warn.length ? `<div class="notice warn"><ul>${warn.map((w) => `<li>${esc(w)}</li>`).join("")}</ul></div>` : "";
    }
    fill(false);
    update();
    $("[data-printer]").onchange = async (e) => { printerId = e.target.value; d.dlg.innerHTML = "<h2>Loading...</h2>"; if (await load()) render(); };
    d.dlg.querySelectorAll("[data-head]").forEach((b) => (b.onclick = () => { pickHead(Number(b.dataset.head)); fill(false); update(); }));
    machineSel.onchange = () => { fill(true); update(); };
    filSel.onchange = update;
    bedSel.onchange = update;
    $("[data-close]").onclick = () => d.dlg.close();

    const go = $("[data-go]");
    go.onclick = async () => {
      const kind = d.dlg.querySelector('input[name="kind"]:checked')?.value || "yolo";
      const from = d.dlg.querySelector('input[name="from"]:checked')?.value || "preset";
      const body = { printer: printerId, toolhead: head, machine: machineSel.value, process: processSel.value,
        filament: filSel.value, bed_type: bedSel.value, test: kind, use_saved: from === "saved" };
      const type = (headInfo().type || "").toUpperCase();
      last.printer = printerId;
      last[printerId] = { ...lastHere, machine: body.machine, process: body.process, bed_type: body.bed_type,
        test: kind, toolhead: head, byType: { ...(lastHere.byType || {}), ...(type ? { [type]: body.filament } : {}) } };
      saveLast(last);
      go.disabled = true;
      go.textContent = "Slicing...";
      d.dlg.querySelectorAll("select, input, [data-head]").forEach((x) => (x.disabled = true));
      const progress = $("[data-progress]");
      progress.innerHTML = `<div class="notice">Sending to the slicer...</div>`;
      let job;
      try { job = await post("/api/calibration/flow/jobs", body); }
      catch (e) {
        progress.innerHTML = `<div class="notice error">${esc(e.message)}</div>`;
        go.disabled = false; go.textContent = "Slice test print";
        d.dlg.querySelectorAll("select, input, [data-head]").forEach((x) => (x.disabled = false));
        return;
      }
      const started = Date.now();
      let timer = null;
      d.stops.push(() => clearTimeout(timer));
      const poll = async () => {
        try { job = await api(`/api/calibration/flow/jobs/${job.id}`); } catch (e) { job = { ...job, message: e.message }; }
        const secs = Math.round((Date.now() - started) / 1000);
        if (job.state === "done" && job.test) {
          onChange?.();
          d.dlg.close();
          printTestDialog(job.test, onChange);
          return;
        }
        if (job.state === "failed") {
          progress.innerHTML = `<div class="notice error">${esc(job.message)}${job.log ? `<details><summary>Slicer log</summary><pre>${esc(job.log.slice(-2000))}</pre></details>` : ""}</div>`;
          go.disabled = false; go.textContent = "Slice test print";
          d.dlg.querySelectorAll("select, input, [data-head]").forEach((x) => (x.disabled = false));
          return;
        }
        progress.innerHTML = `<div class="notice">${esc(job.state === "queued" ? "Waiting for another slice to finish..." : job.message || "Slicing...")} ${secs} s</div>`;
        timer = setTimeout(poll, 1500);
      };
      poll();
    };
  }
  render();
}

function sameMaterial(a, b) {
  const n = (s) => (s || "").toUpperCase().replace(/[^A-Z0-9]/g, "");
  const x = n(a), y = n(b);
  return !x || !y || x.startsWith(y) || y.startsWith(x);
}

// ---------------------------------------------------------------- flow ratio: print the test
export async function printTestDialog(test, onChange) {
  const d = openDialog("print-dialog");
  d.dlg.innerHTML = `<h2>Checking the printer...</h2>`;
  let chk;
  try { chk = await api(`/api/calibration/flow/tests/${test.id}/check`); }
  catch (e) { d.dlg.close(); errorDialog("Can't print the test", e.message); return; }
  const p = chk.printer;
  const row = chk.tools[0];
  const loaded = row?.loaded;
  d.dlg.innerHTML = `
    <h2>Print the flow test on ${esc(p.name)}</h2>
    <p class="sub" style="margin-bottom:12px"><strong>${esc(test.filament)}</strong> on T${test.toolhead} · ${esc(test.label)} ·
      ${test.blocks.length} blocks from ${ratio(test.blocks[0]?.flow)} to ${ratio(test.blocks.at(-1)?.flow)}</p>
    ${camHtml(p)}
    ${chk.blockers.length ? `<div class="notice error"><strong>Can't start:</strong><ul>${chk.blockers.map((b) => `<li>${esc(b)}</li>`).join("")}</ul></div>` : ""}
    <table class="toolmap"><thead><tr><th>Tool</th><th>Test needs</th><th>Loaded now</th><th></th></tr></thead><tbody>
      <tr class="${row?.ok === false ? "warn" : ""}"><td>T${test.toolhead}</td><td>${esc(test.type || "?")}</td>
      <td>${loaded ? (loaded.loaded === false ? "<em>empty</em>" : `${dot(loaded.color)} ${esc(loaded.type)} <small>${esc(loaded.vendor)}</small>`) : "<em>unknown</em>"}</td>
      <td>${row?.ok === false ? "⚠" : "✓"}</td></tr></tbody></table>
    ${chk.warnings.length ? `<div class="notice warn"><ul>${chk.warnings.map((w) => `<li>${esc(w)}</li>`).join("")}</ul></div>
      <label class="check"><input type="checkbox" data-ack> I've checked the toolhead and want to print anyway</label>` : ""}
    <label class="check"><input type="checkbox" data-bed> The bed is clear and the build plate is in place</label>
    <div class="dialog-actions"><button class="ghost" data-close>Later</button><button data-go disabled>Start print</button></div>`;
  startCam(d, p);
  const go = d.$("[data-go]"), bed = d.$("[data-bed]"), ack = d.$("[data-ack]");
  const update = () => { go.disabled = !!chk.blockers.length || !bed.checked || (ack && !ack.checked); };
  bed.onchange = update;
  if (ack) ack.onchange = update;
  d.$("[data-close]").onclick = () => d.dlg.close();
  go.onclick = async () => {
    go.disabled = true;
    go.textContent = "Sending...";
    try {
      await post(`/api/calibration/flow/tests/${test.id}/print`, { bed_clear: bed.checked, accept_warnings: !!ack?.checked });
      d.dlg.innerHTML = `<h2>Printing the flow test</h2>
        <p>When it's done, look at the tops of the blocks in good light. Pick the one whose top is smoothest:
        no gaps between the lines (too little flow) and no ridges or rough, shiny bumps (too much).</p>
        <p>Then press <b>Pick best block</b> under <b>Filament flow ratio</b> on this page.</p>
        <div class="dialog-actions"><button data-close>OK</button></div>`;
      d.$("[data-close]").onclick = () => d.dlg.close();
      onChange?.();
    } catch (e) {
      alert(e.message);
      go.textContent = "Start print";
      update();
    }
  };
}

// ---------------------------------------------------------------- flow ratio: pick the best block
export function pickDialog(test, onChange) {
  const d = openDialog("print-dialog flow-pick");
  const blocks = [...test.blocks].sort((a, b) => a.ratio - b.ratio);
  d.dlg.innerHTML = `
    <h2>Which block is smoothest?</h2>
    <p class="sub" style="margin-bottom:12px"><strong>${esc(test.filament)}</strong> on T${test.toolhead} · ${esc(test.label)},
      started from flow ratio ${ratio(test.base)}.</p>
    <p>Each block has its number on top. Pick the one with the smoothest top surface. If two look equally good, pick
      the one in between, or the lower number.</p>
    <div class="blocks">${blocks.map((b) => `<button type="button" class="ghost" data-block="${esc(b.name)}" aria-pressed="false">
      <b>${esc(b.label)}${test.linear ? "" : "%"}</b><small>${ratio(b.flow)}</small></button>`).join("")}</div>
    <div data-msg></div>
    <div class="dialog-actions"><button class="ghost" data-close>Cancel</button><button data-go disabled>Save flow ratio</button></div>`;
  let chosen = null;
  const go = d.$("[data-go]");
  d.dlg.querySelectorAll("[data-block]").forEach((b) => (b.onclick = () => {
    chosen = b.dataset.block;
    d.dlg.querySelectorAll("[data-block]").forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
    const blk = blocks.find((x) => x.name === chosen);
    go.disabled = false;
    go.textContent = `Save ${ratio(blk.flow)}`;
  }));
  d.$("[data-close]").onclick = () => d.dlg.close();
  go.onclick = async () => {
    go.disabled = true;
    try {
      const res = await post("/api/calibration/flow/pick", { test_id: test.id, block: chosen });
      const next = res.edge
        ? `That block is at the end of the range, so the best ratio may lie further out. Run another test; it will start from ${ratio(res.saved.ratio)}.`
        : test.test === "yolo" ? "For a finer result, run the Fine test next; it starts from this ratio." : "";
      d.dlg.innerHTML = `<h2>Saved</h2>
        <p><strong>${esc(test.filament)}</strong> now slices with flow ratio <b>${ratio(res.saved.ratio)}</b> in Print Pantry.</p>
        ${next ? `<p>${esc(next)}</p>` : ""}
        <p class="sub">Use <b>Preset</b> next to it to download a filament preset with this ratio for the desktop slicer.</p>
        <div class="dialog-actions"><button data-close>OK</button></div>`;
      d.$("[data-close]").onclick = () => d.dlg.close();
      onChange?.();
    } catch (e) {
      d.$("[data-msg]").innerHTML = `<div class="notice error">${esc(e.message)}</div>`;
      go.disabled = false;
    }
  };
}
