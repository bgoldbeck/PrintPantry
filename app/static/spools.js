// Spool inventory (#/spools) and cost estimates for print files.
const esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

async function api(path, opts) {
  const res = await fetch(path, opts);
  if (res.status === 401) { location.href = "/login"; throw new Error("login"); }
  if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText);
  return res.json();
}
const json = (method, body) => ({ method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });

const MATERIALS = ["PLA", "PLA+", "PETG", "ABS", "ASA", "TPU", "PA", "PC", "PVA", "Other"];
const PRINTER_NAMES = { u1: "Snapmaker U1", p1s: "Bambu P1S" };

// ---------------------------------------------------------------- cost estimates
let PRICING = null;
export async function loadPricing(force = false) {
  if (!PRICING || force) PRICING = await api("/api/spools/pricing").catch(() => null);
  return PRICING;
}
const matKey = (m) => String(m || "").toUpperCase().replace(/[^A-Z0-9]/g, "");
function perKg(material) {
  const mats = PRICING?.materials || {};
  const k = matKey(material);
  if (k) {
    for (const [m, v] of Object.entries(mats)) if (m.startsWith(k) || k.startsWith(m)) return v;
  }
  return PRICING?.price_per_kg ?? 20;
}
export const money = (v) => `${PRICING?.currency ?? "$"}${(v ?? 0).toFixed(2)}`;

/** Estimated cost of a plate: its filaments' grams at the price of matching spools, plus power. */
export function estimateCost(printerId, plate) {
  if (!PRICING || !plate) return null;
  let fil = 0, any = false;
  const fils = plate.filaments || [];
  if (fils.some((f) => f.grams)) {
    for (const f of fils) if (f.grams) { fil += (f.grams / 1000) * perKg(f.type); any = true; }
  } else if (plate.grams) {
    fil = (plate.grams / 1000) * perKg(fils[0]?.type);
    any = true;
  }
  let power = 0;
  if (plate.seconds && PRICING.kwh_price) {
    power = ((PRICING.watts?.[printerId] ?? 120) * plate.seconds) / 3600 / 1000 * PRICING.kwh_price;
    any = true;
  }
  return any ? { filament: fil, power, total: fil + power } : null;
}
export function costHtml(printerId, plate) {
  const c = estimateCost(printerId, plate);
  if (!c) return "";
  const tip = `Filament ${money(c.filament)}${c.power ? ` + electricity ${money(c.power)}` : ""}`;
  return `<span title="${esc(tip)}">≈ ${money(c.total)}</span>`;
}

// ---------------------------------------------------------------- page
const where = (s) => (s.printer ? `${PRINTER_NAMES[s.printer] || s.printer} T${s.toolhead + 1}` : "On the shelf");

function spoolCard(s, low) {
  const pct = s.total_g ? Math.max(0, Math.min(100, (s.remaining_g / s.total_g) * 100)) : 0;
  const price = s.price != null ? `${money(s.price)} · ${money(s.price_per_kg)}/kg` : "No price";
  return `<div class="spool${s.low ? " low" : ""}${s.archived ? " archived" : ""}" data-id="${s.id}">
    <div class="spool-head">
      <i class="spool-dot" style="--c:${esc(s.color || "transparent")}"></i>
      <div><div class="name">${esc(s.label)}</div>
        <div class="meta">${s.name ? `${esc([s.vendor, s.material].filter(Boolean).join(" "))} · ` : ""}${esc(where(s))}</div></div>
    </div>
    <div class="bar${s.low ? " low" : ""}"><i style="width:${pct}%"></i></div>
    <div class="spool-facts"><span><b>${Math.round(s.remaining_g)} g</b> of ${Math.round(s.total_g)} g</span><span>${price}</span></div>
    ${s.low ? `<div class="hint warn-text">Running low (under ${Math.round(low)} g)</div>` : ""}
    <div class="file-actions">
      <button class="small" data-weigh>Weigh</button>
      <button class="small ghost" data-place>${s.printer ? "Move" : "Load"}</button>
      <button class="small ghost" data-edit>Edit</button>
    </div></div>`;
}

