// "Process settings" in the Slice dialog: OrcaSlicer's process tab (Quality,
// Strength, Speed, Support, Multimaterial, Others) as changes on top of the
// picked process preset or the project's own settings, plus named presets of
// those changes, shared by all printers.
const esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

async function api(path, opts) {
  const res = await fetch(path, opts);
  if (res.status === 401) { location.href = "/login"; throw new Error("login"); }
  if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText);
  return res.json();
}

let CATALOG = null;
async function catalog() {
  if (!CATALOG) {
    CATALOG = await api("/static/process_catalog.json");
    CATALOG.byKey = {};
    CATALOG.tabs.forEach((t) => t.groups.forEach((g) => g.options.forEach((o) => (CATALOG.byKey[o.key] = o))));
  }
  return CATALOG;
}

const PRINTER_NAMES = { u1: "U1", p1s: "P1S" };
const LAST_KEY = "pp.slice.processPreset";
const remembered = (printerId) => { try { return JSON.parse(localStorage.getItem(LAST_KEY) || "{}")[printerId] || ""; } catch { return ""; } };
function remember(printerId, name) {
  try {
    const all = JSON.parse(localStorage.getItem(LAST_KEY) || "{}");
    all[printerId] = name;
    localStorage.setItem(LAST_KEY, JSON.stringify(all));
  } catch { /* private mode */ }
}

// How a value is shown in its input ("15%" -> "15" for percent settings).
function shown(opt, v) {
  if (v == null) return "";
  v = Array.isArray(v) ? v[0] : v;
  v = String(v);
  if (opt.type === "percent") return v.replace(/%$/, "");
  return v;
}
// Same value? ("0.20" == "0.2", "15" == "15%" for percent settings)
function same(opt, a, b) {
  a = shown(opt, a); b = shown(opt, b);
  if (a === b) return true;
  const na = Number(a.replace(/%$/, "")), nb = Number(b.replace(/%$/, ""));
  return a.endsWith("%") === b.endsWith("%") && a !== "" && b !== "" && !isNaN(na) && !isNaN(nb) && Math.abs(na - nb) < 1e-9;
}
// Would the app take this value? (A Bambu project can hold values OrcaSlicer rejects.)
function acceptable(opt, v) {
  const s = shown(opt, v);
  if (opt.type === "enum") return (opt.choices || []).some((c) => c[0] === s);
  if (opt.type === "bool") return s === "0" || s === "1";
  if (opt.type === "string") return s.length <= 200 && !/[\r\n]/.test(s);
  const pct = opt.type === "floatpct" && s.endsWith("%");
  const n = Number(pct ? s.slice(0, -1) : s);
  if (s === "" || isNaN(n)) return false;
  if (opt.type === "int" && !Number.isInteger(n)) return false;
  if (!pct && opt.min != null && n < opt.min) return false;
  if (!pct && opt.max != null && n > opt.max) return false;
  return true;
}

