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
    opening_info: dict | None = None,
    result_str: str = "",
) -> tuple[str, str]:
    """Промпт для eval_swing, eval_swing_missed, game_over. Возвращает (system, user).

    result_str используется только для game_over: "1-0", "0-1", "1/2-1/2".
    Передаётся явно в текст промпта, иначе Claude угадывает победителя по
    позиции/оценке и в длинных эндшпилях путает белых и чёрных.
    """
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

    # Для game_over явно прописываем результат, чтобы Claude не путался
    game_over_desc = "партия завершена"
    if event_type == "game_over":
        winner_note = {
            "1-0":     f"победа белых ({white})",
            "0-1":     f"победа чёрных ({black})",
            "1/2-1/2": "ничья",
        }.get(result_str, "")
        if winner_note:
            game_over_desc = f"партия завершена — {winner_note}"

    event_desc = {
        "eval_swing": f"резкое изменение позиции — {position_desc}",
        "eval_swing_missed": f"преимущество упущено, {position_desc}",
        "game_over": game_over_desc,
    }.get(event_type, event_type)

    opening_line = ""
    if opening_info:
        op_name = opening_info.get("opening") or opening_info.get("eco") or ""
        eco = opening_info.get("eco", "")
        if op_name:
            opening_line = f"\nДебют: {op_name}" + (f" ({eco})" if eco and eco != op_name else "")

    result_anchor = ""
    if event_type == "game_over" and result_str in ("1-0", "0-1", "1/2-1/2"):
        result_anchor = (
            "\nКРИТИЧНО: результат указан выше — нельзя написать комментарий, "
            "в котором проигравший «давит» или «выигрывает». Сторона, у которой "
            "в счёте «1», — победитель. Не угадывай по последним ходам."
        )

    # Условная формулировка для выигрышных оценок в eval_swing.
    # Bug-репорт: на eval=Мат в 3 / +5 Claude писал «ход победителя», а через
    # 10 минут партия откатывалась до +0.59 — игрок не нашёл лучшего. Преимущество
    # на доске != победа за доской. Жёстко требуем условную форму.
    winning_anchor = ""
    if event_type == "eval_swing" and abs(eval_num) >= 3:
        winning_anchor = (
            "\nКРИТИЧНО: оценка выигрышная, но партия НЕ окончена. Лучший ход — "
            "ещё не сыгранный шанс, а не итог. Формулируй условно: «если найдёт», "
            "«нужна точность», «преимущество требует реализации», «шанс закрутить». "
            "ЗАПРЕЩЕНЫ слова «победитель», «выиграл», «закрыл партию», «ход "
            "победителя», «решено» — пока ход не сделан, ничего не решено."
        )

    # Game over — отдельный пост-результат, не разбор. Глубокая раскадровка
    # переломного момента уходит в отдельный пост 🔍 *Разбор* с диаграммой
    # в нужной позиции (см. send_game_analysis в bot.py). В game_over описывать
    # конкретный ход/момент без диаграммы — значит писать вслепую.
    game_over_anchor = ""
    if event_type == "game_over":
        game_over_anchor = (
            "\nКРИТИЧНО: это пост-результат, НЕ разбор. ЗАПРЕЩЕНО описывать "
            "конкретный ход, момент перелома, фразы вроде «где-то на N-м ходу», "
            "«ход X решил всё», «именно тогда…» — для этого есть отдельный пост "
            "🔍 *Разбор* с диаграммой в нужной позиции. Здесь — 2 коротких "
            "предложения: характер партии (длинный эндшпиль / дебютная "
            "подготовка / атака на короля / техническая реализация) + ОДНА "
            "общая фраза о турнирном смысле результата. Без конкретики ходов."
        )

    user = f"""Партия: {white} (белые) – {black} (чёрные){opening_line}
Ход: {move_count} | Лучший ход по движку: {best_move}
Последние ходы: {moves}{time_note}
Событие: {event_desc}{missed_note}{history_note}
{pos_block}
2–3 предложения: что случилось и что ожидать дальше. Покажи момент глазами игрока — что он видел, какое решение принял и почему это важно.{result_anchor}{winning_anchor}{game_over_anchor}"""

    return SYSTEM_PROMPT, user


