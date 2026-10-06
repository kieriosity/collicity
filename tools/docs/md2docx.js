#!/usr/bin/env node
// Markdown → .docx for the Collicity docs, following the product specification's Word conventions:
// US Letter with 1" margins, Calibri 11 pt, Title 28 pt, Heading 1/2/3 at 16/13/12 pt bold,
// thin single-line table borders with an F2F2F2 header row, Consolas "Code" paragraphs,
// real bulleted and numbered lists, and PNG/JPEG images scaled to the text width.
//
// Usage:
//   node md2docx.js <input.md> [output.docx] [--page-numbers]
//
// Mapping:
//   "# "  → Title (first one only; the paragraph right after it is the byline, set in grey)
//   "## " → Heading 1, "### " → Heading 2, "#### " → Heading 3
//   Requirement IDs such as CON-04 or AC-15 get non-breaking hyphens so Word never splits them.
const fs = require('fs');
const path = require('path');
const { marked } = require('marked');
const d = require('docx');

const args = process.argv.slice(2);
const flags = new Set(args.filter((a) => a.startsWith('--')));
const [SRC, OUT_ARG] = args.filter((a) => !a.startsWith('--'));
if (!SRC) {
  console.error('Usage: node md2docx.js <input.md> [output.docx] [--page-numbers]');
  process.exit(1);
}
const OUT = OUT_ARG || SRC.replace(/\.md$/i, '') + '.docx';
const BASE = path.dirname(path.resolve(SRC));
const tokens = marked.lexer(fs.readFileSync(SRC, 'utf8'));

const PAGE_W = 12240, PAGE_H = 15840, MARGIN = 1440, CONTENT_W = PAGE_W - 2 * MARGIN; // 9360 DXA = 6.5"
const IMAGE_W_PX = 624; // 6.5" at 96 dpi
const BORDER = { style: d.BorderStyle.SINGLE, size: 4, color: 'auto' };

const unescape = (s) => String(s)
  .replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&amp;/g, '&');
const decode = (s) => unescape(s).replace(/\b([A-Z]{2,3})-(\d)/g, '$1‑$2'); // prose only, never code
const plain = (toks) => (toks || []).map((t) => (t.tokens ? plain(t.tokens) : decode(t.text || ''))).join('');

// ---------- inline ----------
function inline(toks, style = {}) {
  const out = [];
  for (const t of toks || []) {
    switch (t.type) {
      case 'text':
        if (t.tokens && t.tokens.length) out.push(...inline(t.tokens, style));
        else out.push(new d.TextRun({ text: decode(t.text), ...style }));
        break;
      case 'escape': out.push(new d.TextRun({ text: decode(t.text), ...style })); break;
      case 'strong': out.push(...inline(t.tokens, { ...style, bold: true })); break;
      case 'em': out.push(...inline(t.tokens, { ...style, italics: true })); break;
      case 'del': out.push(...inline(t.tokens, { ...style, strike: true })); break;
      case 'codespan':
        out.push(new d.TextRun({ text: decode(t.text), ...style, style: 'CodeChar', size: style.size ? style.size - 2 : 20 }));
        break;
      case 'link':
        out.push(new d.ExternalHyperlink({ link: t.href, children: inline(t.tokens, { ...style, style: 'Hyperlink' }) }));
        break;
      case 'br': out.push(new d.TextRun({ text: '', break: 1 })); break;
      case 'image': case 'html': break; // images are block-level here; raw HTML is skipped
      default:
        if (t.text) out.push(new d.TextRun({ text: decode(t.text), ...style }));
    }
  }
  return out;
}

// ---------- images ----------
function imageSize(buf, type) {
  if (type === 'png') return { w: buf.readUInt32BE(16), h: buf.readUInt32BE(20) };
  for (let i = 2; i < buf.length;) { // JPEG: walk segments to the first SOFn marker
    const marker = buf[i + 1], len = buf.readUInt16BE(i + 2);
    if (marker >= 0xc0 && marker <= 0xcf && ![0xc4, 0xc8, 0xcc].includes(marker)) return { h: buf.readUInt16BE(i + 5), w: buf.readUInt16BE(i + 7) };
    i += 2 + len;
  }
  throw new Error('Unreadable JPEG');
}
function imageParagraph(t) {
  const file = path.resolve(BASE, decodeURI(t.href));
  const ext = path.extname(file).toLowerCase();
  const type = ext === '.png' ? 'png' : ext === '.jpg' || ext === '.jpeg' ? 'jpg' : null;
  if (!type) throw new Error(`Unsupported image type: ${file}`);
  const data = fs.readFileSync(file);
  const { w, h } = imageSize(data, type);
  return new d.Paragraph({
    alignment: d.AlignmentType.CENTER,
    spacing: { before: 120, after: 200 },
    children: [new d.ImageRun({ type, data, transformation: { width: IMAGE_W_PX, height: Math.round((h / w) * IMAGE_W_PX) },
      altText: { title: t.text, description: t.text, name: path.basename(file) } })],
  });
}

