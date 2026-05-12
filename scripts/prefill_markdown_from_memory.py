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

from msds_translation_engine import MsdsTranslationEngine, TranslationUnit
from translate_markdown_blocks import (
    parse_md_blocks,
    extract_block_text,
    write_rendered_markdown,
    classify_translation_unit,
)


def prefill_markdown(source_md: Path, output_md: Path, engine: MsdsTranslationEngine) -> dict:
    original = source_md.read_text(encoding="utf-8")
    prefix, blocks = parse_md_blocks(original)
    artifacts = {}
    total = 0
    resolved = 0
    unresolved = []

    for block in blocks:
        text = extract_block_text(block["chunk"])
        block["source_text"] = text
        if not text.strip():
            continue

        total += 1
        unit = TranslationUnit(
            unit_id=block["block_id"],
            text=text,
            kind=classify_translation_unit(block["kind"], text),
        )
        artifact = engine.resolve_local(unit)
        if artifact is not None:
            artifacts[block["block_id"]] = artifact
            resolved += 1
        else:
            unresolved.append(
                {
                    "block_id": block["block_id"],
                    "kind": block["kind"],
                    "text": text,
                }
            )

    write_rendered_markdown(prefix, blocks, artifacts, output_md)
    return {
        "file": source_md.name,
        "total_blocks": total,
        "resolved_blocks": resolved,
        "unresolved_blocks": total - resolved,
        "coverage_ratio": round((resolved / total), 4) if total else 1.0,
        "unresolved": unresolved,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-dir", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--cache-path", required=True)
    parser.add_argument("--report-path", required=True)
    args = parser.parse_args()

    input_dir = Path(args.input_dir)
    output_dir = Path(args.output_dir)
    cache_path = Path(args.cache_path)
    report_path = Path(args.report_path)

    output_dir.mkdir(parents=True, exist_ok=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)

    engine = MsdsTranslationEngine(cache_path=cache_path, log_path=None)
    results = []

    sources = sorted(
        p for p in input_dir.glob("*.md")
        if p.name not in {"INDEX.md", "COVERAGE.md", "WORKFLOW.md"}
    )

    for source_md in sources:
        output_md = output_dir / source_md.name
        results.append(prefill_markdown(source_md, output_md, engine))

    report = {
        "files": results,
        "total_files": len(results),
        "resolved_blocks": sum(item["resolved_blocks"] for item in results),
        "total_blocks": sum(item["total_blocks"] for item in results),
    }
    report["coverage_ratio"] = round(report["resolved_blocks"] / report["total_blocks"], 4) if report["total_blocks"] else 1.0
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
