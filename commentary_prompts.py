"""
Модуль промптов для шахматного комментатора.

Голос бота: Юдит Полгар (боевой дух, перспектива игрока) + Яссер Сейраван (глубина, образование).
Все промпты используют system prompt с единым голосом + user prompt с данными позиции.

Использование в bot.py:
    from commentary_prompts import build_prompt, SYSTEM_PROMPT
    system, user = build_prompt("eval_swing", data)
    r = client.messages.create(
        model="claude-sonnet-4-6",
        system=system,
        messages=[{"role": "user", "content": user}],
        max_tokens=350
    )
"""

import sys
import os

# ---------------------------------------------------------------------------
# Импорт analyze_position.py из skills/chess-commentator/scripts/
# ---------------------------------------------------------------------------
_SCRIPT_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "skills", "chess-commentator", "scripts"
)
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

try:
    from analyze_position import full_analysis, format_text_report
    HAS_FULL_ANALYSIS = True
except ImportError:
    HAS_FULL_ANALYSIS = False


# ===================================================================
# SYSTEM PROMPT — единый голос комментатора
# ===================================================================

SYSTEM_PROMPT = """Ты — шахматный комментатор для русскоязычного Telegram-канала о турнирах.

Твой голос — сплав двух великих комментаторов:

Юдит Полгар — боевой дух и перспектива игрока:
- Пишешь от лица человека, который сам играл на высшем уровне
- Показываешь ход мысли игрока: "здесь напрашивается f5 — нужно вскрыть игру для слонов"
- Уверенный, прямой тон — не ходишь вокруг да около
- Акцент на воле к победе, практических решениях за доской
- Оцениваешь через действия игрока, не абстрактно

Яссер Сейраван — глубина и контекст:
- Исторические параллели, когда уместны — "классический мотив, как в партиях Петросяна"
- Объясняешь сложное простым языком, не снижая уровня
- Читатель чувствует, что узнал что-то новое
- Тёплый тон, но без сентиментальности

ЖЁСТКИЕ ПРАВИЛА:
1. НИКОГДА не пиши числовые оценки движка (+1.5, -2.3, Stockfish оценивает). Описывай словами
2. Описывай позицию ТОЛЬКО по данным анализа — НИКОГДА не придумывай расположение фигур
3. Используй ТОЛЬКО данные о пешечной структуре из анализа — не определяй проходные/изолированные самостоятельно
4. Не называй фигуру "пассивной" или "застрявшей" на открытой/полуоткрытой линии
5. Называй игроков по фамилии
6. Не упоминай что ты ИИ
7. Без заголовков, без markdown, без списков — только обычный текст
8. НЕ перечисляй все факторы подряд — выбери 1-2 главных и расскажи историю
9. "Турнир претендентов" (муж.) / "турнир претенденток" (жен.) — НИКОГДА "Кандидаты"
10. "С одинаковым количеством очков" — НИКОГДА "с одинаковыми очками"
11. "Играл чёрными/белыми" — НИКОГДА "взял чёрными/белыми"
12. Для женского турнира — женский род: "играла", "нашла", "ошиблась"

Шкала словесных оценок:
- Равно: "Позиция равная", "Обоюдоострая борьба"
- Чуть лучше: "Чуть приятнее у белых", "Минимальный перевес"
- Перевес: "У белых получше", "Устойчивый позиционный перевес"
- Серьёзно: "Серьёзное преимущество", "Чёрным предстоит защита"
- Близко к выигрышу: "Позиция близка к выигранной", "Нужна только точность"
- Решено: "Вопрос техники", "Позиция решена"

Всегда добавляй динамику — что будет дальше, что может измениться."""


# ===================================================================
# Анализ позиции — обёртка
# ===================================================================

def get_position_analysis(fen: str, white: str = "", black: str = "") -> str:
    """Полный анализ позиции из FEN. Возвращает текстовый отчёт для промпта.
    Если analyze_position.py недоступен — fallback на базовые функции bot.py."""
    if not fen:
        return ""
    if HAS_FULL_ANALYSIS:
        try:
            data = full_analysis(fen)
            report = format_text_report(data, lang="ru")
            return report
        except Exception as e:
            print(f"[commentary_prompts] full_analysis error: {e}")
    return ""


# ===================================================================
# Шаблоны промптов по типам постов
# ===================================================================

def _position_block(fen: str, white: str = "", black: str = "") -> str:
    """Генерирует блок анализа позиции для вставки в промпт."""
    analysis = get_position_analysis(fen, white, black)
    if analysis:
        return f"\n=== АНАЛИЗ ПОЗИЦИИ (точные данные — используй ТОЛЬКО их) ===\n{analysis}\n"
    return ""


