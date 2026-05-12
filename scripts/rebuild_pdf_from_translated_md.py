import argparse
import json
import re
import sys
from pathlib import Path

import fitz

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.translate_pdf_layout import FONT_BOLD, FONT_UNICODE, fit_and_insert


MARKER_RE = re.compile(r"^<!--\s*(p\d{3}_b\d{3})\s*\|\s*([a-z_]+)\s*-->\s*$")
CODE_FENCE_RE = re.compile(r"^```text\s*$")


def parse_translated_md(md_path: Path) -> dict[str, str]:
    text = md_path.read_text(encoding="utf-8")
    lines = text.splitlines()
    blocks: dict[str, str] = {}
    i = 0
    while i < len(lines):
        match = MARKER_RE.match(lines[i])
        if not match:
            i += 1
            continue

        block_id, kind = match.group(1), match.group(2)
        i += 1
        chunk: list[str] = []
        while i < len(lines) and not MARKER_RE.match(lines[i]):
            chunk.append(lines[i])
            i += 1
        blocks[block_id] = extract_block_text(kind, chunk)
    return blocks


def extract_block_text(kind: str, chunk: list[str]) -> str:
    lines = list(chunk)
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines:
        return ""

    if kind in {"section_heading", "heading"} and lines[0].startswith("#"):
        heading = re.sub(r"^#{2,3}\s*", "", lines[0]).strip()
        body = extract_code_fence(lines[1:])
        return f"{heading}\n{body}".strip() if body else heading
    return extract_code_fence(lines)


def extract_code_fence(lines: list[str]) -> str:
    content: list[str] = []
    inside = False
    for line in lines:
        if CODE_FENCE_RE.match(line):
            inside = True
            continue
        if inside and line.strip() == "```":
            break
        if inside:
            content.append(line)
    if inside:
        return "\n".join(content).strip()
    return "\n".join(lines).strip()


def rebuild_pdf(source_pdf: Path, translated_md: Path, blocks_json: Path, output_pdf: Path) -> None:
    doc = fitz.open(source_pdf)
    block_map = parse_translated_md(translated_md)
    manifest = json.loads(blocks_json.read_text(encoding="utf-8"))

    pages: dict[int, list[dict]] = {}
    for item in manifest:
        pages.setdefault(int(item["page"]), []).append(item)

    for page_number, page_items in pages.items():
        page = doc[page_number - 1]
        page.insert_font(fontname="F0", fontfile=FONT_UNICODE)
        page.insert_font(fontname="F1", fontfile=FONT_BOLD)
        for item in page_items:
            block_id = item["block_id"]
            translated = block_map.get(block_id)
            if not translated:
                continue
            rect = fitz.Rect(item["bbox"])
            style = item.get("style") or {}
            if "color" in style and isinstance(style["color"], list):
                style["color"] = tuple(style["color"])
            fit_and_insert(page, rect, [translated], style)

    output_pdf.parent.mkdir(parents=True, exist_ok=True)
    if output_pdf.exists():
        output_pdf.unlink()
    doc.save(output_pdf, garbage=4, deflate=True)
    doc.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-pdf", required=True)
    parser.add_argument("--translated-md", required=True)
    parser.add_argument("--blocks-json", required=True)
    parser.add_argument("--output-pdf", required=True)
    args = parser.parse_args()

    rebuild_pdf(
        Path(args.source_pdf),
        Path(args.translated_md),
        Path(args.blocks_json),
        Path(args.output_pdf),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
