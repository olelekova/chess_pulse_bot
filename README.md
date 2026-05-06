# ♟️ Chess Candidates Bot — Инструкция запуска

Бот присылает тебе в Telegram живые апдейты с Турнира Претендентов 2026:
- Оценка позиции от Stockfish
- Комментарий гроссмейстера на русском (через Claude)
- Сигналы при резком изменении оценки или неожиданном дебюте

---

## Шаг 1 — Telegram Bot Token

1. Открой Telegram, найди **@BotFather**
2. Напиши `/newbot`
3. Придумай имя и username боту
4. Получишь токен вида: `7412345678:AAFxxxxxxxxxxxxxxxxxxxxxxx`
5. Напиши боту `/start` чтобы он тебя увидел

Твой Chat ID узнаешь так: напиши @userinfobot — он пришлёт твой ID.

---

## Шаг 2 — Anthropic API Key

1. Зайди на https://console.anthropic.com
2. API Keys → Create Key
3. Скопируй ключ

---

## Шаг 3 — Задеплой на Railway (бесплатно)

1. Зайди на https://railway.app и зарегистрируйся
2. New Project → Deploy from GitHub repo  
   (или: New Project → Empty Project → Add Service → GitHub)
3. Загрузи файлы `bot.py` и `requirements.txt`
4. В разделе **Variables** добавь:

```
TELEGRAM_TOKEN=твой_токен_от_BotFather
TELEGRAM_CHAT_ID=твой_chat_id
ANTHROPIC_API_KEY=твой_anthropic_ключ
STOCKFISH_PATH=/usr/games/stockfish
```

5. В **Settings → Deploy** добавь команду старта:
```
pip install stockfish && python bot.py
```

---

## Альтернатива — запустить локально

```bash
# Установить зависимости
pip install -r requirements.txt

# Установить Stockfish
# Mac: brew install stockfish
# Ubuntu: sudo apt install stockfish

# Задать переменные окружения
export TELEGRAM_TOKEN="..."
export TELEGRAM_CHAT_ID="..."
export ANTHROPIC_API_KEY="..."

# Запустить
python bot.py
```

---

## Что присылает бот

**При новой партии:**
```
♟️ caruana — hikaru
Ход 1 | Оценка: +0.15 | Лучший ход: e4

🧠 Комментарий гроссмейстера:
Каруана начинает e4 — классика. Интересно посмотрим
что выберет Накамура: Берлин или что-то более острое...
```

**При резком изменении оценки:**
```
📈 caruana — hikaru  
Ход 24 | Оценка: +2.40 | Лучший ход: Rxf7

🧠 Комментарий гроссмейстера:
Жертва ладьи на f7 — и позиция резко изменилась в пользу
белых. Чёрный король застрял в центре, развитие не завершено...
```

---

## Настройки (в bot.py)

| Параметр | Значение по умолчанию | Смысл |
|---|---|---|
| `POLL_INTERVAL_SECONDS` | 300 | Проверка каждые 5 минут |
| `EVAL_SWING_THRESHOLD` | 1.2 | Оповещение при изменении оценки ≥ 1.2 пешки |
| `NOVELTY_MOVE_THRESHOLD` | 15 | Сигнал если дебют кончился до хода 15 |
