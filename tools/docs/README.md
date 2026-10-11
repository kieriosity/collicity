# Docs tools

Builds the Word versions of the docs in `docs/` and renders their diagram sources to PNG. The Markdown file is the source of truth; regenerate the `.docx` after every edit.

## Setup

```bash
cd tools/docs
npm install
```

## Markdown to .docx

```bash
node md2docx.js "../../docs/Collicity — Technical Design (Draft v0.4).md"
```

This writes the `.docx` next to the `.md`. Pass a second path to write it elsewhere, and add `--page-numbers` for a page-number footer.

The output follows the product specification's Word conventions:
- US Letter with 1-inch margins, Calibri 11 pt.
- `#` becomes the Title; the paragraph after it is the grey byline.
- `##`, `###` and `####` become Heading 1, 2 and 3.
- Tables get thin single-line borders and a grey header row; images are scaled to the text width.
- Requirement IDs such as `CON-04` get non-breaking hyphens, so Word never splits them across lines.

## SVG to PNG

```bash
node svg2png.js                                   # every SVG in docs/images/src
node svg2png.js ../../docs/images/src/tech-architecture.svg
```

PNGs are written to `docs/images/`. Rendering needs the Inter font (Regular, Medium and SemiBold); on Windows, Segoe UI Symbol supplies symbols Inter lacks, such as ∩. Set `FONT_DIR` if Inter is installed somewhere unusual.

## Checking a .docx

Open the file in Word. To review every page at once, export it to PDF from Word (File → Save As → PDF).
