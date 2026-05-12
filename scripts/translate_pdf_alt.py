"""
translate_pdf_alt.py
--------------------
Alternative PDF translator using pdfplumber + reportlab + pypdf
(does NOT require PyMuPDF / fitz).

Workflow per PDF:
  1. Extract text lines with positions via pdfplumber
  2. Group lines → logical blocks
  3. Translate blocks via MsdsTranslationEngine
  4. Build reportlab overlay: white rect + translated text
  5. Merge overlay over original with pypdf
"""
from __future__ import annotations

import argparse
import io
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pdfplumber
from pypdf import PdfReader, PdfWriter
from reportlab.lib.pagesizes import A4
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas as rl_canvas

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from msds_translation_engine import BlockKind, MsdsTranslationEngine, TranslationUnit

# ---------------------------------------------------------------------------
# Font setup – DejaVu Sans has full Cyrillic coverage
# ---------------------------------------------------------------------------
FONT_REGULAR = "DejaVuSans"
FONT_BOLD = "DejaVuSans-Bold"
FONT_REGULAR_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
FONT_BOLD_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

_fonts_registered = False


def _ensure_fonts() -> None:
    global _fonts_registered
    if _fonts_registered:
        return
    pdfmetrics.registerFont(TTFont(FONT_REGULAR, FONT_REGULAR_PATH))
    pdfmetrics.registerFont(TTFont(FONT_BOLD, FONT_BOLD_PATH))
    _fonts_registered = True


MIN_FONT_SIZE = 4.0
MAX_FONT_SIZE = 72.0

# Lines within this vertical gap (points) belong to the same block
BLOCK_GAP_PT = 5.0

# Skip blocks shorter than this (numeric-only, single chars, etc.)
MIN_BLOCK_CHARS = 2

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class TextBlock:
    """A logical group of lines extracted from one PDF page."""
    lines: list[dict]          # pdfplumber line dicts
    x0: float
    y0: float                  # top (pdfplumber coords, y=0 at top)
    x1: float
    y1: float                  # bottom
    text: str
    is_bold: bool
    font_size: float
    page_height: float         # needed to flip coords for reportlab


# ---------------------------------------------------------------------------
# Extraction helpers
# ---------------------------------------------------------------------------

def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    mid = len(s) // 2
    return s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) / 2.0


def _block_is_bold(block_lines: list[dict], chars: list[dict]) -> bool:
    """Check if majority of chars in block area are bold."""
    if not block_lines:
        return False
    top = block_lines[0]["top"]
    bot = block_lines[-1]["bottom"]
    x0 = min(l["x0"] for l in block_lines)
    x1 = max(l["x1"] for l in block_lines)
    block_chars = [
        c for c in chars
        if c["top"] >= top - 1 and c["bottom"] <= bot + 1
        and c["x0"] >= x0 - 2 and c["x1"] <= x1 + 2
    ]
    if not block_chars:
        return False
    bold_count = sum(1 for c in block_chars if "bold" in c.get("fontname", "").lower())
    return bold_count / len(block_chars) >= 0.4


def _block_font_size(block_lines: list[dict], chars: list[dict]) -> float:
    """Approximate dominant font size for the block."""
    if not block_lines:
        return 8.0
    top = block_lines[0]["top"]
    bot = block_lines[-1]["bottom"]
    sizes = [c["size"] for c in chars if c["top"] >= top - 1 and c["bottom"] <= bot + 1 and c.get("size")]
    if sizes:
        return _median(sizes)
    # fallback: use line height
    heights = [l["bottom"] - l["top"] for l in block_lines if l["bottom"] > l["top"]]
    return _median(heights) if heights else 8.0


def group_lines_into_blocks(
    lines: list[dict],
    chars: list[dict],
    page_height: float,
    gap: float = BLOCK_GAP_PT,
) -> list[TextBlock]:
    if not lines:
        return []
    blocks: list[TextBlock] = []
    current: list[dict] = [lines[0]]

    def make_block(group: list[dict]) -> TextBlock:
        text = "\n".join(l["text"] for l in group).strip()
        x0 = min(l["x0"] for l in group)
        x1 = max(l["x1"] for l in group)
        y0 = group[0]["top"]
        y1 = group[-1]["bottom"]
        bold = _block_is_bold(group, chars)
        size = _block_font_size(group, chars)
        return TextBlock(
            lines=group,
            x0=x0, y0=y0, x1=x1, y1=y1,
            text=text,
            is_bold=bold,
            font_size=max(MIN_FONT_SIZE, size),
            page_height=page_height,
        )

    for line in lines[1:]:
        prev = current[-1]
        if line["top"] - prev["bottom"] <= gap:
            current.append(line)
        else:
            blocks.append(make_block(current))
            current = [line]
    blocks.append(make_block(current))
    return blocks


