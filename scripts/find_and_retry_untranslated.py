"""
find_and_retry_untranslated.py
------------------------------
Сканирует все кэши переводов и находит записи где:
  • final_text == source_text (идентичный перевод) И
  • source содержит переводимый английский текст

Для каждой такой записи: удаляет из кэша и отправляет на повторный перевод через API.
Результат: обновлённые кэши с реальными переводами.

Использование:
    python scripts/find_and_retry_untranslated.py [--dry-run] [--model gpt-5.4-mini]
"""
from __future__ import annotations
import argparse
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from msds_translation_engine import BlockKind, MsdsTranslationEngine, TranslationUnit


def has_translatable_english(text: str) -> bool:
    return bool(re.search(r"[A-Za-z]{4,}", text))


def is_numeric_or_code(text: str) -> bool:
    cleaned = re.sub(r"\s+", " ", text).strip()
    if not cleaned:
        return True
    if re.fullmatch(r"[\d .,%<>()/\-:_°]+", cleaned):
        return True
    # Regulatory codes and abbreviations that don't need translation
    code_patterns = (
        r"\bH\d{3}\b", r"\bP\d{3}\b", r"\bEUH\d+\b",
        r"\b\d{2,7}-\d{2}-\d\b",   # CAS numbers
        r"^[A-Z]{2,6}s?$",          # pure abbreviations like RELs, WEELs
        r"^\([A-Z]{2,8}s?\)$",      # (RELs), (WEELs)
        r"^[A-Z]{2,8},\s*[A-Z]{2,8}",  # HAZMAP, IARC etc.
    )
    return any(re.search(p, cleaned) for p in code_patterns)


def scan_for_untranslated(artifact_dir: Path) -> dict[Path, list[str]]:
    """Return {cache_path: [key, ...]} for entries that are identity translations."""
    result: dict[Path, list[str]] = {}
    for cache_path in sorted(artifact_dir.glob("*.translation-cache.json")):
        try:
            raw = json.loads(cache_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        bad_keys = []
        for key, payload in raw.items():
            src = str(payload.get("source_text", "")).strip()
            final = str(payload.get("final_text", "")).strip()
            if final != src:
                continue
            if not has_translatable_english(src):
                continue
            if is_numeric_or_code(src):
                continue
            # Skip very short abbreviations
            words = src.split()
            if len(words) == 1 and len(src) <= 6:
                continue
            bad_keys.append(key)
        if bad_keys:
            result[cache_path] = bad_keys
    return result


def retry_translations(
    artifact_dir: Path,
    model: str,
    dry_run: bool = False,
) -> None:
    untranslated = scan_for_untranslated(artifact_dir)
    if not untranslated:
        print("✓ Нет непереведённых записей в кэшах.")
        return

    total_blocks = sum(len(v) for v in untranslated.values())
    mode_label = "[DRY RUN] " if dry_run else ""
    print(f"\n{mode_label}Найдено {total_blocks} непереведённых записей в {len(untranslated)} файлах\n")

    for cache_path, keys in untranslated.items():
        stem = cache_path.name.replace(".translation-cache.json", "")
        log_path = artifact_dir / f"{stem}.translation-log.jsonl"
        print(f"  {stem}: {len(keys)} записей")
        for k in keys[:3]:
            print(f"    - {k.split('::', 1)[-1][:70]!r}")
        if dry_run:
            continue

        # Delete identity entries so engine will re-translate
        raw = json.loads(cache_path.read_text(encoding="utf-8"))
        for k in keys:
            raw.pop(k, None)
        cache_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")

        # Re-translate deleted entries via engine
        engine = MsdsTranslationEngine(
            model=model,
            source_lang="en",
            target_lang="ru",
            cache_path=cache_path,
            log_path=log_path,
        )
        units = [
            TranslationUnit(
                unit_id=f"retry_{i}",
                text=k.split("::", 1)[-1],
                kind=BlockKind.PARAGRAPH,
            )
            for i, k in enumerate(keys)
        ]
        artifacts = engine.translate_many(units)
        retranslated = sum(
            1 for uid, art in artifacts.items()
            if art.final_text.strip() != units[int(uid.split("_")[1])].text.strip()
        )
        print(f"    → повторно переведено: {retranslated}/{len(units)}")

    if not dry_run:
        print(f"\n✓ Кэши обновлены. Запусти --rerender чтобы применить к PDF.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Найти и повторить перевод identity-записей")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--model", default="gpt-5.4-mini")
    parser.add_argument(
        "--artifact-dir",
        type=Path,
        default=ROOT / "PDF" / "_artifacts",
    )
    args = parser.parse_args()

    if not args.dry_run:
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            # Try .env in root
            env_file = ROOT / ".env"
            if env_file.exists():
                for line in env_file.read_text().splitlines():
                    if line.startswith("OPENAI_API_KEY="):
                        os.environ["OPENAI_API_KEY"] = line.split("=", 1)[1].strip()
                        break
        if not os.environ.get("OPENAI_API_KEY"):
            print("OPENAI_API_KEY не задан. Используй --dry-run или создай .env файл.")
            sys.exit(1)

    retry_translations(
        artifact_dir=args.artifact_dir,
        model=args.model,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()
