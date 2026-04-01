"""
Chess Candidates 2026 — Telegram Bot
Данные: Lichess Broadcast API (OTB турнир)
Stockfish оценка + Claude комментарий на русском
"""

import asyncio
import os
import time
import datetime
import re
import io
import chess
import chess.engine
import chess.pgn
import chess.svg
import cairosvg
import httpx
from telegram import Bot
from anthropic import Anthropic

# ─── НАСТРОЙКИ ────────────────────────────────────────────────
TELEGRAM_TOKEN    = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID  = os.environ["TELEGRAM_CHAT_ID"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
STOCKFISH_PATH    = os.environ.get("STOCKFISH_PATH", "/usr/games/stockfish")

POLL_INTERVAL_SECONDS = 300    # проверять каждые 5 минут
EVAL_SWING_THRESHOLD  = 1.2    # оповещать при изменении оценки ≥ 1.2 пешки
NOVELTY_MOVE_THRESHOLD = 15    # сигнал если дебют кончился до хода 15
OPENING_STATUS_DELAY  = 900    # 15 минут до дебютного анализа

# Lichess Broadcast — FIDE Candidates 2026 (серия целиком)
LICHESS_BROADCAST_ID = "oe4JqS3R"

# ID раундов Open турнира (добавлять по мере появления новых)
# Формат URL: lichess.org/broadcast/fide-candidates-2026-open/round-N/{roundId}
KNOWN_ROUND_IDS = [
    ("uLCZwqAK", "Round 1"),
    ("FRTlzP2X", "Round 2"),
    ("SDizieNR", "Round 3"),
    ("97di6JjX", "Round 4"),
    ("liBI9Brw", "Round 5"),
    ("WmbQrSNz", "Round 6"),
    ("aVbIuZ7Q", "Round 7"),
    ("XYRPyV8x", "Round 8"),
    ("hTVc2Qgj", "Round 9"),
    ("G3oSxPgs", "Round 10"),
    ("seMTFSPr", "Round 11"),
    ("9SHysZuu", "Round 12"),
    ("rFG1W5Tp", "Round 13"),
    ("oBkeHnpi", "Round 14"),
]

# ─── РАСПИСАНИЕ ТУРНИРА ───────────────────────────────────────
# Начало партий: 15:30 Кипр (EEST=UTC+3) = 12:30 UTC = 13:30 Лиссабон = 15:30 Москва
# Дни отдыха: 2, 6, 10, 13 апреля
_UTC = datetime.timezone.utc
ROUND_SCHEDULE = {
    "Round 1":  datetime.datetime(2026, 3, 29, 12, 30, tzinfo=_UTC),
    "Round 2":  datetime.datetime(2026, 3, 30, 12, 30, tzinfo=_UTC),
    "Round 3":  datetime.datetime(2026, 3, 31, 12, 30, tzinfo=_UTC),
    "Round 4":  datetime.datetime(2026, 4,  1, 12, 30, tzinfo=_UTC),
    "Round 5":  datetime.datetime(2026, 4,  3, 12, 30, tzinfo=_UTC),
    "Round 6":  datetime.datetime(2026, 4,  4, 12, 30, tzinfo=_UTC),
    "Round 7":  datetime.datetime(2026, 4,  5, 12, 30, tzinfo=_UTC),
    "Round 8":  datetime.datetime(2026, 4,  7, 12, 30, tzinfo=_UTC),
    "Round 9":  datetime.datetime(2026, 4,  8, 12, 30, tzinfo=_UTC),
    "Round 10": datetime.datetime(2026, 4,  9, 12, 30, tzinfo=_UTC),
    "Round 11": datetime.datetime(2026, 4, 11, 12, 30, tzinfo=_UTC),
    "Round 12": datetime.datetime(2026, 4, 12, 12, 30, tzinfo=_UTC),
    "Round 13": datetime.datetime(2026, 4, 14, 12, 30, tzinfo=_UTC),
    "Round 14": datetime.datetime(2026, 4, 15, 12, 30, tzinfo=_UTC),
}
REST_DAYS = {"2026-04-02", "2026-04-06", "2026-04-10", "2026-04-13"}

# Русские имена игроков (для комментариев и сообщений)
PLAYER_NAMES_RU = {
    "Caruana":        "Каруана",
    "Nakamura":       "Накамура",
    "Giri":           "Гири",
    "Praggnanandhaa": "Прагг",
    "Sindarov":       "Синдаров",
    "Wei":            "Вэй И",
    "Esipenko":       "Есипенко",
    "Bluebaum":       "Блюбаум",
}

# Маппинг фамилий из PGN → username на chess.com для репертуара
PLAYER_CHESS_COM = {
    "Caruana":        "fabiano-caruana",
    "Nakamura":       "hikaru",
    "Giri":           "anish-giri",
    "Praggnanandhaa": "praggnanandhaa",
    "Sindarov":       "sindarov",
    "Wei":            "wei-yi-cn",
    "Esipenko":       "esipenko",
    "Bluebaum":       "bluebaum",
}

# ─── СОСТОЯНИЕ ────────────────────────────────────────────────
seen_games          = {}   # game_id → последний eval_data
game_start_times    = {}   # game_id → timestamp первого обнаружения
games_15min_done    = set()
games_over_sent     = set()  # game_id → уже отправили game_over
games_swing_move    = {}   # game_id → ход последнего eval_swing (кулдаун)
announced_rounds    = set()  # round_id → объявили старт тура
round_summary_done  = set()  # round_id → уже отправили итог тура

# ─── LICHESS API ──────────────────────────────────────────────
async def get_active_round_id() -> tuple[str | None, str | None]:
    """Найти активный раунд Open турнира через PGN endpoint.
    JSON /api/broadcast/round/{id} возвращает 404 — используем только .pgn endpoint.
    Идём от последнего раунда к первому, возвращаем первый где не все партии завершены."""
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        for rid, rname in reversed(KNOWN_ROUND_IDS):
            try:
                r = await client.get(
                    f"https://lichess.org/api/broadcast/round/{rid}.pgn",
                    headers={"User-Agent": "CandidatesBot/1.0"}
                )
                if r.status_code == 200 and r.text.strip():
                    games = split_pgn(r.text)
                    if not games:
                        continue
                    # Раунд считается начавшимся только если есть ходы хотя бы в одной партии
                    started = [g for g in games if count_moves_pgn(g) > 0]
                    if not started:
                        print(f"{rname} ({rid}): {len(games)} партий, ещё не началось")
                        continue
                    finished_count = sum(1 for g in games if is_game_finished(g))
                    print(f"{rname} ({rid}): {len(games)} партий, {finished_count} завершено")
                    # Раунд активен если хотя бы одна партия ещё не завершена
                    if finished_count < len(games):
                        return rid, rname
                    # Все завершены — этот раунд закончен, продолжаем поиск назад
                elif r.status_code == 404:
                    print(f"{rname} ({rid}): раунд ещё не создан (404)")
                    continue
            except Exception as e:
                print(f"Ошибка проверки раунда {rname} ({rid}): {e}")

    # Все известные раунды завершены — возвращаем последний
    last_rid, last_rname = KNOWN_ROUND_IDS[-1]
    print(f"Все раунды завершены, используем последний: {last_rname}")
    return last_rid, last_rname


async def get_round_pgns(round_id: str) -> tuple[list[str], str]:
    """Получить PGN всех партий раунда с Lichess. Возвращает (games, debug_info).
    Правильный endpoint: /api/broadcast/round/{id}.pgn
    """
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        # Основной endpoint (правильный формат Lichess API)
        url = f"https://lichess.org/api/broadcast/round/{round_id}.pgn"
        try:
            r = await client.get(url, headers={"User-Agent": "CandidatesBot/1.0"})
            debug = f"HTTP {r.status_code}, {len(r.text)} байт [{url}]"
            print(f"get_round_pgns: {debug}")
            if r.status_code == 200 and r.text.strip():
                games = split_pgn(r.text)
                return games, debug + f" → {len(games)} партий"
            # Fallback: попробовать через весь broadcast
            r2 = await client.get(
                f"https://lichess.org/api/broadcast/{LICHESS_BROADCAST_ID}.pgn",
                headers={"User-Agent": "CandidatesBot/1.0"}
            )
            debug2 = f"fallback HTTP {r2.status_code}, {len(r2.text)} байт"
            if r2.status_code == 200 and r2.text.strip():
                games = split_pgn(r2.text)
                return games, debug2 + f" → {len(games)} партий"
            preview = r.text[:150].replace('\n', ' ')
            return [], f"{debug} | Ответ: {preview}"
        except Exception as e:
            print(f"Lichess round games error: {e}")
            return [], f"Ошибка: {e}"


def split_pgn(multi_pgn: str) -> list[str]:
    """Разбить мульти-PGN на отдельные партии."""
    games, current = [], []
    for line in multi_pgn.splitlines():
        if line.startswith("[Event ") and current:
            games.append("\n".join(current))
            current = [line]
        else:
            current.append(line)
    if current:
        games.append("\n".join(current))
    return [g for g in games if g.strip()]


def pgn_to_game_data(pgn_text: str) -> dict:
    """Создать game_data из PGN-заголовков. Имена переводятся в русские."""
    white_m = re.search(r'\[White "([^"]+)"\]', pgn_text)
    black_m = re.search(r'\[Black "([^"]+)"\]', pgn_text)
    white_en = white_m.group(1).split(",")[0].strip() if white_m else "White"
    black_en = black_m.group(1).split(",")[0].strip() if black_m else "Black"
    white = PLAYER_NAMES_RU.get(white_en, white_en)
    black = PLAYER_NAMES_RU.get(black_en, black_en)
    return {"white": {"username": white}, "black": {"username": black}}


def pgn_game_id(pgn_text: str) -> str:
    """Уникальный ID партии из PGN (White+Black)."""
    white_m = re.search(r'\[White "([^"]+)"\]', pgn_text)
    black_m = re.search(r'\[Black "([^"]+)"\]', pgn_text)
    w = white_m.group(1) if white_m else "?"
    b = black_m.group(1) if black_m else "?"
    return f"{w}_vs_{b}"


# ─── CHESS.COM — РЕПЕРТУАР ИГРОКА ─────────────────────────────
async def get_player_recent_openings(last_name: str, color: str) -> list[str]:
    """Получить типичные дебюты игрока из архива chess.com."""
    chess_com_user = PLAYER_CHESS_COM.get(last_name)
    if not chess_com_user:
        return []
    openings = []
    now = datetime.datetime.utcnow()
    async with httpx.AsyncClient(timeout=15) as client:
        for delta in range(2):
            dt = now - datetime.timedelta(days=30 * delta)
            url = f"https://api.chess.com/pub/player/{chess_com_user}/games/{dt.year}/{dt.month:02d}"
            try:
                r = await client.get(url, headers={"User-Agent": "CandidatesBot/1.0"})
                if r.status_code == 200:
                    for g in r.json().get("games", []):
                        if g.get("time_class") not in ("classical", "rapid"):
                            continue
                        w_name = g.get("white", {}).get("username", "").lower()
                        player_color = "white" if w_name == chess_com_user.lower() else "black"
                        if player_color != color:
                            continue
                        pgn_text = g.get("pgn", "")
                        m = re.search(r'\[Opening "([^"]+)"\]', pgn_text) or \
                            re.search(r'\[ECO "([^"]+)"\]', pgn_text)
                        if m:
                            openings.append(m.group(1))
            except Exception:
                pass
    return openings[:30]


# ─── АНАЛИЗ ДЕБЮТА ────────────────────────────────────────────
def extract_opening_info(pgn_text: str) -> dict:
    """Извлечь дебют, ходы и время из PGN."""
    result = {"opening": None, "eco": None, "first_moves": [],
              "white_time_remaining": None, "black_time_remaining": None}

    for tag, key in [("Opening", "opening"), ("ECO", "eco")]:
        m = re.search(rf'\[{tag} "([^"]+)"\]', pgn_text)
        if m:
            result[key] = m.group(1)

    try:
        game = chess.pgn.read_game(io.StringIO(pgn_text))
        if game:
            board, moves, node = game.board(), [], game
            while node.variations and len(moves) < 10:
                node = node.variations[0]
                moves.append(board.san(node.move))
                board.push(node.move)
            result["first_moves"] = moves
    except Exception:
        pass

    clk = re.findall(r'\[%clk (\d+:\d+:\d+|\d+:\d+)\]', pgn_text)
    def fmt(t):
        p = t.split(":")
        if len(p) == 3:
            return f"{int(p[0])*60+int(p[1])}м {int(p[2])}с"
        return f"{p[0]}м {p[1]}с"
    if len(clk) >= 2:
        result["white_time_remaining"] = fmt(clk[0::2][-1])
        result["black_time_remaining"] = fmt(clk[1::2][-1])

    return result


def analyze_clocks(pgn_text: str) -> dict:
    """Анализ часов: остаток времени у каждого игрока + самый долгий ход.
    Учитывает инкремент из заголовка TimeControl (например 7200+30)."""
    try:
        game = chess.pgn.read_game(io.StringIO(pgn_text))
        if not game:
            return {}

        # Инкремент из TimeControl (ФИДЕ Кандидаты: 7200+30)
        tc = game.headers.get("TimeControl", "7200+30")
        try:
            parts = tc.split("+")
            initial = float(parts[0])
            increment = float(parts[1]) if len(parts) > 1 else 0.0
        except Exception:
            initial, increment = 7200.0, 30.0

        def fmt(secs):
            if secs is None or secs < 0:
                return "?"
            secs = int(secs)
            h, rem = divmod(secs, 3600)
            m, s = divmod(rem, 60)
            return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"

        # Собираем ходы с часами
        node = game
        white_clocks, black_clocks = [], []   # остаток после хода (секунды)
        move_sans = []
        while node.variations:
            node = node.variations[0]
            clk = node.clock()   # секунды остатка или None
            move_sans.append(node.san())
            if len(move_sans) % 2 == 1:
                white_clocks.append(clk)
            else:
                black_clocks.append(clk)

        # Текущий остаток
        white_rem = white_clocks[-1] if white_clocks else None
        black_rem = black_clocks[-1] if black_clocks else None

        # Самый долгий ход (с учётом инкремента: потрачено = prev + inc - current)
        longest_secs = 0
        longest = None

        prev_w = initial
        for i, clk in enumerate(white_clocks):
            if clk is not None and prev_w is not None:
                spent = prev_w + increment - clk
                if spent > longest_secs:
                    longest_secs = spent
                    san = move_sans[i * 2] if i * 2 < len(move_sans) else "?"
                    longest = {"move_num": i + 1, "san": san,
                               "color": "white", "secs": spent}
            if clk is not None:
                prev_w = clk

        prev_b = initial
        for i, clk in enumerate(black_clocks):
            if clk is not None and prev_b is not None:
                spent = prev_b + increment - clk
                if spent > longest_secs:
                    longest_secs = spent
                    san = move_sans[i * 2 + 1] if i * 2 + 1 < len(move_sans) else "?"
                    longest = {"move_num": i + 1, "san": san,
                               "color": "black", "secs": spent}
            if clk is not None:
                prev_b = clk

        return {
            "white_rem": fmt(white_rem),
            "black_rem": fmt(black_rem),
            "longest": longest,
            "longest_str": fmt(longest_secs) if longest else None,
        }
    except Exception as e:
        print(f"Clock analysis error: {e}")
        return {}


def get_board_png(pgn_text: str) -> bytes | None:
    """Сгенерировать PNG изображение доски из PGN."""
    try:
        game = chess.pgn.read_game(io.StringIO(pgn_text))
        if not game:
            return None
        board = game.board()
        last_move = None
        for move in game.mainline_moves():
            last_move = move
            board.push(move)
        svg_text = chess.svg.board(
            board=board,
            lastmove=last_move,
            size=400,
            colors={"square light": "#f0d9b5", "square dark": "#b58863",
                    "square light lastmove": "#cdd16e", "square dark lastmove": "#aaa23b"}
        )
        png_bytes = cairosvg.svg2png(bytestring=svg_text.encode())
        return png_bytes
    except Exception as e:
        print(f"Board image error: {e}")
        return None


def count_moves_pgn(pgn_text: str) -> int:
    """Посчитать число полуходов в партии из PGN (без Stockfish)."""
    try:
        game = chess.pgn.read_game(io.StringIO(pgn_text))
        if game:
            return len(list(game.mainline_moves()))
    except Exception:
        pass
    # Fallback: считаем номера ходов из текста
    nums = re.findall(r'(\d+)\.', pgn_text)
    return int(nums[-1]) * 2 if nums else 0


# ─── STOCKFISH ────────────────────────────────────────────────
def evaluate_position(pgn_text: str) -> dict | None:
    try:
        game = chess.pgn.read_game(io.StringIO(pgn_text))
        if not game:
            return None
        board = game.board()
        moves = list(game.mainline_moves())
        for m in moves:
            board.push(m)

        with chess.engine.SimpleEngine.popen_uci(STOCKFISH_PATH) as engine:
            info = engine.analyse(board, chess.engine.Limit(depth=20))

        score = info["score"].white()
        if score.is_mate():
            eval_num = 99.0 if score.mate() > 0 else -99.0
            eval_str = f"Мат в {score.mate()}"
        else:
            eval_num = score.score() / 100.0
            eval_str = f"{eval_num:+.2f}"

        best = info.get("pv", [None])[0]
        # Вычислить SAN ходов заново — game.board() даёт начальную позицию,
        # нельзя звать san() для ходов середины партии на ней
        temp_board = game.board()
        all_san = []
        for m in moves:
            try:
                all_san.append(temp_board.san(m))
            except Exception:
                all_san.append(m.uci())
            temp_board.push(m)

        return {
            "eval_num":   eval_num,
            "eval_str":   eval_str,
            "best_move":  board.san(best) if best else "—",
            "move_count": len(moves),
            "moves_san":  all_san[-10:],
        }
    except Exception as e:
        print(f"Stockfish error: {e}")
        return None


# ─── CLAUDE ───────────────────────────────────────────────────
def get_gm_commentary(game_data: dict, eval_data: dict, event_type: str) -> str:
    client = Anthropic(api_key=ANTHROPIC_API_KEY)
    white = game_data["white"]["username"]
    black = game_data["black"]["username"]
    moves = ', '.join(eval_data.get('moves_san', []))

    if event_type in ("eval_swing", "game_over"):
        # Стиль: аналитик с мнением — конкретные оценки, предсказания, лёгкая провокация
        event_desc = {
            "eval_swing": f"оценка резко изменилась до {eval_data['eval_str']}",
            "game_over":  "партия завершена",
        }.get(event_type, event_type)
        prompt = f"""Ты — шахматный аналитик с характером, комментируешь турнир претендентов 2026 для Telegram-канала.

Партия: {white} (белые) – {black} (чёрные)
Ход: {eval_data['move_count']} | Оценка: {eval_data['eval_str']} | Лучший ход: {eval_data['best_move']}
Последние ходы: {moves}
Событие: {event_desc}

Напиши 3–4 предложения в стиле: конкретный факт о позиции → твоя оценка ситуации с мнением → прогноз или интрига.
Пиши уверенно, можно с лёгкой иронией. Называй игроков по фамилии. Не упоминай что ты ИИ.
Без заголовков, списков и markdown-форматирования. Только обычный текст."""
        max_tokens = 350

    else:
        # Стиль: телеграфный — только факты, коротко и по делу
        event_desc = {
            "new_game": f"партия началась, ход {eval_data['move_count']}",
            "novelty":  f"дебют завершился рано, ход {eval_data['move_count']}",
        }.get(event_type, event_type)
        prompt = f"""Ты — шахматный комментатор, пишешь для Telegram-канала о турнире претендентов 2026.

Партия: {white} (белые) – {black} (чёрные)
Ход: {eval_data['move_count']} | Оценка: {eval_data['eval_str']} | Лучший ход: {eval_data['best_move']}
Последние ходы: {moves}
Событие: {event_desc}

Напиши 2–3 коротких фактических предложения: что происходит на доске и почему это важно.
Никакой воды, только конкретика. Называй игроков по фамилии. Не упоминай что ты ИИ.
Без заголовков, списков и markdown-форматирования. Только обычный текст."""
        max_tokens = 220

    r = client.messages.create(
        model="claude-sonnet-4-6", max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}]
    )
    return r.content[0].text