// ---------- tables ----------
// Each column is at least as wide as its longest unbreakable word (Word breaks at spaces and after
// real hyphens), so no word is split mid-word; the remaining width goes to the wordier columns.
function table(t) {
  const rows = [t.header, ...t.rows];
  const n = t.header.length;
  const CHAR = 104, PAD = 240;
  const longestWord = (s) => Math.max(0, ...s.split(/\s+/).flatMap((w) => w.split(/(?<=-)/)).map((x) => x.length));
  const minW = [], pref = [];
  for (let i = 0; i < n; i++) {
    const texts = rows.map((r) => plain((r[i] || {}).tokens) || decode((r[i] || {}).text || ''));
    minW.push(Math.min(Math.round(Math.max(...texts.map((s, ri) => longestWord(s) * (ri === 0 ? 1.1 : 1))) * CHAR + PAD), 3000));
    const lens = texts.map((s) => Math.min(s.length, 160));
    pref.push(Math.max(4, 0.6 * (lens.reduce((a, b) => a + b, 0) / lens.length) + 0.4 * Math.max(...lens)));
  }
  let widths = minW.slice();
  const spare = CONTENT_W - widths.reduce((a, b) => a + b, 0);
  if (spare > 0) {
    const ps = pref.reduce((a, b) => a + b, 0);
    widths = widths.map((w, i) => w + Math.floor((spare * pref[i]) / ps));
  } else {
    const k = CONTENT_W / widths.reduce((a, b) => a + b, 0);
    widths = widths.map((w) => Math.floor(w * k));
  }
  widths[widths.length - 1] += CONTENT_W - widths.reduce((a, b) => a + b, 0);

  const mkRow = (cells, header) => new d.TableRow({
    tableHeader: header,
    children: cells.map((c, i) => new d.TableCell({
      width: { size: widths[i], type: d.WidthType.DXA },
      shading: header ? { type: d.ShadingType.CLEAR, color: 'auto', fill: 'F2F2F2' } : undefined,
      margins: { top: 60, bottom: 60, left: 100, right: 100 },
      children: [new d.Paragraph({ spacing: { before: 0, after: 0, line: 252 },
        children: inline(c.tokens, header ? { bold: true, size: 19 } : { size: 19 }) })],
    })),
  });
  return new d.Table({
    width: { size: CONTENT_W, type: d.WidthType.DXA },
    columnWidths: widths,
    layout: d.TableLayoutType.FIXED,
    borders: { top: BORDER, bottom: BORDER, left: BORDER, right: BORDER, insideHorizontal: BORDER, insideVertical: BORDER },
    rows: [mkRow(t.header, true), ...t.rows.map((r) => mkRow(r, false))],
  });
}

// ---------- lists ----------
let orderedInstance = 0;
function list(t, level, out) {
  const instance = t.ordered ? ++orderedInstance : 0;
  for (const item of t.items) {
    let first = true;
    for (const bt of item.tokens) {
      if (bt.type === 'list') { list(bt, level + 1, out); continue; }
      if (bt.type === 'space') continue;
      const runs = bt.tokens ? inline(bt.tokens) : inline([{ type: 'text', text: bt.text || bt.raw || '' }]);
      out.push(new d.Paragraph({
        numbering: first ? (t.ordered ? { reference: 'ordered', level, instance } : { reference: 'bullets', level }) : undefined,
        indent: first ? undefined : { left: 720 * (level + 1) },
        spacing: { before: 0, after: 60 },
        children: runs,
      }));
      first = false;
    }
  }
}

