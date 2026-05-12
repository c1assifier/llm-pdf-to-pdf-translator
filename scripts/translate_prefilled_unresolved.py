import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from msds_translation_engine import MsdsTranslationEngine, TranslationArtifact, TranslationUnit
from translate_markdown_blocks import (
    classify_translation_unit,
    extract_block_text,
    parse_md_blocks,
    write_rendered_markdown,
)


def iter_chunks(items: list[TranslationUnit], chunk_size: int) -> list[list[TranslationUnit]]:
    return [items[i : i + chunk_size] for i in range(0, len(items), chunk_size)]


def load_unresolved_map(report_path: Path) -> dict[str, set[str]]:
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    unresolved_map: dict[str, set[str]] = {}
    for file_info in payload.get("files", []):
        unresolved_map[file_info["file"]] = {item["block_id"] for item in file_info.get("unresolved", [])}
    return unresolved_map


def build_prefilled_artifact(source_text: str, final_text: str, kind: str) -> TranslationArtifact:
    return TranslationArtifact(
        source_text=source_text,
        translated_text=final_text,
        normalized_text=final_text,
        final_text=final_text,
        kind=kind,
        backend="prefilled",
        from_cache=True,
    )


def translate_unresolved_file(
    source_md: Path,
    prefilled_md: Path,
    output_md: Path,
    unresolved_ids: set[str],
    engine: MsdsTranslationEngine,
    chunk_size: int,
) -> dict:
    prefix, source_blocks = parse_md_blocks(source_md.read_text(encoding="utf-8"))
    _, prefilled_blocks = parse_md_blocks(prefilled_md.read_text(encoding="utf-8"))
    prefilled_text_by_id = {
        block["block_id"]: extract_block_text(block["chunk"])
        for block in prefilled_blocks
    }

    artifacts: dict[str, TranslationArtifact] = {}
    units: list[TranslationUnit] = []

    for block in source_blocks:
        source_text = extract_block_text(block["chunk"])
        block["source_text"] = source_text
        if not source_text.strip():
            continue

        block_kind = classify_translation_unit(block["kind"], source_text)
        if block["block_id"] not in unresolved_ids:
            final_text = prefilled_text_by_id.get(block["block_id"], source_text)
            artifacts[block["block_id"]] = build_prefilled_artifact(
                source_text,
                final_text,
                block_kind.value,
            )
            continue

        local_hit = engine.resolve_local(
            TranslationUnit(
                unit_id=block["block_id"],
                text=source_text,
                kind=block_kind,
            )
        )
        if local_hit is not None:
            artifacts[block["block_id"]] = local_hit
            continue

        units.append(
            TranslationUnit(
                unit_id=block["block_id"],
                text=source_text,
                kind=block_kind,
            )
        )

    partial_md = output_md.with_suffix(".partial.md")
    if partial_md.exists():
        partial_md.unlink()

    translated_count = 0
    for chunk in iter_chunks(units, chunk_size):
        result = engine.translate_many(chunk)
        artifacts.update(result)
        translated_count += len(chunk)
        write_rendered_markdown(prefix, source_blocks, artifacts, partial_md)

    write_rendered_markdown(prefix, source_blocks, artifacts, partial_md)
    if output_md.exists():
        output_md.unlink()
    partial_md.rename(output_md)

    return {
        "file": source_md.name,
        "total_blocks": len([b for b in source_blocks if b.get("source_text", "").strip()]),
        "prefilled_blocks": len(artifacts) - translated_count,
        "translated_blocks": translated_count,
        "unresolved_requested": len(units),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-dir", required=True)
    parser.add_argument("--prefilled-dir", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--artifact-dir", required=True)
    parser.add_argument("--report-path", required=True)
    parser.add_argument("--cache-path", required=True)
    parser.add_argument("--model", default="gpt-5.4-mini")
    parser.add_argument("--source-lang", default="en")
    parser.add_argument("--target-lang", default="ru")
    parser.add_argument("--chunk-size", type=int, default=8)
    parser.add_argument("--skip-existing", action="store_true")
    parser.add_argument("--file")
    args = parser.parse_args()

    source_dir = Path(args.source_dir)
    prefilled_dir = Path(args.prefilled_dir)
    output_dir = Path(args.output_dir)
    artifact_dir = Path(args.artifact_dir)
    report_path = Path(args.report_path)
    cache_path = Path(args.cache_path)

    output_dir.mkdir(parents=True, exist_ok=True)
    artifact_dir.mkdir(parents=True, exist_ok=True)

    unresolved_map = load_unresolved_map(report_path)
    sources = [source_dir / args.file] if args.file else sorted(
        p for p in source_dir.glob("*.md")
        if p.name not in {"INDEX.md", "COVERAGE.md", "WORKFLOW.md"}
    )

    summary = []
    for source_md in sources:
        output_md = output_dir / source_md.name
        if args.skip_existing and output_md.exists():
            continue

        prefilled_md = prefilled_dir / source_md.name
        if not prefilled_md.exists():
            continue

        stem = source_md.stem
        engine = MsdsTranslationEngine(
            model=args.model,
            source_lang=args.source_lang,
            target_lang=args.target_lang,
            cache_path=cache_path,
            log_path=artifact_dir / f"{stem}.unresolved-log.jsonl",
        )
        summary.append(
            translate_unresolved_file(
                source_md,
                prefilled_md,
                output_md,
                unresolved_map.get(source_md.name, set()),
                engine,
                args.chunk_size,
            )
        )

    (artifact_dir / "unresolved_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
