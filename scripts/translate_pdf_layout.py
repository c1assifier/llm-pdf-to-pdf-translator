import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import fitz

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from msds_translation_engine import BlockKind, MsdsTranslationEngine, TranslationUnit


FONT_UNICODE = "/System/Library/Fonts/Supplemental/Arial Unicode.ttf"
FONT_BOLD = "/System/Library/Fonts/Supplemental/Arial Bold.ttf"
MIN_FONT_SIZE = 4.0
# Minimum font size below which we skip overlay and leave original English.
# 5pt is acceptable for dense EU REACH table cells; below this we preserve English.
# The dry-run approach (paint only on success) makes this safe: if text won't fit
# even at 5pt, the original is preserved — no white smear.
MIN_VISIBLE_SIZE = 4.5


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


@dataclass
class RenderUnit:
    unit_id: str
    source_text: str
    kind: BlockKind
    rect: fitz.Rect
    style: dict
    page_number: int
    block_index: int
    line_index: int | None = None


def color_to_rgb(value: int) -> tuple[float, float, float]:
    return (
        ((value >> 16) & 255) / 255.0,
        ((value >> 8) & 255) / 255.0,
        (value & 255) / 255.0,
    )


def block_text(block: dict) -> str:
    lines = []
    for line in block.get("lines", []):
        text = "".join(span.get("text", "") for span in line.get("spans", []))
        text = clean_extracted_text(text).strip()
        if text:
            lines.append(text)
    return "\n".join(lines).strip()


def clean_extracted_text(text: str) -> str:
    text = re.sub(r"<\\[A-Za-zА-Яа-я]+>", "", text)
    text = re.sub(r"<\\[^>]*>", "", text)
    text = re.sub(r"\n?\s*\.\s*\n", "\n", text)
    return text


def is_bold_font(font_name: str) -> bool:
    return "bold" in str(font_name).lower()


def block_style(block: dict, kind: BlockKind | None = None) -> dict:
    spans = [
        span
        for line in block.get("lines", [])
        for span in line.get("spans", [])
        if span.get("text", "").strip()
    ]
    if not spans:
        return {"fontsize": 10.0, "color": (0.0, 0.0, 0.0), "fontname": "F0"}
    first = spans[0]
    size = max(span.get("size", 10.0) for span in spans)
    total_chars = sum(len(span.get("text", "").strip()) for span in spans) or 1
    bold_chars = sum(
        len(span.get("text", "").strip())
        for span in spans
        if is_bold_font(str(span.get("font", "")))
    )
    bold_ratio = bold_chars / total_chars

    if kind == BlockKind.HEADING:
        use_bold = bold_ratio >= 0.25 or any(is_bold_font(str(span.get("font", ""))) for span in spans)
    else:
        use_bold = bold_ratio >= 0.7

    fontname = "F1" if use_bold else "F0"
    return {"fontsize": float(size), "color": color_to_rgb(int(first.get("color", 0))), "fontname": fontname}


def line_style(line: dict) -> dict:
    spans = [span for span in line.get("spans", []) if span.get("text", "").strip()]
    if not spans:
        return {"fontsize": 7.0, "color": (0.0, 0.0, 0.0), "fontname": "F0"}
    first = spans[0]
    fontname = "F1" if any(is_bold_font(str(span.get("font", ""))) for span in spans) else "F0"
    return {
        "fontsize": max(float(span.get("size", 7.0)) for span in spans),
        "color": color_to_rgb(int(first.get("color", 0))),
        "fontname": fontname,
        "min_font_size": 4.5,
        "lineheights": [0.95, 0.9, 0.85],
    }


def cluster_column_edges(block: dict) -> list[float]:
    spans = [
        span
        for line in block.get("lines", [])
        for span in line.get("spans", [])
        if span.get("text", "").strip()
    ]
    return sorted({round(span["bbox"][0], 1) for span in spans})


