import argparse
import json
import os
import time
import subprocess
import sys
from pathlib import Path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-dir", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--artifact-dir", required=True)
    parser.add_argument("--source-lang", default="en")
    parser.add_argument("--target-lang", default="ru")
    parser.add_argument("--model", default="gpt-5.4-mini")
    parser.add_argument("--skip-existing", action="store_true")
    parser.add_argument("--until-done", action="store_true")
    parser.add_argument("--max-passes", type=int, default=10)
    parser.add_argument("--sleep-seconds", type=int, default=3)
    return parser


def run_pass(args: argparse.Namespace, input_dir: Path, output_dir: Path, artifact_dir: Path) -> dict:
    layout_script = Path(__file__).with_name("translate_pdf_layout.py")
    summary_path = artifact_dir / "msds_batch_summary.jsonl"
    pdfs = sorted(input_dir.glob("*.pdf"))
    translated_before = len(list(output_dir.glob("*_ru.pdf")))
    processed = 0
    succeeded = 0
    failed = 0

    for source in pdfs:
        output = output_dir / f"{source.stem}_ru.pdf"
        record = {
            "source": str(source),
            "output": str(output),
            "success": False,
        }

        if args.skip_existing and output.exists():
            record["success"] = True
            record["skipped"] = True
            with summary_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            continue

        cmd = [
            sys.executable,
            str(layout_script),
            str(source),
            str(output),
            "--artifact-dir",
            str(artifact_dir),
            "--model",
            args.model,
            "--source-lang",
            args.source_lang,
            "--target-lang",
            args.target_lang,
        ]

        completed = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            env=os.environ.copy(),
        )
        processed += 1
        record["success"] = completed.returncode == 0
        if record["success"]:
            succeeded += 1
        else:
            failed += 1
        if completed.stdout.strip():
            record["stdout"] = completed.stdout[-4000:]
        if completed.stderr.strip():
            record["stderr"] = completed.stderr[-4000:]
        with summary_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    translated_after = len(list(output_dir.glob("*_ru.pdf")))
    return {
        "processed": processed,
        "succeeded": succeeded,
        "failed": failed,
        "translated_before": translated_before,
        "translated_after": translated_after,
        "new_outputs": translated_after - translated_before,
        "total_inputs": len(pdfs),
    }


def main() -> int:
    args = build_parser().parse_args()
    input_dir = Path(args.input_dir)
    output_dir = Path(args.output_dir)
    artifact_dir = Path(args.artifact_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    artifact_dir.mkdir(parents=True, exist_ok=True)

    passes = args.max_passes if args.until_done else 1
    progress_path = artifact_dir / "msds_batch_progress.json"
    overall = []

    for pass_index in range(1, passes + 1):
        result = run_pass(args, input_dir, output_dir, artifact_dir)
        result["pass"] = pass_index
        overall.append(result)
        progress_path.write_text(json.dumps(overall, ensure_ascii=False, indent=2), encoding="utf-8")

        if not args.until_done:
            break
        if result["translated_after"] >= result["total_inputs"]:
            break
        if result["new_outputs"] <= 0:
            break
        time.sleep(max(0, args.sleep_seconds))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
