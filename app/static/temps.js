// Settings page: remembered temperatures per printer, filament preset and plate.
// The Slice dialog's "Remember" tick fills this table; here they can be added,
// changed or deleted in one place. Saved values win over the preset's own.
const esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

async function api(path, opts) {
  const res = await fetch(path, opts);
  if (res.status === 401) { location.href = "/login"; throw new Error("login"); }
  if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText);
  return res.json();
}

const FIELDS = [["nozzle", "Nozzle", 150, 350], ["nozzle_first", "Nozzle 1st layer", 150, 350],
  ["bed", "Bed", 0, 130], ["bed_first", "Bed 1st layer", 0, 130]];
const option = (v, label, sel) => `<option value="${esc(v)}"${sel ? " selected" : ""}>${esc(label ?? v)}</option>`;

// The preset's own temperatures for a plate.
function presetTemps(f, plate) {
  if (!f) return {};
  return { nozzle: f.nozzle, nozzle_first: f.nozzle_first, bed: f.beds?.[plate], bed_first: f.beds_first?.[plate] };
}

function inputs(vals, base, attrs = "") {
  return FIELDS.map(([k, label, lo, hi]) => `<td><input type="number" class="num temp-in" inputmode="numeric" step="1"
      min="${lo}" max="${hi}" data-k="${k}" value="${vals[k] ?? ""}" placeholder="${base[k] ?? "–"}"
      aria-label="${esc(label)} °C" title="Preset says ${base[k] ?? "–"} °C"${attrs}></td>`).join("");
}

function readRow(tr) {
  const out = {};
  for (const inp of tr.querySelectorAll("[data-k]")) {
    if (inp.value.trim() === "") continue;
    const [, label, lo, hi] = FIELDS.find((f) => f[0] === inp.dataset.k);
    const n = Number(inp.value);
    if (!Number.isFinite(n) || n < lo || n > hi) throw new Error(`${label} must be ${lo} to ${hi} °C`);
    out[inp.dataset.k] = n;
  }
  if (!Object.keys(out).length) throw new Error("Enter at least one temperature");
  return out;
}

export async function settingsPage(view, printers) {
  document.title = "Settings · Print Pantry";
  view.innerHTML = `<div class="crumbs"><a href="#/">All categories</a></div>
    <div class="page-head"><h1>Settings</h1></div><p class="empty">Loading...</p>`;
  const [saved, ...opts] = await Promise.all([
    api("/api/slicer/temps"),
    ...printers.map((p) => api(`/api/slicer/filaments?printer=${encodeURIComponent(p.id)}`).catch((e) => ({ error: e.message }))),
  ]);
  const temps = saved.temps;
  const say = (box, text, error) => { box.innerHTML = text ? `<div class="notice ${error ? "error" : ""} small-notice">${esc(text)}</div>` : ""; };

  view.innerHTML = `<div class="crumbs"><a href="#/">All categories</a></div>
    <div class="page-head"><h1>Settings</h1></div>
    <h2>Temperatures</h2>
    <p class="sub">Your nozzle and bed temperatures for each filament on each plate. The Slice dialog fills these in
      instead of the preset's (tick <strong>Remember</strong> there to add one). Empty boxes keep the preset's value, shown in grey.</p>
    ${printers.map((p, n) => {
      const o = opts[n];
      if (o.error) return `<h3>${esc(p.name)}</h3><div class="notice warn">Can't read the filament presets: ${esc(o.error)}</div>`;
      const fils = o.filaments, plates = o.bed_types;
      const plateLabel = (b) => plates.find((x) => x.name === b)?.label || b;
      const rows = temps.filter((t) => t.printer === p.id);
      const firstFil = fils[0]?.name || "", firstPlate = plates[0]?.name || "";
      return `<section class="temp-settings" data-printer="${esc(p.id)}">
        <h3>${esc(p.name)}</h3>
        <div data-msg></div>
        <table class="flow-table temp-table"><thead><tr><th>Filament preset</th><th>Plate</th>
          ${FIELDS.map(([, l]) => `<th>${l} °C</th>`).join("")}<th></th></tr></thead><tbody>
          ${rows.map((t) => {
            const base = presetTemps(fils.find((f) => f.name === t.filament), t.plate);
            return `<tr data-fil="${esc(t.filament)}" data-plate="${esc(t.plate)}">
              <td>${esc(t.filament)}</td><td>${esc(plateLabel(t.plate))}</td>${inputs(t, base)}
              <td class="row-actions"><button class="small" data-save>Save</button><button class="small ghost" data-del>Delete</button></td></tr>`;
          }).join("") || `<tr class="none"><td colspan="7"><small>Nothing saved yet for ${esc(p.name)}.</small></td></tr>`}
          <tr class="add-row" data-add>
            <td><select data-add-fil>${fils.map((f) => option(f.name, f.user ? `${f.name} (yours)` : f.name, f.name === firstFil)).join("")}</select></td>
            <td><select data-add-plate>${plates.map((b) => option(b.name, b.label, b.name === firstPlate)).join("")}</select></td>
            ${inputs({}, presetTemps(fils[0], firstPlate))}
            <td class="row-actions"><button class="small" data-save>Add</button></td></tr>
        </tbody></table></section>`;
    }).join("")}`;

  view.querySelectorAll(".temp-settings").forEach((sec) => {
    const printer = sec.dataset.printer;
    const o = opts[printers.findIndex((p) => p.id === printer)];
    const msg = sec.querySelector("[data-msg]");
    const add = sec.querySelector("[data-add]");
    // The add row's boxes start at the preset's values (or what's saved) for the picked pair.
    const fillAdd = () => {
      const fil = add.querySelector("[data-add-fil]").value, plate = add.querySelector("[data-add-plate]").value;
      const base = presetTemps(o.filaments.find((f) => f.name === fil), plate);
      const mine = temps.find((t) => t.printer === printer && t.filament === fil && t.plate === plate) || {};
      add.querySelectorAll("[data-k]").forEach((inp) => {
        inp.value = mine[inp.dataset.k] ?? base[inp.dataset.k] ?? "";
        inp.placeholder = base[inp.dataset.k] ?? "–";
      });
    };
    add.querySelectorAll("select").forEach((s) => (s.onchange = fillAdd));
    fillAdd();
    sec.addEventListener("click", async (e) => {
      const b = e.target.closest("button");
      if (!b) return;
      const tr = b.closest("tr");
      const filament = tr.dataset.fil ?? tr.querySelector("[data-add-fil]")?.value;
      const plate = tr.dataset.plate ?? tr.querySelector("[data-add-plate]")?.value;
      try {
        b.disabled = true;
        if (b.hasAttribute("data-del")) {
          await api(`/api/slicer/temps?printer=${encodeURIComponent(printer)}&filament=${encodeURIComponent(filament)}&plate=${encodeURIComponent(plate)}`, { method: "DELETE" });
        } else {
          await api("/api/slicer/temps", { method: "PUT", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ printer, filament, plate, ...readRow(tr) }) });
        }
        await settingsPage(view, printers);
      } catch (err) {
        if (err.message === "login") return;
        b.disabled = false;
        say(msg, err.message, true);
      }
    });
  });
}
