from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


RENDER_EVENTS = {"block_rendered", "table_cell_rendered"}


def iter_jsonl(path: Path) -> list[dict]:
    events: list[dict] = []
    if not path.exists():
        return events
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


def latest_session(events: list[dict]) -> list[dict]:
    start = 0
    for index, event in enumerate(events):
        if event.get("event") == "file_start":
            start = index
    return events[start:]


def has_translatable_english(text: str) -> bool:
    return bool(re.search(r"[A-Za-z]{4,}", text))


def is_code_or_numeric(text: str) -> bool:
    cleaned = re.sub(r"\s+", " ", text).strip()
    if not cleaned:
        return True
    if re.fullmatch(r"[\d .,%<>()/\-:_°§]+", cleaned):
        return True
    patterns = (
        r"\bH\d{3}\b",
        r"\bP\d{3}\b",
        r"\bEUH\d+\b",
        r"\b\d{2,7}-\d{2}-\d\b",
        r"^[A-Z]{2,8}s?$",
        r"^\([A-Z]{2,8}s?\)$",
    )
    return any(re.search(pattern, cleaned) for pattern in patterns)


def should_preserve_identity(text: str) -> bool:
    cleaned = re.sub(r"\s+", " ", text).strip()
    lowered = cleaned.lower()
    if not cleaned:
        return True
    if "number(s)" in lowered:
        return True
    if re.search(r"[\w.+-]+@[\w.-]+\.\w+", cleaned):
        return True
    if re.search(r"https?://|www\.", cleaned, flags=re.IGNORECASE):
        return True
    if re.search(r"\b(?:Tel|Telephone|Phone|Emergency Tel|number\(s\))\b", cleaned, flags=re.IGNORECASE):
        return True
    if re.fullmatch(r"[A-Z](?:[A-Z0-9.-]*[A-Z0-9])(?:\.[A-Z0-9-]+)+", cleaned):
        return True
    if re.search(r"Wilhelmsen Ships Service|Drew Marine|CHEMWATCH", cleaned, flags=re.IGNORECASE):
        return True
    if re.search(r"\b(?:SARANEX|AlphaTec|DermaShield|MICROFLEX)\b", cleaned, flags=re.IGNORECASE):
        return True
    # PPE model names and product identifiers are intentionally preserved.
    if re.fullmatch(r"[A-Z][A-Za-z]+(?:[®™])?(?:\s+[A-Z][A-Za-z]+(?:[®™])?)*\s+[\w./#-]+", cleaned):
        return True
    return False


def cache_untranslated_count(cache_path: Path) -> int:
    if not cache_path.exists():
        return 0
    try:
        raw = json.loads(cache_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return 0
    count = 0
    for payload in raw.values():
        source = str(payload.get("source_text", "")).strip()
        final = str(payload.get("final_text", "")).strip()
        if final != source:
            continue
        if not has_translatable_english(source) or is_code_or_numeric(source):
            continue
        if should_preserve_identity(source):
            continue
        count += 1
    return count


def summarize(artifact_dir: Path, input_dir: Path | None = None) -> list[dict]:
    allowed_stems: set[str] | None = None
    if input_dir is not None:
        allowed_stems = {f"{path.stem}_ru" for path in input_dir.glob("*.pdf")}

    rows: list[dict] = []
    for log_path in sorted(artifact_dir.glob("*.translation-log.jsonl")):
        stem = log_path.name.replace(".translation-log.jsonl", "")
        if allowed_stems is not None and stem not in allowed_stems:
            continue
        events = latest_session(iter_jsonl(log_path))
        rendered = [e for e in events if e.get("event") in RENDER_EVENTS]
        failures = [e for e in rendered if not e.get("success")]
        chunk_fallback = [e for e in events if e.get("event") == "chunk_fallback"]
        translation_issues = [
            e
            for e in events
            if e.get("event") == "translation" and e.get("issues")
        ]
        cache_path = artifact_dir / log_path.name.replace(".translation-log.jsonl", ".translation-cache.json")
        rows.append(
            {
                "file": log_path.name.replace(".translation-log.jsonl", ""),
                "rendered": len(rendered),
                "render_failed": len(failures),
                "chunk_fallback": len(chunk_fallback),
                "translation_issues": len(translation_issues),
                "cache_identity": cache_untranslated_count(cache_path),
                "samples": [str(e.get("source_preview", ""))[:90] for e in failures[:3]],
            }
        )
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description="Report MSDS translation/render quality from logs and caches.")
    parser.add_argument(
        "--artifact-dir",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "PDF" / "_artifacts",
    )
    parser.add_argument("--input-dir", type=Path, help="Limit report to PDFs currently present in this input directory.")
    parser.add_argument("--only-problems", action="store_true", default=True)
    parser.add_argument("--show-samples", action="store_true")
    args = parser.parse_args()

    rows = summarize(args.artifact_dir, args.input_dir)
    if args.only_problems:
        rows = [
            row
            for row in rows
            if row["render_failed"] or row["chunk_fallback"] or row["translation_issues"] or row["cache_identity"]
        ]
    rows.sort(key=lambda row: (row["render_failed"], row["cache_identity"], row["translation_issues"]), reverse=True)

    print("file\trender_failed/rendered\tcache_identity\ttranslation_issues\tchunk_fallback")
    for row in rows:
        print(
            f"{row['file']}\t{row['render_failed']}/{row['rendered']}\t"
            f"{row['cache_identity']}\t{row['translation_issues']}\t{row['chunk_fallback']}"
        )
        if args.show_samples and row["samples"]:
            for sample in row["samples"]:
                print(f"  - {sample}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
