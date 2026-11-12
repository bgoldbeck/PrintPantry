// Printer pages: live status cards (#/printers) and the "Print on ..." dialog.
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
function fmtAge(ts) {
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
function cameraLoop(img, printerId, every, onFrame) {
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
    timer = setTimeout(tick, every);
  }
  tick();
  return () => { alive = false; clearTimeout(timer); };
}

// ---------------------------------------------------------------- status card
function tempsHtml(st) {
  return st.temps.map((t) => `<span class="temp${t.target ? " hot" : ""}"><b>${esc(t.name)}</b> ${fmtTemp(t.current)}${t.target ? ` / ${fmtTemp(t.target)}` : ""}</span>`).join("");
}
function toolsHtml(st) {
  return st.toolheads.map((t) => `<span class="tool${t.active ? " active" : ""}${t.loaded === false ? " empty" : ""}" title="${esc([t.vendor, t.type].filter(Boolean).join(" ") || "Unknown")}">
      <i style="--c:${esc(t.color || "transparent")}"></i>T${t.index} ${esc(t.loaded === false ? "empty" : t.type || "?")}</span>`).join("");
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
    ${buttons.length ? `<div class="file-actions">${buttons.join("")}</div>` : ""}`;
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
      </section>`).join("")}</div>`;

  const stops = [];
  function paint(p) {
    const card = view.querySelector(`.printer-card[data-id="${p.id}"]`);
    if (!card) return;
    const st = p.status;
    const s = card.querySelector("[data-state]");
    s.textContent = STATE_LABEL[st.state] || st.state;
    s.dataset.state = st.state;
    card.querySelector("[data-body]").innerHTML = cardBody(p);
  }
  list.forEach(paint);

  const onClick = async (e) => {
    const b = e.target.closest("[data-act]");
    if (!b) return;
    const card = b.closest(".printer-card");
    const act = b.dataset.act;
    if (act === "cancel" && !confirm("Cancel this print? It can't be resumed.")) return;
    b.disabled = true;
    try { paint(await api(`/api/printers/${card.dataset.id}/${act}`, { method: "POST" })); }
    catch (err) { alert(err.message); b.disabled = false; }
  };
  view.addEventListener("click", onClick);
  stops.push(() => view.removeEventListener("click", onClick));

  for (const p of list.filter((x) => x.can_camera)) {
    const card = view.querySelector(`.printer-card[data-id="${p.id}"]`);
    const age = card.querySelector("[data-age]");
    let taken = null, err = "";
    const label = () => { age.textContent = err || (taken ? `Picture from ${fmtAge(taken)}` : ""); };
    stops.push(cameraLoop(card.querySelector(".cam img"), p.id, 3000, (t, e) => { taken = t; err = e; label(); }));
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
function swatch(type, color) {
  return `<span class="swatch"><i style="--c:${esc(color || "#999")}"></i>${esc(type || "?")}</span>`;
}

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
  const rows = chk.tools.map((t) => `<tr class="${t.ok ? "" : "warn"}">
      <td>T${t.index}</td><td>${swatch(t.file.type, t.file.color)}</td>
      <td>${t.loaded ? (t.loaded.loaded === false ? "<em>empty</em>" : `${swatch(t.loaded.type, t.loaded.color)} <small>${esc(t.loaded.vendor)}</small>`) : "<em>unknown</em>"}</td>
      <td>${t.ok ? "✓" : "⚠"}</td></tr>`).join("");
  dlg.innerHTML = `
    <h2>Print on ${esc(p.name)}</h2>
    <p class="sub" style="margin-bottom:12px"><strong>${esc(f.name)}</strong>${plate.seconds ? ` · about ${fmtDur(plate.seconds)}` : ""}</p>
    ${p.can_camera ? `<div class="cam"><img alt="Camera" class="nocam"><span class="cam-age" data-age>Getting a fresh picture...</span></div>` : ""}
    ${chk.blockers.length ? `<div class="notice error"><strong>Can't start:</strong><ul>${chk.blockers.map((b) => `<li>${esc(b)}</li>`).join("")}</ul></div>` : ""}
    ${rows ? `<table class="toolmap"><thead><tr><th>Tool</th><th>File needs</th><th>Loaded now</th><th></th></tr></thead><tbody>${rows}</tbody></table>` : ""}
    ${chk.warnings.length ? `<div class="notice warn"><ul>${chk.warnings.map((w) => `<li>${esc(w)}</li>`).join("")}</ul></div>
      <label class="check"><input type="checkbox" data-ack> I've checked the toolheads and want to print anyway</label>` : ""}
    <label class="check"><input type="checkbox" data-bed> The bed is clear and the build plate is in place</label>
    <div class="dialog-actions"><button class="ghost" data-close>Cancel</button><button data-go disabled>Start print</button></div>`;

  const go = dlg.querySelector("[data-go]");
  const bed = dlg.querySelector("[data-bed]");
  const ack = dlg.querySelector("[data-ack]");
  const update = () => { go.disabled = !!chk.blockers.length || !bed.checked || (ack && !ack.checked); };
  bed.onchange = update;
  if (ack) ack.onchange = update;
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
        body: JSON.stringify({ file_id: fileId, bed_clear: bed.checked, accept_warnings: !!ack?.checked }),
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
