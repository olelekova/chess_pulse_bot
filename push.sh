#!/bin/bash
# Скрипт для отправки изменений в GitHub
# Запускай из папки chess bot в терминале: bash push.sh

REPO_URL="https://github.com/olelekova/chess_pulse_bot.git"
FOLDER="$(cd "$(dirname "$0")" && pwd)"

cd "$FOLDER"

# Инициализация git если нужно
if [ ! -d ".git" ]; then
    echo "⚙️  Первый запуск — настраиваю git..."
    git init
    git remote add origin "$REPO_URL"
    git branch -M main
fi

# Настройка автора
git config user.email "olelekova@gmail.com"
git config user.name "Chess Bot"

# Коммит и пуш
git add bot.py Dockerfile requirements.txt render.yaml push.sh \
        commentary_prompts.py tournaments.yaml tournaments_config.py \
        AGENT_RUNBOOK.md TOURNAMENTS_README.md
git commit -m "Wire tournaments.yaml into bot.py; add TePe Sigeman 2026 profile"
git push --force origin main

echo "✅ Готово! Render задеплоит автоматически."
