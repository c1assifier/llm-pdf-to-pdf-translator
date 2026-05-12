#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
#  MSDS PDF Translator
#  Автоматически находит непереведённые файлы в GHS_MSDS/ и переводит их.
#
#  Использование:
#    bash run_remaining_translations.sh [модель] [--fix]
#
#  Примеры:
#    bash run_remaining_translations.sh               # новые файлы
#    bash run_remaining_translations.sh gpt-5.4-mini --fix  # перепереводит файлы с ошибками
#    bash run_remaining_translations.sh gpt-5.4-mini        # явное указание модели
#
#  Режим --fix:
#    Чистит плохие кэши (где API упал и закэшировал непереведённый текст),
#    удаляет проблемные PDF и перепереводит их заново.
# ─────────────────────────────────────────────────────────────────────────────

# ── Цвета ────────────────────────────────────────────────────────────────────
RESET='\033[0m'
BOLD='\033[1m'
DIM='\033[2m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
RED='\033[0;31m'
CYAN='\033[0;36m'
WHITE='\033[1;37m'
BLUE='\033[0;34m'

# ── Конфигурация ─────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INPUT_DIR="$SCRIPT_DIR/GHS_MSDS"
OUTPUT_DIR="$SCRIPT_DIR/PDF/translated"
ARTIFACT_DIR="$SCRIPT_DIR/PDF/_artifacts"
MODEL="gpt-5.4-mini"
FIX_MODE=false

# ── Загрузка .env ─────────────────────────────────────────────────────────────
if [ -f "$SCRIPT_DIR/.env" ]; then
    set -a
    # shellcheck disable=SC1090
    source "$SCRIPT_DIR/.env"
    set +a
fi

RERENDER_MODE=false

for arg in "$@"; do
    case "$arg" in
        --fix) FIX_MODE=true ;;
        --rerender) RERENDER_MODE=true ;;
        --*) ;;
        *) MODEL="$arg" ;;
    esac
done

TRANSLATE_SCRIPT="$SCRIPT_DIR/scripts/translate_pdf_layout.py"
FIX_SCRIPT="$SCRIPT_DIR/scripts/fix_identity_cache.py"
RETRY_SCRIPT="$SCRIPT_DIR/scripts/find_and_retry_untranslated.py"
CACHE_QUALITY_SCRIPT="$SCRIPT_DIR/scripts/cache_quality_fix.py"

mkdir -p "$OUTPUT_DIR" "$ARTIFACT_DIR"

# ── Хелперы ──────────────────────────────────────────────────────────────────
print_header() {
    echo ""
    echo -e "${BOLD}${CYAN}╔══════════════════════════════════════════════════════════╗${RESET}"
    echo -e "${BOLD}${CYAN}║          MSDS PDF Translator  ·  EN → RU                ║${RESET}"
    echo -e "${BOLD}${CYAN}╚══════════════════════════════════════════════════════════╝${RESET}"
    echo -e "  ${DIM}Папка источников :${RESET} ${WHITE}$INPUT_DIR${RESET}"
    echo -e "  ${DIM}Папка результатов:${RESET} ${WHITE}$OUTPUT_DIR${RESET}"
    echo -e "  ${DIM}Модель           :${RESET} ${WHITE}$MODEL${RESET}"

    # Проверка API ключа
    if [ -z "$OPENAI_API_KEY" ]; then
        echo ""
        echo -e "  ${RED}${BOLD}✗ OPENAI_API_KEY не задан!${RESET}"
        echo -e "  ${DIM}  Создай .env файл рядом со скриптом:${RESET}"
        echo -e "  ${WHITE}  echo 'OPENAI_API_KEY=sk-...' > .env${RESET}"
        echo ""
        exit 1
    else
        local key_preview="${OPENAI_API_KEY:0:8}...${OPENAI_API_KEY: -4}"
        echo -e "  ${DIM}API ключ         :${RESET} ${GREEN}${key_preview}${RESET}"
    fi
    echo ""
}

print_divider() {
    echo -e "  ${DIM}──────────────────────────────────────────────────────────${RESET}"
}

progress_bar() {
    local current=$1
    local total=$2
    local width=40
    local filled=$(( current * width / total ))
    local empty=$(( width - filled ))
    local bar=""
    for ((i=0; i<filled; i++)); do bar+="█"; done
    for ((i=0; i<empty; i++)); do bar+="░"; done
    echo -ne "  ${DIM}[${RESET}${CYAN}${bar}${RESET}${DIM}]${RESET} ${BOLD}${current}/${total}${RESET}"
}

