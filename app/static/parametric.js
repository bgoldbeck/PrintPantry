// "Customize" dialog for parametric models: OpenSCAD (.scad) files with
// Customizer comments and FreeCAD (.FCStd) files with a parameter sheet.
// The form is built from the parameters the app reads out of the file; the
// NAS renders the STL (one job at a time, shared with slicing) into the
// model's "generated" folder, where it shows up like any other STL.
const esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

async function api(path, opts) {
  const res = await fetch(path, opts);
  if (res.status === 401) { location.href = "/login"; throw new Error("login"); }
  if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText);
  return res.json();
}
const json = (method, body) => ({ method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });

// "TextOffsetY" / "text_offset_y" -> "Text offset Y"
export function humanize(name) {
  const words = String(name).replace(/[_$]+/g, " ").replace(/([a-z0-9])([A-Z])/g, "$1 $2")
    .replace(/([A-Z]+)([A-Z][a-z])/g, "$1 $2").trim().split(/\s+/);
  return words.map((w, i) => (i === 0 ? w[0].toUpperCase() + w.slice(1)
    : /^[A-Z0-9]+$/.test(w) ? w : w.toLowerCase())).join(" ");
}

const fontName = (v) => String(v || "").split(/[\\/]/).pop();
const toolName = (kind) => (kind === "scad" ? "OpenSCAD" : "FreeCAD");
const decimals = (n) => (String(n).split(".")[1] || "").length;

// Saved sets hold every value as text (OpenSCAD's format); turn them back.
function fromSaved(p, raw) {
  if (raw === undefined) return p.value;
  if (p.type === "bool") return raw === true || raw === "true" || raw === "1" || raw === 1;
  if (Array.isArray(p.value)) {
    try { return JSON.parse(raw); } catch { return p.value; }
  }
  if (typeof p.value === "number") return Number(raw);
  return String(raw);
}

function inputHtml(p, value, fonts) {
  const id = `pp-${p.name}`;
  if (p.type === "bool") {
    return `<label class="check"><input type="checkbox" id="${id}" data-p="${esc(p.name)}"${value ? " checked" : ""}> ${esc(p.description || p.label)}</label>`;
  }
  if (p.type === "choice") {
    return `<select id="${id}" data-p="${esc(p.name)}">${p.options.map((o) =>
      `<option value="${esc(o.value)}"${String(o.value) === String(value) ? " selected" : ""}>${esc(o.label)}</option>`).join("")}</select>`;
  }
  if (p.type === "slider") {
    const step = p.step ?? (Number.isInteger(p.value) && Number.isInteger(p.min) && Number.isInteger(p.max) ? 1 : "any");
    return `<div class="range-row"><input type="range" data-range="${esc(p.name)}" min="${p.min}" max="${p.max}" step="${step}" value="${esc(value)}">
      <input type="number" class="num" id="${id}" data-p="${esc(p.name)}" step="${step}" value="${esc(value)}"></div>`;
  }
  if (p.type === "number") {
    const step = Number.isInteger(value) ? 1 : 10 ** -Math.max(1, decimals(value));
    return `<div class="range-row"><input type="number" class="num" id="${id}" data-p="${esc(p.name)}" step="${step}" value="${esc(value)}">${p.unit ? `<span class="muted">${esc(p.unit)}</span>` : ""}</div>`;
  }
  if (p.type === "font") {
    const current = fontName(value);
    const list = [...new Set([current, ...(fonts || [])])].filter(Boolean);
    return `<select id="${id}" data-p="${esc(p.name)}" data-font>${list.map((f) =>
      `<option value="${esc(f)}"${f === current ? " selected" : ""}>${esc(f)}${f === current && !(fonts || []).includes(f) ? " (not on the NAS yet)" : ""}</option>`).join("")}</select>`;
  }
  if (p.type === "vector") {
    return `<input type="text" id="${id}" data-p="${esc(p.name)}" data-vector value="${esc(value.join(", "))}">`;
  }
  return `<input type="text" id="${id}" data-p="${esc(p.name)}" value="${esc(value)}">`;
}

