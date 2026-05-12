import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from msds_translation_engine import SOURCE_GLOSSARY


MARKER_RE = re.compile(r"^<!--\s*(p\d{3}_b\d{3})\s*\|\s*([a-z_]+)\s*-->\s*$")
CODE_FENCE_RE = re.compile(r"^```text\s*$")


def parse_md_blocks(md_text: str) -> list[dict]:
    lines = md_text.splitlines()
    blocks: list[dict] = []
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
        blocks.append({"block_id": block_id, "kind": kind, "chunk": chunk})
    return blocks


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


def extract_block_text(chunk: list[str]) -> str:
    lines = list(chunk)
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines:
        return ""

    if lines[0].startswith("## "):
        heading = lines[0][3:].strip()
        body = extract_code_fence(lines[1:])
        return f"{heading}\n{body}".strip() if body else heading
    if lines[0].startswith("### "):
        heading = lines[0][4:].strip()
        body = extract_code_fence(lines[1:])
        return f"{heading}\n{body}".strip() if body else heading
    return extract_code_fence(lines)


def normalize_text(text: str) -> str:
    text = text.replace("\u00a0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def is_numeric_or_code(text: str) -> bool:
    cleaned = normalize_text(text)
    if not cleaned:
        return True
    if re.fullmatch(r"[\d .,%<>()/\-:]+", cleaned):
        return True
    if re.search(r"\bH\d{3}\b|\bP\d{3}\b|\bEUH\d+\b", cleaned):
        return True
    if re.search(r"\b\d{2,7}-\d{2}-\d\b", cleaned):
        return True
    return False


def short_files(files: set[str], limit: int = 6) -> str:
    names = sorted(files)
    if len(names) <= limit:
        return ", ".join(names)
    return ", ".join(names[:limit]) + f" +{len(names) - limit}"


def build_line_memory(cache_dir: Path) -> dict[str, str]:
    memory: dict[str, str] = {normalize_text(k): v for k, v in SOURCE_GLOSSARY.items()}

    for cache_file in sorted(cache_dir.glob("*.json")):
        if cache_file.name.startswith("_"):
            continue
        try:
            payload = json.loads(cache_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue

        for artifact in payload.values():
            source_text = normalize_text(str(artifact.get("source_text", "")))
            final_text = normalize_text(str(artifact.get("final_text", "")))
            if not source_text or not final_text:
                continue

            src_lines = [line.strip() for line in source_text.splitlines() if line.strip()]
            dst_lines = [line.strip() for line in final_text.splitlines() if line.strip()]
            if len(src_lines) != len(dst_lines):
                continue

            for src, dst in zip(src_lines, dst_lines):
                if len(src) > 160 or len(dst) > 220:
                    continue
                if is_numeric_or_code(src) and src == dst:
                    continue
                memory[normalize_text(src)] = dst

    return memory


def render_table(rows: list[dict], title: str, intro: list[str]) -> list[str]:
    lines = [f"# {title}", ""]
    lines.extend(intro)
    lines.extend(["", "| EN | RU | Count | Files |", "|---|---|---:|---|"])
    for row in rows:
        en = row["en"].replace("\n", "<br>")
        ru = row["ru"].replace("\n", "<br>")
        files = row["files"].replace("\n", " ")
        lines.append(f"| {en} | {ru} | {row['count']} | {files} |")
    lines.append("")
    return lines


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-dir", required=True)
    parser.add_argument("--cache-dir", required=True)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()

    input_dir = Path(args.input_dir)
    cache_dir = Path(args.cache_dir)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    line_memory = build_line_memory(cache_dir)

    line_counts: Counter[str] = Counter()
    line_files: dict[str, set[str]] = defaultdict(set)
    block_counts: Counter[str] = Counter()
    block_files: dict[str, set[str]] = defaultdict(set)

    md_files = sorted(
        p for p in input_dir.glob("*.md")
        if p.name not in {"INDEX.md", "COVERAGE.md", "WORKFLOW.md"}
    )

    for md_file in md_files:
        blocks = parse_md_blocks(md_file.read_text(encoding="utf-8"))
        for block in blocks:
            text = normalize_text(extract_block_text(block["chunk"]))
            if not text:
                continue

            lines = [normalize_text(line) for line in text.splitlines() if normalize_text(line)]
            for line in lines:
                if is_numeric_or_code(line):
                    continue
                if len(line) > 160:
                    continue
                line_counts[line] += 1
                line_files[line].add(md_file.name)

            if len(text) <= 420 and text.count("\n") <= 10:
                block_counts[text] += 1
                block_files[text].add(md_file.name)

    repeated_lines = []
    for line, count in line_counts.items():
        if count < 2:
            continue
        repeated_lines.append(
            {
                "en": line,
                "ru": line_memory.get(normalize_text(line), ""),
                "count": count,
                "files": short_files(line_files[line]),
            }
        )

    repeated_lines.sort(key=lambda item: (-item["count"], item["en"]))

    repeated_blocks = []
    for block, count in block_counts.items():
        if count < 2:
            continue
        if len(block.splitlines()) < 2:
            continue
        repeated_blocks.append(
            {
                "en": block,
                "ru": line_memory.get(normalize_text(block), ""),
                "count": count,
                "files": short_files(block_files[block]),
            }
        )

    repeated_blocks.sort(key=lambda item: (-item["count"], item["en"]))

    repeated_lines_path = output_dir / "MSDS_REPEATED_LINES.md"
    repeated_blocks_path = output_dir / "MSDS_REPEATED_SHORT_BLOCKS.md"
    summary_path = output_dir / "README.md"

    repeated_lines_path.write_text(
        "\n".join(
            render_table(
                repeated_lines,
                "MSDS Repeated Lines",
                [
                    f"- Source dir: `{input_dir}`",
                    f"- Source files scanned: `{len(md_files)}`",
                    "- `RU` is prefilled when translation was already learned from glossary/cache; otherwise it is left blank.",
                    "- This is the best place to bulk-approve repeated phrases before rebuilding PDFs.",
                ],
            )
        ),
        encoding="utf-8",
    )

    repeated_blocks_path.write_text(
        "\n".join(
            render_table(
                repeated_blocks,
                "MSDS Repeated Short Blocks",
                [
                    f"- Source dir: `{input_dir}`",
                    f"- Source files scanned: `{len(md_files)}`",
                    "- These are short repeated multi-line blocks that appear in multiple files.",
                    "- Filling these once gives much better reuse than translating word-by-word.",
                ],
            )
        ),
        encoding="utf-8",
    )

    summary_path.write_text(
        "\n".join(
            [
                "# MSDS Phrasebook Export",
                "",
                f"- Markdown source dir: `{input_dir}`",
                f"- Cache dir: `{cache_dir}`",
                f"- Files scanned: `{len(md_files)}`",
                f"- Repeated lines: `{len(repeated_lines)}`",
                f"- Repeated short blocks: `{len(repeated_blocks)}`",
                "",
                "## Files",
                "",
                f"- [MSDS_REPEATED_LINES.md]({repeated_lines_path.name})",
                f"- [MSDS_REPEATED_SHORT_BLOCKS.md]({repeated_blocks_path.name})",
            ]
        ),
        encoding="utf-8",
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
