#!/usr/bin/env bash
# Запуск сервера на Ubuntu в Docker: api + Postgres + Redis, перезапуск после падений и ребута.
#
#   ./scripts/server_up.sh          первый запуск и обновление (после git pull)
#
# При первом запуске создаёт .env.prod со случайными JWT_SECRET и паролем Postgres.
# Не терять и не менять пароль: база уже создана с ним, а смена JWT_SECRET разлогинит всех.
set -euo pipefail

cd "$(dirname "$0")/.."

if ! docker compose version >/dev/null 2>&1; then
  echo "Нет docker compose. Поставьте: sudo apt install docker-compose-plugin" >&2
  exit 1
fi
if ! docker info >/dev/null 2>&1; then
  echo "Docker недоступен текущему пользователю: sudo usermod -aG docker \$USER и перелогиниться (или запуск через sudo)" >&2
  exit 1
fi
if command -v systemctl >/dev/null 2>&1 && [ "$(systemctl is-enabled docker 2>/dev/null)" != "enabled" ]; then
  echo "Внимание: docker не в автозапуске, после ребута сервер не поднимется: sudo systemctl enable docker" >&2
fi

if [ ! -f .env.prod ]; then
  echo "Создаю .env.prod со случайными секретами…"
  umask 077
  cat > .env.prod <<EOF
ENVIRONMENT=prod
API_PORT=8000
JWT_SECRET=$(openssl rand -hex 32)
POSTGRES_PASSWORD=$(openssl rand -hex 16)
GUEST_START_BALANCE=10000
EOF
fi

# ponytail: перезапуск api теряет фишки на столах (см. PROGRESS.md) — обновлять, когда никто не играет
docker compose --env-file .env.prod up -d --build --remove-orphans

PORT="$(grep -E '^API_PORT=' .env.prod | cut -d= -f2)"
PORT="${PORT:-8000}"
echo "Жду /health на порту ${PORT}…"
for _ in $(seq 1 60); do
  if curl -fsS "http://localhost:${PORT}/health" >/dev/null 2>&1; then
    docker compose --env-file .env.prod ps
    echo "Готово: http://<адрес сервера>:${PORT}  (логи: docker compose logs -f api)"
    exit 0
  fi
  sleep 1
done

echo "Сервер не ответил за 60 с. Логи:" >&2
docker compose --env-file .env.prod logs --tail 50 api >&2
exit 1