async def get_opening_analysis(game_data: dict, opening_info: dict,
                                white_rep: list, black_rep: list) -> str:
    client = Anthropic(api_key=ANTHROPIC_API_KEY)
    white = game_data["white"]["username"]
    black = game_data["black"]["username"]

    prompt = f"""Ты — гроссмейстер, анализируешь дебют на турнире претендентов 2026. Прошло 15 минут.

Партия: {white} (белые) vs {black} (чёрные)
Дебют: {opening_info.get('opening') or opening_info.get('eco') or 'неизвестен'} (ECO: {opening_info.get('eco','—')})
Первые ходы: {' '.join(opening_info.get('first_moves',[])[:8])}
Остаток времени: {white} — {opening_info.get('white_time_remaining','?')} | {black} — {opening_info.get('black_time_remaining','?')}

Репертуар {white} за белых: {', '.join(white_rep[:10]) or 'нет данных'}
Репертуар {black} за чёрных: {', '.join(black_rep[:10]) or 'нет данных'}

3 предложения максимум: свой ли дебют для каждого, кто выглядит увереннее и что говорит расход времени.
Никакой воды. Пиши по-русски, без заголовков, без списков, без markdown-форматирования. Только обычный текст. Не упоминай что ты ИИ."""

    r = client.messages.create(
        model="claude-sonnet-4-6", max_tokens=280,
        messages=[{"role": "user", "content": prompt}]
    )
    return r.content[0].text


