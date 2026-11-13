// Printer pages: live status cards (#/printers) and the "Print on ..." dialog.
import { swatchHtml } from "./filament.js";
import { calibrationDialog, calibrationHtml, flowSection, flowWizard } from "./calibration.js";
const esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

async function api(path, opts) {
  const res = await fetch(path, opts);
  if (res.status === 401) { location.href = "/login"; throw new Error("login"); }
  if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText);
  return res.json();
}

function fmtDur(sec) {
  if (sec == null) return "";
  sec = Math.max(0, Math.round(sec));
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60);
  return h ? `${h}h ${String(m).padStart(2, "0")}m` : `${m}m`;
}
const fmtTemp = (t) => (t == null ? "–" : `${Math.round(t)}°`);
export function fmtAge(ts) {
  if (!ts) return "";
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 5) return "just now";
  if (s < 90) return `${Math.round(s)} s ago`;
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  return `${(s / 3600).toFixed(1)} h ago`;
}

const STATE_LABEL = {
  offline: "Offline", idle: "Ready", printing: "Printing", paused: "Paused", complete: "Finished",
  cancelled: "Cancelled", error: "Error", starting: "Starting up", not_set_up: "Not set up",
};

let PRINTERS = null;
export async function loadPrinters() {
  try { PRINTERS = (await api("/api/printers")).printers; } catch { PRINTERS = PRINTERS || []; }
  return PRINTERS;
}
export const canPrintOn = (id) => !!PRINTERS?.find((p) => p.id === id && p.can_print);

// ---------------------------------------------------------------- camera
// Swap frames only once loaded so the picture never flashes blank.
export function cameraLoop(img, printerId, every, onFrame) {
  let timer = null, alive = true, busy = false;
  async function tick() {
    if (!alive) return;
    if (!document.hidden && !busy) {
      busy = true;
      try {
        const res = await fetch(`/api/printers/${printerId}/snapshot?t=${Date.now()}`, { cache: "no-store" });
        if (res.ok) {
          const taken = Number(res.headers.get("X-Frame-Time")) || null;
          const url = URL.createObjectURL(await res.blob());
          const old = img.src;
          img.onload = () => { if (old.startsWith("blob:")) URL.revokeObjectURL(old); };
          img.src = url;
          img.classList.remove("nocam");
          onFrame?.(taken, "");
        } else {
          onFrame?.(null, (await res.json().catch(() => ({}))).detail || "Camera unavailable");
        }
      } catch (e) { onFrame?.(null, e.message); }
      busy = false;
    }
    clearTimeout(timer);
    timer = setTimeout(tick, every);
  }
  tick();
  const stop = () => { alive = false; clearTimeout(timer); };
  // Fetch a new frame after `delay` ms instead of waiting for the next tick
  // (used after the light is switched, so the picture shows the change).
  stop.kick = (delay = 0) => {
    if (!alive) return;
    clearTimeout(timer);
    timer = setTimeout(tick, delay);
  };
  return stop;
}

// ---------------------------------------------------------------- status card
function tempsHtml(st) {
  return st.temps.map((t) => `<span class="temp${t.target ? " hot" : ""}"><b>${esc(t.name)}</b> ${fmtTemp(t.current)}${t.target ? ` / ${fmtTemp(t.target)}` : ""}</span>`).join("");
}
function toolsHtml(st) {
  return st.toolheads.map((t) => `<button type="button" data-tool="${t.index}" class="tool${t.active ? " active" : ""}${t.loaded === false ? " empty" : ""}"
      title="${esc([t.vendor, t.type].filter(Boolean).join(" ") || "Unknown")}${t.custom_color ? " (colour set by hand)" : ""}. Click to set the colour.">
      <i style="--c:${esc(t.color || "transparent")}"></i>T${t.index + 1} ${esc(t.loaded === false ? "empty" : t.type || "?")}${t.custom_color ? " ✎" : ""}</button>`).join("");
}

