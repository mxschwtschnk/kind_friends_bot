mkdir -p scripts
cat > scripts/start_codespace.sh <<'SH'
#!/usr/bin/env bash
set -euo pipefail

# 1) Порт, который слушает aiohttp
export PORT="${PORT:-10000}"

# 2) Динамический публичный URL Codespaces для вебхука
export WEBHOOK_URL="https://${CODESPACE_NAME}-${PORT}.${GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN}/webhook"
echo "Using WEBHOOK_URL: $WEBHOOK_URL"

# 3) Зависимости и запуск бота (BOT_TOKEN, DATABASE_URL и т.п. приходят из Codespaces Secrets)
pip install -r requirements.txt
python bot.py
SH
chmod +x scripts/start_codespace.sh
