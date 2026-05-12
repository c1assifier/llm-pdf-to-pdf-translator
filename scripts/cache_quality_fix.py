"""
cache_quality_fix.py
--------------------
Комплексная чистка кэшей переводов. Четыре прохода:

1. SECTION HEADERS — удаляет записи вида 'Section N - TITLE' где финал частично
   переведён (только Раздел, а название осталось английским). Движок теперь
   переводит их локально через MSDS_SECTION_TITLES, без API.

2. NEAR-IDENTITY — удаляет записи где нормализованный финал ≥85% совпадает
   с нормализованным источником и источник содержит переводимый английский.
   Эти блоки пойдут на повторный перевод через API.

3. HTML CLEANUP — очищает HTML-теги (<br>, <p> и т.д.) из final_text
   прямо в кэше, без перевода.

4. META-RESPONSES — удаляет записи где API вернул инструкцию/мета-ответ
   вместо перевода ("Please provide...", "No changes needed...", и т.п.).

Использование:
    python scripts/cache_quality_fix.py [--dry-run] [--artifact-dir PATH]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from difflib import SequenceMatcher
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ── Хелперы ──────────────────────────────────────────────────────────────────

def _normalize(text: str) -> str:
    """Убрать пунктуацию, пробелы, нижний регистр — для сравнения."""
    text = re.sub(r"[^\w\s]", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip().lower()


def _similarity(a: str, b: str) -> float:
    na, nb = _normalize(a), _normalize(b)
    if not na or not nb:
        return 0.0
    return SequenceMatcher(None, na, nb).ratio()


def _has_translatable_english(text: str) -> bool:
    return bool(re.search(r"[A-Za-z]{4,}", text))


def _is_code_or_abbrev(text: str) -> bool:
    cleaned = re.sub(r"\s+", " ", text).strip()
    if not cleaned:
        return True
    if re.fullmatch(r"[\d .,%<>()/\-:_°§]+", cleaned):
        return True
    code_patterns = (
        r"\bH\d{3}\b", r"\bP\d{3}\b", r"\bEUH\d+\b",
        r"\b\d{2,7}-\d{2}-\d\b",
        r"^[A-Z]{2,6}s?$",
        r"^\([A-Z]{2,8}s?\)$",
    )
    return any(re.search(p, cleaned) for p in code_patterns)


_META_RESPONSE_RE = re.compile(
    r"^(?:Please provide|No changes needed|The provided|I need the|"
    r"This is already|The translation is|Note:|Here is the translation|"
    r"Here's the translation|\[Translation\]|Could you please|"
    r"It seems like|There is no text|I don't see any text|"
    r"The text appears to be|Unfortunately|I cannot|I'm unable)",
    re.IGNORECASE,
)


def _is_meta_response(final: str) -> bool:
    """API вернул мета-ответ (инструкцию) вместо перевода."""
    return bool(_META_RESPONSE_RE.match(final.strip()))


def _strip_html(text: str) -> str:
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[A-Za-z][^>]{0,40}>", "", text)
    text = re.sub(r"</[A-Za-z]+>", "", text)
    return text.strip()


def _is_section_header(src: str) -> bool:
    return bool(re.match(r"^Section\s+\d+\s*[-–]", src.strip(), re.IGNORECASE))


def _section_still_english(src: str, final: str) -> bool:
    """Заголовок раздела: финал содержит английское название (не переведён)."""
    if not _is_section_header(src):
        return False
    # Если финал содержит оригинальное английское название раздела → плохой перевод
    m_src = re.match(r"^Section\s+\d+\s*[-–]\s*(.+)$", src.strip(), re.IGNORECASE | re.DOTALL)
    m_fin = re.match(r"^Раздел\s+\d+\s*[-–]\s*(.+)$", final.strip(), re.IGNORECASE | re.DOTALL)
    if m_src and m_fin:
        src_title = m_src.group(1).strip().upper()
        fin_title = m_fin.group(1).strip().upper()
        # Если название раздела в финале совпадает с английским → не переведено
        return fin_title == src_title or _similarity(src_title, fin_title) > 0.85
    # Если финал начинается с "Раздел" но дальше английский — тоже плохо
    if re.match(r"^Раздел\s+\d+", final.strip(), re.IGNORECASE):
        return _has_translatable_english(final.split("–", 1)[-1])
    # Если финал всё ещё начинается с "Section" — не переведён вообще
    return bool(re.match(r"^Section\s+\d+", final.strip(), re.IGNORECASE))


# ── Основная логика ───────────────────────────────────────────────────────────

def process_cache(cache_path: Path, dry_run: bool) -> dict:
    try:
        raw: dict = json.loads(cache_path.read_text(encoding="utf-8"))
    except Exception as e:
        return {"file": cache_path.name, "error": str(e)}

    section_deleted = []
    near_identity_deleted = []
    html_fixed = []
    meta_deleted = []

    for key in list(raw.keys()):
        payload = raw[key]
        src = str(payload.get("source_text", "")).strip()
        final = str(payload.get("final_text", "")).strip()

        # 1. Заголовки разделов с частичным переводом
        if _section_still_english(src, final):
            section_deleted.append(key)
            if not dry_run:
                del raw[key]
            continue

        # 2. Near-identity: нормализованный финал ≈ источник
        if _has_translatable_english(src) and not _is_code_or_abbrev(src):
            sim = _similarity(src, final)
            if sim >= 0.85 and len(src) >= 10:
                near_identity_deleted.append(key)
                if not dry_run:
                    del raw[key]
                continue

        # 3. HTML-теги в final_text
        if re.search(r"<[A-Za-z][^>]*>|<br\s*/?>", final, re.IGNORECASE):
            cleaned = _strip_html(final)
            if cleaned != final:
                html_fixed.append(key)
                if not dry_run:
                    raw[key]["final_text"] = cleaned
                    if "normalized_text" in raw[key]:
                        raw[key]["normalized_text"] = cleaned
                    if "translated_text" in raw[key]:
                        raw[key]["translated_text"] = cleaned

        # 4. Мета-ответы API (инструкции вместо перевода)
        if _is_meta_response(final):
            meta_deleted.append(key)
            if not dry_run:
                del raw[key]
            continue

    if not dry_run and (section_deleted or near_identity_deleted or html_fixed or meta_deleted):
        cache_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")

    return {
        "file": cache_path.name,
        "section_deleted": len(section_deleted),
        "near_identity_deleted": len(near_identity_deleted),
        "html_fixed": len(html_fixed),
        "meta_deleted": len(meta_deleted),
        "sample_section": [k.split("::", 1)[-1][:60] for k in section_deleted[:2]],
        "sample_near_id": [k.split("::", 1)[-1][:60] for k in near_identity_deleted[:2]],
        "sample_meta": [k.split("::", 1)[-1][:60] for k in meta_deleted[:2]],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Комплексная чистка кэшей переводов")
    parser.add_argument("--dry-run", action="store_true", help="Только показать, не менять")
    parser.add_argument(
        "--artifact-dir", type=Path,
        default=ROOT / "PDF" / "_artifacts",
    )
    args = parser.parse_args()

    artifact_dir: Path = args.artifact_dir
    if not artifact_dir.exists():
        print(f"Папка не найдена: {artifact_dir}")
        sys.exit(1)

    cache_files = sorted(artifact_dir.glob("*.translation-cache.json"))
    mode = "[DRY RUN] " if args.dry_run else ""
    print(f"\n{mode}Обработка {len(cache_files)} кэш-файлов...\n")

    total_section = total_near_id = total_html = total_meta = 0
    for cf in cache_files:
        result = process_cache(cf, dry_run=args.dry_run)
        s = result.get("section_deleted", 0)
        n = result.get("near_identity_deleted", 0)
        h = result.get("html_fixed", 0)
        m = result.get("meta_deleted", 0)
        total_section += s
        total_near_id += n
        total_html += h
        total_meta += m
        if s + n + h + m > 0:
            stem = result["file"].replace(".translation-cache.json", "")
            print(f"  {stem}:")
            if s:
                print(f"    заголовки разделов: удалено {s} (будут переведены локально)")
                for ex in result.get("sample_section", []):
                    print(f"      - {ex!r}")
            if n:
                print(f"    near-identity:       удалено {n} (пойдут на повторный API)")
                for ex in result.get("sample_near_id", []):
                    print(f"      - {ex!r}")
            if h:
                print(f"    HTML очищен:         {h} записей")
            if m:
                print(f"    мета-ответы API:     удалено {m} (пойдут на повторный API)")
                for ex in result.get("sample_meta", []):
                    print(f"      - {ex!r}")

    print(f"\n{'─'*55}")
    print(f"  Заголовки разделов удалены  : {total_section}")
    print(f"  Near-identity удалены        : {total_near_id}")
    print(f"  HTML-записи очищены          : {total_html}")
    print(f"  Мета-ответы API удалены      : {total_meta}")
    if args.dry_run:
        print("\n  Запусти без --dry-run чтобы применить изменения.")
    else:
        print(f"\n  ✓ Готово. Запусти --rerender чтобы обновить PDF.")


if __name__ == "__main__":
    main()