def _block_lines(block: dict) -> list[dict]:
    lines = []
    for line in block.get("lines", []):
        text = clean_extracted_text("".join(span.get("text", "") for span in line.get("spans", []))).strip()
        if text:
            lines.append({"line": line, "text": text, "rect": fitz.Rect(line["bbox"])})
    return lines


def should_split_dense_block(block: dict, kind: BlockKind) -> bool:
    if kind == BlockKind.TABLE_CELL:
        return False
    lines = _block_lines(block)
    if len(lines) < 2 or len(lines) > 10:
        return False
    # Split only compact table/card rows. Long prose blocks must stay block-level;
    # otherwise every wrapped sentence becomes a new translation unit and layout
    # quality gets worse.
    if any(len(item["text"]) > 120 for item in lines):
        return False
    heights = sorted(item["rect"].height for item in lines if item["rect"].height > 0)
    if not heights:
        return False
    median_height = heights[len(heights) // 2]
    block_rect = fitz.Rect(block["bbox"])
    if block_rect.height > 32:
        return False
    x_positions = {round(item["rect"].x0, 1) for item in lines}
    if len(x_positions) >= 2 and block_rect.height <= median_height * 3.2:
        return True
    if len(x_positions) >= 3 and block_rect.height <= median_height * 5.0:
        return True
    return False


def looks_like_wrapped_paragraph_cell(block: dict) -> bool:
    lines = _block_lines(block)
    if len(lines) < 4:
        return False
    x_positions = [round(item["rect"].x0, 1) for item in lines]
    if not x_positions:
        return False
    dominant = max(x_positions.count(x) for x in set(x_positions))
    long_lines = sum(1 for item in lines if len(item["text"]) >= 70)
    return dominant / len(x_positions) >= 0.65 and long_lines >= 2


def looks_like_composition_table_residue(text: str) -> bool:
    """
    Some ExxonMobil composition tables extract leftover rows as one prose block
    containing several chemical names, CAS/EC numbers, concentrations, and H/R
    classifications. Translating that combined block expands it far beyond the
    table and collides with the next section. Chemical identifiers are better
    preserved as-is in this narrow table context.
    """
    cleaned = re.sub(r"\s+", " ", text).strip()
    if not cleaned:
        return False
    cas_count = len(re.findall(r"\b\d{2,7}-\d{2}-\d\b", cleaned))
    has_concentration = bool(re.search(r"(?:<|>|0[,\.]\d+|\d+)\s*(?:-|<)?\s*\d*\s*%", cleaned))
    has_classification = bool(re.search(r"\b(?:H\d{3}|R\d{2}(?:/\d{2})?|Skin Sens|Aquatic|Repr\.|Xi;|N;)\b", cleaned))
    uppercase_words = re.findall(r"\b[A-Z][A-Z0-9,'./-]{2,}\b", cleaned)
    return cas_count >= 2 and has_concentration and (has_classification or len(uppercase_words) >= 8)


def _text_luminance(color: tuple) -> float:
    r, g, b = color[0], color[1], color[2]
    return 0.299 * r + 0.587 * g + 0.114 * b


def _find_bg_fill_color(page: fitz.Page, rect: fitz.Rect) -> tuple[float, float, float] | None:
    """
    Find the fill color of a vector path that contains this text rect.
    Used to detect colored-background headers (e.g. white text on dark blue).

    IMPORTANT: only counts a fill as a background if it covers ≥70% of the
    text rect. This prevents GHS pictogram border/icon paths (which merely
    touch the rect edge) from being mistaken for a cell background color,
    which would cause _draw_bg to paint a dark/black rectangle over the
    pictogram column.
    """
    try:
        drawings = page.get_drawings()
        best: tuple[float, float, float] | None = None
        best_area = float("inf")
        rect_area = rect.width * rect.height
        if rect_area <= 0:
            return None
        for path in drawings:
            fill = path.get("fill")
            if not fill or len(fill) < 3:
                continue
            fill_rect = path.get("rect")
            if fill_rect is None:
                continue
            fr = fitz.Rect(fill_rect)
            # Must substantially cover our rect (≥70%) — not just touch a corner.
            # This filters out GHS pictogram icons, border lines, etc.
            intersection = fr & rect
            if intersection.is_empty:
                continue
            intersection_area = intersection.width * intersection.height
            if intersection_area / rect_area < 0.70:
                continue
            r, g, b = fill[0], fill[1], fill[2]
            if 0.299 * r + 0.587 * g + 0.114 * b > 0.85:
                continue  # skip white/light backgrounds
            area = fr.width * fr.height
            if area < best_area:
                best_area = area
                best = (r, g, b)
        return best
    except Exception:
        return None


def draw_white_rect(page: fitz.Page, rect: fitz.Rect) -> None:
    page.draw_rect(rect, color=None, fill=(1, 1, 1), overlay=True)


def _rect_intersects_any(rect: fitz.Rect, others: list[fitz.Rect], *, tolerance: float = 0.35) -> bool:
    probe = fitz.Rect(rect.x0 + tolerance, rect.y0 + tolerance, rect.x1 - tolerance, rect.y1 - tolerance)
    if not probe.is_valid or probe.is_empty:
        probe = rect
    for other in others:
        intersection = probe & other
        if not intersection.is_empty and intersection.width > tolerance and intersection.height > tolerance:
            return True
    return False


def fit_and_insert(page: fitz.Page, rect: fitz.Rect, candidates: list[str], style: dict) -> dict:
    if not rect.is_valid or rect.is_empty:
        return {"success": False, "reason": "invalid-rect"}

    deduped = []
    for text in candidates:
        cleaned = text.strip()
        if cleaned and cleaned not in deduped:
            deduped.append(cleaned)
    if style.get("prefer_short"):
        deduped.sort(key=lambda item: (len(item), item.count("\n")))
    if not deduped:
        return {"success": False, "reason": "empty-candidate-list"}

    base_size = max(float(style.get("fontsize", 10.0)), MIN_FONT_SIZE)
    min_font_size = float(style.get("min_font_size", MIN_FONT_SIZE))
    # Use MIN_VISIBLE_SIZE as the effective floor: below this, leave English original.
    effective_min = max(min_font_size, MIN_VISIBLE_SIZE)
    lineheights = style.get("lineheights", [1.0, 0.95, 0.9])
    page_rect = page.rect
    avoid_rects = [fitz.Rect(item) for item in style.get("avoid_rects", [])]
    insert_rects = [rect]
    if style.get("no_expand"):
        padded = rect
        one_line = rect
    else:
        # PyMuPDF bboxes can be too tight vertically for insert_textbox(), especially
        # when one visual row was extracted as several spans. A small expansion makes
        # those rows render without resorting to unreadably tiny type.
        padded = fitz.Rect(
            max(page_rect.x0, rect.x0 - 1.0),
            max(page_rect.y0, rect.y0 - 1.0),
            min(page_rect.x1, rect.x1 + 3.0),
            min(page_rect.y1, rect.y1 + max(3.0, rect.height * 0.45)),
        )
    if padded != rect:
        insert_rects.append(padded)
        if rect.height <= 10.0:
            one_line = fitz.Rect(
                max(page_rect.x0, rect.x0 - 1.0),
                max(page_rect.y0, rect.y0 - 1.5),
                min(page_rect.x1, rect.x1 + 5.0),
                min(page_rect.y1, rect.y1 + 6.0),
            )
            if one_line not in insert_rects:
                insert_rects.append(one_line)
    insert_rects = [
        candidate
        for index, candidate in enumerate(insert_rects)
        if index == 0 or not _rect_intersects_any(candidate, avoid_rects)
    ]

    # ── Determine text and background colors ─────────────────────────────────
    # If the original text is white/light (white-on-colored-background header),
    # use black text on white background so the translation is always readable.
    text_color = tuple(style.get("color", (0.0, 0.0, 0.0)))
    if _text_luminance(text_color) > 0.85:
        text_color = (0.0, 0.0, 0.0)  # force black text

    # bg_rect: the rectangle used for erasing the original text background.
    # For table cells this is tight to the source span (not the full column width)
    # so we don't paint over adjacent GHS pictogram Form XObjects.
    raw_bg = style.get("bg_rect")
    bg_rect = fitz.Rect(raw_bg) if raw_bg else rect

    # Try to detect actual background color (e.g. colored table cell/header).
    # NOTE: _find_bg_fill_color requires ≥70% coverage so GHS icon border paths
    # (which only touch the rect edge) are NOT mistaken for cell background.
    detected_bg = _find_bg_fill_color(page, bg_rect)
    bg_color = detected_bg if detected_bg is not None else (1.0, 1.0, 1.0)

    # ── KEY FIX: "dry-run first, paint only on success" ──────────────────────
    # OLD (buggy) approach: draw white rect FIRST, then try insert.
    #   → if insert fails, white rect stays = original English is erased, nothing shown.
    #   → result: blank white smears all over the document.
    #
    # NEW approach:
    #   1. Dry-run: insert text in bg_color (invisible ink) to check if it fits.
    #   2. Only if it fits: draw bg rect to cover original, then insert real text.
    #   3. If nothing fits: don't touch the page → original English stays intact.

    # Pre-load font object for TextWriter path (Type3 PDF workaround).
    # TextWriter bypasses the page's font resource registry entirely,
    # so it works even on PDFs whose Type3 fonts cause insert_font() to fail.
    _tw_font: fitz.Font | None = None
    if style.get("use_fontfile"):
        fn = style.get("fontname", "F0")
        font_path = FONT_BOLD if fn == "F1" else FONT_UNICODE
        try:
            _tw_font = fitz.Font(fontfile=font_path)
        except Exception:
            return {"success": False, "reason": "font-load-failed"}

    for candidate_index, text in enumerate(deduped):
        font_size = base_size
        while font_size >= effective_min:
            for lineheight in lineheights:
                for rect_index, insert_rect in enumerate(insert_rects):
                    if _tw_font is not None:
                        # ── TextWriter path (Type3 PDF conflict workaround) ─────────
                        # fill_textbox() fills the internal buffer but does NOT write to
                        # the page. Depending on PyMuPDF version, success is returned as
                        # either "" or an empty list.
                        try:
                            writer = fitz.TextWriter(page.rect)
                            overflow = writer.fill_textbox(
                                insert_rect, text,
                                font=_tw_font,
                                fontsize=font_size,
                                lineheight=lineheight,
                                align=0,
                                warn=False,
                            )
                        except Exception:
                            overflow = "error"
                        if overflow == "" or overflow == []:  # all text fits
                            try:
                                page.draw_rect(bg_rect, color=None, fill=bg_color, overlay=True)
                                writer.write_text(page, color=text_color, opacity=1, overlay=True)
                            except Exception:
                                pass  # page draw failed (Type3 page-level error) — skip silently
                            return {
                                "success": True,
                                "candidate_index": candidate_index,
                                "rect_index": rect_index,
                                "fontsize": font_size,
                                "lineheight": lineheight,
                            }
                    else:
                        # ── Normal path: pre-registered fonts (F0/F1) ───────────────
                        # Step 1 — invisible dry-run (text color = bg_color = hidden ink).
                        try:
                            probe = page.insert_textbox(
                                insert_rect, text,
                                fontname=style.get("fontname", "F0"),
                                fontsize=font_size,
                                color=bg_color,
                                lineheight=lineheight,
                                align=0,
                                overlay=True,
                            )
                        except Exception:
                            probe = -1
                        if probe >= 0:
                            # Step 2 — text fits! Erase original, insert translation.
                            try:
                                page.draw_rect(bg_rect, color=None, fill=bg_color, overlay=True)
                            except Exception:
                                pass
                            page.insert_textbox(
                                insert_rect, text,
                                fontname=style.get("fontname", "F0"),
                                fontsize=font_size,
                                color=text_color,
                                lineheight=lineheight,
                                align=0,
                                overlay=True,
                            )
                            return {
                                "success": True,
                                "candidate_index": candidate_index,
                                "rect_index": rect_index,
                                "fontsize": font_size,
                                "lineheight": lineheight,
                            }
            font_size -= 0.5

    # Nothing fit above MIN_VISIBLE_SIZE.
    # Original English is preserved — no white rect was drawn, page untouched.
    return {
        "success": False,
        "reason": "skipped-too-small",
        "fontsize": effective_min,
    }


def build_page_units(page: fitz.Page, engine: MsdsTranslationEngine, page_number: int) -> tuple[list[TranslationUnit], list[RenderUnit]]:
    translation_units: list[TranslationUnit] = []
    render_units: list[RenderUnit] = []

    blocks = [block for block in page.get_text("dict")["blocks"] if block["type"] == 0]
    for block_index, block in enumerate(blocks, start=1):
        text = block_text(block)
        if not text:
            continue

        classification = engine.classify_block(text, block)
        if looks_like_composition_table_residue(text):
            engine.log_event(
                "block_skipped",
                page=page_number,
                block=block_index,
                kind=classification.kind.value,
                reason="composition-table-residue",
                source_preview=text[:200],
            )
            continue
        if classification.kind == BlockKind.NUMERIC_CODE:
            engine.log_event(
                "block_skipped",
                page=page_number,
                block=block_index,
                kind=classification.kind.value,
                reason="numeric-or-code",
            )
            continue

        if should_split_dense_block(block, classification.kind):
            lines = _block_lines(block)
            x_positions = sorted({round(item["rect"].x0, 1) for item in lines})
            block_right = fitz.Rect(block["bbox"]).x1
            for line_index, item in enumerate(lines, start=1):
                source_text = item["text"]
                if engine.is_numeric_or_code(source_text):
                    continue
                rect = fitz.Rect(item["rect"])
                col_index = min(range(len(x_positions)), key=lambda i: abs(x_positions[i] - round(rect.x0, 1)))
                next_x = x_positions[col_index + 1] if col_index + 1 < len(x_positions) else block_right
                right = max(rect.x1 + 2, next_x - 3)
                text_rect = fitz.Rect(rect.x0, rect.y0 - 0.4, right, rect.y1 + 0.4)
                bg_rect = fitz.Rect(rect.x0, rect.y0 - 0.4, min(rect.x1 + 4, right), rect.y1 + 0.4)
                unit_id = f"p{page_number}_b{block_index}_l{line_index}"
                translation_units.append(TranslationUnit(unit_id=unit_id, text=source_text, kind=BlockKind.TABLE_CELL))
                style = line_style(item["line"])
                style["bg_rect"] = [bg_rect.x0, bg_rect.y0, bg_rect.x1, bg_rect.y1]
                style["no_expand"] = True
                if text_rect.width <= 95:
                    style["prefer_short"] = True
                    style["min_font_size"] = 4.0
                render_units.append(
                    RenderUnit(
                        unit_id=unit_id,
                        source_text=source_text,
                        kind=BlockKind.TABLE_CELL,
                        rect=text_rect,
                        style=style,
                        page_number=page_number,
                        block_index=block_index,
                        line_index=line_index,
                    )
                )
            continue

        if classification.kind == BlockKind.TABLE_CELL:
            if looks_like_wrapped_paragraph_cell(block):
                unit_id = f"p{page_number}_b{block_index}"
                translation_units.append(TranslationUnit(unit_id=unit_id, text=text, kind=BlockKind.PARAGRAPH))
                style = block_style(block, BlockKind.PARAGRAPH)
                style["min_font_size"] = 4.5
                style["lineheights"] = [0.95, 0.9, 0.85, 0.8]
                render_units.append(
                    RenderUnit(
                        unit_id=unit_id,
                        source_text=text,
                        kind=BlockKind.PARAGRAPH,
                        rect=fitz.Rect(block["bbox"]),
                        style=style,
                        page_number=page_number,
                        block_index=block_index,
                    )
                )
                continue

            columns = cluster_column_edges(block)
            block_right = block["bbox"][2]
            for line_index, line in enumerate(block.get("lines", []), start=1):
                spans = [span for span in line.get("spans", []) if span.get("text", "").strip()]
                if not spans:
                    continue
                span = spans[0]
                source_text = clean_extracted_text(span.get("text", "")).strip()
                if not source_text:
                    continue
                if engine.is_numeric_or_code(source_text):
                    continue

                x0, y0, x1, y1 = span["bbox"]
                col_index = min(range(len(columns)), key=lambda i: abs(columns[i] - round(x0, 1)))
                next_x = columns[col_index + 1] if col_index + 1 < len(columns) else block_right
                right = max(x1 + 2, next_x - 3)
                # text_rect: full column width — insert_textbox uses this so
                # translated text (which may be wider than source) can flow properly.
                rect = fitz.Rect(x0, y0 - 0.5, right, y1 + 0.5)
                # bg_rect: tight to actual span — background erase covers only
                # the source text area, NOT adjacent GHS pictogram columns which
                # are Form XObjects with no text and no detected column edge.
                span_right = min(x1 + 4, right)
                bg_rect = fitz.Rect(x0, y0 - 0.5, span_right, y1 + 0.5)
                unit_id = f"p{page_number}_b{block_index}_l{line_index}"
                translation_units.append(TranslationUnit(unit_id=unit_id, text=source_text, kind=BlockKind.TABLE_CELL))
                style = {
                    "fontsize": float(span.get("size", 6.0)),
                    "color": color_to_rgb(int(span.get("color", 0))),
                    "fontname": "F1" if "bold" in str(span.get("font", "")).lower() else "F0",
                    "min_font_size": 5.0,
                    "lineheights": [0.95, 0.9, 0.85],
                    # bg_rect is tight to the source span so the white erase
                    # doesn't bleed into adjacent GHS pictogram columns.
                    "bg_rect": [bg_rect.x0, bg_rect.y0, bg_rect.x1, bg_rect.y1],
                }
                if rect.width <= 95:
                    style["prefer_short"] = True
                    style["no_expand"] = True
                    style["min_font_size"] = 4.0
                render_units.append(
                    RenderUnit(
                        unit_id=unit_id,
                        source_text=source_text,
                        kind=BlockKind.TABLE_CELL,
                        rect=rect,
                        style=style,
                        page_number=page_number,
                        block_index=block_index,
                        line_index=line_index,
                    )
                )
            continue

        unit_id = f"p{page_number}_b{block_index}"
        translation_units.append(TranslationUnit(unit_id=unit_id, text=text, kind=classification.kind))
        render_units.append(
            RenderUnit(
                unit_id=unit_id,
                source_text=text,
                kind=classification.kind,
                rect=fitz.Rect(block["bbox"]),
                style=block_style(block, classification.kind),
                page_number=page_number,
                block_index=block_index,
            )
        )

    return translation_units, render_units


def render_page_units(
    page: fitz.Page,
    engine: MsdsTranslationEngine,
    artifacts: dict[str, Any],
    render_units: list[RenderUnit],
) -> list[dict]:
    problematic_blocks: list[dict] = []
    for render_unit in render_units:
        try:
            artifact = artifacts[render_unit.unit_id]
            avoid_rects = [
                other.rect
                for other in render_units
                if other.unit_id != render_unit.unit_id and other.rect.intersects(render_unit.rect + (-4, -3, 8, 8))
            ]
            style = dict(render_unit.style)
            if avoid_rects:
                style["avoid_rects"] = [[r.x0, r.y0, r.x1, r.y1] for r in avoid_rects]
            fit = fit_and_insert(page, render_unit.rect, artifact.fit_candidates, style)
            issues = list(artifact.issues)
            if not fit["success"]:
                issues.append(fit.get("reason", "fit-failed"))
                problematic_blocks.append(
                    {
                        "page": render_unit.page_number,
                        "block": render_unit.block_index,
                        "line": render_unit.line_index,
                        "kind": render_unit.kind.value,
                        "problems": issues,
                        "source_preview": render_unit.source_text[:200],
                    }
                )

            event = "table_cell_rendered" if render_unit.kind == BlockKind.TABLE_CELL else "block_rendered"
            engine.log_event(
                event,
                page=render_unit.page_number,
                block=render_unit.block_index,
                line=render_unit.line_index,
                kind=render_unit.kind.value,
                success=fit["success"],
                issues=artifact.issues,
                fit=fit,
                source_preview=render_unit.source_text[:200],
            )
        except Exception as unit_err:
            # One render unit failed (e.g. m_internal on Type3 page) — skip it,
            # continue rendering the rest of the page.
            engine.log_event(
                "unit_error",
                page=render_unit.page_number,
                block=render_unit.block_index,
                error=repr(unit_err),
                source_preview=render_unit.source_text[:100],
            )

    return problematic_blocks


def translate_pdf(
    source: Path,
    output: Path,
    *,
    source_lang: str,
    target_lang: str,
    model: str,
    artifact_dir: Path | None = None,
) -> dict:
    output.parent.mkdir(parents=True, exist_ok=True)
    artifact_root = artifact_dir or output.parent
    artifact_root.mkdir(parents=True, exist_ok=True)
    cache_path = artifact_root / f"{output.stem}.translation-cache.json"
    log_path = artifact_root / f"{output.stem}.translation-log.jsonl"
    problematic_blocks: list[dict] = []

    engine = MsdsTranslationEngine(
        model=model,
        source_lang=source_lang,
        target_lang=target_lang,
        cache_path=cache_path,
        log_path=log_path,
    )
    engine.log_event("file_start", source=str(source), output=str(output), model=model)

    try:
        doc = fitz.open(source)

        # ── Detect Type3 font conflict at document level ─────────────────────────
        # Type3 fonts cause 'NoneType has no attribute m_internal' in fitz when
        # insert_font() is called. A single failure poisons subsequent pages too,
        # so we probe page 1 and if it fails, skip insert_font for the whole doc
        # and use TextWriter (which bypasses the page font resource registry).
        type3_doc = False
        try:
            doc[0].insert_font(fontname="F0", fontfile=FONT_UNICODE)
            doc[0].insert_font(fontname="F1", fontfile=FONT_BOLD)
        except Exception as font_probe_err:
            type3_doc = True
            engine.log_event("font_warning", page=1, error=repr(font_probe_err))

        # Re-open to get a clean doc state (probe may have partially modified page 1).
        doc.close()
        doc = fitz.open(source)

        for page_number, page in enumerate(doc, start=1):
            fonts_registered = False
            if not type3_doc:
                # Try registering Unicode fonts on this specific page.
                try:
                    page.insert_font(fontname="F0", fontfile=FONT_UNICODE)
                    page.insert_font(fontname="F1", fontfile=FONT_BOLD)
                    fonts_registered = True
                except Exception as font_err:
                    # First failure → mark entire doc as Type3
                    type3_doc = True
                    engine.log_event("font_warning", page=page_number, error=repr(font_err))

            try:
                blocks = [block for block in page.get_text("dict", flags=fitz.TEXT_PRESERVE_WHITESPACE)["blocks"] if block["type"] == 0]
            except Exception:
                try:
                    # Fallback: simpler extraction flags
                    blocks = [block for block in page.get_text("dict")["blocks"] if block["type"] == 0]
                except Exception:
                    engine.log_event("page_skipped", page=page_number, reason="get_text failed")
                    continue

            engine.log_event("page_start", page=page_number, blocks=len(blocks))

            try:
                translation_units, render_units = build_page_units(page, engine, page_number)
                # If fonts couldn't be pre-registered (Type3 conflict), mark render
                # units to use TextWriter (fitz.Font + TextWriter bypasses font resource
                # registry entirely — safe on all pages of Type3 documents).
                if not fonts_registered:
                    for ru in render_units:
                        ru.style["use_fontfile"] = True
                artifacts = engine.translate_many(translation_units)
                problematic_blocks.extend(render_page_units(page, engine, artifacts, render_units))
            except Exception as page_err:
                engine.log_event("page_error", page=page_number, error=repr(page_err))
                # Page saved as-is (original English), continue to next page

        if output.exists():
            output.unlink()
        doc.save(output, garbage=4, deflate=True)
        doc.close()

        summary = {
            "source": str(source),
            "output": str(output),
            "success": True,
            "problematic_blocks": len(problematic_blocks),
        }
        engine.log_event("file_success", **summary)
        return {"success": True, "problematic_blocks": problematic_blocks, "log_path": str(log_path)}
    except Exception as exc:
        engine.log_event(
            "file_failure",
            source=str(source),
            output=str(output),
            error=repr(exc),
            problematic_blocks=problematic_blocks,
        )
        return {
            "success": False,
            "problematic_blocks": problematic_blocks,
            "error": repr(exc),
            "log_path": str(log_path),
        }


def process_directory(
    input_dir: Path,
    output_dir: Path,
    *,
    source_lang: str,
    target_lang: str,
    model: str,
    artifact_dir: Path | None = None,
) -> list[dict]:
    output_dir.mkdir(parents=True, exist_ok=True)
    artifact_root = artifact_dir or output_dir
    artifact_root.mkdir(parents=True, exist_ok=True)
    summary_path = artifact_root / "msds_batch_summary.jsonl"
    results: list[dict] = []

    for source in sorted(input_dir.glob("*.pdf")):
        output = output_dir / f"{source.stem}_ru.pdf"
        result = translate_pdf(
            source,
            output,
            source_lang=source_lang,
            target_lang=target_lang,
            model=model,
            artifact_dir=artifact_root,
        )
        record = {"source": str(source), "output": str(output), **result}
        with summary_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        results.append(record)

    return results


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", nargs="?")
    parser.add_argument("output", nargs="?")
    parser.add_argument("--input-dir")
    parser.add_argument("--output-dir")
    parser.add_argument("--source-lang", default="en")
    parser.add_argument("--target-lang", default="ru")
    parser.add_argument("--model", default="gpt-5.4-mini")
    parser.add_argument("--artifact-dir")
    return parser


def main() -> None:
    load_env_file(ROOT / ".env")
    parser = build_parser()
    args = parser.parse_args()

    if args.input_dir and args.output_dir:
        process_directory(
            Path(args.input_dir),
            Path(args.output_dir),
            source_lang=args.source_lang,
            target_lang=args.target_lang,
            model=args.model,
            artifact_dir=Path(args.artifact_dir) if args.artifact_dir else None,
        )
        return

    if not args.source or not args.output:
        parser.error("Either provide source/output or --input-dir/--output-dir")

    result = translate_pdf(
        Path(args.source),
        Path(args.output),
        source_lang=args.source_lang,
        target_lang=args.target_lang,
        model=args.model,
        artifact_dir=Path(args.artifact_dir) if args.artifact_dir else None,
    )
    if not result.get("success"):
        error = result.get("error", "unknown error")
        print(f"FAILED: {error}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