elapsed_since() {
    local start=$1
    local now
    now=$(date +%s)
    local delta=$(( now - start ))
    if (( delta < 60 )); then
        echo "${delta}с"
    else
        echo "$(( delta / 60 ))м $(( delta % 60 ))с"
    fi
}

format_size() {
    local file=$1
    if [ -f "$file" ]; then
        local bytes
        bytes=$(wc -c < "$file" 2>/dev/null || echo 0)
        if (( bytes >= 1048576 )); then
            echo "$(( bytes / 1048576 )) МБ"
        else
            echo "$(( bytes / 1024 )) КБ"
        fi
    else
        echo "—"
    fi
}

# ── Сбор файлов для перевода ──────────────────────────────────────────────────
collect_pending() {
    PENDING=()
    ALREADY_DONE=()

    for src_path in "$INPUT_DIR"/*.pdf; do
        [ -f "$src_path" ] || continue
        local src_file
        src_file="$(basename "$src_path")"
        local stem="${src_file%.pdf}"
        local out_path="$OUTPUT_DIR/${stem}_ru.pdf"

        if [ -f "$out_path" ]; then
            ALREADY_DONE+=("$src_file")
        else
            PENDING+=("$src_file")
        fi
    done
}

# ─────────────────────────────────────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────────────────────────────────────
SCRIPT_START=$(date +%s)

print_header
collect_pending

# ── Режим --rerender: перерендерить все уже переведённые файлы ───────────────
# Используется когда был обновлён код рендеринга (новые шрифты, фон заголовков,
# другой раскладкой текста) — переводы берутся из кэша, поэтому работает быстро.
if [ "$RERENDER_MODE" = true ]; then
    echo -e "  ${CYAN}${BOLD}Режим --rerender: удаляю все PDF и перерисовываю из кэша...${RESET}"
    echo ""

    # Шаг 0: Почистить кэши перед перерендером
    echo -ne "  ${DIM}Чищу кэши (заголовки разделов / near-identity / HTML)...${RESET}"
    python "$CACHE_QUALITY_SCRIPT" --artifact-dir "$ARTIFACT_DIR" 2>/dev/null | grep -E "разделов|near-identity|HTML|Заголовки|Near|итого" | while IFS= read -r line; do
        echo -e "    ${DIM}${line}${RESET}"
    done
    echo -e "  ${GREEN}✓ Кэши очищены${RESET}"
    echo ""

    RERENDER_COUNT=0
    for src_path in "$INPUT_DIR"/*.pdf; do
        [ -f "$src_path" ] || continue
        src_file="$(basename "$src_path")"
        stem="${src_file%.pdf}"
        out_path="$OUTPUT_DIR/${stem}_ru.pdf"
        if [ -f "$out_path" ]; then
            rm "$out_path"
            RERENDER_COUNT=$(( RERENDER_COUNT + 1 ))
        fi
    done
    echo -e "  ${DIM}Удалено ${RERENDER_COUNT} PDF — будут переведены из кэша.${RESET}"
    echo ""

    # Пересобрать список pending
    collect_pending
fi

# ── Режим --fix: найти файлы с проблемами и добавить их в очередь ────────────
if [ "$FIX_MODE" = true ]; then
    echo -e "  ${YELLOW}${BOLD}Режим --fix: поиск файлов с identity-переводами...${RESET}"
    echo ""

    # Найти файлы где в ПОСЛЕДНЕМ запуске было много chunk-fallback
    # (смотрим только с последнего file_start, чтобы игнорировать старые сессии)
    FIX_CANDIDATES=()
    for src_path in "$INPUT_DIR"/*.pdf; do
        [ -f "$src_path" ] || continue
        src_file="$(basename "$src_path")"
        stem="${src_file%.pdf}"
        out_path="$OUTPUT_DIR/${stem}_ru.pdf"
        log_path="$ARTIFACT_DIR/${stem}_ru.translation-log.jsonl"

        [ -f "$log_path" ] || continue

        # Считаем chunk-fallback только в последней сессии (после последнего file_start)
        count=$(python3 - "$log_path" <<'PYEOF'
import sys, json
path = sys.argv[1]
events = []
try:
    with open(path) as f:
        for line in f:
            try: events.append(json.loads(line))
            except: pass
except: pass
last_start = 0
for i, e in enumerate(events):
    if e.get("event") == "file_start":
        last_start = i
recent = events[last_start:]
print(sum(1 for e in recent if e.get("event") == "chunk_fallback"))
PYEOF
        )

        if [ "${count:-0}" -gt 5 ]; then
            FIX_CANDIDATES+=("$src_file")
            echo -e "  ${YELLOW}⚠ ${src_file}${RESET} ${DIM}(chunk-fallback: ${count}x в последней сессии)${RESET}"
        fi
    done

    if [ "${#FIX_CANDIDATES[@]}" -eq 0 ]; then
        echo -e "  ${GREEN}✓ Проблемных файлов не найдено.${RESET}"
        echo ""
        exit 0
    fi

    echo ""
    echo -e "  ${BOLD}Найдено ${#FIX_CANDIDATES[@]} файлов для исправления.${RESET}"
    echo ""

    # Очистить кэши
    echo -ne "  ${DIM}Очищаю плохие cache-записи...${RESET}"
    python "$FIX_SCRIPT" --artifact-dir "$ARTIFACT_DIR" 2>/dev/null
    echo -e " ${GREEN}✓${RESET}"
    echo ""

    # Удалить плохие PDF и добавить в очередь перевода
    for f in "${FIX_CANDIDATES[@]}"; do
        stem="${f%.pdf}"
        out_path="$OUTPUT_DIR/${stem}_ru.pdf"
        if [ -f "$out_path" ]; then
            rm "$out_path"
        fi
    done

    # Заменить PENDING на список для фиксинга
    PENDING=("${FIX_CANDIDATES[@]}")
    ALREADY_DONE=()
fi

TOTAL_SRC=$(( ${#PENDING[@]} + ${#ALREADY_DONE[@]} ))
TOTAL_PENDING=${#PENDING[@]}
TOTAL_DONE=${#ALREADY_DONE[@]}

# ── Статус до начала ─────────────────────────────────────────────────────────
if [ "$FIX_MODE" = false ]; then
    echo -e "  ${DIM}Найдено источников  :${RESET} ${BOLD}${TOTAL_SRC}${RESET}"
    echo -e "  ${DIM}Уже переведено      :${RESET} ${GREEN}${TOTAL_DONE}${RESET}"
    echo -e "  ${DIM}Нужно перевести     :${RESET} ${YELLOW}${TOTAL_PENDING}${RESET}"
    echo ""
fi

if [ "$TOTAL_PENDING" -eq 0 ]; then
    echo -e "  ${GREEN}${BOLD}✓ Все файлы уже переведены!${RESET}"
    echo ""
    exit 0
fi

print_divider
echo ""

# ── Перевод ───────────────────────────────────────────────────────────────────
SUCCESS=0
FAILED=0
FAILED_FILES=()

for i in "${!PENDING[@]}"; do
    SRC_FILE="${PENDING[$i]}"
    SRC_PATH="$INPUT_DIR/$SRC_FILE"
    STEM="${SRC_FILE%.pdf}"
    OUTPUT_FILE="${STEM}_ru.pdf"
    OUTPUT_PATH="$OUTPUT_DIR/$OUTPUT_FILE"
    NUM=$(( i + 1 ))

    # Прогресс-бар
    echo -ne "\r"
    progress_bar "$NUM" "$TOTAL_PENDING"
    echo ""

    # Название файла
    echo -e "  ${BOLD}${WHITE}${NUM}/${TOTAL_PENDING}${RESET}  ${SRC_FILE}"

    FILE_START=$(date +%s)
    echo -ne "  ${DIM}↳ Перевожу...${RESET}"

    # Запуск перевода (stderr → /dev/null чтобы не засорять консоль)
    if python "$TRANSLATE_SCRIPT" \
        "$SRC_PATH" \
        "$OUTPUT_PATH" \
        --artifact-dir "$ARTIFACT_DIR" \
        --model "$MODEL" \
        --source-lang en \
        --target-lang ru 2>/dev/null; then

        ELAPSED=$(elapsed_since "$FILE_START")
        SIZE=$(format_size "$OUTPUT_PATH")
        echo -e "\r  ${GREEN}✓ Готово${RESET}  ${DIM}(${ELAPSED}, ${SIZE})${RESET}          "
        SUCCESS=$(( SUCCESS + 1 ))
    else
        echo -e "\r  ${RED}✗ Ошибка${RESET}                              "
        FAILED=$(( FAILED + 1 ))
        FAILED_FILES+=("$SRC_FILE")
    fi

    echo ""
done

# ── Чистка качества кэша ─────────────────────────────────────────────────────
# После перевода: удалить заголовки разделов с частичным переводом,
# near-identity записи и HTML-артефакты из кэшей.
if [ "$SUCCESS" -gt 0 ] || [ "$FIX_MODE" = true ]; then
    echo -ne "  ${DIM}Чищу кэши (разделы / near-identity / HTML)...${RESET}"
    CACHE_FIX_OUT=$(python "$CACHE_QUALITY_SCRIPT" --artifact-dir "$ARTIFACT_DIR" 2>/dev/null)
    CACHE_SEC=$(echo "$CACHE_FIX_OUT" | python3 -c "import sys,re; t=sys.stdin.read(); m=re.search(r'Заголовки разделов удалены\s*:\s*([0-9]+)',t); print(m.group(1) if m else '0')")
    CACHE_NI=$(echo  "$CACHE_FIX_OUT" | python3 -c "import sys,re; t=sys.stdin.read(); m=re.search(r'Near-identity удалены\s*:\s*([0-9]+)',t); print(m.group(1) if m else '0')")
    CACHE_HT=$(echo  "$CACHE_FIX_OUT" | python3 -c "import sys,re; t=sys.stdin.read(); m=re.search(r'HTML-записи очищены\s*:\s*([0-9]+)',t); print(m.group(1) if m else '0')")
    if [ "${CACHE_SEC:-0}" -gt 0 ] || [ "${CACHE_NI:-0}" -gt 0 ] || [ "${CACHE_HT:-0}" -gt 0 ]; then
        echo -e "\r  ${YELLOW}↻ Кэш: разделы=${CACHE_SEC}, near-id=${CACHE_NI}, html=${CACHE_HT} — перерисовываю затронутые PDF${RESET}    "
    else
        echo -e "\r  ${GREEN}✓ Кэши чистые.${RESET}                                           "
    fi
    echo ""
fi

# ── Поиск и повтор непереведённых фрагментов ─────────────────────────────────
# После каждого прогона: сканируем кэши на identity-переводы и ретраим через API.
# Это добивает блоки которые API вернул без перевода в прошлых сессиях.
if [ "$SUCCESS" -gt 0 ] || [ "$FIX_MODE" = true ] || [ "$RERENDER_MODE" = true ]; then
    echo -ne "  ${DIM}Ищу непереведённые фрагменты...${RESET}"
    RETRY_OUT=$(python "$RETRY_SCRIPT" \
        --artifact-dir "$ARTIFACT_DIR" \
        --model "$MODEL" 2>/dev/null)
    RETRY_FOUND=$(echo "$RETRY_OUT" | python3 -c "import sys,re; t=sys.stdin.read(); m=re.search(r'Найдено ([0-9]+)',t); print(m.group(1) if m else '0')")
    if [ "${RETRY_FOUND:-0}" -gt 0 ]; then
        echo -e "\r  ${YELLOW}↻ Найдено ${RETRY_FOUND} непереведённых — повторяю...${RESET}    "
        echo "$RETRY_OUT" | grep -E "повторно|записей" | head -10 | while IFS= read -r line; do
            echo -e "    ${DIM}${line}${RESET}"
        done
        echo ""
    else
        echo -e "\r  ${GREEN}✓ Все фрагменты переведены.${RESET}                        "
        echo ""
    fi
fi

# ── Итоговый отчёт ────────────────────────────────────────────────────────────
TOTAL_ELAPSED=$(elapsed_since "$SCRIPT_START")
TOTAL_NOW=$(( TOTAL_DONE + SUCCESS ))

print_divider
echo ""
echo -e "  ${BOLD}${WHITE}Результат${RESET}"
echo ""
echo -e "  ${GREEN}✓ Переведено сейчас :${RESET} ${BOLD}${SUCCESS}${RESET}"
echo -e "  ${DIM}  Уже было готово   : ${TOTAL_DONE}${RESET}"
echo -e "  ${BOLD}  Итого в папке     : ${TOTAL_NOW} / ${TOTAL_SRC}${RESET}"

if [ "$FAILED" -gt 0 ]; then
    echo ""
    echo -e "  ${RED}✗ Ошибки (${FAILED}):${RESET}"
    for f in "${FAILED_FILES[@]}"; do
        echo -e "    ${DIM}• ${f}${RESET}"
    done
fi

echo ""
echo -e "  ${DIM}Время работы  : ${RESET}${TOTAL_ELAPSED}"
echo -e "  ${DIM}Папка с PDF   : ${RESET}${WHITE}${OUTPUT_DIR}${RESET}"
echo ""

if [ "$FAILED" -eq 0 ] && [ "$TOTAL_NOW" -eq "$TOTAL_SRC" ]; then
    echo -e "  ${BOLD}${GREEN}🎉 Все ${TOTAL_SRC} файлов переведены!${RESET}"
elif [ "$FAILED" -eq 0 ]; then
    echo -e "  ${BOLD}${GREEN}✓ Сессия завершена без ошибок.${RESET}"
else
    echo -e "  ${YELLOW}⚠ Завершено с ошибками. Запусти скрипт повторно для ретрая.${RESET}"
fi

echo ""
