import argparse
import json
import re
from pathlib import Path

import fitz


def clean_text(text: str) -> str:
    text = re.sub(r"<\\[A-Za-zА-Яа-я]+>", "", text)
    text = re.sub(r"<\\[^>]*>", "", text)
    text = text.replace("\u00a0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def block_text(block: dict) -> str:
    lines = []
    for line in block.get("lines", []):
        text = "".join(span.get("text", "") for span in line.get("spans", []))
        text = clean_text(text)
        if text:
            lines.append(text)
    return "\n".join(lines).strip()


def color_to_rgb(value: int) -> tuple[float, float, float]:
    return (
        ((value >> 16) & 255) / 255.0,
        ((value >> 8) & 255) / 255.0,
        (value & 255) / 255.0,
    )


def is_bold_font(font_name: str) -> bool:
    return "bold" in str(font_name).lower()


def block_style(block: dict, kind: str) -> dict:
    spans = [
        span
        for line in block.get("lines", [])
        for span in line.get("spans", [])
        if span.get("text", "").strip()
    ]
    if not spans:
        return {
            "fontsize": 10.0,
            "color": [0.0, 0.0, 0.0],
            "fontname": "F0",
            "min_font_size": 4.0,
            "lineheights": [1.0, 0.95, 0.9],
        }
    first = spans[0]
    size = max(float(span.get("size", 10.0)) for span in spans)
    total_chars = sum(len(span.get("text", "").strip()) for span in spans) or 1
    bold_chars = sum(
        len(span.get("text", "").strip())
        for span in spans
        if is_bold_font(str(span.get("font", "")))
    )
    bold_ratio = bold_chars / total_chars
    use_bold = bold_ratio >= 0.25 if kind in {"heading", "section_heading"} else bold_ratio >= 0.7
    return {
        "fontsize": size,
        "color": list(color_to_rgb(int(first.get("color", 0)))),
        "fontname": "F1" if use_bold else "F0",
        "min_font_size": 4.0,
        "lineheights": [1.0, 0.95, 0.9],
    }


def looks_like_section_heading(text: str) -> bool:
    return bool(re.match(r"^Section\s+\d+\s*[-–]", text, flags=re.IGNORECASE))


def uppercase_ratio(text: str) -> float:
    letters = [ch for ch in text if ch.isalpha()]
    if not letters:
        return 0.0
    uppers = [ch for ch in letters if ch.isupper()]
    return len(uppers) / len(letters)


def looks_like_heading(text: str) -> bool:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return False
    if looks_like_section_heading(lines[0]):
        return True
    if len(lines) <= 2 and len(text) <= 140 and uppercase_ratio(text) >= 0.55:
        return True
    first = lines[0]
    if len(lines) > 1 and len(first) <= 60 and uppercase_ratio(first) >= 0.7:
        return True
    return False


def block_kind(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return "empty"
    if looks_like_section_heading(lines[0]):
        return "section_heading"
    if looks_like_heading(text):
        return "heading"
    return "text_block"


def render_block(block_id: str, text: str) -> str:
    lines = [line.rstrip() for line in text.splitlines() if line.strip()]
    if not lines:
        return ""

    kind = block_kind(text)
    prefix = f"<!-- {block_id} | {kind} -->\n"

    if looks_like_section_heading(lines[0]):
        section = lines[0]
        rest = "\n".join(lines[1:]).strip()
        if rest:
            return f"{prefix}## {section}\n\n```text\n{rest}\n```"
        return f"{prefix}## {section}"

    if looks_like_heading(text):
        if len(lines) > 1:
            heading = lines[0]
            body = "\n".join(lines[1:]).strip()
            if body:
                return f"{prefix}### {heading}\n\n```text\n{body}\n```"
            return f"{prefix}### {heading}"
        return f"{prefix}### {lines[0]}"

    return f"{prefix}```text\n{text}\n```"


def export_pdf_to_md(source_pdf: Path, output_md: Path) -> None:
    doc = fitz.open(source_pdf)
    manifest_path = output_md.with_suffix(".blocks.json")
    parts = [
        f"# {source_pdf.name}",
        "",
        f"- Source PDF: `{source_pdf}`",
        f"- Pages: `{doc.page_count}`",
        "- Export format: block-preserving markdown with stable block IDs",
        "",
    ]
    manifest: list[dict] = []
    total_blocks = 0

    for page_number, page in enumerate(doc, start=1):
        parts.append(f"## Page {page_number}")
        parts.append("")
        blocks = [block for block in page.get_text("dict").get("blocks", []) if block.get("type") == 0]
        for block_index, block in enumerate(blocks, start=1):
            text = block_text(block)
            if not text:
                continue
            block_id = f"p{page_number:03d}_b{block_index:03d}"
            total_blocks += 1
            rendered = render_block(block_id, text)
            if rendered:
                parts.append(rendered)
                parts.append("")
            kind = block_kind(text)
            manifest.append(
                {
                    "block_id": block_id,
                    "page": page_number,
                    "block_index": block_index,
                    "bbox": block.get("bbox"),
                    "kind": kind,
                    "style": block_style(block, kind),
                    "text": text,
                }
            )

    output_md.write_text("\n".join(parts).rstrip() + "\n", encoding="utf-8")
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"pages": doc.page_count, "blocks": total_blocks, "manifest_path": str(manifest_path)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-dir", required=True)
    parser.add_argument("--translated-dir", required=True)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()

    source_dir = Path(args.source_dir)
    translated_dir = Path(args.translated_dir)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    translated = {p.name.replace("_ru.pdf", ".pdf") for p in translated_dir.glob("*_ru.pdf")}
    remaining = sorted([p for p in source_dir.glob("*.pdf") if p.name not in translated])

    index_lines = [
        "# Remaining PDF Markdown Export",
        "",
        f"- Source dir: `{source_dir}`",
        f"- Output dir: `{output_dir}`",
        f"- Remaining PDFs exported: `{len(remaining)}`",
        "- Each file has stable block IDs in markdown and a `.blocks.json` sidecar manifest",
        "",
        "## Files",
        "",
    ]
    coverage_rows = [
        "# Markdown Export Coverage",
        "",
        "| File | Pages | Blocks | Markdown | Manifest |",
        "|---|---:|---:|---|---|",
    ]

    for pdf in remaining:
        md_name = f"{pdf.stem}.md"
        output_md = output_dir / md_name
        info = export_pdf_to_md(pdf, output_md)
        index_lines.append(f"- [{md_name}]({md_name})")
        coverage_rows.append(
            f"| {pdf.name} | {info['pages']} | {info['blocks']} | `{md_name}` | `{Path(info['manifest_path']).name}` |"
        )

    (output_dir / "INDEX.md").write_text("\n".join(index_lines).rstrip() + "\n", encoding="utf-8")
    (output_dir / "COVERAGE.md").write_text("\n".join(coverage_rows).rstrip() + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