# ─── TELEGRAM ─────────────────────────────────────────────────
async def send_update(bot: Bot, message: str):
    """Отправить сообщение. При ошибке Markdown — повторить без форматирования."""
    # Telegram text max = 4096 символов
    message = message[:4090] + "…" if len(message) > 4096 else message
    try:
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message, parse_mode="Markdown")
    except Exception as e:
        print(f"Markdown send error: {e} — retrying as plain text")
        try:
            plain = re.sub(r'[*_`]', '', message)
            await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=plain)
        except Exception as e2:
            print(f"Plain text send also failed: {e2}")


async def send_update_with_photo(bot: Bot, message: str, pgn: str):
    """Отправить сообщение с изображением доски. При ошибке — текст без картинки."""
    png = get_board_png(pgn)
    if png:
        # Telegram caption max = 1024 символа
        caption = message[:1020] + "…" if len(message) > 1024 else message
        try:
            await bot.send_photo(
                chat_id=TELEGRAM_CHAT_ID,
                photo=png,
                caption=caption,
                parse_mode="Markdown"
            )
            return
        except Exception as e:
            print(f"Photo send error: {e}")
    await send_update(bot, message)


def is_game_finished(pgn_text: str) -> bool:
    """Партия завершена если результат не '*'."""
    m = re.search(r'\[Result "([^"]+)"\]', pgn_text)
    return bool(m) and m.group(1) in ("1-0", "0-1", "1/2-1/2")


