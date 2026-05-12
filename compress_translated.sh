#!/bin/bash
# Сжимает файлы из PDF/translated/ в PDF/compressed_part_3/
# PDF/translated/ НЕ ТРОГАЕТСЯ.

set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

INPUT="PDF/translated"
OUTPUT="PDF/compressed_part_3"

mkdir -p "$OUTPUT"
echo "📁 Сжимаю в: $OUTPUT"
echo "────────────────────────────────────"

# Проверяем наличие ghostscript
GS_BIN=""
for candidate in gs ghostscript /usr/local/bin/gs /opt/homebrew/bin/gs; do
    if command -v "$candidate" &>/dev/null; then
        GS_BIN="$candidate"
        break
    fi
done

if [ -z "$GS_BIN" ]; then
    echo "❌ Ghostscript не найден."
    echo "   Установи: brew install ghostscript"
    exit 1
fi

echo "✓ Ghostscript: $GS_BIN ($(${GS_BIN} --version 2>/dev/null))"
echo ""

SUCCESS=0
FAIL=0

for src in "$INPUT"/*.pdf; do
    name=$(basename "$src")
    out="$OUTPUT/$name"

    echo -n "  📄 $name ... "

    ERR=$("$GS_BIN" -sDEVICE=pdfwrite \
          -dCompatibilityLevel=1.4 \
          -dPDFSETTINGS=/ebook \
          -dNOPAUSE -dQUIET -dBATCH \
          -sOutputFile="$out" \
          "$src" 2>&1)

    if [ $? -eq 0 ]; then
        orig=$(du -k "$src" | cut -f1)
        comp=$(du -k "$out" | cut -f1)
        echo "✓  ${orig}KB → ${comp}KB"
        ((SUCCESS++)) || true
    else
        echo "✗"
        echo "     Ошибка: $ERR"
        cp "$src" "$out"
        ((FAIL++)) || true
    fi
done

echo "────────────────────────────────────"
echo "✅ Готово: $SUCCESS сжато, $FAIL скопировано без сжатия"
echo "📂 Результат: $SCRIPT_DIR/$OUTPUT/"
