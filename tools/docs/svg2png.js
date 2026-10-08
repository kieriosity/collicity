#!/usr/bin/env node
// Renders the docs' SVG diagram sources to PNG with resvg, using Inter (Regular, Medium, SemiBold)
// and Segoe UI Symbol as a fallback for symbols Inter lacks, such as ∩.
//
// Usage:
//   node svg2png.js [file.svg ...]    (default: every SVG in docs/images/src)
//
// Each PNG is written to docs/images/ with the SVG's base name. Set FONT_DIR if Inter is installed
// somewhere other than the usual system font folders.
const fs = require('fs');
const os = require('os');
const path = require('path');
const { Resvg } = require('@resvg/resvg-js');

const ROOT = path.resolve(__dirname, '..', '..');
const SRC_DIR = path.join(ROOT, 'docs', 'images', 'src');
const OUT_DIR = path.join(ROOT, 'docs', 'images');
const WANTED = /^(Inter-(Regular|Medium|SemiBold)\.(ttf|otf)|seguisym\.ttf)$/i;

function findFonts(dir, depth = 0, found = []) {
  let entries = [];
  try { entries = fs.readdirSync(dir, { withFileTypes: true }); } catch { return found; }
  for (const e of entries) {
    const p = path.join(dir, e.name);
    if (e.isFile() && WANTED.test(e.name)) found.push(p);
    else if (e.isDirectory() && depth < 3) findFonts(p, depth + 1, found);
  }
  return found;
}

const fontDirs = [
  process.env.FONT_DIR,
  path.join(process.env.WINDIR || 'C:\\Windows', 'Fonts'),
  path.join(process.env.LOCALAPPDATA || '', 'Microsoft', 'Windows', 'Fonts'),
  path.join(os.homedir(), 'Library', 'Fonts'),
  '/Library/Fonts',
  '/usr/share/fonts',
  path.join(os.homedir(), '.local', 'share', 'fonts'),
].filter(Boolean);
const fontFiles = [...new Set(fontDirs.flatMap((dir) => findFonts(dir)))];
if (!fontFiles.some((f) => /Inter-Regular/i.test(f))) {
  console.warn('Warning: Inter not found; the diagrams will render in a fallback font.');
}

const inputs = process.argv.slice(2).length
  ? process.argv.slice(2)
  : fs.readdirSync(SRC_DIR).filter((f) => f.endsWith('.svg')).map((f) => path.join(SRC_DIR, f));

for (const input of inputs) {
  const svg = fs.readFileSync(input, 'utf8');
  const png = new Resvg(svg, { font: { fontFiles, loadSystemFonts: false, defaultFontFamily: 'Inter' } }).render().asPng();
  const out = path.join(OUT_DIR, path.basename(input, '.svg') + '.png');
  fs.writeFileSync(out, png);
  console.log(`Wrote ${path.relative(ROOT, out)}`);
}