def extract_blocks(pdf_path: Path) -> list[list[TextBlock]]:
    """Return a list of block-lists, one per page."""
    pages_blocks: list[list[TextBlock]] = []
    with pdfplumber.open(str(pdf_path)) as pdf:
        for page in pdf.pages:
            try:
                lines = page.extract_text_lines()
                chars = page.chars
                blocks = group_lines_into_blocks(lines, chars, page.height)
                pages_blocks.append(blocks)
            except Exception:
                pages_blocks.append([])
    return pages_blocks


# ---------------------------------------------------------------------------
# Translation helpers
# ---------------------------------------------------------------------------

def _detect_kind(block: TextBlock, engine: MsdsTranslationEngine) -> BlockKind:
    text = block.text.strip()
    if engine.is_numeric_or_code(text):
        return BlockKind.NUMERIC_CODE
    if block.is_bold and len(text) <= 140:
        return BlockKind.HEADING
    if block.font_size >= 9.5 and "\n" not in text and len(text) <= 140:
        return BlockKind.HEADING
    # Simple heuristic for table cell (short, single line, uppercase-heavy)
    if "\n" not in text and len(text) <= 60:
        upper_ratio = sum(1 for c in text if c.isupper()) / max(len([c for c in text if c.isalpha()]), 1)
        if upper_ratio >= 0.7:
            return BlockKind.HEADING
    return BlockKind.PARAGRAPH


def translate_blocks(
    pages_blocks: list[list[TextBlock]],
    engine: MsdsTranslationEngine,
) -> dict[str, str]:
    """
    Build TranslationUnit list, batch-translate, return {unit_id: final_text}.
    """
    units: list[TranslationUnit] = []
    for page_idx, blocks in enumerate(pages_blocks):
        for block_idx, block in enumerate(blocks):
            text = block.text.strip()
            if len(text) < MIN_BLOCK_CHARS:
                continue
            uid = f"p{page_idx:03d}_b{block_idx:03d}"
            kind = _detect_kind(block, engine)
            units.append(TranslationUnit(unit_id=uid, text=text, kind=kind))

    results = engine.translate_many(units)
    return {uid: art.final_text for uid, art in results.items()}


# ---------------------------------------------------------------------------
# Overlay rendering (reportlab)
# ---------------------------------------------------------------------------

def _fit_text(
    c: rl_canvas.Canvas,
    text: str,
    x: float,
    y_bottom_rl: float,
    width: float,
    height: float,
    font_name: str,
    base_size: float,
) -> None:
    """Draw text into a rectangle, shrinking font if needed."""
    text = text.strip()
    if not text:
        return

    # Try to fit with decreasing font sizes
    for size in [base_size, base_size * 0.9, base_size * 0.8, base_size * 0.7,
                 max(MIN_FONT_SIZE, base_size * 0.6), MIN_FONT_SIZE]:
        size = max(MIN_FONT_SIZE, size)
        c.setFont(font_name, size)
        line_height = size * 1.15

        # Wrap text manually
        wrapped = _wrap_text(c, text, width, font_name, size)
        total_h = len(wrapped) * line_height

        if total_h <= height + size * 0.5 or size <= MIN_FONT_SIZE:
            # Draw from top of the rect downward
            y = y_bottom_rl + height - size  # start of first baseline
            for line in wrapped:
                if y < y_bottom_rl - size:
                    break
                c.drawString(x + 1, y, line)
                y -= line_height
            return


def _wrap_text(
    c: rl_canvas.Canvas,
    text: str,
    max_width: float,
    font_name: str,
    font_size: float,
) -> list[str]:
    """Word-wrap text to fit within max_width."""
    result: list[str] = []
    for paragraph in text.split("\n"):
        words = paragraph.split()
        if not words:
            result.append("")
            continue
        current = words[0]
        for word in words[1:]:
            candidate = current + " " + word
            if c.stringWidth(candidate, font_name, font_size) <= max_width:
                current = candidate
            else:
                result.append(current)
                current = word
        result.append(current)
    return result