def get_game_result(pgn_text: str) -> str:
    m = re.search(r'\[Result "([^"]+)"\]', pgn_text)
    return m.group(1) if m else "*"


async def send_round_summary(bot: Bot, round_name: str, games_pgn: list[str]):
    """Отправить итоговый разбор тура после завершения всех партий."""
    results_lines = []
    results_for_claude = []
    for pgn in games_pgn:
        gd = pgn_to_game_data(pgn)
        w, b = gd["white"]["username"], gd["black"]["username"]
        res = get_game_result(pgn)
        info = extract_opening_info(pgn)
        n_moves = count_moves_pgn(pgn)
        n_moves_str = str(n_moves) if n_moves else "?"
        icon = "½" if res == "1/2-1/2" else ("1–0" if res == "1-0" else "0–1")
        opening = info.get("opening") or info.get("eco") or "неизвестно"
        first_moves = " ".join(info.get("first_moves", [])[:6])
        results_lines.append(f"*{w} – {b}*: {icon} ({n_moves_str} ходов)")
        results_for_claude.append(
            f"• {w} (белые) vs {b} (чёрные): {res}, {n_moves_str} ходов. "
            f"Дебют: {opening}. Первые ходы: {first_moves}."
        )

    # Claude-разбор в стиле шахматного Telegram-канала
    client = Anthropic(api_key=ANTHROPIC_API_KEY)
    prompt = f"""Ты пишешь для русскоязычного шахматного Telegram-канала. Подводишь итоги {round_name} турнира претендентов 2026 в Пафосе.

Результаты:
{chr(10).join(results_for_claude)}

Требования к тексту:
- Стиль: короткие фактические предложения, как в спортивной новостной заметке
- 5-7 предложений, никаких заголовков (#), никаких маркированных списков
- По каждой решающей партии: кто владел инициативой, в каком дебюте, какой был ключевой момент или ошибка, какой эндшпиль
- По ничьим: было ли равно с дебюта или кто-то перевёл
- В конце — одна фраза о турнирной интриге
- Шахматные термины на русском: "ферзевый гамбит", "ладейный эндшпиль", "разноцветные слоны" и т.д.
- В самом конце новой строкой: #турнир_претендентов
- Не упоминай что ты ИИ"""

    r = client.messages.create(
        model="claude-sonnet-4-6", max_tokens=500,
        messages=[{"role": "user", "content": prompt}]
    )
    analysis = r.content[0].text.strip()
    # Убрать случайные markdown-заголовки если Claude всё же добавил
    analysis = re.sub(r'^#+\s+', '', analysis, flags=re.MULTILINE)

    results_block = "\n".join(results_lines)
    msg = (f"🏁 *{round_name} — итоги*\n\n"
           f"{results_block}\n\n"
           f"{analysis}")
    await send_update(bot, msg)


