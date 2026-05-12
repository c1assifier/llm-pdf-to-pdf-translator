#!/bin/bash
# Чистка .venv от мусорных пакетов (notebooklm, playwright, и т.д.)
# Оставляет только то, что нужно для перевода MSDS:
#   PyMuPDF (fitz), openai + зависимости (httpx, pydantic, anyio, certifi...)

set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SITE="$SCRIPT_DIR/.venv/lib/python3.12/site-packages"

if [ ! -d "$SITE" ]; then
    echo "❌ Не нашёл .venv/lib/python3.12/site-packages"
    echo "   Убедись что запускаешь из папки msds-pdf-translator/"
    exit 1
fi

BEFORE=$(du -sh "$SITE" | cut -f1)
echo "📦 Размер до: $BEFORE"
echo ""
echo "🗑  Удаляю лишние пакеты..."

# Packages to remove — не нужны для MSDS-перевода
JUNK_DIRS=(
    playwright
    pyee
    notebooklm
    deep_translator
    click
    markdown_it
    mdurl
    pygments
    PIL
    bs4
    requests
    charset_normalizer
    soupsieve
    rich
    tqdm
    yt_dlp
    urllib3
)

JUNK_DISTINFO=(
    "playwright-"
    "pyee-"
    "notebooklm_py-"
    "deep_translator-"
    "click-"
    "markdown_it_py-"
    "mdurl-"
    "Pygments-"
    "pillow-"
    "beautifulsoup4-"
    "soupsieve-"
    "requests-"
    "charset_normalizer-"
    "rich-"
    "tqdm-"
    "yt_dlp-"
    "urllib3-"
)

for pkg in "${JUNK_DIRS[@]}"; do
    TARGET="$SITE/$pkg"
    if [ -d "$TARGET" ]; then
        rm -rf "$TARGET"
        echo "   ✓ $pkg"
    fi
done

for prefix in "${JUNK_DISTINFO[@]}"; do
    for d in "$SITE/${prefix}"*.dist-info; do
        [ -d "$d" ] && rm -rf "$d" && echo "   ✓ $(basename $d)"
    done
done

AFTER=$(du -sh "$SITE" | cut -f1)
echo ""
echo "✅ Готово!"
echo "   Было:  $BEFORE"
echo "   Стало: $AFTER"
echo ""
echo "Оставлено: PyMuPDF, openai, httpx, httpcore, pydantic, anyio, certifi, idna, h11, jiter, distro, annotated-types, greenlet, sniffio"