export async function spoolsPage(view, printers) {
  document.title = "Spools · Print Pantry";
  let showArchived = false;
  async function paint() {
    const [data] = await Promise.all([api(`/api/spools?archived=${showArchived}`), loadPricing(true)]);
    const st = data.settings;
    const active = data.spools.filter((s) => !s.archived);
    const loaded = active.filter((s) => s.printer);
    const shelf = active.filter((s) => !s.printer);
    const archived = data.spools.filter((s) => s.archived);
    const left = active.reduce((a, s) => a + s.remaining_g, 0);
    const value = active.reduce((a, s) => a + (s.remaining_g / 1000) * s.price_per_kg, 0);
    view.innerHTML = `
      <div class="crumbs"><a href="#/">All categories</a></div>
      <div class="page-head"><h1>Spools</h1><button data-add>Add spool</button></div>
      <p class="sub">${active.length} spool${active.length === 1 ? "" : "s"} · ${(left / 1000).toFixed(2)} kg of filament worth about ${money(value)}.
        Prints take their grams off the spool in the toolhead that printed them; weigh a spool now and then to correct it.</p>
      ${active.length ? "" : `<div class="notice">No spools yet. Press <strong>Add spool</strong> and enter how much filament is on it and what it cost.
        Spools on the U1 are matched to T1–T4 by the material, colour and brand the printer reads from the spool.</div>`}
      ${loaded.length ? `<h2>On printers</h2><div class="spools">${loaded.map((s) => spoolCard(s, st.low_grams)).join("")}</div>` : ""}
      ${shelf.length ? `<h2>On the shelf</h2><div class="spools">${shelf.map((s) => spoolCard(s, st.low_grams)).join("")}</div>` : ""}
      ${showArchived && archived.length ? `<h2>Used up</h2><div class="spools">${archived.map((s) => spoolCard(s, st.low_grams)).join("")}</div>` : ""}
      <p><button class="small ghost" data-archived>${showArchived ? "Hide used-up spools" : "Show used-up spools"}</button></p>
      <h2>Cost settings</h2>
      <form class="cost-settings" data-settings>
        <label>Currency <input name="currency" value="${esc(st.currency)}" maxlength="4" size="3"></label>
        <label>Default price per kg <input name="price_per_kg" type="number" step="0.01" min="0" value="${st.price_per_kg}"></label>
        <label>Electricity per kWh <input name="kwh_price" type="number" step="0.001" min="0" value="${st.kwh_price}"> <small class="muted">0 = leave out</small></label>
        ${(printers || []).map((p) => `<label>${esc(p.name)} watts <input name="w_${esc(p.id)}" type="number" step="1" min="0" value="${st.watts[p.id] ?? 120}"></label>`).join("")}
        <label>Warn under (g) <input name="low_grams" type="number" step="10" min="0" value="${st.low_grams}"></label>
        <div><button class="small">Save</button> <span class="hint" data-saved></span></div>
      </form>`;
    const byId = Object.fromEntries(data.spools.map((s) => [s.id, s]));
    view.querySelector("[data-add]").onclick = () => editDialog(null, paint);
    view.querySelector("[data-archived]").onclick = () => { showArchived = !showArchived; paint(); };
    view.querySelectorAll(".spool").forEach((el) => {
      const s = byId[el.dataset.id];
      el.querySelector("[data-weigh]").onclick = () => weighDialog(s, paint);
      el.querySelector("[data-edit]").onclick = () => editDialog(s, paint);
      el.querySelector("[data-place]").onclick = () => placeDialog(s, printers, paint);
    });
    view.querySelector("[data-settings]").onsubmit = async (e) => {
      e.preventDefault();
      const fd = new FormData(e.target);
      const watts = {};
      for (const [k, v] of fd) if (k.startsWith("w_")) watts[k.slice(2)] = Number(v);
      try {
        await api("/api/spools/settings", json("PUT", {
          currency: fd.get("currency"), price_per_kg: Number(fd.get("price_per_kg")),
          kwh_price: Number(fd.get("kwh_price")), low_grams: Number(fd.get("low_grams")), watts,
        }));
        await loadPricing(true);
        view.querySelector("[data-saved]").textContent = "Saved";
      } catch (err) { alert(err.message); }
    };
  }
  await paint();
}

function dialog(html) {
  const dlg = document.createElement("dialog");
  dlg.innerHTML = html;
  document.body.appendChild(dlg);
  dlg.addEventListener("close", () => dlg.remove());
  dlg.querySelectorAll("[data-close]").forEach((b) => (b.onclick = () => dlg.close()));
  dlg.showModal();
  return dlg;
}

