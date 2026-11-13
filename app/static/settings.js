// Printer settings dialog: each control is read back from the printer after a change.
const esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

async function api(path, opts) {
  const res = await fetch(path, opts);
  if (res.status === 401) { location.href = "/login"; throw new Error("login"); }
  if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText);
  return res.json();
}

function control(s) {
  const dis = s.disabled ? " disabled" : "";
  if (s.kind === "toggle") {
    const unknown = s.value == null;
    return `<span class="seg${unknown ? " unknown" : ""}" role="group" aria-label="${esc(s.label)}">
      <button type="button" class="small${s.value === true ? "" : " ghost"}" data-set="${esc(s.id)}" data-val="true" aria-pressed="${s.value === true}"${dis}>On</button>
      <button type="button" class="small${s.value === false ? "" : " ghost"}" data-set="${esc(s.id)}" data-val="false" aria-pressed="${s.value === false}"${dis}>Off</button>
    </span>`;
  }
  if (s.kind === "choice") {
    return `<select data-set="${esc(s.id)}"${dis}>
      ${s.value == null ? `<option value="" selected disabled>Unknown</option>` : ""}
      ${s.options.map((o) => `<option value="${esc(JSON.stringify(o.value))}"${o.value === s.value ? " selected" : ""}>${esc(o.label)}</option>`).join("")}
    </select>`;
  }
  return `<span class="num-set"><input type="number" class="num" data-num="${esc(s.id)}" value="${s.value ?? ""}"
      min="${s.min ?? ""}" max="${s.max ?? ""}" step="${s.step ?? 1}"${dis}>${s.unit ? `<span>${esc(s.unit)}</span>` : ""}
      <button type="button" class="small ghost" data-apply="${esc(s.id)}"${dis}>Set</button></span>`;
}

function settingsHtml(items) {
  const groups = [...new Set(items.map((s) => s.group))];
  return groups.map((g) => `<fieldset class="set-group"><legend>${esc(g)}</legend>
    ${items.filter((s) => s.group === g).map((s) => `
      <div class="set-row" data-row="${esc(s.id)}">
        <div class="set-label"><b>${esc(s.label)}</b>
          ${s.about ? `<small>${esc(s.about)}</small>` : ""}
          ${s.note ? `<small class="set-note">${esc(s.note)}</small>` : ""}
          ${s.disabled ? `<small class="set-note">${esc(s.disabled)}</small>` : ""}
        </div>
        <div class="set-ctl">${control(s)}</div>
      </div>`).join("")}
  </fieldset>`).join("");
}

export function settingsDialog(p) {
  const dlg = document.createElement("dialog");
  dlg.className = "print-dialog settings-dialog";
  dlg.innerHTML = `<h2>${esc(p.name)} settings</h2>
    <p class="sub">Read from the printer just now. A change counts once the printer shows it back.</p>
    <div data-msg></div><div data-list><p class="empty">Asking the printer...</p></div>
    <div class="dialog-actions"><button class="ghost" data-close>Close</button></div>`;
  document.body.appendChild(dlg);
  dlg.addEventListener("close", () => dlg.remove());
  dlg.querySelector("[data-close]").onclick = () => dlg.close();
  dlg.showModal();
  const list = dlg.querySelector("[data-list]");
  const msg = dlg.querySelector("[data-msg]");
  const show = (items) => { list.innerHTML = settingsHtml(items); };
  const say = (text, error) => { msg.innerHTML = text ? `<div class="notice ${error ? "error" : ""} small-notice">${esc(text)}</div>` : ""; };

  api(`/api/printers/${p.id}/settings`).then((r) => show(r.settings))
    .catch((e) => { list.innerHTML = ""; say(e.message, true); });

  let busy = false;
  async function change(id, value) {
    if (busy) return;
    busy = true;
    const row = list.querySelector(`[data-row="${CSS.escape(id)}"]`);
    row?.classList.add("saving");
    list.querySelectorAll("button, select, input").forEach((el) => { el.disabled = true; });
    say("Sending to the printer...");
    try {
      const r = await api(`/api/printers/${p.id}/settings/${encodeURIComponent(id)}`, {
        method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ value }),
      });
      show(r.settings);
      const label = r.settings.find((s) => s.id === id)?.label || id;
      say(`${label}: confirmed by the printer.`);
    } catch (e) {
      say(e.message, true);
      try { show((await api(`/api/printers/${p.id}/settings`)).settings); } catch { /* keep the message */ }
    } finally {
      busy = false;
    }
  }

  list.addEventListener("click", (e) => {
    const b = e.target.closest("[data-set][data-val]");
    if (b && b.getAttribute("aria-pressed") !== "true") { change(b.dataset.set, b.dataset.val === "true"); return; }
    const a = e.target.closest("[data-apply]");
    if (a) {
      const inp = list.querySelector(`[data-num="${CSS.escape(a.dataset.apply)}"]`);
      if (inp && inp.value !== "") change(a.dataset.apply, Number(inp.value));
    }
  });
  list.addEventListener("change", (e) => {
    const sel = e.target.closest("select[data-set]");
    if (sel) change(sel.dataset.set, JSON.parse(sel.value));
  });
  list.addEventListener("keydown", (e) => {
    const inp = e.target.closest("[data-num]");
    if (inp && e.key === "Enter" && inp.value !== "") change(inp.dataset.num, Number(inp.value));
  });
}