async def get_round_preview(pairs: list[tuple[str, str]]) -> str:
    """Claude генерирует H2H статистику и прогноз для каждой партии тура."""
    client = Anthropic(api_key=ANTHROPIC_API_KEY)
    pairs_text = "\n".join([f"• {w} (белые) – {b} (чёрные)" for w, b in pairs])

    prompt = f"""Ты — шахматный аналитик, пишешь превью тура Кандидатов 2026 для Telegram-канала.

Пары тура:
{pairs_text}

Для каждой партии напиши строго в таком формате (одна строка на партию):

*Белый – Чёрный*: X:Y. [Факт] — [Прогноз]

Где X:Y — общий шахматный счёт в классических OTB партиях: победа = 1 очко, ничья = 0.5.
Примеры: "3:2.5", "1.5:0.5", "0:1", "первая встреча"

Правила:
- Счёт ВСЕГДА в формате X:Y через двоеточие, только цифры — никаких слов
- Если встреч мало или нет — "первая встреча" или "N партий: X:Y"
- Данные ОБЯЗАТЕЛЬНО для всех 4 пар
- Факт: дебютная специализация, характерный результат, особенность стиля — одно предложение
- Прогноз: острая борьба / позиционная игра / теоретическая дуэль — одно предложение
- Только реальные факты
- Не упоминай что ты ИИ
- Никаких заголовков — только 4 строки"""

    r = client.messages.create(
        model="claude-sonnet-4-6", max_tokens=500,
        messages=[{"role": "user", "content": prompt}]
    )
    return r.content[0].text.strip()


