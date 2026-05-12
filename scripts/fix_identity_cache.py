"""
fix_identity_cache.py
---------------------
Находит и удаляет из кэшей записи, где перевод == оригинал (identity-переводы).
Такие записи появляются когда API упал и движок закэшировал непереведённый текст.

Запуск:
    python scripts/fix_identity_cache.py [--dry-run] [--file STEM]

    --dry-run   только показать что будет удалено, не трогать файлы
    --file      обработать конкретный файл (по имени без _ru.pdf)
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


def has_translatable_english(text: str) -> bool:
    """Текст содержит английские слова, которые нужно переводить."""
    return bool(re.search(r"[A-Za-z]{4,}", text))


def is_numeric_or_code(text: str) -> bool:
    cleaned = re.sub(r"\s+", " ", text).strip()
    if not cleaned:
        return True
    if re.fullmatch(r"[\d .,%<>()/\-:_]+", cleaned):
        return True
    code_patterns = (r"\bH\d{3}\b", r"\bP\d{3}\b", r"\bEUH\d+\b", r"\b\d{2,7}-\d{2}-\d\b")
    if any(re.search(p, cleaned) for p in code_patterns):
        return True
    return False


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
    if re.fullmatch(r"[A-Z](?:[A-Z0-9.-]*[A-Z0-9])(?:\.[A-Z0-9-]+)+", cleaned):
        return True
    if re.search(r"\b(?:Wilhelmsen Ships Service|Drew Marine|CHEMWATCH|SARANEX|AlphaTec|DermaShield|MICROFLEX)\b", cleaned, flags=re.IGNORECASE):
        return True
    return False


def clean_cache(cache_path: Path, dry_run: bool = False) -> dict:
    if not cache_path.exists():
        return {"file": str(cache_path), "status": "not_found"}

    raw = json.loads(cache_path.read_text(encoding="utf-8"))
    total = len(raw)
    bad_keys = []

    for key, payload in raw.items():
        src = str(payload.get("source_text", "")).strip()
        final = str(payload.get("final_text", "")).strip()
        issues = list(payload.get("issues", []))

        # Identity: перевод == оригинал
        if not final or final == src:
            if has_translatable_english(src) and not is_numeric_or_code(src) and not should_preserve_identity(src):
                bad_keys.append(key)
                continue

        # fallback-identity явно прописан
        fallback_issue = any(
            issue == "fallback-identity" or issue == "untranslated-text" or issue.startswith("chunk-fallback:")
            for issue in issues
        )
        if fallback_issue and has_translatable_english(src) and not should_preserve_identity(src):
            bad_keys.append(key)

    if not dry_run and bad_keys:
        for k in bad_keys:
            del raw[k]
        cache_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")

    return {
        "file": cache_path.name,
        "total_entries": total,
        "removed": len(bad_keys),
        "remaining": total - len(bad_keys),
        "dry_run": dry_run,
        "bad_keys_sample": bad_keys[:5],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Очистка identity-переводов из кэша")
    parser.add_argument("--dry-run", action="store_true", help="Только показать, не удалять")
    parser.add_argument("--file", help="Обработать конкретный кэш-файл (stem без _ru)")
    parser.add_argument(
        "--artifact-dir",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "PDF" / "_artifacts",
    )
    args = parser.parse_args()

    artifact_dir = args.artifact_dir
    if not artifact_dir.exists():
        print(f"Папка не найдена: {artifact_dir}")
        sys.exit(1)

    if args.file:
        pattern = f"*{args.file}*.translation-cache.json"
        cache_files = list(artifact_dir.glob(pattern))
    else:
        cache_files = sorted(artifact_dir.glob("*.translation-cache.json"))

    if not cache_files:
        print("Кэш-файлы не найдены.")
        sys.exit(0)

    mode = "[DRY RUN] " if args.dry_run else ""
    print(f"\n{mode}Обработка {len(cache_files)} кэш-файлов...\n")

    total_removed = 0
    for cf in cache_files:
        result = clean_cache(cf, dry_run=args.dry_run)
        removed = result["removed"]
        total_removed += removed
        if removed > 0:
            action = "удалено" if not args.dry_run else "будет удалено"
            print(f"  {result['file']}: {action} {removed} из {result['total_entries']} записей")
            for k in result["bad_keys_sample"]:
                src = k.split("::", 1)[-1][:70]
                print(f"    - {repr(src)}")

    print(f"\nИтого {mode}{'удалено' if not args.dry_run else 'будет удалено'}: {total_removed} записей")
    if args.dry_run:
        print("Запустите без --dry-run чтобы применить изменения.")


if __name__ == "__main__":
    main()
