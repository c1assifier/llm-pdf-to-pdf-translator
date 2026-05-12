#!/usr/bin/env python3
"""
Export MSDS translation cache → clean HTML document.

Usage:
    python scripts/export_translation_html.py \
        --artifact-dir PDF/_artifacts \
        --output-dir PDF/html \
        [--stem "ACC PLUS_ru"]   # optional: process one file
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# ── HTML template ─────────────────────────────────────────────────────────────
HTML_HEADER = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    font-family: Arial, Helvetica, sans-serif;
    font-size: 10pt;
    color: #111;
    background: #fff;
    max-width: 900px;
    margin: 0 auto;
    padding: 20px 30px 40px;
  }}
  h1 {{
    font-size: 16pt;
    font-weight: bold;
    color: #1a2e4a;
    border-bottom: 2px solid #1a2e4a;
    padding-bottom: 6px;
    margin: 20px 0 10px;
  }}
  h2 {{
    font-size: 12pt;
    font-weight: bold;
    color: #1a2e4a;
    background: #dde5f0;
    padding: 5px 8px;
    margin: 14px 0 6px;
    border-left: 4px solid #1a2e4a;
  }}
  h3 {{
    font-size: 10pt;
    font-weight: bold;
    color: #333;
    margin: 10px 0 4px;
  }}
  p {{
    margin: 4px 0 8px;
    line-height: 1.5;
    white-space: pre-wrap;
  }}
  .meta {{
    color: #666;
    font-size: 9pt;
    margin: 2px 0;
  }}
  .product-name {{
    font-size: 20pt;
    font-weight: bold;
    color: #1a2e4a;
    margin: 10px 0 4px;
  }}
  .section-header {{
    font-size: 11pt;
    font-weight: bold;
    background: #1a2e4a;
    color: #fff;
    padding: 6px 10px;
    margin: 18px 0 6px;
    border-radius: 3px;
  }}
  table {{
    width: 100%;
    border-collapse: collapse;
    margin: 8px 0 14px;
    font-size: 9pt;
  }}
  th {{
    background: #1a2e4a;
    color: #fff;
    padding: 5px 7px;
    text-align: left;
    font-weight: bold;
    border: 1px solid #4a6080;
  }}
  td {{
    padding: 4px 7px;
    border: 1px solid #c0c8d8;
    vertical-align: top;
  }}
  tr:nth-child(even) td {{
    background: #f2f5fa;
  }}
  .label-row td:first-child {{
    font-weight: bold;
    background: #e8edf5;
    width: 35%;
    color: #1a2e4a;
  }}
  .untranslated {{
    color: #555;
    font-style: italic;
  }}
  .page-break {{
    border-top: 1px dashed #ccc;
    margin: 15px 0;
    font-size: 8pt;
    color: #999;
    text-align: center;
    padding-top: 4px;
  }}
  .footer {{
    margin-top: 30px;
    padding-top: 10px;
    border-top: 1px solid #ccc;
    font-size: 8pt;
    color: #888;
  }}
</style>
</head>
<body>
"""

HTML_FOOTER = """
<div class="footer">Перевод сгенерирован автоматически. Исходный документ: {source_name}</div>
</body>
</html>
"""

# ── Section header detection ──────────────────────────────────────────────────
_SECTION_RE = re.compile(
    r"^(?:РАЗДЕЛ|SECTION|Раздел)\s+\d+", re.IGNORECASE
)
_SUBSECTION_RE = re.compile(
    r"^\d+\.\d+[\.\d]*\s+\S"
)


def detect_heading_level(text: str) -> int:
    """Return 1 (section), 2 (subsection), 3 (label) or 0 (regular)."""
    t = text.strip()
    if _SECTION_RE.match(t):
        return 1
    if _SUBSECTION_RE.match(t):
        return 2
    return 3


def html_escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
         .replace("<", "&lt;")
         .replace(">", "&gt;")
         .replace('"', "&quot;")
         .replace("\xa0", "&nbsp;")
    )


def is_numeric_or_code(text: str) -> bool:
    """Lines that are purely data / codes — rendered as-is."""
    t = text.strip()
    if re.match(r"^[\d\s\.\-\+\(\)/,°%<>≤≥~±×x·*_]+$", t):
        return True
    if re.match(r"^[A-Z0-9\-]{3,}\s*$", t):
        return True
    return False


