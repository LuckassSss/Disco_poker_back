#!/usr/bin/env bash
# Запуск сервера на своей машине без Docker.
#
#   ./scripts/run_local.sh
#
# Скрипт сам создаст виртуальное окружение, поставит зависимости и поднимет
# сервер на http://localhost:8000. Postgres и Redis при этом не нужны: сервер
# напишет предупреждение и продолжит работать на памяти — играть можно сразу.
set -euo pipefail

cd "$(dirname "$0")/.."

PYTHON="${PYTHON:-python3}"
PORT="${PORT:-8000}"

if ! command -v "$PYTHON" >/dev/null 2>&1; then
  echo "Не нашёл $PYTHON. Поставьте Python 3.11+ и повторите." >&2
  exit 1
fi

VERSION="$("$PYTHON" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
case "$VERSION" in
  3.11|3.12|3.13|3.14) ;;
  *) echo "Нужен Python 3.11 или новее, найден $VERSION" >&2; exit 1 ;;
esac

if [ ! -d .venv ]; then
  echo "Создаю виртуальное окружение…"
  "$PYTHON" -m venv .venv
fi

# shellcheck disable=SC1091
source .venv/bin/activate

echo "Ставлю зависимости…"
pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt

if [ ! -f .env ]; then
  cp .env.example .env
  echo "Создал .env из .env.example"
fi

echo
echo "  Сервер:     http://localhost:${PORT}"
echo "  Проверка:   curl http://localhost:${PORT}/health"
echo "  WebSocket:  ws://localhost:${PORT}/ws?token=<JWT>"
echo "  Документы:  http://localhost:${PORT}/docs"
echo
echo "  Боты за стол (в другом окне, из этой же папки):"
echo "    python3 scripts/bot_client.py --bots 5 --hands 20"
echo

exec uvicorn app.main:app --host 0.0.0.0 --port "$PORT" --ws websockets