def build_turning_point_prompt(
    white: str, black: str, result_ru: str,
    what_happened: str, fen: str,
    followup_line: str = "",
) -> tuple[str, str]:
    """Промпт для turning_point. Возвращает (system, user).

    followup_line — конкретные ходы СОПЕРНИКА после переломного момента
    (5 полуходов с динамикой оценки). Без этого Claude обрывает разбор на
    общем «соперник этим воспользовался», не показывая КАК.
    """
    pos_block = _position_block(fen, white, black)

    followup_block = ""
    if followup_line:
        followup_block = (
            f"\nПродолжение партии (ходы СРАЗУ после переломного, "
            f"с динамикой оценки):\n{followup_line}\n"
            f"Используй эти ходы, чтобы рассказать КОНКРЕТНО, как именно "
            f"соперник воспользовался ошибкой / удержал найденный ресурс."
        )

    user = f"""Партия: {white} (белые) – {black} (чёрные), результат: {result_ru}
Что произошло: {what_happened}
{pos_block}{followup_block}
3 предложения:
1) Почему этот ход сильный/слабый — что увидел игрок и как это сработало.
2) КОНКРЕТНО, что соперник сыграл следом и как это закрепило преимущество
   (ссылайся на конкретные ходы из «Продолжения партии», не пиши «решающая
   перестройка» в общих словах).
3) Закрытие — оценка позиции после этих 5 ходов или короткая историческая
   параллель, если уместна.

ЗАПРЕЩЕНО обрывать на «соперник воспользовался» / «получил темп для
решающей игры» / «другой участок доски» без указания конкретного хода."""

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
    standings_text: str, is_women: bool = False,
    tournament_display: str = "",
    hashtag: str = "",
    female_players: list[str] | None = None,
) -> tuple[str, str]:
    """Промпт для round_summary. Возвращает (system, user).

    female_players — русские имена/фамилии женщин в составе. Используется для
    правильного рода глаголов в смешанных турнирах (Sigeman: только Чжу).
    Если is_women=True — все игроки считаются женщинами автоматически.
    """
    # Название и хэштег: либо передали явно (для нового турнира),
    # либо fallback на «турнир претендентов» по флагу is_women.
    if not tournament_display:
        tournament_display = (
            "женского турнира претендентов" if is_women
            else "турнира претендентов"
        )
    if not hashtag:
        hashtag = "#турнир_претенденток" if is_women else "#турнир_претендентов"

    # Список женщин: либо явно передан, либо по умолчанию = все (если is_women).
    if female_players is None:
        female_players = []
    female_set = list(dict.fromkeys(female_players))   # дедуп, сохранить порядок

    gender_block = ""
    if is_women and not female_set:
        # Чисто женский турнир — общий женский род
        gender_block = """
ГЕНДЕР: все игроки — женщины. Используй женский род глаголов:
"играла", "ошиблась", "нашла", "выиграла", "выпустила перевес",
"перехватила инициативу"."""
    elif female_set:
        # Смешанный турнир — точечный список
        women_list = ", ".join(female_set)
        gender_block = f"""
ГЕНДЕР (КРИТИЧНО): женщины в составе — {women_list}. ТОЛЬКО для них женский
род («играла», «ошиблась», «нашла», «выиграла», «соперница», «противница»).
Для всех остальных игроков — мужской («играл», «ошибся», «соперник»).
Не угадывай по имени — используй этот список как единственный источник."""

    # Бывшее «… 2026» убрано: год должен быть в tournament_display, если нужен
    user = f"""Напиши итоги {round_name} — {tournament_display}.

Результаты:
{chr(10).join(results_for_claude)}

Таблица после этого тура: {standings_text}

Правила для этого поста:
- 2–3 предложения на каждую партию: дебют, переломный момент, характер борьбы
- Переломные моменты через действие игрока: "выпустил перевес", "перехватил инициативу" — НЕ через цифры
- ВАЖНО: логика субъекта — если А ошибся, перевес у Б. Если перевес не сконвертирован — это Б не сконвертировал
- НЕ описывай фигуры на доске — это итоги, не анализ позиции
- Если несколько партий начались одним дебютом — обыграй: "Второй Каталон тура..."
- В конце — 1–2 фразы турнирной интриги с ТОЧНЫМИ очками из таблицы{gender_block}
- В самом конце новой строкой: {hashtag}"""

    return SYSTEM_PROMPT, user


def build_round_preview_prompt(
    pairs_text: str, num_pairs: int
) -> tuple[str, str]:
    """Промпт для round_preview. Возвращает (system, user).

    Без блока «История встреч»: турниры не обязательно круговые, а тащить
    H2H по всей классике из внешних баз ненадёжно. Превью держится на
    стиле игроков, дебютной специализации и интриге тура.
    """
    user = f"""Превью тура. Пары:
{pairs_text}

Для КАЖДОЙ пары — РОВНО 2 предложения:
1) Факт о стиле или дебютной специализации одного из соперников (или короткое
   сопоставление стилей — кто во что играет)
2) Чего ожидать в этой партии — как гроссмейстер-аналитик, который знает обоих

ЗАПРЕЩЕНО:
- Упоминать счёт личных встреч, статистику H2H, «они уже играли» и т.п.
- Выдумывать прошлые партии, результаты, годы и места встреч
- Цифровые оценки, проценты, рейтинги
- Местоимения «он/она» без фамилии в начале предложения

Формат — через пустую строку между парами:
*Белый – Чёрный*: [Факт о стиле/дебюте].
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