// ctx: { printerId, fileId, base() -> { mode: "project" (the 3MF's own settings) | "presets", process, machine } }
export function createProcessPanel(box, ctx) {
  const state = { cat: null, base: {}, baseKey: "", overrides: {}, presets: [], loaded: "", open: false,
    tab: 0, advanced: false, filter: "", saving: false, error: "" };
  box.className = "field proc-field";
  box.innerHTML = `<span>Settings</span><div class="proc"><small class="muted">Loading settings...</small></div>`;
  const root = box.querySelector(".proc");

  const changedKeys = () => Object.keys(state.overrides);
  const label = (k) => state.cat?.byKey[k]?.label || k;
  const valueText = (k) => {
    const o = state.cat.byKey[k], v = shown(o, state.overrides[k]);
    if (o.type === "bool") return v === "1" ? "on" : "off";
    if (o.type === "enum") return (o.choices.find((c) => c[0] === v) || [v, v])[1];
    return `${v}${o.type === "percent" ? "%" : o.unit && !/%/.test(o.unit) ? ` ${o.unit}` : ""}`;
  };

  function summary() {
    const keys = changedKeys();
    if (!keys.length) return `<small class="hint">${state.open ? "" : "No changes. Edit to change walls, infill, speeds, supports and more, like in OrcaSlicer."}</small>`;
    const list = keys.slice(0, 6).map((k) => `${esc(label(k))} <b>${esc(valueText(k))}</b>`).join(" · ");
    return `<small class="hint proc-summary">${keys.length} changed: ${list}${keys.length > 6 ? ` · and ${keys.length - 6} more` : ""}</small>`;
  }

  function presetOptions() {
    const opts = [`<option value="">${changedKeys().length && !state.loaded ? "Your changes (not saved)" : "No saved preset"}</option>`];
    for (const p of state.presets) {
      const where = PRINTER_NAMES[p.printer] || p.printer;
      opts.push(`<option value="${esc(p.name)}"${p.name === state.loaded ? " selected" : ""}>${esc(p.name)} · ${Object.keys(p.overrides).length} setting${Object.keys(p.overrides).length === 1 ? "" : "s"}${where && where.toLowerCase() !== ctx.printerId ? ` · made on ${esc(where)}` : ""}</option>`);
    }
    return opts.join("");
  }

  function input(o) {
    const has = o.key in state.overrides;
    const v = shown(o, has ? state.overrides[o.key] : state.base[o.key]);
    const known = o.key in state.base || has;
    const tip = esc(o.tooltip || "");
    let ctl;
    if (o.type === "bool") {
      ctl = `<input type="checkbox" data-k="${o.key}"${v === "1" ? " checked" : ""}>`;
    } else if (o.type === "enum") {
      const choices = o.choices.some((c) => c[0] === v) || !v ? o.choices : [[v, v], ...o.choices];
      ctl = `<select data-k="${o.key}">${v ? "" : `<option value="" selected>(default)</option>`}${choices.map((c) =>
        `<option value="${esc(c[0])}"${c[0] === v ? " selected" : ""}>${esc(c[1])}</option>`).join("")}</select>`;
    } else {
      const unit = o.type === "percent" ? "%" : o.unit || "";
      ctl = `<span class="proc-num"><input type="text" inputmode="${o.type === "string" ? "text" : "decimal"}" data-k="${o.key}" value="${esc(v)}"${known ? "" : ` placeholder="default"`}>${unit ? `<small>${esc(unit)}</small>` : ""}</span>`;
    }
    const baseV = state.base[o.key];
    const was = has && baseV != null ? `<small class="proc-was">was ${esc(o.type === "bool" ? (shown(o, baseV) === "1" ? "on" : "off")
      : o.type === "enum" ? (o.choices.find((c) => c[0] === shown(o, baseV)) || [0, shown(o, baseV)])[1] : shown(o, baseV))}</small>` : "";
    return `<label class="proc-opt${has ? " changed" : ""}" title="${tip}"><span class="proc-label">${esc(o.label)}${o.advanced ? "" : ""}</span>
      <span class="proc-ctl">${ctl}${has ? `<button type="button" class="proc-reset" data-reset="${o.key}" title="Undo this change">↺</button>` : ""}</span>${was}</label>`;
  }

  function visible(o) {
    if (state.filter) {
      const f = state.filter.toLowerCase();
      return o.label.toLowerCase().includes(f) || o.key.includes(f) || (o.tooltip || "").toLowerCase().includes(f);
    }
    return state.advanced || !o.advanced || o.key in state.overrides;
  }

  function editor() {
    const cat = state.cat;
    const tabs = cat.tabs.map((t, i) => {
      const n = t.groups.reduce((a, g) => a + g.options.filter((o) => o.key in state.overrides).length, 0);
      return `<button type="button" class="proc-tab" data-tab="${i}" aria-pressed="${!state.filter && i === state.tab}">${esc(t.name)}${n ? ` <b>${n}</b>` : ""}</button>`;
    }).join("");
    const shownTabs = state.filter ? cat.tabs : [cat.tabs[state.tab]];
    const groups = shownTabs.flatMap((t) => t.groups.map((g) => {
      const opts = g.options.filter(visible);
      return opts.length ? `<fieldset class="proc-group"><legend>${state.filter ? `${esc(t.name)} › ` : ""}${esc(g.name)}</legend>${opts.map(input).join("")}</fieldset>` : "";
    })).join("");
    return `<div class="proc-editor">
      <div class="proc-tabs">${tabs}</div>
      <div class="proc-tools">
        <input type="search" data-filter placeholder="Find a setting" value="${esc(state.filter)}">
        <label class="check"><input type="checkbox" data-adv${state.advanced ? " checked" : ""}> Advanced</label>
      </div>
      <div class="proc-groups">${groups || `<small class="hint">Nothing here${state.advanced ? "" : " without Advanced"}.</small>`}</div>
    </div>`;
  }

  function actions() {
    const n = changedKeys().length;
    return `<div class="proc-actions">
      ${state.saving ? `<input type="text" data-name maxlength="60" placeholder="Preset name" value="${esc(state.loaded)}"><button type="button" data-save-go>Save</button><button type="button" class="ghost" data-save-cancel>Cancel</button>`
        : `<button type="button" class="ghost" data-save${n ? "" : " disabled"}>Save as preset</button>
           ${state.loaded ? `<button type="button" class="ghost" data-delete>Delete "${esc(state.loaded)}"</button>` : ""}
           ${n ? `<button type="button" class="ghost" data-clear>Undo all changes</button>` : ""}`}
    </div>${state.error ? `<div class="notice error">${esc(state.error)}</div>` : ""}`;
  }

  function render() {
    if (!state.cat) return;
    const scroll = root.querySelector(".proc-groups")?.scrollTop || 0;
    const focus = document.activeElement?.matches?.("[data-filter]");
    root.innerHTML = `
      <div class="proc-head">
        <select data-preset aria-label="Saved settings">${presetOptions()}</select>
        <button type="button" class="ghost" data-toggle aria-expanded="${state.open}">${state.open ? "Hide" : "Edit"}</button>
      </div>
      ${summary()}
      ${state.open ? editor() + actions() : ""}`;
    const g = root.querySelector(".proc-groups");
    if (g) g.scrollTop = scroll;
    if (focus) { const f = root.querySelector("[data-filter]"); f.focus(); f.setSelectionRange(f.value.length, f.value.length); }
  }

  function setValue(k, v) {
    const o = state.cat.byKey[k];
    if (state.base[k] != null && same(o, v, state.base[k])) delete state.overrides[k];
    else state.overrides[k] = o.type === "percent" && v !== "" ? `${String(v).replace(/%$/, "")}%` : v;
    state.error = "";
  }

  root.addEventListener("change", (e) => {
    const t = e.target;
    if (t.matches("[data-preset]")) return loadPreset(t.value);
    if (t.matches("[data-adv]")) { state.advanced = t.checked; return render(); }
    const k = t.dataset.k;
    if (!k) return;
    setValue(k, t.type === "checkbox" ? (t.checked ? "1" : "0") : t.value.trim());
    render();
  });
  root.addEventListener("input", (e) => {
    if (e.target.matches("[data-filter]")) { state.filter = e.target.value.trim(); render(); }
  });
  root.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && e.target.matches("[data-name]")) { e.preventDefault(); save(e.target.value); }
    else if (e.key === "Enter" && e.target.matches("[data-k]")) { e.preventDefault(); e.target.blur(); }
  });
  root.addEventListener("click", async (e) => {
    const b = e.target.closest("button");
    if (!b) return;
    if (b.matches("[data-toggle]")) { state.open = !state.open; render(); }
    else if (b.matches("[data-tab]")) { state.tab = Number(b.dataset.tab); state.filter = ""; render(); }
    else if (b.matches("[data-reset]")) { delete state.overrides[b.dataset.reset]; render(); }
    else if (b.matches("[data-clear]")) { state.overrides = {}; state.loaded = ""; remember(ctx.printerId, ""); render(); }
    else if (b.matches("[data-save]")) { state.saving = true; render(); root.querySelector("[data-name]")?.focus(); }
    else if (b.matches("[data-save-cancel]")) { state.saving = false; render(); }
    else if (b.matches("[data-save-go]")) save(root.querySelector("[data-name]").value);
    else if (b.matches("[data-delete]")) del();
  });

  async function save(name) {
    name = (name || "").trim();
    if (!name) { state.error = "Give the preset a name"; return render(); }
    const base = ctx.base();
    try {
      const p = await api("/api/slicer/process-presets", { method: "PUT", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name, printer: ctx.printerId, process: base.mode === "project" ? "(project settings)" : base.process,
          machine: base.machine, overrides: state.overrides }) });
      state.presets = state.presets.filter((x) => x.name !== p.name).concat(p).sort((a, b) => a.name.localeCompare(b.name));
      state.loaded = p.name;
      state.overrides = { ...p.overrides };
      state.saving = false;
      state.error = "";
      remember(ctx.printerId, p.name);
    } catch (err) { state.error = err.message; }
    render();
  }
  async function del() {
    const name = state.loaded;
    try {
      await api(`/api/slicer/process-presets?name=${encodeURIComponent(name)}`, { method: "DELETE" });
      state.presets = state.presets.filter((x) => x.name !== name);
      state.loaded = "";
      remember(ctx.printerId, "");
    } catch (err) { state.error = err.message; }
    render();
  }
  function loadPreset(name) {
    const p = state.presets.find((x) => x.name === name);
    state.loaded = p ? p.name : "";
    state.overrides = p ? { ...p.overrides } : {};
    // Values that match this printer's preset aren't changes here.
    for (const k of Object.keys(state.overrides)) {
      const o = state.cat.byKey[k];
      if (!o) delete state.overrides[k];
      else if (state.base[k] != null && same(o, state.overrides[k], state.base[k])) delete state.overrides[k];
    }
    remember(ctx.printerId, state.loaded);
    render();
  }
  // The values shown come from the picked process preset (or the project).
  let seq = 0;
  async function refreshBase() {
    const b = ctx.base();
    const key = `${b.mode}|${b.process}`;
    if (key === state.baseKey || (!b.process && b.mode !== "project")) return;
    state.baseKey = key;
    const mine = ++seq;
    try {
      const get = (mode) => api(`/api/slicer/process?${new URLSearchParams({ printer: ctx.printerId, process: b.process || "", mode, file_id: ctx.fileId })}`);
      // The author's profile goes on top of the process preset (which fills in what the file lacks).
      const [preset, own] = await Promise.all([b.process ? get("presets") : { values: {} }, b.mode === "project" ? get("project") : null]);
      if (mine !== seq) return;
      state.base = { ...(preset.values || {}), ...(own?.values || {}) };
    } catch (err) {
      if (mine !== seq) return;
      state.base = {};
      state.error = `Couldn't read the preset's values: ${err.message}`;
    }
    // A change that now equals the base isn't a change.
    for (const k of Object.keys(state.overrides)) {
      const o = state.cat.byKey[k];
      if (state.base[k] != null && same(o, state.overrides[k], state.base[k]) && !state.loaded) delete state.overrides[k];
    }
    render();
  }

  (async () => {
    try {
      state.cat = await catalog();
      state.presets = (await api("/api/slicer/process-presets")).presets || [];
    } catch (err) {
      root.innerHTML = `<small class="hint">Process settings aren't available: ${esc(err.message)}</small>`;
      return;
    }
    await refreshBase();
    const want = remembered(ctx.printerId);
    if (want && state.presets.some((p) => p.name === want)) loadPreset(want);
    else render();
  })();

  return {
    refresh: refreshBase,
    // Settings to send with the slice; checks values the way the app will.
    overrides() {
      if (!state.cat) return {};
      const bad = Object.entries(state.overrides).filter(([k, v]) => !acceptable(state.cat.byKey[k], v));
      if (bad.length) throw new Error(`Check ${bad.map(([k]) => label(k)).join(", ")}`);
      return { ...state.overrides };
    },
    get name() { return state.loaded; },
  };
}