def build_overlay(
    pages_blocks: list[list[TextBlock]],
    translations: dict[str, str],
    page_sizes: list[tuple[float, float]],
) -> bytes:
    """Build a reportlab PDF (overlay) and return it as bytes."""
    _ensure_fonts()
    buf = io.BytesIO()
    # Use first page size as default; we'll set per-page
    w0, h0 = page_sizes[0] if page_sizes else (595, 842)
    c = rl_canvas.Canvas(buf, pagesize=(w0, h0))

    for page_idx, blocks in enumerate(pages_blocks):
        pw, ph = page_sizes[page_idx] if page_idx < len(page_sizes) else (w0, h0)
        c.setPageSize((pw, ph))

        for block_idx, block in enumerate(blocks):
            uid = f"p{page_idx:03d}_b{block_idx:03d}"
            translated = translations.get(uid)
            if not translated or not translated.strip():
                continue

            source_text = block.text.strip()
            if translated.strip() == source_text:
                # Untranslated – skip overlay (leave original)
                continue

            # Block coordinates in pdfplumber: y=0 at top
            # reportlab: y=0 at bottom → flip
            x0 = block.x0
            x1 = min(block.x1, pw - 2)
            y0_pl = block.y0   # top in pdfplumber
            y1_pl = block.y1   # bottom in pdfplumber

            # Convert to reportlab coords
            rl_top = ph - y0_pl      # reportlab y of block top
            rl_bot = ph - y1_pl      # reportlab y of block bottom
            # rl_bot < rl_top in reportlab (y grows upward)

            rect_x = x0
            rect_y = rl_bot          # lower-left corner
            rect_w = max(x1 - x0, 10)
            rect_h = max(rl_top - rl_bot, block.font_size * 1.3)

            # White background to erase original
            c.setFillColorRGB(1, 1, 1)
            c.rect(rect_x - 1, rect_y - 1, rect_w + 2, rect_h + 2, fill=1, stroke=0)

            # Draw translated text
            font_name = FONT_BOLD if block.is_bold else FONT_REGULAR
            c.setFillColorRGB(0, 0, 0)
            _fit_text(
                c,
                translated,
                rect_x, rect_y,
                rect_w, rect_h,
                font_name,
                block.font_size,
            )

        c.showPage()

    c.save()
    return buf.getvalue()


# ---------------------------------------------------------------------------
# PDF merge
# ---------------------------------------------------------------------------

def merge_overlay(source_path: Path, overlay_bytes: bytes, output_path: Path) -> None:
    """Merge the overlay PDF on top of the source PDF and write output."""
    source_reader = PdfReader(str(source_path))
    overlay_reader = PdfReader(io.BytesIO(overlay_bytes))
    writer = PdfWriter()

    for page_idx, src_page in enumerate(source_reader.pages):
        if page_idx < len(overlay_reader.pages):
            overlay_page = overlay_reader.pages[page_idx]
            src_page.merge_page(overlay_page)
        writer.add_page(src_page)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("wb") as fh:
        writer.write(fh)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def translate_pdf(
    source: Path,
    output: Path,
    artifact_dir: Path,
    model: str = "gpt-5.4-mini",
    source_lang: str = "en",
    target_lang: str = "ru",
) -> dict:
    artifact_dir.mkdir(parents=True, exist_ok=True)
    cache_path = artifact_dir / f"{output.stem}.translation-cache.json"
    log_path = artifact_dir / f"{output.stem}.translation-log.jsonl"

    engine = MsdsTranslationEngine(
        model=model,
        source_lang=source_lang,
        target_lang=target_lang,
        cache_path=cache_path,
        log_path=log_path,
    )

    print(f"  Extracting blocks from {source.name}...", flush=True)
    pages_blocks = extract_blocks(source)

    # Collect page sizes
    page_sizes: list[tuple[float, float]] = []
    with pdfplumber.open(str(source)) as pdf:
        for page in pdf.pages:
            page_sizes.append((float(page.width), float(page.height)))

    total_blocks = sum(len(b) for b in pages_blocks)
    print(f"  {len(pages_blocks)} pages, {total_blocks} blocks", flush=True)

    print("  Translating...", flush=True)
    translations = translate_blocks(pages_blocks, engine)

    translated_count = sum(
        1 for v in translations.values()
        if v.strip() and v.strip() != pages_blocks[
            int(k.split("_")[0][1:])
        ][int(k.split("_")[1][1:])].text.strip()
        for k in [next(kk for kk in translations if kk == k)]
    )
    print(f"  Building overlay ({len(translations)} translations)...", flush=True)
    overlay_bytes = build_overlay(pages_blocks, translations, page_sizes)

    print(f"  Merging and writing {output.name}...", flush=True)
    merge_overlay(source, overlay_bytes, output)

    result = {
        "source": str(source),
        "output": str(output),
        "pages": len(pages_blocks),
        "blocks": total_blocks,
        "translations": len(translations),
        "success": True,
    }
    print(f"  Done: {output}", flush=True)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Translate a GHS/MSDS PDF to Russian")
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--artifact-dir", type=Path, required=True)
    parser.add_argument("--model", default="gpt-5.4-mini")
    parser.add_argument("--source-lang", default="en")
    parser.add_argument("--target-lang", default="ru")
    args = parser.parse_args()

    result = translate_pdf(
        source=args.source,
        output=args.output,
        artifact_dir=args.artifact_dir,
        model=args.model,
        source_lang=args.source_lang,
        target_lang=args.target_lang,
    )
    if not result["success"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