def build_pulse_prompt(
    white: str, black: str, label: str,
    move_count: int, eval_str: str, moves_san: list[str],
    fen: str, clock_white: str = "?", clock_black: str = "?",
    time_note: str = ""
) -> tuple[str, str]:
    """Промпт для pulse-апдейта. Возвращает (system, user)."""
    moves = ", ".join(moves_san)
    pos_block = _position_block(fen, white, black)

    user = f"""Партия: {white} (белые) – {black} (чёрные), идёт {label}
Ход: {move_count} | Оценка: {eval_str}
Часы: {white} — {clock_white}, {black} — {clock_black}
{time_note}
Последние ходы: {moves}
{pos_block}
Напиши ровно 2 предложения: что сейчас происходит на доске и кто выглядит лучше. Покажи позицию глазами игрока — что бы ты чувствовал за доской."""

    return SYSTEM_PROMPT, user


def build_eval_swing_prompt(
    white: str, black: str,
    move_count: int, best_move: str, moves_san: list[str],
    fen: str, eval_num: float, event_type: str,
    time_note: str = "",
    missed_note: str = "",
    history_note: str = "",
    opening_info: dict | None = None
) -> tuple[str, str]:
    """Промпт для eval_swing, eval_swing_missed, game_over. Возвращает (system, user)."""
    moves = ", ".join(moves_san)
    pos_block = _position_block(fen, white, black)

    # Качественное описание
    if abs(eval_num) < 0.5:
        position_desc = "позиция примерно равная"
    elif eval_num >= 0.5 and eval_num < 1.5:
        position_desc = f"у белых ({white}) небольшой перевес"
    elif eval_num >= 1.5:
        position_desc = f"у белых ({white}) серьёзное преимущество"
    elif eval_num <= -0.5 and eval_num > -1.5:
        position_desc = f"у чёрных ({black}) небольшой перевес"
    else:
        position_desc = f"у чёрных ({black}) серьёзное преимущество"

    event_desc = {
        "eval_swing": f"резкое изменение позиции — {position_desc}",
        "eval_swing_missed": f"преимущество упущено, {position_desc}",
        "game_over": f"партия завершена",
    }.get(event_type, event_type)

    opening_line = ""
    if opening_info:
        op_name = opening_info.get("opening") or opening_info.get("eco") or ""
        eco = opening_info.get("eco", "")
        if op_name:
            opening_line = f"\nДебют: {op_name}" + (f" ({eco})" if eco and eco != op_name else "")

    user = f"""Партия: {white} (белые) – {black} (чёрные){opening_line}
Ход: {move_count} | Лучший ход по движку: {best_move}
Последние ходы: {moves}{time_note}
Событие: {event_desc}{missed_note}{history_note}
{pos_block}
2–3 предложения: что случилось и что ожидать дальше. Покажи момент глазами игрока — что он видел, какое решение принял и почему это важно."""

    return SYSTEM_PROMPT, user


def build_turning_point_prompt(
    white: str, black: str, result_ru: str,
    what_happened: str, fen: str
) -> tuple[str, str]:
    """Промпт для turning_point. Возвращает (system, user)."""
    pos_block = _position_block(fen, white, black)

    user = f"""Партия: {white} (белые) – {black} (чёрные), результат: {result_ru}
Что произошло: {what_happened}
{pos_block}
2–3 предложения: почему этот ход сильный/слабый и что он изменил. Объясни через понимание позиции — что увидел игрок и как это сработало. Если уместно — историческая параллель."""

    return SYSTEM_PROMPT, user


def build_opening_analysis_prompt(
    white: str, black: str,
    opening: str, eco: str, first_moves: list[str],
    white_time: str, black_time: str,
    white_rep: list[str], black_rep: list[str]
) -> tuple[str, str]:
    """Промпт для opening_analysis (15-минутный дебютный разбор). Возвращает (system, user)."""
    user = f"""Партия: {white} (белые) vs {black} (чёрные)
Дебют: {opening} (ECO: {eco})
Первые ходы: {' '.join(first_moves[:8])}
Остаток времени: {white} — {white_time} | {black} — {black_time}

Репертуар {white} за белых: {', '.join(white_rep[:10]) or 'нет данных'}
Репертуар {black} за чёрных: {', '.join(black_rep[:10]) or 'нет данных'}

3 предложения максимум: свой ли дебют для каждого, кто выглядит увереннее и что говорит расход времени. Оцени как гроссмейстер за доской — что этот выбор дебюта говорит о намерениях каждого игрока."""

    return SYSTEM_PROMPT, user


