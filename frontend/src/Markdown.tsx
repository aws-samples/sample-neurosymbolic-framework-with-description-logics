// A small, dependency-free Markdown renderer for bot answers.
//
// The query programs emit a constrained Markdown subset (bold, italics, inline
// code, links, bulleted/numbered lists, headings, and simple pipe tables). We
// deliberately avoid pulling in react-markdown + a sanitizer: the input set is
// known and small, and the host is memory-constrained. Safety model: ALL raw
// text is HTML-escaped FIRST, so no source character can inject markup; then a
// fixed set of transforms re-introduces a whitelisted set of tags. Links are
// restricted to http(s)/mailto and rendered with rel="noopener noreferrer".

function escapeHtml(s: string): string {
  return s
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

// Inline spans: code, bold, italic, links. Operates on already-escaped text.
function renderInline(text: string): string {
  let out = text;
  // Inline code first so its contents are not further transformed.
  out = out.replace(/`([^`]+)`/g, (_m, code) => `<code>${code}</code>`);
  // Bold (**x** or __x__), then italic (*x* or _x_).
  out = out.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  out = out.replace(/__([^_]+)__/g, "<strong>$1</strong>");
  out = out.replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>");
  out = out.replace(/(^|[^_])_([^_\n]+)_/g, "$1<em>$2</em>");
  // Links [text](url) — only safe schemes.
  out = out.replace(
    /\[([^\]]+)\]\((https?:\/\/[^\s)]+|mailto:[^\s)]+)\)/g,
    (_m, label, href) =>
      `<a href="${href}" target="_blank" rel="noopener noreferrer">${label}</a>`,
  );
  return out;
}

// Is this line a pipe-table separator like | --- | :--: | ?
function isTableSep(line: string): boolean {
  return /^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$/.test(line);
}

function splitRow(line: string): string[] {
  return line
    .trim()
    .replace(/^\||\|$/g, "")
    .split("|")
    .map((c) => c.trim());
}

/** Convert a constrained Markdown subset (already-escaped) to safe HTML. */
export function markdownToHtml(src: string): string {
  const lines = escapeHtml(src).split("\n");
  const html: string[] = [];
  let i = 0;

  const flushParagraph = (buf: string[]) => {
    if (buf.length) html.push(`<p>${renderInline(buf.join(" "))}</p>`);
    buf.length = 0;
  };

  const para: string[] = [];

  while (i < lines.length) {
    const line = lines[i];

    // Blank line -> paragraph break.
    if (!line.trim()) {
      flushParagraph(para);
      i += 1;
      continue;
    }

    // Heading (# .. ######)
    const h = /^(#{1,6})\s+(.*)$/.exec(line);
    if (h) {
      flushParagraph(para);
      const level = h[1].length;
      html.push(`<h${level}>${renderInline(h[2])}</h${level}>`);
      i += 1;
      continue;
    }

    // Pipe table: a header row followed by a separator row.
    if (line.includes("|") && i + 1 < lines.length && isTableSep(lines[i + 1])) {
      flushParagraph(para);
      const header = splitRow(line);
      i += 2; // skip header + separator
      const rows: string[][] = [];
      while (i < lines.length && lines[i].includes("|") && lines[i].trim()) {
        rows.push(splitRow(lines[i]));
        i += 1;
      }
      const thead =
        "<thead><tr>" +
        header.map((c) => `<th>${renderInline(c)}</th>`).join("") +
        "</tr></thead>";
      const tbody =
        "<tbody>" +
        rows
          .map(
            (r) =>
              "<tr>" + r.map((c) => `<td>${renderInline(c)}</td>`).join("") + "</tr>",
          )
          .join("") +
        "</tbody>";
      html.push(`<table>${thead}${tbody}</table>`);
      continue;
    }

    // Unordered list (-, *, +) — consecutive items.
    if (/^\s*[-*+]\s+/.test(line)) {
      flushParagraph(para);
      const items: string[] = [];
      while (i < lines.length && /^\s*[-*+]\s+/.test(lines[i])) {
        items.push(`<li>${renderInline(lines[i].replace(/^\s*[-*+]\s+/, ""))}</li>`);
        i += 1;
      }
      html.push(`<ul>${items.join("")}</ul>`);
      continue;
    }

    // Ordered list (1. 2. ...) — consecutive items.
    if (/^\s*\d+\.\s+/.test(line)) {
      flushParagraph(para);
      const items: string[] = [];
      while (i < lines.length && /^\s*\d+\.\s+/.test(lines[i])) {
        items.push(`<li>${renderInline(lines[i].replace(/^\s*\d+\.\s+/, ""))}</li>`);
        i += 1;
      }
      html.push(`<ol>${items.join("")}</ol>`);
      continue;
    }

    // Otherwise accumulate into the current paragraph.
    para.push(line);
    i += 1;
  }
  flushParagraph(para);
  return html.join("");
}

/** Render a Markdown string as safe HTML inside a themed container. */
export function Markdown({ text }: { text: string }) {
  return (
    <div
      className="markdown"
      dangerouslySetInnerHTML={{ __html: markdownToHtml(text) }}
    />
  );
}
