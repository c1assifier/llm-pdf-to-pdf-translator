import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.rebuild_pdf_from_translated_md import rebuild_pdf


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-pdf-dir", required=True)
    parser.add_argument("--translated-md-dir", required=True)
    parser.add_argument("--blocks-dir", required=True)
    parser.add_argument("--output-pdf-dir", required=True)
    parser.add_argument("--skip-existing", action="store_true")
    args = parser.parse_args()

    source_pdf_dir = Path(args.source_pdf_dir)
    translated_md_dir = Path(args.translated_md_dir)
    blocks_dir = Path(args.blocks_dir)
    output_pdf_dir = Path(args.output_pdf_dir)
    output_pdf_dir.mkdir(parents=True, exist_ok=True)

    for translated_md in sorted(translated_md_dir.glob("*.md")):
        if translated_md.name in {"INDEX.md", "COVERAGE.md", "WORKFLOW.md"}:
            continue
        stem = translated_md.stem
        source_pdf = source_pdf_dir / f"{stem}.pdf"
        blocks_json = blocks_dir / f"{stem}.blocks.json"
        output_pdf = output_pdf_dir / f"{stem}_ru.pdf"
        if args.skip_existing and output_pdf.exists():
            continue
        if not source_pdf.exists() or not blocks_json.exists():
            continue
        rebuild_pdf(source_pdf, translated_md, blocks_json, output_pdf)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