def build_new_game_prompt(
    white: str, black: str,
    move_count: int, best_move: str, moves_san: list[str],
    fen: str, event_type: str,
    time_note: str = "",
    opening_info: dict | None = None
) -> tuple[str, str]:
    """Промпт для new_game / novelty. Возвращает (system, user)."""
    moves = ", ".join(moves_san)
    pos_block = _position_block(fen, white, black)

    event_desc = {
        "new_game": f"партия началась, ход {move_count}",
        "novelty":  f"дебют завершился рано, ход {move_count}",
    }.get(event_type, event_type)

    opening_line = ""
    if opening_info:
        op_name = opening_info.get("opening") or opening_info.get("eco") or ""
        eco = opening_info.get("eco", "")
        if op_name:
            opening_line = f"\nДебют (из PGN): {op_name}" + (f" ({eco})" if eco and eco != op_name else "")

    user = f"""Партия: {white} (белые) – {black} (чёрные){opening_line}
Ход: {move_count} | Лучший ход: {best_move}
Последние ходы: {moves}{time_note}
Событие: {event_desc}
{pos_block}
2–3 предложения: охарактеризуй дебют и чего ожидать. Что выбор дебюта говорит о намерениях игроков — кто играет на победу, кто готов к длинной борьбе."""

    return SYSTEM_PROMPT, user


def build_round_summary_prompt(
    round_name: str, results_for_claude: list[str],
    standings_text: str, is_women: bool = False
) -> tuple[str, str]:
    """Промпт для round_summary. Возвращает (system, user)."""
    tournament = "женского турнира претендентов" if is_women else "турнира претендентов"
    hashtag = "#турнир_претенденток" if is_women else "#турнир_претендентов"

    gender_rules = ""
    if is_women:
        gender_rules = """
- Используй женский род глаголов: "играла", "ошиблась", "нашла", "выиграла", "выпустила"
- "выпустила перевес", "перехватила инициативу" — женский род"""

    user = f"""Напиши итоги {round_name} {tournament} 2026.

Результаты:
{chr(10).join(results_for_claude)}

Таблица после этого тура: {standings_text}

Правила для этого поста:
- 2–3 предложения на каждую партию: дебют, переломный момент, характер борьбы
- Переломные моменты через действие игрока: "выпустил перевес", "перехватил инициативу" — НЕ через цифры
- ВАЖНО: логика субъекта — если А ошибся, перевес у Б. Если перевес не сконвертирован — это Б не сконвертировал
- НЕ описывай фигуры на доске — это итоги, не анализ позиции
- Если несколько партий начались одним дебютом — обыграй: "Второй Каталон тура..."
- В конце — 1–2 фразы турнирной интриги с ТОЧНЫМИ очками из таблицы{gender_rules}
- В самом конце новой строкой: {hashtag}"""

    return SYSTEM_PROMPT, user


def build_round_preview_prompt(
    pairs_text: str, num_pairs: int
) -> tuple[str, str]:
    """Промпт для round_preview. Возвращает (system, user)."""
    user = f"""Превью тура. Пары (с результатами этого турнира):
{pairs_text}

Для КАЖДОЙ пары — РОВНО 2 предложения:
1) Счёт в этом турнире + факт о стиле/дебютной специализации
2) Чего ожидать — как гроссмейстер-аналитик, который знает обоих игроков

Формат — через пустую строку между парами:
*Белый – Чёрный*: X:Y в этом турнире. [Факт].
[Прогноз].

Строго 2 предложения на пару, ровно {num_pairs} пар — пропускать нельзя."""

    return SYSTEM_PROMPT, user


# ===================================================================
# Универсальная точка входа
# ===================================================================

def build_prompt(event_type: str, **kwargs) -> tuple[str, str]:
    """Универсальный роутер — возвращает (system, user) для любого типа поста.

    Аргументы зависят от типа:
        pulse: white, black, label, move_count, eval_str, moves_san, fen, clock_white, clock_black, time_note
        eval_swing/eval_swing_missed/game_over: white, black, move_count, best_move, moves_san, fen, eval_num, ...
        turning_point: white, black, result_ru, what_happened, fen
        opening_analysis: white, black, opening, eco, first_moves, white_time, black_time, white_rep, black_rep
        new_game/novelty: white, black, move_count, best_move, moves_san, fen, ...
        round_summary: round_name, results_for_claude, standings_text, is_women
        round_preview: pairs_text, num_pairs
    """
    builders = {
        "pulse": build_pulse_prompt,
        "eval_swing": build_eval_swing_prompt,
        "eval_swing_missed": build_eval_swing_prompt,
        "game_over": build_eval_swing_prompt,
        "turning_point": build_turning_point_prompt,
        "opening_analysis": build_opening_analysis_prompt,
        "new_game": build_new_game_prompt,
        "novelty": build_new_game_prompt,
        "round_summary": build_round_summary_prompt,
        "round_preview": build_round_preview_prompt,
    }

    builder = builders.get(event_type)
    if builder is None:
        raise ValueError(f"Unknown event_type: {event_type}")

    # Для builders, которые принимают event_type — передаём, если не уже в kwargs
    if event_type in ("eval_swing", "eval_swing_missed", "game_over", "new_game", "novelty"):
        kwargs.setdefault("event_type", event_type)

    return builder(**kwargs)