function editDialog(s, done) {
  const v = s || { total_g: 1000, empty_g: 200, material: "PLA", auto: true };
  const dlg = dialog(`<h2>${s ? "Edit spool" : "Add spool"}</h2>
    <form class="spool-form" method="dialog">
      <label>Brand <input name="vendor" value="${esc(v.vendor || "")}" placeholder="Snapmaker, Bambu, eSun..."></label>
      <label>Material <input name="material" list="materials" value="${esc(v.material || "")}" required></label>
      <datalist id="materials">${MATERIALS.map((m) => `<option value="${m}">`).join("")}</datalist>
      <label>Colour <input name="color" type="color" value="${esc(v.color || "#FFFFFF")}"></label>
      <label>Name <input name="name" value="${esc(v.name || "")}" placeholder="optional, e.g. Matte Black"></label>
      <label>Filament on a full spool (g) <input name="total_g" type="number" min="1" step="1" value="${v.total_g}"></label>
      <label>Left now (g) <input name="remaining_g" type="number" min="0" step="1" value="${v.remaining_g ?? v.total_g}"></label>
      <label>Empty spool weighs (g) <input name="empty_g" type="number" min="0" step="1" value="${v.empty_g}"></label>
      <label>Price paid <input name="price" type="number" min="0" step="0.01" value="${v.price ?? ""}" placeholder="for the whole spool"></label>
      <label class="check"><input name="auto" type="checkbox"${v.auto ? " checked" : ""}> Match it to a toolhead automatically</label>
      <div class="dialog-actions">
        ${s ? `<button type="button" class="ghost" data-archive>${s.archived ? "Back in use" : "Used up"}</button>
               <button type="button" class="danger ghost" data-delete>Delete</button>` : ""}
        <button type="button" class="ghost" data-close>Cancel</button><button>Save</button></div>
    </form>`);
  const form = dlg.querySelector("form");
  form.onsubmit = async (e) => {
    e.preventDefault();
    const fd = new FormData(form);
    const body = {
      vendor: fd.get("vendor"), material: fd.get("material"), color: fd.get("color"), name: fd.get("name"),
      total_g: Number(fd.get("total_g")), remaining_g: Number(fd.get("remaining_g")), empty_g: Number(fd.get("empty_g")),
      price: fd.get("price") === "" ? null : Number(fd.get("price")), auto: fd.get("auto") === "on",
    };
    try {
      await api(s ? `/api/spools/${s.id}` : "/api/spools", json(s ? "PUT" : "POST", body));
      dlg.close();
      done();
    } catch (err) { alert(err.message); }
  };
  dlg.querySelector("[data-archive]")?.addEventListener("click", async () => {
    await api(`/api/spools/${s.id}`, json("PUT", { archived: !s.archived }));
    dlg.close(); done();
  });
  dlg.querySelector("[data-delete]")?.addEventListener("click", async () => {
    if (!confirm(`Delete ${s.label}? Its history stays, but the spool is gone.`)) return;
    await api(`/api/spools/${s.id}`, { method: "DELETE" });
    dlg.close(); done();
  });
}

function weighDialog(s, done) {
  const dlg = dialog(`<h2>Weigh ${esc(s.label)}</h2>
    <p class="sub">Put the spool on a kitchen scale. The empty spool (${Math.round(s.empty_g)} g) is taken off for you.
      Print Pantry thinks about ${Math.round(s.remaining_g)} g are left.</p>
    <form method="dialog">
      <label>Scale reading (g) <input name="gross" type="number" min="0" step="1" autofocus></label>
      <p class="hint" data-left></p>
      <div class="dialog-actions"><button type="button" class="ghost" data-close>Cancel</button><button>Save</button></div>
    </form>`);
  const input = dlg.querySelector("[name=gross]");
  input.oninput = () => {
    const g = Number(input.value) - s.empty_g;
    dlg.querySelector("[data-left]").textContent = input.value ? `${Math.max(0, Math.round(g))} g of filament left` : "";
  };
  dlg.querySelector("form").onsubmit = async (e) => {
    e.preventDefault();
    try {
      await api(`/api/spools/${s.id}/weigh`, json("POST", { gross_g: Number(input.value) }));
      dlg.close(); done();
    } catch (err) { alert(err.message); }
  };
}

function placeDialog(s, printers, done) {
  const opts = [];
  for (const p of printers || []) {
    if (!p.configured) continue;
    const heads = p.status.toolheads.length || (p.id === "u1" ? 4 : 1);
    for (let i = 0; i < heads; i++) {
      const t = p.status.toolheads[i];
      const what = t ? (t.loaded === false ? "empty" : [t.vendor, t.type].filter(Boolean).join(" ")) : "";
      opts.push(`<option value="${esc(p.id)}:${i}"${s.printer === p.id && s.toolhead === i ? " selected" : ""}>${esc(p.name)} T${i + 1}${what ? ` (${esc(what)})` : ""}</option>`);
    }
  }
  const dlg = dialog(`<h2>Where is ${esc(s.label)}?</h2>
    <form method="dialog">
      <label>Toolhead <select name="at"><option value="">On the shelf</option>${opts.join("")}</select></label>
      <p class="hint">Print Pantry moves it back to the shelf by itself when the printer reports a different spool there.</p>
      <div class="dialog-actions"><button type="button" class="ghost" data-close>Cancel</button><button>Save</button></div>
    </form>`);
  dlg.querySelector("form").onsubmit = async (e) => {
    e.preventDefault();
    const at = dlg.querySelector("[name=at]").value;
    const [printer, toolhead] = at ? at.split(":") : [null, null];
    try {
      await api(`/api/spools/${s.id}/assign`, json("POST", { printer, toolhead: toolhead == null ? null : Number(toolhead) }));
      dlg.close(); done();
    } catch (err) { alert(err.message); }
  };
}