async def send_round_start(bot: Bot, round_name: str, games_pgn: list[str]):
    today = datetime.datetime.utcnow().strftime("%-d %B %Y")
    pairs = []
    for pgn in games_pgn:
        gd = pgn_to_game_data(pgn)
        w, b = gd["white"]["username"], gd["black"]["username"]
        pairs.append((w, b))

    # Время начала по Лиссабону и Москве
    start_utc = ROUND_SCHEDULE.get(round_name)
    time_line = ""
    if start_utc:
        lisbon = (start_utc + datetime.timedelta(hours=1)).strftime("%H:%M")
        moscow = (start_utc + datetime.timedelta(hours=3)).strftime("%H:%M")
        time_line = f"\n🕐 *{lisbon} Лиссабон / {moscow} Москва*\n"

    # H2H прогноз от Claude
    try:
        preview = await get_round_preview(pairs)
    except Exception as e:
        print(f"Preview error: {e}")
        preview = ""

    pairs_lines = "\n".join([f"• *{w}* — *{b}*" for w, b in pairs])

    msg = (f"🏆 *Турнир Претендентов — {round_name}*\n"
           f"_{today}_"
           f"{time_line}\n"
           f"Пары тура:\n{pairs_lines}\n\n"
           f"📊 *История встреч в классике:*\n{preview}\n\n"
           f"Слежу за всеми партиями 📡")
    await send_update(bot, msg)


