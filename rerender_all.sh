#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
#  Перерендер всех переведённых PDF с исправленным кодом.
#  Переводы берутся из кэша — API не вызывается, деньги не тратятся.
#
#  Запуск:
#    bash rerender_all.sh
# ─────────────────────────────────────────────────────────────────────────────

set -e

# Автоматически находим папку скрипта
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "📁 Папка проекта: $SCRIPT_DIR"
echo ""

# Активируем окружение
if [ ! -f ".venv/bin/activate" ]; then
    echo "❌ Не найдено .venv — убедись что окружение создано"
    exit 1
fi
source .venv/bin/activate

# Проверяем PyMuPDF
python3 -c "import fitz" 2>/dev/null || {
    echo "❌ PyMuPDF не установлен. Запусти: pip install PyMuPDF"
    exit 1
}

# Считаем файлы
TOTAL=0
SUCCESS=0
SKIPPED=0

echo "🔄 Перерендериваю все PDF из кэша..."
echo "────────────────────────────────────"

for src in GHS_MSDS/*.pdf; do
    stem=$(basename "$src" .pdf)
    cache="PDF/_artifacts/${stem}_ru.translation-cache.json"
    out="PDF/translated/${stem}_ru.pdf"

    if [ ! -f "$cache" ]; then
        echo "  ⏭  $stem — нет кэша, пропускаю"
        ((SKIPPED++)) || true
        continue
    fi

    ((TOTAL++)) || true
    echo -n "  📄 $stem ... "

    if PYTHONPATH=. python3 scripts/translate_pdf_layout.py \
        "$src" "$out" \
        --source-lang en --target-lang ru \
        --model gpt-5.4-mini \
        --artifact-dir PDF/_artifacts \
        > /tmp/msds_render_$$.log 2>&1; then
        echo "✓"
        ((SUCCESS++)) || true
    else
        echo "✗ (см. /tmp/msds_render_$$.log)"
    fi
done

echo "────────────────────────────────────"
echo ""
echo "✅ Готово: $SUCCESS/$TOTAL файлов перерендерено"
[ $SKIPPED -gt 0 ] && echo "   Пропущено (нет кэша): $SKIPPED"
echo ""
echo "📂 Результат: $SCRIPT_DIR/PDF/translated/"
