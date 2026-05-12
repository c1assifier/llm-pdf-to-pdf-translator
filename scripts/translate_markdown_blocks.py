import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from msds_translation_engine import BlockKind, MsdsTranslationEngine, TranslationUnit


MARKER_RE = re.compile(r"^<!--\s*(p\d{3}_b\d{3})\s*\|\s*([a-z_]+)\s*-->\s*$")
CODE_FENCE_RE = re.compile(r"^```text\s*$")


def parse_md_blocks(md_text: str) -> tuple[list[str], list[dict]]:
    lines = md_text.splitlines()
    prefix: list[str] = []
    blocks: list[dict] = []
    i = 0
    while i < len(lines):
        match = MARKER_RE.match(lines[i])
        if not match:
            prefix.append(lines[i])
            i += 1
            continue

        block_id, kind = match.group(1), match.group(2)
        i += 1
        chunk: list[str] = []
        while i < len(lines) and not MARKER_RE.match(lines[i]):
            chunk.append(lines[i])
            i += 1
        blocks.append({"block_id": block_id, "kind": kind, "chunk": chunk})
    return prefix, blocks


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


def render_translated_chunk(kind: str, translated_text: str) -> list[str]:
    translated_text = translated_text.strip()
    if not translated_text:
        return [""]

    lines = translated_text.splitlines()
    if kind == "section_heading":
        heading = lines[0]
        body = lines[1:]
        out = [f"## {heading}"]
        if body:
            out.extend(["", "```text", *body, "```"])
        return out

    if kind == "heading":
        heading = lines[0]
        body = lines[1:]
        out = [f"### {heading}"]
        if body:
            out.extend(["", "```text", *body, "```"])
        return out

    return ["```text", *lines, "```"]


def write_rendered_markdown(prefix: list[str], blocks: list[dict], artifacts: dict, output_md: Path) -> None:
    rendered: list[str] = list(prefix)
    if rendered and rendered[-1] != "":
        rendered.append("")

    for block in blocks:
        rendered.append(f"<!-- {block['block_id']} | {block['kind']} -->")
        translated = artifacts.get(block["block_id"])
        text = translated.final_text if translated else block["source_text"]
        rendered.extend(render_translated_chunk(block["kind"], text))
        rendered.append("")

    output_md.parent.mkdir(parents=True, exist_ok=True)
    output_md.write_text("\n".join(rendered).rstrip() + "\n", encoding="utf-8")


def to_block_kind(kind: str) -> BlockKind:
    mapping = {
        "section_heading": BlockKind.HEADING,
        "heading": BlockKind.HEADING,
        "text_block": BlockKind.PARAGRAPH,
    }
    return mapping.get(kind, BlockKind.PARAGRAPH)


def classify_translation_unit(block_kind_name: str, text: str) -> BlockKind:
    unit_kind = to_block_kind(block_kind_name)
    if unit_kind != BlockKind.HEADING:
        return unit_kind

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return unit_kind

    body = lines[1:]
    body_text = "\n".join(body)
    bulletish = any(
        line.startswith(("•", "-", "*", "■", "IF ", "For ", "Use ", "Wear ", "Store ", "Keep "))
        for line in body
    )
    if len(lines) >= 3 and (len(body_text) >= 80 or bulletish):
        return BlockKind.PARAGRAPH
    return unit_kind


def iter_chunks(items: list[TranslationUnit], chunk_size: int) -> list[list[TranslationUnit]]:
    return [items[i : i + chunk_size] for i in range(0, len(items), chunk_size)]


def load_json_dict(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def build_shared_cache(artifact_dir: Path, shared_cache_path: Path) -> Path:
    if shared_cache_path.exists():
        return shared_cache_path

    merged: dict = {}
    for cache_file in sorted(artifact_dir.glob("*.md-translation-cache.json")):
        merged.update(load_json_dict(cache_file))

    shared_cache_path.write_text(
        json.dumps(merged, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return shared_cache_path


def translate_markdown(source_md: Path, output_md: Path, engine: MsdsTranslationEngine, chunk_size: int) -> None:
    original = source_md.read_text(encoding="utf-8")
    prefix, blocks = parse_md_blocks(original)
    units: list[TranslationUnit] = []

    for block in blocks:
        text = extract_block_text(block["chunk"])
        block["source_text"] = text
        if text.strip():
            units.append(
                TranslationUnit(
                    unit_id=block["block_id"],
                    text=text,
                    kind=classify_translation_unit(block["kind"], text),
                )
            )

    partial_md = output_md.with_suffix(".partial.md")
    if partial_md.exists():
        partial_md.unlink()

    artifacts = {}
    for chunk in iter_chunks(units, chunk_size):
        result = engine.translate_many(chunk)
        artifacts.update(result)
        write_rendered_markdown(prefix, blocks, artifacts, partial_md)

    write_rendered_markdown(prefix, blocks, artifacts, partial_md)
    if output_md.exists():
        output_md.unlink()
    partial_md.rename(output_md)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-dir", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--artifact-dir", required=True)
    parser.add_argument("--model", default="gpt-5.4-mini")
    parser.add_argument("--source-lang", default="en")
    parser.add_argument("--target-lang", default="ru")
    parser.add_argument("--chunk-size", type=int, default=8)
    parser.add_argument("--skip-existing", action="store_true")
    parser.add_argument("--file")
    args = parser.parse_args()

    input_dir = Path(args.input_dir)
    output_dir = Path(args.output_dir)
    artifact_dir = Path(args.artifact_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    shared_cache_path = build_shared_cache(artifact_dir, artifact_dir / "_shared_translation_cache.json")

    sources = [input_dir / args.file] if args.file else sorted(input_dir.glob("*.md"))
    sources = [p for p in sources if p.name not in {"INDEX.md", "COVERAGE.md", "WORKFLOW.md"} and not p.name.endswith(".blocks.json")]

    for source in sources:
        output = output_dir / source.name
        if args.skip_existing and output.exists():
            continue
        stem = source.stem
        engine = MsdsTranslationEngine(
            model=args.model,
            source_lang=args.source_lang,
            target_lang=args.target_lang,
            cache_path=shared_cache_path,
            log_path=artifact_dir / f"{stem}.md-translation-log.jsonl",
        )
        translate_markdown(source, output, engine, args.chunk_size)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