async def send_15min_status(bot: Bot, game_data: dict, pgn: str):
    info = extract_opening_info(pgn)
    white = game_data["white"]["username"]
    black = game_data["black"]["username"]

    white_rep, black_rep = await asyncio.gather(
        get_player_recent_openings(white, "white"),
        get_player_recent_openings(black, "black"),
    )
    analysis = await get_opening_analysis(game_data, info, white_rep, black_rep)

    opening = info.get("opening") or info.get("eco") or "дебют"
    eco = f" ({info['eco']})" if info.get("eco") else ""
    moves = " ".join(info.get("first_moves", [])[:6])
    wt = info.get("white_time_remaining", "—")
    bt = info.get("black_time_remaining", "—")

    msg = (f"🕐 *{white} — {black}* | 15 минут\n"
           f"Дебют: {opening}{eco}\n"
           f"Ходы: `{moves}`\n"
           f"⏱ {white}: {wt} | {black}: {bt}\n\n"
           f"🧠 {analysis}")
    await send_update_with_photo(bot, msg, pgn)


def format_event_msg(game_data: dict, eval_data: dict, event_type: str,
                     commentary: str, clock_info: dict | None = None) -> str:
    w = game_data["white"]["username"]
    b = game_data["black"]["username"]
    icons = {"eval_swing": "📈", "game_over": "🏁", "new_game": "♟️"}

    # Строка часов — всегда
    clock_line = ""
    if clock_info:
        wr = clock_info.get("white_rem", "?")
        br = clock_info.get("black_rem", "?")
        clock_line = f"\n⏱ {w}: `{wr}` | {b}: `{br}`"

        # Долгий ход — только для ключевых событий
        if event_type in ("eval_swing", "game_over") and clock_info.get("longest"):
            lt = clock_info["longest"]
            thinker = w if lt["color"] == "white" else b
            clock_line += (f"\n🤔 Дольше всего думал: *{thinker}* — "
                           f"ход {lt['move_num']}. {lt['san']} ({clock_info['longest_str']})")

    return (f"{icons.get(event_type,'♟️')} *{w} — {b}*\n"
            f"Ход {eval_data['move_count']} | Оценка: `{eval_data['eval_str']}` | "
            f"Лучший: `{eval_data['best_move']}`"
            f"{clock_line}\n\n"
            f"🧠 {commentary}")