const FILAMENT_COLORS = [
  ["White", "#FFFFFF"], ["Black", "#1A1A1A"], ["Grey", "#8A8D8F"], ["Silver", "#C0C0C0"],
  ["Yellow", "#FFD400"], ["Orange", "#FF7A00"], ["Red", "#D7263D"], ["Pink", "#F49AC2"],
  ["Purple", "#7B4FBF"], ["Blue", "#1F5FBF"], ["Light blue", "#6EC1E4"], ["Green", "#2E9E44"],
  ["Lime", "#A4D233"], ["Brown", "#7A4E2D"], ["Beige", "#E8D5B0"], ["Gold", "#C9A227"],
];

// Closest everyday name for a colour, e.g. "#FFD200" -> "Yellow".
function colorName(hex) {
  const rgb = (h) => [1, 3, 5].map((k) => parseInt(h.slice(k, k + 2), 16));
  if (!/^#[0-9a-f]{6}$/i.test(hex || "")) return "";
  const [r, g, b] = rgb(hex);
  let best = "", dist = Infinity;
  for (const [n, c] of FILAMENT_COLORS) {
    const [R, G, B] = rgb(c);
    const d = (r - R) ** 2 * 0.3 + (g - G) ** 2 * 0.59 + (b - B) ** 2 * 0.11;
    if (d < dist) { dist = d; best = n; }
  }
  return best;
}

// Pick the colour shown for a toolhead. Saved in Print Pantry; it resets by itself
// when the printer reports a different spool for that toolhead.
function colorDialog(p, t, onSaved) {
  const dlg = document.createElement("dialog");
  dlg.innerHTML = `<h2>T${t.index + 1} colour</h2>
    <p class="sub" style="margin-bottom:12px">${esc([t.vendor, t.type].filter(Boolean).join(" ") || "Filament")} on ${esc(p.name)}.
      This only changes what Print Pantry shows. It resets when you load a different spool.</p>
    <div class="colors big">${FILAMENT_COLORS.map(([n, c]) =>
      `<button type="button" style="--c:${c}" data-color="${c}" title="${n}" aria-label="${n}" aria-pressed="${c === (t.color || "").toUpperCase()}"></button>`).join("")}</div>
    <div class="dialog-row"><label>Other colour <input type="color" data-custom value="${esc(t.color || "#FFFFFF")}"></label></div>
    <div class="dialog-actions">
      ${t.custom_color ? `<button type="button" class="ghost" data-reset>Use the printer's colour</button>` : ""}
      <button type="button" class="ghost" data-close>Cancel</button></div>`;
  document.body.appendChild(dlg);
  const save = async (color) => {
    try {
      const res = await api(`/api/printers/${p.id}/toolheads/${t.index}/color`, {
        method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ color }),
      });
      dlg.close();
      onSaved(res);
    } catch (e) { alert(e.message); }
  };
  dlg.addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (!b) return;
    if (b.dataset.color) save(b.dataset.color);
    else if ("reset" in b.dataset) save(null);
    else if ("close" in b.dataset) dlg.close();
  });
  dlg.querySelector("[data-custom]").addEventListener("change", (e) => save(e.target.value.toUpperCase()));
  dlg.addEventListener("close", () => dlg.remove());
  dlg.showModal();
}

function cardBody(p) {
  const st = p.status;
  if (!p.configured) return `<p class="empty">${esc(p.setup_hint)}</p>`;
  const job = ["printing", "paused"].includes(st.state) || (st.file && ["complete", "cancelled", "error"].includes(st.state));
  const pct = st.progress != null ? Math.round(st.progress * 100) : null;
  const facts = [
    st.layer != null && st.total_layers ? `Layer ${st.layer} / ${st.total_layers}` : "",
    st.print_seconds ? `${fmtDur(st.print_seconds)} elapsed` : "",
    st.eta_seconds != null && st.state === "printing" ? `about ${fmtDur(st.eta_seconds)} left` : "",
  ].filter(Boolean).map((t) => `<span>${t}</span>`).join("");
  const buttons = [];
  if (st.online && st.light != null) {
    buttons.push(`<button class="small ghost light${st.light ? " on" : ""}" data-act="${st.light ? "light_off" : "light_on"}"
      aria-pressed="${st.light}">${st.light ? "Light off" : "Light on"}</button>`);
  }
  if (st.state === "printing") buttons.push(`<button class="small ghost" data-act="pause">Pause</button>`);
  if (st.state === "paused") buttons.push(`<button class="small" data-act="resume">Resume</button>`);
  if (["printing", "paused"].includes(st.state)) buttons.push(`<button class="small danger" data-act="cancel">Cancel print</button>`);
  return `
    ${st.message && (st.state === "error" || st.state === "offline" || st.state === "starting") ? `<div class="notice error small-notice">${esc(st.message)}</div>` : ""}
    ${job ? `<div class="job">
        <div class="job-file" title="${esc(st.file)}">${esc(st.file)}</div>
        ${pct != null ? `<div class="bar"><i style="width:${pct}%"></i></div><div class="job-facts"><span><b>${pct}%</b></span>${facts}</div>` : ""}
      </div>` : ""}
    ${st.temps.length ? `<div class="temps">${tempsHtml(st)}</div>` : ""}
    ${st.toolheads.length ? `<div class="tools">${toolsHtml(st)}</div>` : ""}
    ${buttons.length ? `<div class="file-actions">${buttons.join("")}</div>` : ""}
    ${calibrationHtml(p)}`;
}

export async function printersPage(view) {
  document.title = "Printers · Print Pantry";
  const list = [...await loadPrinters()].sort((a, b) => b.configured - a.configured);
  view.innerHTML = `<div class="crumbs"><a href="#/">All categories</a></div>
    <h1>Printers</h1><p class="sub">Status updates every few seconds. The camera refreshes about every 3 seconds.</p>
    <div class="printer-grid">${list.map((p) => `
      <section class="panel printer-card" data-id="${p.id}" style="--c:${p.color}">
        <div class="printer-head"><h2><span class="badge printer" style="--c:${p.color}">${esc(p.name)}</span></h2>
          <span class="state" data-state></span></div>
        ${p.can_camera ? `<div class="cam"><img alt="${esc(p.name)} camera" class="nocam"><span class="cam-age" data-age></span></div>` : ""}
        <div data-body></div>
      </section>`).join("")}</div>
    <section class="panel flow-panel" data-flow-panel></section>`;

  const stops = [];
  const latest = new Map(list.map((p) => [p.id, p]));
  function paint(p) {
    latest.set(p.id, p);
    const card = view.querySelector(`.printer-card[data-id="${p.id}"]`);
    if (!card) return;
    const st = p.status;
    const s = card.querySelector("[data-state]");
    s.textContent = STATE_LABEL[st.state] || st.state;
    s.dataset.state = st.state;
    card.querySelector("[data-body]").innerHTML = cardBody(p);
  }
  list.forEach(paint);
  const flowBox = view.querySelector("[data-flow-panel]");
  const refreshFlow = () => flowSection(flowBox, () => [...latest.values()]);
  refreshFlow();

  const onClick = async (e) => {
    const chip = e.target.closest("[data-tool]");
    if (chip) {
      const p = latest.get(chip.closest(".printer-card").dataset.id);
      const t = p?.status.toolheads.find((x) => x.index === Number(chip.dataset.tool));
      if (t) colorDialog(p, t, paint);
      return;
    }
    const cal = e.target.closest("[data-calib]");
    if (cal) {
      const p = latest.get(cal.closest(".printer-card").dataset.id);
      if (p) calibrationDialog(p, cal.dataset.calib, paint);
      return;
    }
    const fl = e.target.closest("[data-flow]");
    if (fl) {
      flowWizard([...latest.values()], { printer: fl.dataset.flow }, refreshFlow);
      return;
    }
    const b = e.target.closest("[data-act]");
    if (!b) return;
    const card = b.closest(".printer-card");
    if (!card) return;
    const act = b.dataset.act;
    if (act === "cancel" && !confirm("Cancel this print? It can't be resumed.")) return;
    if (act === "estop" && !confirm("Emergency stop? Motors and heaters stop at once and the printer's firmware restarts.")) return;
    b.disabled = true;
    try {
      paint(await api(`/api/printers/${card.dataset.id}/${act}`, { method: "POST" }));
      if (act.startsWith("light")) {
        // The camera writes a frame about every 2 s and its exposure takes a few
        // seconds to settle, so grab a few quick frames after the switch.
        const cam = cams.get(card.dataset.id);
        [800, 2500, 5000].forEach((ms) => setTimeout(() => cam?.kick(), ms));
      }
    }
    catch (err) { alert(err.message); b.disabled = false; }
  };
  view.addEventListener("click", onClick);
  stops.push(() => view.removeEventListener("click", onClick));
  const cams = new Map();

  for (const p of list.filter((x) => x.can_camera)) {
    const card = view.querySelector(`.printer-card[data-id="${p.id}"]`);
    const age = card.querySelector("[data-age]");
    let taken = null, err = "";
    const label = () => { age.textContent = err || (taken ? `Picture from ${fmtAge(taken)}` : ""); };
    const cam = cameraLoop(card.querySelector(".cam img"), p.id, 3000, (t, e) => { taken = t; err = e; label(); });
    cams.set(p.id, cam);
    stops.push(cam);
    const ageTimer = setInterval(label, 1000);
    stops.push(() => clearInterval(ageTimer));
  }

  let alive = true;
  (async function poll() {
    while (alive) {
      await new Promise((r) => setTimeout(r, 3000));
      if (!alive || document.hidden) continue;
      try { (await loadPrinters()).forEach(paint); } catch { /* keep the last view */ }
    }
  })();
  stops.push(() => { alive = false; });
  return () => stops.forEach((f) => f());
}

// ---------------------------------------------------------------- print dialog
const swatch = (type, color) => swatchHtml(type, color);

export async function openPrintDialog(fileId, printerId) {
  const dlg = document.createElement("dialog");
  dlg.className = "print-dialog";
  dlg.innerHTML = `<h2>Checking the printer...</h2>`;
  document.body.appendChild(dlg);
  let stopCam = null;
  dlg.addEventListener("close", () => { stopCam?.(); dlg.remove(); });
  dlg.showModal();

  let chk;
  try {
    chk = await api(`/api/printers/${printerId}/check?file_id=${encodeURIComponent(fileId)}`);
  } catch (e) {
    dlg.innerHTML = `<h2>Can't print right now</h2><div class="notice error">${esc(e.message)}</div>
      <div class="dialog-actions"><button class="ghost" data-close>Close</button></div>`;
    dlg.querySelector("[data-close]").onclick = () => dlg.close();
    return;
  }
  const p = chk.printer, f = chk.file;
  const plate = (f.plates || [])[0] || {};
  const heads = () => chk.printer.status.toolheads || [];
  let map = { ...chk.tool_map };  // file filament -> toolhead, as the server suggested

  // One button per toolhead, showing its colour, so you can see which spool you're picking.
  const headChip = (t, h) => {
    const empty = h.loaded === false;
    const what = empty ? "empty" : [h.type || "?", h.vendor].filter(Boolean).join(" ");
    const cname = !empty && h.color ? colorName(h.color) : "";
    return `<button type="button" class="headpick${empty ? " empty" : ""}" data-map="${t.file_index}" data-head="${h.index}"
        aria-pressed="${h.index === t.index}" title="T${h.index + 1}: ${esc([cname, what].filter(Boolean).join(" "))}${h.custom_color ? " (colour set by hand)" : ""}">
        <i style="--c:${esc(empty ? "transparent" : h.color || "#999")}"></i>
        <span><b>T${h.index + 1}</b> ${esc(cname)}<small>${esc(what)}</small></span></button>`;
  };
  function checksHtml() {
    const rows = chk.tools.map((t) => `<tr class="${t.ok ? "" : "warn"}">
        <td>${swatch(t.file.type, t.file.color)}<small class="muted"> filament ${t.file_index + 1}</small></td>
        <td>${heads().length > 1 ? `<div class="headpicks" role="group" aria-label="Toolhead for filament ${t.file_index + 1}">${heads().map((h) => headChip(t, h)).join("")}</div>`
          : `T${t.index + 1} ${t.loaded ? swatch(t.loaded.type, t.loaded.color) : "<em>unknown</em>"}`}</td>
        <td>${t.ok ? "✓" : "⚠"}</td></tr>`).join("");
    return `
      ${chk.blockers.length ? `<div class="notice error"><strong>Can't start:</strong><ul>${chk.blockers.map((b) => `<li>${esc(b)}</li>`).join("")}</ul></div>` : ""}
      ${rows ? `<table class="toolmap"><thead><tr><th>File needs</th><th>Print with</th><th></th></tr></thead><tbody>${rows}</tbody></table>
        <p class="hint">The file's colour is whatever the slicer had set; only the material is checked.</p>` : ""}
      ${chk.warnings.length ? `<div class="notice warn"><ul>${chk.warnings.map((w) => `<li>${esc(w)}</li>`).join("")}</ul></div>
        <label class="check"><input type="checkbox" data-ack> I've checked the toolheads and want to print anyway</label>` : ""}`;
  }

  dlg.innerHTML = `
    <h2>Print on ${esc(p.name)}</h2>
    <p class="sub" style="margin-bottom:12px"><strong>${esc(f.name)}</strong>${plate.seconds ? ` · about ${fmtDur(plate.seconds)}` : ""}</p>
    ${p.can_camera ? `<div class="cam"><img alt="Camera" class="nocam"><span class="cam-age" data-age>Getting a fresh picture...</span></div>` : ""}
    <div data-checks>${checksHtml()}</div>
    <label class="check"><input type="checkbox" data-bed> The bed is clear and the build plate is in place</label>
    <div class="dialog-actions"><button class="ghost" data-close>Cancel</button><button data-go disabled>Start print</button></div>`;

  const go = dlg.querySelector("[data-go]");
  const bed = dlg.querySelector("[data-bed]");
  const checks = dlg.querySelector("[data-checks]");
  const ack = () => checks.querySelector("[data-ack]");
  const update = () => { go.disabled = !!chk.blockers.length || !bed.checked || (ack() && !ack().checked); };
  bed.onchange = update;
  checks.addEventListener("change", (e) => { if (e.target.matches("[data-ack]")) update(); });
  checks.addEventListener("click", async (e) => {
    const sel = e.target.closest("[data-head]");
    if (!sel || sel.getAttribute("aria-pressed") === "true") return;
    map[sel.dataset.map] = Number(sel.dataset.head);
    const q = Object.entries(map).map(([k, v]) => `${k}:${v}`).join(",");
    try {
      chk = await api(`/api/printers/${printerId}/check?file_id=${encodeURIComponent(fileId)}&map=${q}`);
      map = { ...chk.tool_map };
      checks.innerHTML = checksHtml();
    } catch (err) { alert(err.message); }
    update();
  });
  dlg.querySelector("[data-close]").onclick = () => dlg.close();

  if (p.can_camera) {
    const age = dlg.querySelector("[data-age]");
    stopCam = cameraLoop(dlg.querySelector(".cam img"), printerId, 3000, (t, e) => {
      age.textContent = e || (t ? `Picture from ${fmtAge(t)}` : "Live picture");
    });
  }

  go.onclick = async () => {
    go.disabled = true;
    go.textContent = "Sending...";
    try {
      await api(`/api/printers/${printerId}/print`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ file_id: fileId, bed_clear: bed.checked, accept_warnings: !!ack()?.checked, tool_map: map }),
      });
      dlg.close();
      location.hash = "#/printers";
    } catch (e) {
      alert(e.message);
      go.textContent = "Start print";
      update();
    }
  };
}
