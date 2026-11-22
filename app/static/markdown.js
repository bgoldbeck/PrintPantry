// Small, safe Markdown renderer for print guides. Everything is escaped first;
// only the syntax below turns into HTML. Supports headings, paragraphs, bold /
// italic / code / links, bullet + numbered + task lists (one nesting level),
// tables, horizontal rules and GitHub callouts (> [!TIP] ...).

const escHtml = (s) => String(s).replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

function inline(text) {
  const codes = [];
  let s = escHtml(text).replace(/`([^`]+)`/g, (_, c) => `\u0000${codes.push(c) - 1}\u0000`);
  s = s.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,
    (_, label, url) => `<a href="${url}" target="_blank" rel="noopener">${label}</a>`);
  s = s.replace(/(^|[\s(])(https?:\/\/[^\s<)]+)/g,
    (_, pre, url) => `${pre}<a href="${url}" target="_blank" rel="noopener">${url}</a>`);
  s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>").replace(/__([^_]+)__/g, "<strong>$1</strong>");
  s = s.replace(/(^|[^*\w])\*([^*\s][^*]*)\*/g, "$1<em>$2</em>");
  s = s.replace(/~~([^~]+)~~/g, "<del>$1</del>");
  return s.replace(/\u0000(\d+)\u0000/g, (_, i) => `<code>${codes[+i]}</code>`);
}

// Section colour by heading words; anything else cycles through the palette.
const SECTION_KINDS = [
  [/part|print list|bill of|bom|what to print|files?/i, "parts"],
  [/setting|slic|material|filament/i, "settings"],
  [/hardware|tool|need|supplies|screw/i, "hardware"],
  [/assembl|build|step|how to/i, "assembly"],
  [/tip|problem|issue|trouble|known|warning|gotcha|faq/i, "tips"],
  [/overview|about|summary|what it is|intro/i, "overview"],
];
const CYCLE = ["overview", "parts", "settings", "hardware", "assembly", "tips"];

function sectionKind(title, i) {
  for (const [re, kind] of SECTION_KINDS) if (re.test(title)) return kind;
  return CYCLE[i % CYCLE.length];
}

const CALLOUTS = { NOTE: "Note", TIP: "Tip", IMPORTANT: "Important", WARNING: "Warning", CAUTION: "Caution" };

function splitRow(line) {
  return line.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((c) => c.trim());
}

function blocks(lines) {
  let html = "";
  let i = 0;
  const isTableSep = (l) => /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/.test(l || "");
  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) { i++; continue; }
    let m;
    if ((m = line.match(/^(#{3,6})\s+(.*)$/))) {
      const n = Math.min(m[1].length + 1, 6);
      html += `<h${n}>${inline(m[2])}</h${n}>`;
      i++;
    } else if (/^\s*([-*_])(\s*\1){2,}\s*$/.test(line)) {
      html += "<hr>";
      i++;
    } else if (/^\s*>/.test(line)) {
      const quote = [];
      while (i < lines.length && /^\s*>/.test(lines[i])) quote.push(lines[i++].replace(/^\s*>\s?/, ""));
      const head = quote[0].match(/^\[!(\w+)\]\s*(.*)$/);
      if (head && CALLOUTS[head[1].toUpperCase()]) {
        const kind = head[1].toUpperCase();
        const rest = [head[2], ...quote.slice(1)].filter((l, k) => k > 0 || l.trim());
        html += `<div class="callout ${kind.toLowerCase()}"><div class="callout-title">${CALLOUTS[kind]}</div>${blocks(rest)}</div>`;
      } else {
        html += `<blockquote>${blocks(quote)}</blockquote>`;
      }
    } else if (line.includes("|") && isTableSep(lines[i + 1])) {
      const head = splitRow(line);
      i += 2;
      const rows = [];
      while (i < lines.length && lines[i].includes("|") && lines[i].trim()) rows.push(splitRow(lines[i++]));
      html += `<div class="table-wrap"><table><thead><tr>${head.map((c) => `<th>${inline(c)}</th>`).join("")}</tr></thead><tbody>${
        rows.map((r) => `<tr>${head.map((_, k) => `<td>${inline(r[k] || "")}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`;
    } else if (/^\s*([-*+]|\d+[.)])\s+/.test(line)) {
      html += list(lines, i, (next) => { i = next; });
    } else if (/^```/.test(line)) {
      const code = [];
      i++;
      while (i < lines.length && !/^```/.test(lines[i])) code.push(lines[i++]);
      i++;
      html += `<pre><code>${escHtml(code.join("\n"))}</code></pre>`;
    } else {
      const para = [];
      while (i < lines.length && lines[i].trim() && !/^(#{1,6}\s|\s*>|```|\s*([-*+]|\d+[.)])\s+)/.test(lines[i])
        && !(lines[i].includes("|") && isTableSep(lines[i + 1]))) para.push(lines[i++].trim());
      if (!para.length) para.push(lines[i++].trim());
      html += `<p>${para.map(inline).join("<br>")}</p>`;
    }
  }
  return html;
}

function list(lines, start, done) {
  const indentOf = (l) => l.match(/^\s*/)[0].length;
  const base = indentOf(lines[start]);
  const ordered = /^\s*\d+[.)]\s/.test(lines[start]);
  const items = [];
  let i = start;
  while (i < lines.length) {
    const l = lines[i];
    if (!l.trim()) {
      // A blank line ends the list unless the next line continues it.
      if (i + 1 < lines.length && indentOf(lines[i + 1]) >= base && /^\s*([-*+]|\d+[.)])\s+/.test(lines[i + 1])) { i++; continue; }
      break;
    }
    const ind = indentOf(l);
    const m = l.match(/^\s*([-*+]|\d+[.)])\s+(.*)$/);
    if (m && ind === base) {
      items.push({ text: m[2], sub: [] });
      i++;
    } else if (ind > base && items.length) {
      items[items.length - 1].sub.push(l);
      i++;
    } else break;
  }
  done(i);
  const tag = ordered ? "ol" : "ul";
  return `<${tag}>${items.map(({ text, sub }) => {
    const task = text.match(/^\[([ xX])\]\s+(.*)$/);
    const body = task ? `<input type="checkbox" disabled${task[1] !== " " ? " checked" : ""}> ${inline(task[2])}` : inline(text);
    const nested = sub.length ? blocks(sub.map((s) => s.replace(new RegExp(`^\\s{0,${base + 4}}`), ""))) : "";
    return `<li${task ? ' class="task"' : ""}>${body}${nested}</li>`;
  }).join("")}</${tag}>`;
}

/** Markdown to HTML. "# Title" becomes the guide title; each "## " starts a coloured section card. */
export function renderMarkdown(md) {
  const lines = String(md || "").replace(/\r\n?/g, "\n").split("\n");
  let title = "";
  const intro = [];
  const sections = [];
  for (const line of lines) {
    const h1 = line.match(/^#\s+(.*)$/);
    const h2 = line.match(/^##\s+(.*)$/);
    if (h1 && !title && !sections.length) title = h1[1];
    else if (h2) sections.push({ title: h2[1], lines: [] });
    else (sections.length ? sections[sections.length - 1].lines : intro).push(line);
  }
  let html = title ? `<h1 class="guide-title">${inline(title)}</h1>` : "";
  if (intro.some((l) => l.trim())) html += `<div class="guide-intro">${blocks(intro)}</div>`;
  sections.forEach((s, k) => {
    html += `<section class="guide-section g-${sectionKind(s.title, k)}"><h2>${inline(s.title)}</h2>${blocks(s.lines)}</section>`;
  });
  return html;
}