# ─── ГЛАВНЫЙ ЦИКЛ ─────────────────────────────────────────────
async def main():
    bot = Bot(token=TELEGRAM_TOKEN)
    print("✅ Бот запущен. Слежу за турниром претендентов 2026...")

    while True:
        try:
            now = time.time()
            round_id, round_name = await get_active_round_id()

            if not round_id:
                print("Нет активного раунда на Lichess, жду...")
                await asyncio.sleep(POLL_INTERVAL_SECONDS)
                continue

            games_pgn, pgn_debug = await get_round_pgns(round_id)
            print(f"Раунд {round_name} ({round_id}): {pgn_debug}")

            # ── Анонс нового раунда ──────────────────────────
            # Анонсируем только когда хотя бы одна партия реально началась (ход > 0)
            # и не все партии уже завершены (раунд не прошлый)
            games_started = [p for p in games_pgn if count_moves_pgn(p) > 0]
            if (games_started
                    and round_id not in announced_rounds
                    and not all(is_game_finished(p) for p in games_pgn)):
                announced_rounds.add(round_id)
                await send_round_start(bot, round_name, games_pgn)

            for pgn in games_pgn:
                game_id = pgn_game_id(pgn)
                game_data = pgn_to_game_data(pgn)

                if game_id not in game_start_times:
                    game_start_times[game_id] = now

                eval_data = evaluate_position(pgn)
                if not eval_data:
                    continue

                prev = seen_games.get(game_id)
                event_type = None

                # Не отправлять ничего пока партия не началась (ход 0 = игроки ещё не сели)
                if eval_data["move_count"] == 0:
                    seen_games[game_id] = eval_data
                    continue

                # 1. Конец партии — отправить один раз
                if is_game_finished(pgn) and game_id not in games_over_sent and prev is not None:
                    games_over_sent.add(game_id)
                    event_type = "game_over"

                # 2. Новая партия — первое обнаружение с ходами
                elif (prev is None or prev.get("move_count", 0) == 0) and not is_game_finished(pgn):
                    event_type = "new_game"

                # 3. Резкое изменение оценки — кулдаун минимум 5 ходов
                elif prev and abs(eval_data["eval_num"] - prev["eval_num"]) >= EVAL_SWING_THRESHOLD:
                    last_swing = games_swing_move.get(game_id, 0)
                    if eval_data["move_count"] - last_swing >= 5:
                        games_swing_move[game_id] = eval_data["move_count"]
                        event_type = "eval_swing"

                if event_type:
                    commentary = get_gm_commentary(game_data, eval_data, event_type)
                    clock_info = analyze_clocks(pgn)
                    msg = format_event_msg(game_data, eval_data, event_type, commentary, clock_info)
                    await send_update_with_photo(bot, msg, pgn)

                seen_games[game_id] = eval_data

                # ── 15-минутный анализ дебюта ──────────────────
                elapsed = now - game_start_times.get(game_id, now)
                if (elapsed >= OPENING_STATUS_DELAY
                        and game_id not in games_15min_done
                        and eval_data["move_count"] >= 5):
                    games_15min_done.add(game_id)
                    await send_15min_status(bot, game_data, pgn)

            # ── Итоговый разбор тура ─────────────────────────
            if (games_pgn
                    and round_id not in round_summary_done
                    and len(games_pgn) >= 2
                    and all(is_game_finished(p) for p in games_pgn)):
                round_summary_done.add(round_id)
                await send_round_summary(bot, round_name, games_pgn)

        except Exception as e:
            print(f"Loop error: {e}")

        await asyncio.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    asyncio.run(main())