def format_block(kind: str, source: str, final: str) -> str:
    """Return HTML fragment for one block."""
    # Use final if it's a meaningful translation, else source
    text = final.strip() if final.strip() and final.strip() != source.strip() else source.strip()
    escaped = html_escape(text)
    was_translated = (final.strip() and final.strip() != source.strip())

    if kind == "heading":
        level = detect_heading_level(text)
        if level == 1:
            return f'<div class="section-header">{escaped}</div>\n'
        elif level == 2:
            return f'<h2>{escaped}</h2>\n'
        else:
            return f'<h3>{escaped}</h3>\n'
    elif kind == "paragraph":
        css = "p" if was_translated else 'p class="untranslated"'
        return f"<{css}>{escaped}</{css.split()[0]}>\n"
    elif kind == "table_cell":
        # Table cells are accumulated separately — handled by caller
        return ""
    else:
        return f"<p>{escaped}</p>\n"


# ── Table builder ─────────────────────────────────────────────────────────────

def build_label_table(rows: list[tuple[str, str]]) -> str:
    """Two-column label→value table (Section 1 style)."""
    html = '<table>\n'
    for label, value in rows:
        html += f'<tr class="label-row"><td>{html_escape(label)}</td><td>{html_escape(value)}</td></tr>\n'
    html += '</table>\n'
    return html


# ── Main export logic ─────────────────────────────────────────────────────────

def export_one(cache_path: Path, log_path: Path, output_path: Path) -> bool:
    """Export single MSDS translation to HTML."""
    try:
        with open(cache_path, encoding="utf-8") as f:
            cache: dict = json.load(f)
    except Exception as e:
        print(f"  ✗ Cannot read cache: {e}", file=sys.stderr)
        return False

    # Read block order from log (latest run)
    ordered_keys: list[str] = []
    if log_path.exists():
        try:
            with open(log_path, encoding="utf-8") as f:
                lines = f.readlines()
            # Find latest run
            runs: list[list] = []
            current: list = []
            for line in lines:
                try:
                    e = json.loads(line)
                    if e.get("event") == "file_start":
                        if current:
                            runs.append(current)
                        current = [e]
                    else:
                        current.append(e)
                except Exception:
                    pass
            if current:
                runs.append(current)
            latest = runs[-1] if runs else []
            for e in latest:
                if e.get("event") in ("block_rendered", "table_cell_rendered"):
                    src_preview = e.get("source_preview", "")
                    # Match to cache key by source_text prefix
                    for key, entry in cache.items():
                        src = entry.get("source_text", "")
                        if src.startswith(src_preview[:30]) or src_preview.startswith(src[:30]):
                            if key not in ordered_keys:
                                ordered_keys.append(key)
                                break
        except Exception:
            pass

    # Fallback: use cache insertion order
    if not ordered_keys:
        ordered_keys = list(cache.keys())

    # Add any cache keys not in ordered list
    for k in cache:
        if k not in ordered_keys:
            ordered_keys.append(k)

    # ── Build HTML ────────────────────────────────────────────────────────────
    stem = cache_path.stem.replace(".translation-cache", "")
    product_name = stem.replace("_ru", "").replace("_", " ")

    html_parts = [HTML_HEADER.format(title=product_name)]
    html_parts.append(f'<div class="product-name">{html_escape(product_name)}</div>\n')
    html_parts.append(f'<p class="meta">Паспорт безопасности — перевод EN → RU</p>\n<hr>\n')

    prev_page = 1
    for key in ordered_keys:
        entry = cache.get(key)
        if not entry:
            continue

        kind = entry.get("kind", "paragraph")
        source = entry.get("source_text", "").strip()
        final = entry.get("final_text", "").strip()

        if not source:
            continue

        # Skip pure numeric / code blocks
        if is_numeric_or_code(source):
            continue

        fragment = format_block(kind, source, final)
        if fragment:
            html_parts.append(fragment)

    html_parts.append(HTML_FOOTER.format(source_name=cache_path.name))

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("".join(html_parts), encoding="utf-8")
    size_kb = output_path.stat().st_size // 1024
    print(f"  ✓ {output_path.name} ({size_kb}KB)")
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="Export MSDS translation cache to HTML")
    parser.add_argument("--artifact-dir", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--stem", default=None, help="Process only this stem (e.g. 'ACC PLUS_ru')")
    args = parser.parse_args()

    artifact_dir: Path = args.artifact_dir
    output_dir: Path = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    caches = sorted(artifact_dir.glob("*.translation-cache.json"))
    if args.stem:
        caches = [c for c in caches if args.stem in c.name]

    if not caches:
        print("No cache files found.", file=sys.stderr)
        sys.exit(1)

    print(f"Generating HTML for {len(caches)} file(s)...")
    success = 0
    for cache_path in caches:
        stem = cache_path.name.replace(".translation-cache.json", "")
        log_path = artifact_dir / f"{stem}.translation-log.jsonl"
        out_name = stem.replace("_ru", "") + "_ru.html"
        output_path = output_dir / out_name

        if export_one(cache_path, log_path, output_path):
            success += 1

    print(f"\n✓ Готово: {success}/{len(caches)} файлов → {output_dir}/")


if __name__ == "__main__":
    main()