// Parts that belong in one multi-colour print usually share a name prefix
// ("SignBase", "SignWhite"); a "Clip" printed apart does not. Pick the largest
// such group as the default for the 3MF. Remembered per file after that.
function guessCombine(parts) {
  let best = [];
  for (const a of parts) {
    for (let n = Math.min(a.name.length, 12); n >= 3; n--) {
      const group = parts.filter((p) => p.name.slice(0, n).toLowerCase() === a.name.slice(0, n).toLowerCase());
      if (group.length >= 2) { if (group.length > best.length) best = group; break; }
    }
  }
  return best.map((p) => p.name);
}
const PARTS_KEY = "pp.param.parts";
function loadParts(fileId) {
  try { return JSON.parse(localStorage.getItem(PARTS_KEY) || "{}")[fileId] || null; } catch { return null; }
}
function saveParts(fileId, choice) {
  try {
    const all = JSON.parse(localStorage.getItem(PARTS_KEY) || "{}");
    all[fileId] = choice;
    localStorage.setItem(PARTS_KEY, JSON.stringify(all));
  } catch { /* private mode */ }
}

export async function openCustomizeDialog(file, onDone) {
  const dlg = document.createElement("dialog");
  dlg.className = "slice-dialog param-dialog";
  dlg.innerHTML = `<h2>Reading ${esc(file.name)}...</h2>`;
  document.body.appendChild(dlg);
  let timer = null;
  let finished = null;
  dlg.addEventListener("close", () => { clearTimeout(timer); dlg.remove(); if (finished) onDone?.(finished); });
  dlg.showModal();

  let info;
  try {
    info = await api(`/api/parametric/${encodeURIComponent(file.id)}`);
  } catch (e) {
    dlg.innerHTML = `<h2>Can't customize this</h2><div class="notice error">${esc(e.message)}</div>
      <div class="dialog-actions"><button class="ghost" data-close>Close</button></div>`;
    dlg.querySelector("[data-close]").onclick = () => dlg.close();
    return;
  }
  const kind = info.file.kind;
  const tool = toolName(kind);
  const tools = info.tools || {};
  const toolOk = kind === "scad" ? tools.openscad : tools.freecad;
  const params = info.params;
  let presets = info.presets || {};
  let values = Object.fromEntries(params.map((p) => [p.name, p.value]));
  const groups = [...new Set(params.map((p) => p.group))];
  let activeGroup = groups[0];
  const allParts = info.parts.map((p) => p.name);
  const savedParts = loadParts(file.id);
  const partChoice = {
    stl: (savedParts?.stl || allParts).filter((n) => allParts.includes(n)),
    combine: (savedParts?.combine || guessCombine(info.parts)).filter((n) => allParts.includes(n)),
  };

  function formHtml() {
    return groups.map((g) => `<fieldset class="param-group" data-group="${esc(g)}"${groups.length > 1 && g !== activeGroup ? " hidden" : ""}>
      ${params.filter((p) => p.group === g).map((p) => {
        const label = p.label === p.name ? humanize(p.name) : p.label;
        if (p.type === "bool") return `<div class="param bool">${inputHtml(p, values[p.name], tools.fonts)}${p.description ? `<small class="muted">${esc(label)}</small>` : ""}</div>`;
        return `<label class="param" for="pp-${esc(p.name)}"><span class="param-label">${esc(label)}</span>
          ${p.description && p.description !== label ? `<small class="muted">${esc(p.description)}</small>` : ""}
          ${inputHtml(p, values[p.name], tools.fonts)}</label>`;
      }).join("")}</fieldset>`).join("");
  }

  const presetNames = () => Object.keys(presets).sort((a, b) => a.localeCompare(b));
  const presetOptions = (sel = "") => `<option value="">Defaults from the file</option>${presetNames().map((n) =>
    `<option value="${esc(n)}"${n === sel ? " selected" : ""}>${esc(n)}</option>`).join("")}`;

  dlg.innerHTML = `
    <h2>Customize <span class="muted">${esc(file.name)}</span></h2>
    ${toolOk ? "" : `<div class="notice warn">${tool} isn't ready on the NAS${tools.error ? ` (${esc(tools.error)})` : ""}. You can still change and save values.</div>`}
    ${params.length ? "" : `<div class="notice warn">No parameters found. ${kind === "scad"
      ? "Plain values at the top of the file (before the first module) become parameters, e.g. <code>width = 40; // [10:200]</code>."
      : "Give spreadsheet cells an alias (right-click a cell, Properties, Alias) and they become parameters."}</div>`}
    <div class="param-presets">
      <label class="field"><span>Parameter set</span><select data-preset>${presetOptions()}</select></label>
      <div class="preset-tools"><button type="button" class="small ghost" data-save>Save as...</button>
        <button type="button" class="small ghost" data-delete hidden>Delete set</button></div>
    </div>
    ${groups.length > 1 ? `<div class="param-tabs" role="tablist">${groups.map((g) =>
      `<button type="button" class="small${g === activeGroup ? "" : " ghost"}" data-tab="${esc(g)}">${esc(g)}</button>`).join("")}</div>` : ""}
    <form class="param-form" data-form>${formHtml()}</form>
    ${info.parts.length ? `<div class="field parts-field"><span>Parts</span><table class="parts-table">
      <thead><tr><th></th><th>STL</th>${info.parts.length > 1 ? `<th title="Put these parts together in one 3MF: the first ticked part prints with filament 1, the next with filament 2, and so on">Multi-colour 3MF</th>` : ""}</tr></thead>
      <tbody>${info.parts.map((p) => `<tr><td>${esc(p.label)}</td>
        <td><input type="checkbox" data-part value="${esc(p.name)}"${partChoice.stl.includes(p.name) ? " checked" : ""} aria-label="${esc(p.label)} as STL"></td>
        ${info.parts.length > 1 ? `<td><input type="checkbox" data-combine value="${esc(p.name)}"${partChoice.combine.includes(p.name) ? " checked" : ""} aria-label="${esc(p.label)} in the 3MF"></td>` : ""}</tr>`).join("")}
      </tbody></table></div>` : ""}
    <label class="field"><span>Save as</span><input type="text" data-variant value="custom" maxlength="60"
      title="Name added to the file: ${esc(file.name.replace(/\.[^.]+$/, ""))} - <name>.stl. The same name replaces the last one."></label>
    <div data-progress></div>
    <div class="dialog-actions"><button class="ghost" data-close>Close</button>
      <button data-go${toolOk && params.length + info.parts.length ? "" : " disabled"}>Make STL</button></div>`;

  const $ = (sel) => dlg.querySelector(sel);
  const form = $("[data-form]"), progress = $("[data-progress]"), go = $("[data-go]");
  const presetSel = $("[data-preset]"), variant = $("[data-variant]"), del = $("[data-delete]");

  function read() {
    form.querySelectorAll("[data-p]").forEach((el) => {
      const p = params.find((x) => x.name === el.dataset.p);
      if (!p) return;
      if (el.type === "checkbox") values[p.name] = el.checked;
      else if (el.type === "number" || (p.type === "choice" && typeof p.value === "number")) values[p.name] = el.value === "" ? p.value : Number(el.value);
      else if ("vector" in el.dataset) values[p.name] = el.value.split(/[,\s]+/).filter(Boolean).map(Number);
      else values[p.name] = el.value;
    });
    return values;
  }
  function wire() {
    form.querySelectorAll("[data-range]").forEach((r) => {
      const box = form.querySelector(`[data-p="${CSS.escape(r.dataset.range)}"]`);
      r.oninput = () => { box.value = r.value; };
      box.oninput = () => { r.value = box.value; };
    });
  }
  function redraw() { form.innerHTML = formHtml(); wire(); }
  wire();

  dlg.querySelectorAll("[data-tab]").forEach((b) => (b.onclick = () => {
    activeGroup = b.dataset.tab;
    dlg.querySelectorAll("[data-tab]").forEach((x) => x.classList.toggle("ghost", x !== b));
    form.querySelectorAll("[data-group]").forEach((fs) => (fs.hidden = fs.dataset.group !== activeGroup));
  }));

  presetSel.onchange = () => {
    const set = presets[presetSel.value] || {};
    values = Object.fromEntries(params.map((p) => [p.name, presetSel.value ? fromSaved(p, set[p.name]) : p.value]));
    variant.value = presetSel.value || "custom";
    del.hidden = !presetSel.value;
    redraw();
  };
  $("[data-save]").onclick = async () => {
    const name = (prompt("Name for this parameter set", presetSel.value || variant.value !== "custom" ? variant.value : "") || "").trim();
    if (!name) return;
    try {
      const r = await api(`/api/parametric/${encodeURIComponent(file.id)}/presets`, json("PUT", { name, values: read() }));
      presets = r.presets;
      presetSel.innerHTML = presetOptions(name);
      variant.value = name;
      del.hidden = false;
      progress.innerHTML = `<div class="notice">Saved "${esc(name)}" next to ${esc(file.name)}.</div>`;
    } catch (e) { progress.innerHTML = `<div class="notice error">${esc(e.message)}</div>`; }
  };
  del.onclick = async () => {
    const name = presetSel.value;
    if (!name || !confirm(`Delete the parameter set "${name}"?`)) return;
    try {
      const r = await api(`/api/parametric/${encodeURIComponent(file.id)}/presets`, json("PUT", { name, values: null }));
      presets = r.presets;
      presetSel.innerHTML = presetOptions("");
      presetSel.onchange();
    } catch (e) { progress.innerHTML = `<div class="notice error">${esc(e.message)}</div>`; }
  };
  $("[data-close]").onclick = () => dlg.close();

  const lock = (on) => dlg.querySelectorAll("select, input, [data-save], [data-delete], [data-tab]").forEach((x) => (x.disabled = on));
  go.onclick = async () => {
    const body = {
      file_id: file.id, values: read(), variant: variant.value.trim() || "custom",
      parts: [...dlg.querySelectorAll("[data-part]:checked")].map((c) => c.value),
      combine: [...dlg.querySelectorAll("[data-combine]:checked")].map((c) => c.value),
    };
    if (body.combine.length < 2) body.combine = [];
    if (info.parts.length && !body.parts.length && !body.combine.length) {
      progress.innerHTML = `<div class="notice error">Tick at least one part (or two for the 3MF).</div>`;
      return;
    }
    if (info.parts.length) saveParts(file.id, { stl: body.parts, combine: body.combine });
    // Only send what differs from the file, so the file's own formulas and
    // unit handling stay in charge of everything else.
    const changed = {};
    for (const p of params) if (JSON.stringify(body.values[p.name]) !== JSON.stringify(p.value)) changed[p.name] = body.values[p.name];
    for (const p of params) if (p.type === "font" && p.name in changed) changed[p.name] = fontName(changed[p.name]);
    body.values = changed;
    go.disabled = true;
    go.textContent = "Working...";
    lock(true);
    progress.innerHTML = `<div class="notice">Sending to ${tool} on the NAS...</div>`;
    let job;
    try {
      job = await api("/api/parametric/jobs", json("POST", body));
    } catch (e) {
      progress.innerHTML = `<div class="notice error">${esc(e.message)}</div>`;
      go.disabled = false;
      go.textContent = "Make STL";
      lock(false);
      return;
    }
    const started = Date.now();
    const poll = async () => {
      try { job = await api(`/api/parametric/jobs/${job.id}`); } catch (e) { job = { ...job, message: e.message }; }
      const secs = Math.round((Date.now() - started) / 1000);
      if (job.state === "done") {
        const warn = job.warnings?.length ? `<div class="notice warn"><ul>${job.warnings.map((w) => `<li>${esc(w)}</li>`).join("")}</ul></div>` : "";
        progress.innerHTML = `<div class="notice success"><span class="notice-icon">✓</span><div class="notice-body"><strong>Done in ${secs} s!</strong>
          Saved ${job.outputs.map((p) => `<code>${esc(p.split("/").pop())}</code>`).join(", ")}. Close to see it and slice it.</div></div>${warn}`;
        finished = job;
        go.textContent = "Make STL";
        go.disabled = false;
        lock(false);
        $("[data-close]").textContent = "Show it";
        return;
      }
      if (job.state === "failed") {
        progress.innerHTML = `<div class="notice error">${esc(job.message)}${job.log ? `<details><summary>${tool} log</summary><pre>${esc(job.log.slice(-3000))}</pre></details>` : ""}</div>`;
        go.textContent = "Make STL";
        go.disabled = false;
        lock(false);
        return;
      }
      const msg = job.state === "queued" ? "Waiting for the slicer to finish another job..." : job.message || "Rendering...";
      progress.innerHTML = `<div class="notice"><span class="spinner"></span> ${esc(msg)} <span class="muted">${secs} s</span></div>`;
      timer = setTimeout(poll, 1200);
    };
    poll();
  };
}