// ---------- blocks ----------
const children = [];
let title = null, sawByline = false;
for (const t of tokens) {
  switch (t.type) {
    case 'heading': {
      if (t.depth === 1 && !title) {
        title = plain(t.tokens).replace(/‑/g, '-');
        children.push(new d.Paragraph({ style: 'Title', children: inline(t.tokens) }));
        break;
      }
      const heading = { 1: d.HeadingLevel.HEADING_1, 2: d.HeadingLevel.HEADING_1, 3: d.HeadingLevel.HEADING_2 }[t.depth] || d.HeadingLevel.HEADING_3;
      children.push(new d.Paragraph({ heading, keepNext: true, children: inline(t.tokens) }));
      break;
    }
    case 'paragraph': {
      const images = (t.tokens || []).filter((x) => x.type === 'image');
      if (images.length) { images.forEach((im) => children.push(imageParagraph(im))); break; }
      if (title && !sawByline) {
        sawByline = true;
        children.push(new d.Paragraph({ spacing: { after: 240 }, children: inline(t.tokens, { color: '595959' }) }));
        break;
      }
      children.push(new d.Paragraph({ children: inline(t.tokens) }));
      break;
    }
    case 'list':
      list(t, 0, children);
      children.push(new d.Paragraph({ spacing: { after: 0 }, children: [] }));
      break;
    case 'table':
      children.push(table(t));
      children.push(new d.Paragraph({ spacing: { after: 60 }, children: [] }));
      break;
    case 'code': {
      const lines = unescape(t.text).split('\n');
      lines.forEach((l, i) => children.push(new d.Paragraph({
        style: 'Code',
        spacing: { before: i ? 0 : 60, after: i === lines.length - 1 ? 160 : 0 },
        children: [new d.TextRun({ text: l || ' ' })],
      })));
      break;
    }
    case 'blockquote':
      for (const bt of t.tokens || []) {
        if (bt.tokens) children.push(new d.Paragraph({ style: 'Quote', children: inline(bt.tokens) }));
      }
      break;
    case 'hr':
      children.push(new d.Paragraph({ border: { bottom: { style: d.BorderStyle.SINGLE, size: 6, color: 'BFBFBF', space: 1 } }, children: [] }));
      break;
    case 'space': case 'html': break;
    default:
      if (t.text) children.push(new d.Paragraph({ children: [new d.TextRun(decode(t.text))] }));
  }
}

const levels = (formats, text) => formats.map((format, level) => ({
  level, format, text: text(level), alignment: d.AlignmentType.LEFT,
  style: { paragraph: { indent: { left: 720 * (level + 1), hanging: 360 } } },
}));
const bullets = ['•', '◦', '▪'];

const doc = new d.Document({
  creator: 'Collicity',
  title: title || path.basename(SRC, '.md'),
  styles: {
    default: { document: { run: { font: 'Calibri', size: 22 }, paragraph: { spacing: { after: 120, line: 264 } } } },
    paragraphStyles: [
      { id: 'Title', name: 'Title', basedOn: 'Normal', next: 'Normal', run: { size: 56 }, paragraph: { spacing: { after: 80 } } },
      { id: 'Heading1', name: 'Heading 1', basedOn: 'Normal', next: 'Normal', quickFormat: true, run: { size: 32, bold: true }, paragraph: { spacing: { before: 360, after: 120 }, outlineLevel: 0 } },
      { id: 'Heading2', name: 'Heading 2', basedOn: 'Normal', next: 'Normal', quickFormat: true, run: { size: 26, bold: true }, paragraph: { spacing: { before: 280, after: 100 }, outlineLevel: 1 } },
      { id: 'Heading3', name: 'Heading 3', basedOn: 'Normal', next: 'Normal', quickFormat: true, run: { size: 24, bold: true }, paragraph: { spacing: { before: 240, after: 80 }, outlineLevel: 2 } },
      { id: 'Code', name: 'Code', basedOn: 'Normal', run: { font: 'Consolas', size: 17 }, paragraph: { spacing: { after: 0, line: 240 }, shading: { type: d.ShadingType.CLEAR, color: 'auto', fill: 'F5F5F5' } } },
      { id: 'Quote', name: 'Quote', basedOn: 'Normal', run: { italics: true, color: '595959' }, paragraph: { indent: { left: 720 } } },
    ],
    characterStyles: [
      { id: 'CodeChar', name: 'Code Char', basedOn: 'DefaultParagraphFont', run: { font: 'Consolas' } },
      { id: 'Hyperlink', name: 'Hyperlink', basedOn: 'DefaultParagraphFont', run: { color: '0563C1', underline: { type: d.UnderlineType.SINGLE } } },
    ],
  },
  numbering: { config: [
    { reference: 'bullets', levels: levels([d.LevelFormat.BULLET, d.LevelFormat.BULLET, d.LevelFormat.BULLET], (l) => bullets[l]) },
    { reference: 'ordered', levels: levels([d.LevelFormat.DECIMAL, d.LevelFormat.LOWER_LETTER, d.LevelFormat.LOWER_ROMAN], (l) => `%${l + 1}.`) },
  ] },
  sections: [{
    properties: { page: { size: { width: PAGE_W, height: PAGE_H }, margin: { top: MARGIN, right: MARGIN, bottom: MARGIN, left: MARGIN, header: 720, footer: 720 } } },
    footers: flags.has('--page-numbers') ? { default: new d.Footer({ children: [new d.Paragraph({ alignment: d.AlignmentType.CENTER,
      children: [new d.TextRun({ children: [d.PageNumber.CURRENT], size: 18, color: '808080' })] })] }) } : undefined,
    children,
  }],
});

d.Packer.toBuffer(doc).then((buf) => {
  fs.writeFileSync(OUT, buf);
  console.log(`Wrote ${OUT} (${buf.length} bytes)`);
});
