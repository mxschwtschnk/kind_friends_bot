#!/usr/bin/env bash
set -euo pipefail

# На каком порту слушает твой aiohttp
export PORT="${PORT:-10000}"

# Готовим публичный URL Codespaces для этого порта
# Пример: https://<CODESPACE_NAME>-10000.<forwarding-domain>/webhook
export WEBHOOK_URL="https://${CODESPACE_NAME}-${PORT}.${GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN}/webhook"

echo "Using WEBHOOK_URL: $WEBHOOK_URL"

# Убедимся, что зависимости поставлены
pip install -r requirements.txt

# Запускаем бота (он сам прочитает BOT_TOKEN, DATABASE_URL и т.п. из Secrets)
python bot.py
