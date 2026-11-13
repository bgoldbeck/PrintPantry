// Filament colour chips shared by the model page and the print dialogs.
// A slicer writes some colour even when nobody picked one (OrcaSlicer's
// default orange, or the preset's own colour when we slice an STL). Those
// say nothing about the real spool, so they show as "any colour".
const esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

export const PLACEHOLDER_COLOURS = new Set(["#F2754E"]);

export function realColour(c) {
  const v = String(c || "").trim().toUpperCase();
  if (!/^#[0-9A-F]{6}/.test(v)) return "";
  return PLACEHOLDER_COLOURS.has(v.slice(0, 7)) ? "" : v.slice(0, 7);
}

/** `<span class="swatch">` with a dot and a label (type plus any extra text). */
export function swatchHtml(type, colour, extra = "", title = "") {
  const c = realColour(colour);
  const dot = c ? `<i style="--c:${esc(c)}"></i>` : `<i class="any" title="Any colour"></i>`;
  const tip = title || (c ? "" : "Any colour");
  return `<span class="swatch"${tip ? ` title="${esc(tip)}"` : ""}>${dot}${esc(type || "?")}${extra ? ` ${esc(extra)}` : ""}</span>`;
}
