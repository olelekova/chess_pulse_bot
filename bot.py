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
from telegram import Bot, Update
from telegram.ext import Application, CommandHandler, ContextTypes
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

# Периодические пульс-апдейты (секунды от старта партии)
# 60 мин — миттельшпиль; 120 мин — контроль хода 40; 180 мин — поздняя стадия
PULSE_INTERVALS = [3600, 7200, 10800]
PULSE_LABELS    = {3600: "1 час", 7200: "2 часа", 10800: "3 часа"}

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
games_baseline_eval = {}   # game_id → eval_data на момент последнего уведомления (базовая линия для swing)
game_start_times    = {}   # game_id → timestamp первого обнаружения
games_15min_done    = set()
games_over_sent     = set()  # game_id → уже отправили game_over
games_swing_move    = {}   # game_id → ход последнего eval_swing (кулдаун)
announced_rounds    = set()  # round_id → объявили старт тура
round_summary_done  = set()  # round_id → уже отправили итог тура
games_pulse_sent    = {}   # game_id → set(секунд) уже отправленных пульс-апдейтов

# ─── LICHESS API ──────────────────────────────────────────────
async def get_active_round_id() -> tuple[str | None, str | None]:
    """Найти активный раунд Open турнира через PGN endpoint.
    JSON /api/broadcast/round/{id} возвращает 404 — используем только .pgn endpoint.
    Идём от последнего раунда к первому.
    Приоритет: раунд с незавершёнными партиями. Если такого нет — последний
    завершённый раунд (чтобы бот мог отправить game_over и итоги тура)."""
    latest_finished = None  # самый поздний завершённый раунд
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
                    # Все завершены — запоминаем как самый поздний завершённый
                    if latest_finished is None:
                        latest_finished = (rid, rname)
                elif r.status_code == 404:
                    print(f"{rname} ({rid}): раунд ещё не создан (404)")
                    continue
            except Exception as e:
                print(f"Ошибка проверки раунда {rname} ({rid}): {e}")

    # Нет активного раунда — возвращаем последний завершённый
    # (нужно для отправки game_over и итогов тура)
    if latest_finished:
        print(f"Нет активного раунда, используем последний завершённый: {latest_finished[1]}")
        return latest_finished
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


def normalize_player_name(raw: str) -> str:
    """Извлечь фамилию из PGN-имени и перевести в русское.
    PGN может содержать: 'Praggnanandhaa, R', 'Praggnanandhaa R.',
    'Wei, Yi', 'Wei Yi' — нужно сопоставить с ключами PLAYER_NAMES_RU."""
    # Вариант 1: до запятой
    last_name = raw.split(",")[0].strip()
    if last_name in PLAYER_NAMES_RU:
        return PLAYER_NAMES_RU[last_name]
    # Вариант 2: первое слово (фамилия без инициалов)
    first_word = raw.split()[0].strip().rstrip(",.")
    if first_word in PLAYER_NAMES_RU:
        return PLAYER_NAMES_RU[first_word]
    # Вариант 3: поиск по подстроке
    for key, ru_name in PLAYER_NAMES_RU.items():
        if key.lower() in raw.lower():
            return ru_name
    return raw


def pgn_to_game_data(pgn_text: str) -> dict:
    """Создать game_data из PGN-заголовков. Имена переводятся в русские."""
    white_m = re.search(r'\[White "([^"]+)"\]', pgn_text)
    black_m = re.search(r'\[Black "([^"]+)"\]', pgn_text)
    result_m = re.search(r'\[Result "([^"]+)"\]', pgn_text)
    white_raw = white_m.group(1).strip() if white_m else "White"
    black_raw = black_m.group(1).strip() if black_m else "Black"
    white = normalize_player_name(white_raw)
    black = normalize_player_name(black_raw)
    result = result_m.group(1) if result_m else "*"
    return {"white": {"username": white}, "black": {"username": black}, "result": result}


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
            "white_rem":     fmt(white_rem),
            "black_rem":     fmt(black_rem),
            "white_rem_sec": white_rem,
            "black_rem_sec": black_rem,
            "longest":       longest,
            "longest_str":   fmt(longest_secs) if longest else None,
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


def get_board_png_at_move(pgn_text: str, move_index: int) -> bytes | None:
    """Сгенерировать PNG доски на конкретном полуходу (0 = начало)."""
    try:
        game = chess.pgn.read_game(io.StringIO(pgn_text))
        if not game:
            return None
        board = game.board()
        last_move = None
        for i, move in enumerate(game.mainline_moves()):
            if i >= move_index:
                break
            last_move = move
            board.push(move)
        svg_text = chess.svg.board(
            board=board,
            lastmove=last_move,
            size=400,
            colors={"square light": "#f0d9b5", "square dark": "#b58863",
                    "square light lastmove": "#cdd16e", "square dark lastmove": "#aaa23b"}
        )
        return cairosvg.svg2png(bytestring=svg_text.encode())
    except Exception as e:
        print(f"Board at move error: {e}")
        return None


def _build_tp_dict(causing_idx: int, evals: list, san_list: list,
                   moves: list, game) -> dict:
    """Собрать dict переломного момента по индексу хода."""
    eval_idx = causing_idx + 1  # evals[causing_idx+1] = оценка ПОСЛЕ этого хода
    move_num = causing_idx // 2 + 1
    color = "белых" if causing_idx % 2 == 0 else "чёрных"
    san = san_list[causing_idx]
    eval_before = evals[causing_idx]
    eval_after = evals[eval_idx]

    board_at_tp = game.board()
    for j in range(causing_idx + 1):
        board_at_tp.push(moves[j])

    return {
        "move_index": causing_idx + 1,
        "move_num": move_num,
        "color": color,
        "san": san,
        "eval_before": f"{eval_before:+.2f}",
        "eval_after": f"{eval_after:+.2f}",
        "swing": abs(eval_after - eval_before),
        "fen": board_at_tp.fen(),
    }


# Кэш результатов find_turning_points — чтобы не гонять Stockfish дважды
_tp_cache: dict[str, list[dict]] = {}


def find_turning_points(pgn_text: str) -> list[dict]:
    """Найти два переломных момента партии:
    1) Ход, больше всего сдвинувший оценку В ПОЛЬЗУ белых (delta > 0)
    2) Ход, больше всего сдвинувший оценку В ПОЛЬЗУ чёрных (delta < 0)
    Возвращает список из 1-2 dict. Результат кэшируется по game_id.
    Пропускает первые 10 полуходов (дебютная теория)."""

    game_id = pgn_game_id(pgn_text)
    if game_id in _tp_cache:
        return _tp_cache[game_id]

    try:
        game = chess.pgn.read_game(io.StringIO(pgn_text))
        if not game:
            return []
        moves = list(game.mainline_moves())
        if len(moves) < 12:
            return []

        # SAN-нотация для всех ходов
        san_list = []
        temp = game.board()
        for m in moves:
            san_list.append(temp.san(m))
            temp.push(m)

        # Stockfish: depth=10, пропускаем первые 10 полуходов
        evals = []
        with chess.engine.SimpleEngine.popen_uci(STOCKFISH_PATH) as engine:
            b = game.board()
            for i, move in enumerate(moves):
                if i < 10:
                    evals.append(None)  # placeholder — не анализируем
                else:
                    info = engine.analyse(b, chess.engine.Limit(depth=10))
                    sc = info["score"].white()
                    if sc.is_mate():
                        ev = 99.0 if sc.mate() > 0 else -99.0
                    else:
                        ev = sc.score() / 100.0
                    evals.append(ev)
                b.push(move)

        # Найти два переломных момента: лучший для белых и лучший для чёрных
        # delta = evals[i] - evals[i-1], положительная = в пользу белых
        best_for_white = (0.0, None)  # (delta, idx)
        best_for_black = (0.0, None)  # (abs_delta, idx)
        for i in range(11, len(evals)):
            if evals[i] is None or evals[i - 1] is None:
                continue
            delta = evals[i] - evals[i - 1]
            if delta > best_for_white[0]:
                best_for_white = (delta, i)
            if delta < 0 and abs(delta) > best_for_black[0]:
                best_for_black = (abs(delta), i)

        results = []
        for _, idx in [best_for_black, best_for_white]:
            if idx is not None:
                causing_idx = idx - 1
                tp = _build_tp_dict(causing_idx, evals, san_list, moves, game)
                if tp["swing"] >= 0.8:  # минимальный порог — не показывать мелкие колебания
                    results.append(tp)

        # Сортируем по номеру хода (хронологически)
        results.sort(key=lambda x: x["move_index"])
        _tp_cache[game_id] = results
        return results

    except Exception as e:
        print(f"Turning point error: {e}")
        return []


def find_turning_point(pgn_text: str) -> dict | None:
    """Обратная совместимость — возвращает первый (главный) переломный момент."""
    tps = find_turning_points(pgn_text)
    return tps[0] if tps else None


def fen_to_piece_list(fen: str, white_name: str, black_name: str) -> str:
    """Разбирает FEN и возвращает читаемый список фигур по клеткам.
    Например: 'Белые (Накамура): Крg1, Лf1, п a2 g2 h2\nЧёрные (Каруана): Крg8, Лb2, п a5 c6'
    """
    piece_names = {
        "K": "Кр", "Q": "Ф", "R": "Л", "B": "С", "N": "К", "P": "п",
    }
    try:
        board = chess.Board(fen)
    except Exception:
        return ""

    white_pieces: dict[str, list[str]] = {}
    black_pieces: dict[str, list[str]] = {}

    for sq in chess.SQUARES:
        piece = board.piece_at(sq)
        if piece is None:
            continue
        sq_name = chess.square_name(sq)
        sym = piece_names.get(piece.symbol().upper(), piece.symbol().upper())
        if piece.color == chess.WHITE:
            white_pieces.setdefault(sym, []).append(sq_name)
        else:
            black_pieces.setdefault(sym, []).append(sq_name)

    def fmt_side(pieces: dict[str, list[str]]) -> str:
        order = ["Кр", "Ф", "Л", "С", "К", "п"]
        parts = []
        for sym in order:
            if sym in pieces:
                squares = " ".join(sorted(pieces[sym]))
                parts.append(f"{sym} {squares}")
        return ", ".join(parts)

    w = fmt_side(white_pieces)
    b = fmt_side(black_pieces)
    return f"Белые ({white_name}): {w}\nЧёрные ({black_name}): {b}"


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

        # Вычислить SAN ходов заново — game.board() даёт начальную позицию
        temp_board = game.board()
        all_san = []
        for m in moves:
            try:
                all_san.append(temp_board.san(m))
            except Exception:
                all_san.append(m.uci())
            temp_board.push(m)

        # Если партия завершена (мат/пат/сдача), Stockfish может не справиться —
        # возвращаем данные без оценки, чтобы game_over всё равно отправился
        if board.is_game_over():
            result = game.headers.get("Result", "*")
            if result == "1-0":
                eval_num, eval_str = 99.0, "1-0"
            elif result == "0-1":
                eval_num, eval_str = -99.0, "0-1"
            else:
                eval_num, eval_str = 0.0, "½-½"
            return {
                "eval_num":   eval_num,
                "eval_str":   eval_str,
                "best_move":  "—",
                "move_count": len(moves),
                "moves_san":  all_san[-10:],
                "fen":        board.fen(),
            }

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

        return {
            "eval_num":   eval_num,
            "eval_str":   eval_str,
            "best_move":  board.san(best) if best else "—",
            "move_count": len(moves),
            "moves_san":  all_san[-10:],
            "fen":        board.fen(),
        }
    except Exception as e:
        print(f"Stockfish error: {e}")
        return None


# ─── CLAUDE ───────────────────────────────────────────────────
def get_gm_commentary(game_data: dict, eval_data: dict, event_type: str,
                      clock_info: dict | None = None) -> str:
    client = Anthropic(api_key=ANTHROPIC_API_KEY)
    white = game_data["white"]["username"]
    black = game_data["black"]["username"]
    moves = ', '.join(eval_data.get('moves_san', []))

    # Явная подсказка Claude о времени
    time_note = ""
    if clock_info:
        ws = clock_info.get("white_rem_sec", 0)
        bs = clock_info.get("black_rem_sec", 0)
        diff_min = int(abs(ws - bs) // 60)
        if diff_min >= 2:
            leader = white if ws > bs else black
            time_note = f"\nПо времени опережает {leader} (на ~{diff_min} мин)"

    if event_type in ("eval_swing", "eval_swing_missed", "game_over"):
        result_str = game_data.get("result", "*")
        result_ru = {"1-0": f"1-0 ({white})", "0-1": f"0-1 ({black})",
                     "1/2-1/2": "½-½"}.get(result_str, "результат неизвестен")

        # Качественное описание ситуации вместо числовой оценки
        ev = eval_data["eval_num"]
        if abs(ev) < 0.5:
            position_desc = "позиция примерно равная"
        elif ev >= 0.5 and ev < 1.5:
            position_desc = f"у белых ({white}) небольшой перевес"
        elif ev >= 1.5:
            position_desc = f"у белых ({white}) серьёзное преимущество"
        elif ev <= -0.5 and ev > -1.5:
            position_desc = f"у чёрных ({black}) небольшой перевес"
        else:
            position_desc = f"у чёрных ({black}) серьёзное преимущество"

        missed_note = ""
        if event_type == "eval_swing_missed":
            base_ev = eval_data.get("baseline_eval_num", 0)
            missed_side = white if base_ev > 0 else black
            missed_note = f"\nВАЖНО: {missed_side} упустил(а) серьёзное преимущество! Позиция выровнялась. Укажи кто и как упустил."
        elif event_type == "eval_swing":
            if abs(ev) >= 2.0:
                missed_note = f"\nСитуация становится критической — {position_desc}."
        event_desc = {
            "eval_swing": f"резкое изменение позиции — {position_desc}",
            "eval_swing_missed": f"преимущество упущено, {position_desc}",
            "game_over":  f"партия завершена — {result_ru}",
        }.get(event_type, event_type)

        prompt = f"""Ты — шахматный комментатор турнира претендентов 2026, пишешь для Telegram-канала. Стиль — как у лучших шахматных журналистов: живой, конкретный, с характером.

Партия: {white} (белые) – {black} (чёрные)
Ход: {eval_data['move_count']} | Лучший ход по движку: {eval_data['best_move']}
Последние ходы: {moves}{time_note}
Событие: {event_desc}{missed_note}

Правила:
- НЕ ПИШИ числовые оценки движка ("+1.5", "-2.3" и т.д.) — опиши ситуацию словами
- Назови конкретный ход если он ключевой и объясни почему он важен
- 3–4 предложения: что случилось → почему это важно → что ожидать дальше
- Пиши уверенно, можно с иронией. Называй игроков по фамилии
- Не упоминай что ты ИИ. Без заголовков и markdown. Только обычный текст."""
        max_tokens = 350

    else:
        # Стиль: телеграфный — только факты, коротко и по делу
        event_desc = {
            "new_game": f"партия началась, ход {eval_data['move_count']}",
            "novelty":  f"дебют завершился рано, ход {eval_data['move_count']}",
        }.get(event_type, event_type)
        prompt = f"""Ты — шахматный комментатор турнира претендентов 2026, пишешь для Telegram-канала.

Партия: {white} (белые) – {black} (чёрные)
Ход: {eval_data['move_count']} | Лучший ход: {eval_data['best_move']}
Последние ходы: {moves}{time_note}
Событие: {event_desc}

Напиши 2–3 коротких предложения: какой дебют, чего ожидать от этой пары.
Не пиши числовые оценки движка. Называй игроков по фамилии.
Не упоминай что ты ИИ. Без заголовков и markdown."""
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


async def get_turning_point_commentary(game_data: dict, tp: dict, result: str) -> str:
    """Claude комментирует переломный момент партии."""
    client = Anthropic(api_key=ANTHROPIC_API_KEY)
    white = game_data["white"]["username"]
    black = game_data["black"]["username"]
    result_ru = {"1-0": f"1-0 ({white})", "0-1": f"0-1 ({black})",
                 "1/2-1/2": "½-½"}.get(result, "результат неизвестен")

    ev_before = float(tp['eval_before'])
    ev_after = float(tp['eval_after'])
    delta = ev_after - ev_before
    side = tp['color']
    who = white if side == "белых" else black
    move_san = f"{tp['move_num']}. {tp['san']}" if side == "белых" else f"{tp['move_num']}...{tp['san']}"

    # Определяем что произошло
    if side == "белых" and delta > 0.5:
        what_happened = f"{who} усилил позицию ходом {move_san}"
    elif side == "белых" and delta < -0.5:
        what_happened = f"{who} допустил ошибку ходом {move_san}"
    elif side == "чёрных" and delta < -0.5:
        what_happened = f"{who} нашёл сильный ход {move_san}"
    elif side == "чёрных" and delta > 0.5:
        what_happened = f"{who} ошибся ходом {move_san}"
    else:
        what_happened = f"ход {move_san} стал поворотным моментом"

    prompt = f"""Ты — шахматный комментатор, пишешь разбор хода для Telegram-канала о турнире претендентов 2026.

Партия: {white} (белые) – {black} (чёрные), результат: {result_ru}
Что произошло: {what_happened}

Напиши 2–3 предложения: почему этот ход сильный/слабый, что он изменил в позиции.
НЕ пиши числовые оценки движка. Объясняй по-человечески: какая угроза, какая слабость.
Называй игроков по фамилии. Без заголовков и markdown."""

    r = client.messages.create(
        model="claude-sonnet-4-6", max_tokens=250,
        messages=[{"role": "user", "content": prompt}]
    )
    return r.content[0].text.strip()


async def send_game_analysis(bot: Bot, game_data: dict, pgn: str):
    """Отправить разбор завершённой партии с переломным моментом."""
    white = game_data["white"]["username"]
    black = game_data["black"]["username"]
    result = game_data.get("result", "*")
    result_icon = {"1-0": "⚪️ 1-0", "0-1": "⚫️ 0-1", "1/2-1/2": "🤝 ½-½"}.get(result, "")

    # Используем кэшированный результат — Stockfish не запускается повторно
    loop = asyncio.get_event_loop()
    tp = await loop.run_in_executor(None, find_turning_point, pgn)
    if not tp:
        print(f"No turning point found for {white} vs {black}")
        return

    commentary = await get_turning_point_commentary(game_data, tp, result)
    png = get_board_png_at_move(pgn, tp["move_index"])

    side = tp['color']
    move_san = f"{tp['move_num']}. {tp['san']}" if side == "белых" else f"{tp['move_num']}...{tp['san']}"

    msg = (f"🔍 *Разбор: {white} — {black}* {result_icon}\n"
           f"Переломный момент: ход *{move_san}* ({tp['color']})\n\n"
           f"{commentary}")

    if png:
        await send_update_with_photo(bot, msg, pgn_override_png=png)
    else:
        await send_update(bot, msg)


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


async def send_update_with_photo(bot: Bot, message: str, pgn: str = "", pgn_override_png: bytes | None = None):
    """Отправить сообщение с изображением доски. При ошибке — текст без картинки.
    pgn_override_png позволяет передать уже готовый PNG (например, позицию в нужный момент).
    Если сообщение длиннее 1024 символов — фото с коротким заголовком, полный текст отдельно."""
    png = pgn_override_png if pgn_override_png is not None else get_board_png(pgn)
    if png:
        try:
            if len(message) > 1024:
                # Берём первую строку (заголовок) как caption к фото
                first_line = message.split("\n")[0]
                caption = first_line[:1020] if len(first_line) > 1020 else first_line
                await bot.send_photo(
                    chat_id=TELEGRAM_CHAT_ID,
                    photo=png,
                    caption=caption,
                    parse_mode="Markdown"
                )
                # Полный текст без первой строки — отдельным сообщением
                rest = message[len(first_line):].strip()
                if rest:
                    await send_update(bot, rest)
            else:
                await bot.send_photo(
                    chat_id=TELEGRAM_CHAT_ID,
                    photo=png,
                    caption=message,
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
    """Отправить единый итоговый разбор тура после завершения всех партий.
    Включает результаты, переломные моменты и таблицу — всё одним текстовым сообщением."""
    results_lines = []
    results_for_claude = []
    loop = asyncio.get_event_loop()

    for pgn in games_pgn:
        gd = pgn_to_game_data(pgn)
        w, b = gd["white"]["username"], gd["black"]["username"]
        res = get_game_result(pgn)
        info = extract_opening_info(pgn)
        n_moves = count_moves_pgn(pgn)
        n_moves_str = str(n_moves) if n_moves else "?"
        opening = info.get("opening") or info.get("eco") or "неизвестно"
        first_moves = " ".join(info.get("first_moves", [])[:6])
        if res == "1-0":
            result_line = f"⚪️ 1-0 *{w} – {b}* ({n_moves_str} ходов)"
        elif res == "0-1":
            result_line = f"⚫️ 0-1 *{w} – {b}* ({n_moves_str} ходов)"
        else:
            result_line = f"🤝 ½-½ *{w} – {b}* ({n_moves_str} ходов)"
        results_lines.append(result_line)

        # Переломные моменты — описываем по-человечески
        tps = await loop.run_in_executor(None, find_turning_points, pgn)
        tp_descs = []
        for tp in tps:
            ev_before = float(tp['eval_before'])
            ev_after = float(tp['eval_after'])
            delta = ev_after - ev_before
            side = tp['color']
            move_san = f"{tp['move_num']}. {tp['san']}" if side == "белых" else f"{tp['move_num']}...{tp['san']}"
            who = w if side == "белых" else b

            # Описание действия
            if side == "белых":
                if delta > 0.5:
                    action = f"{who} усилил позицию ходом {move_san}"
                elif delta < -0.5:
                    action = f"{who} ошибся ходом {move_san}"
                else:
                    action = f"ход {move_san} ({who}) стал поворотным"
            else:
                if delta < -0.5:
                    action = f"{who} нашёл сильный ход {move_san}"
                elif delta > 0.5:
                    action = f"{who} ошибся ходом {move_san}"
                else:
                    action = f"ход {move_san} ({who}) стал поворотным"

            # Качественная оценка последствий
            if abs(ev_before) >= 2.0 and abs(ev_after) < 0.8:
                consequence = "преимущество упущено, позиция уравнялась"
            elif abs(ev_before) >= 2.0 and abs(ev_after) >= 0.8:
                consequence = "преимущество заметно сократилось"
            elif abs(ev_before) < 0.8 and abs(ev_after) >= 2.0:
                consequence = "позиция из равной стала решающей"
            elif abs(ev_before) < 0.8 and abs(ev_after) < 0.8:
                consequence = "позиция осталась примерно равной"
            else:
                consequence = "оценка заметно изменилась"
            tp_descs.append(f"{action} — {consequence}")

        tp_info = ""
        if len(tp_descs) == 1:
            tp_info = f" Ключевой момент: {tp_descs[0]}."
        elif len(tp_descs) >= 2:
            tp_info = f" Ключевые моменты: 1) {tp_descs[0]}; 2) {tp_descs[1]}."

        results_for_claude.append(
            f"• {w} (белые) vs {b} (чёрные): {res}, {n_moves_str} ходов. "
            f"Дебют: {opening}. Первые ходы: {first_moves}.{tp_info}"
        )

    # Таблица очков для контекста
    try:
        points, rounds_played = await calculate_standings()
        sorted_pts = sorted(points.items(), key=lambda x: -x[1])
        standings_text = ", ".join(f"{n} {p}" for n, p in sorted_pts if p > 0)
    except Exception:
        standings_text = "(таблица недоступна)"

    # Claude-разбор в стиле шахматного Telegram-канала
    client = Anthropic(api_key=ANTHROPIC_API_KEY)
    prompt = f"""Ты пишешь итоги {round_name} турнира претендентов 2026 для русскоязычного шахматного Telegram-канала.

Результаты:
{chr(10).join(results_for_claude)}

Таблица после этого тура: {standings_text}

Стиль — как у chess.com или ChessBase, но на русском:
- Пиши как спортивный журналист: короткие фактические предложения, живой язык
- 2–3 предложения на каждую партию: название дебюта, переломный момент (используй данные выше), характер борьбы
- Переломные моменты описывай через действие игрока, не через цифры оценки движка. НЕ ПИШИ числовые оценки вроде "+0.86" или "-3.03". Вместо этого: "Накамура выпустил перевес", "Гири перехватил инициативу", "позиция уравнялась"
- Конкретный ход можно назвать, но объясняй его смысл по-человечески
- НЕ описывай фигуры на доске и их расположение — это не нужно для итогов
- В конце — 1–2 фразы о турнирной интриге с ТОЧНЫМИ очками из таблицы выше
- Шахматные термины на русском
- Без заголовков (#), без маркированных списков, без markdown
- В самом конце новой строкой: #турнир_претендентов
- Не упоминай что ты ИИ"""

    r = client.messages.create(
        model="claude-sonnet-4-6", max_tokens=700,
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
    # Автоматически показать таблицу очков после итогов тура
    await asyncio.sleep(2)
    await send_standings(bot, TELEGRAM_CHAT_ID)


async def get_tournament_h2h() -> dict[tuple[str, str], dict]:
    """Собрать реальные результаты H2H из уже сыгранных раундов этого турнира.
    Возвращает {(white, black): {"w_pts": float, "b_pts": float, "games": int}}."""
    h2h: dict[tuple[str, str], dict] = {}
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        for rid, _ in KNOWN_ROUND_IDS:
            try:
                r = await client.get(
                    f"https://lichess.org/api/broadcast/round/{rid}.pgn",
                    headers={"User-Agent": "CandidatesBot/1.0"}
                )
                if r.status_code != 200 or not r.text.strip():
                    continue
                for pgn in split_pgn(r.text):
                    if not is_game_finished(pgn):
                        continue
                    gd = pgn_to_game_data(pgn)
                    w, b = gd["white"]["username"], gd["black"]["username"]
                    res = gd.get("result", "*")
                    key = (w, b)
                    if key not in h2h:
                        h2h[key] = {"w_pts": 0.0, "b_pts": 0.0, "games": 0}
                    h2h[key]["games"] += 1
                    if res == "1-0":
                        h2h[key]["w_pts"] += 1.0
                    elif res == "0-1":
                        h2h[key]["b_pts"] += 1.0
                    elif res == "1/2-1/2":
                        h2h[key]["w_pts"] += 0.5
                        h2h[key]["b_pts"] += 0.5
            except Exception:
                pass
    return h2h


async def get_round_preview(pairs: list[tuple[str, str]]) -> str:
    """Claude генерирует превью тура с реальными H2H из этого турнира + исторический контекст."""
    client = Anthropic(api_key=ANTHROPIC_API_KEY)

    # Загружаем реальные результаты этого турнира
    tournament_h2h = await get_tournament_h2h()

    # Строим блок с данными для каждой пары
    pairs_lines = []
    for w, b in pairs:
        key = (w, b)
        if key in tournament_h2h:
            d = tournament_h2h[key]
            score_line = f"в этом турнире: {d['w_pts']}:{d['b_pts']} ({d['games']} партий)"
        else:
            score_line = "в этом турнире ещё не встречались"
        pairs_lines.append(f"• {w} (белые) – {b} (чёрные) | {score_line}")

    pairs_text = "\n".join(pairs_lines)

    prompt = f"""Ты — шахматный аналитик, пишешь превью тура Кандидатов 2026 для Telegram-канала.

Пары тура (с реальными результатами этого турнира):
{pairs_text}

Для каждой партии напиши строго в таком формате (одна строка):
*Белый – Чёрный*: [счёт в этом турнире]. [Факт о стиле/дебюте] — [Прогноз на эту партию]

Правила:
- Счёт: используй данные выше как есть — "X:Y в этом турнире" или "первая встреча в турнире"
- Факт: дебютная специализация или характерный стиль игрока — одно предложение
- Прогноз: чего ожидать от этой конкретной партии — одно предложение
- Называй игроков по фамилии
- Никаких заголовков, никакого markdown кроме *имён*
- Ровно 4 строки"""

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


async def send_pulse_update(bot: Bot, game_data: dict, pgn: str, label: str,
                           eval_data: dict | None = None, clock_info: dict | None = None):
    """Короткий пульс-апдейт: оценка + часы + 2 предложения Claude.
    eval_data и clock_info можно передать извне чтобы не гонять Stockfish повторно."""
    white = game_data["white"]["username"]
    black = game_data["black"]["username"]

    if eval_data is None:
        eval_data = evaluate_position(pgn)
    if eval_data is None:
        return
    if clock_info is None:
        clock_info = analyze_clocks(pgn)

    wr = clock_info.get("white_rem", "?") if clock_info else "?"
    br = clock_info.get("black_rem", "?") if clock_info else "?"

    client = Anthropic(api_key=ANTHROPIC_API_KEY)
    moves = ", ".join(eval_data.get("moves_san", []))
    fen = eval_data.get("fen", "")
    # Явно вычисляем кто опережает по времени — чтобы Claude не перепутал
    ws = clock_info.get("white_rem_sec", 0) if clock_info else 0
    bs = clock_info.get("black_rem_sec", 0) if clock_info else 0
    diff = abs(ws - bs)
    diff_min = int(diff // 60)
    if diff_min >= 2:
        time_leader = white if ws > bs else black
        time_note = f"По времени опережает {time_leader} (на ~{diff_min} мин)"
    else:
        time_note = "По времени примерно равны"

    piece_list = fen_to_piece_list(fen, white, black)
    prompt = f"""Ты — шахматный комментатор, пишешь короткий апдейт для Telegram-канала о турнире претендентов 2026.

Партия: {white} (белые) – {black} (чёрные), идёт {label}
Ход: {eval_data['move_count']} | Оценка: {eval_data['eval_str']}
Часы: {white} — {wr}, {black} — {br}
{time_note}
Последние ходы: {moves}
Фигуры на доске (точные данные):
{piece_list}

Описывай позицию ТОЛЬКО по списку фигур выше — не придумывай расположения.
Ровно 2 предложения: что сейчас происходит на доске и кто выглядит лучше.
Без воды, только факт + оценка. Без заголовков и markdown."""

    r = client.messages.create(
        model="claude-sonnet-4-6", max_tokens=150,
        messages=[{"role": "user", "content": prompt}]
    )
    commentary = r.content[0].text.strip()

    msg = (f"📍 *{white} — {black}* | {label}\n"
           f"Ход {eval_data['move_count']} | Оценка: `{eval_data['eval_str']}`\n"
           f"⏱ {white}: `{wr}` | {black}: `{br}`\n\n"
           f"{commentary}")
    await send_update_with_photo(bot, msg, pgn)


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
    icons = {"eval_swing": "📈", "eval_swing_missed": "😱", "game_over": "🏁", "new_game": "♟️"}

    # Строка часов — всегда
    clock_line = ""
    if clock_info:
        wr = clock_info.get("white_rem", "?")
        br = clock_info.get("black_rem", "?")
        clock_line = f"\n⏱ {w}: `{wr}` | {b}: `{br}`"

        # Долгий ход — только для ключевых событий
        if event_type in ("eval_swing", "eval_swing_missed", "game_over") and clock_info.get("longest"):
            lt = clock_info["longest"]
            thinker = w if lt["color"] == "white" else b
            clock_line += (f"\n🤔 Дольше всего думал: *{thinker}* — "
                           f"ход {lt['move_num']}. {lt['san']} ({clock_info['longest_str']})")

    result_line = ""
    if event_type == "game_over":
        res = game_data.get("result", "*")
        result_emoji = {"1-0": "⚪️ 1-0", "0-1": "⚫️ 0-1",
                        "1/2-1/2": "🤝 ½-½"}.get(res, "")
        if result_emoji:
            result_line = f"\n{result_emoji}"

    return (f"{icons.get(event_type,'♟️')} *{w} — {b}*{result_line}\n"
            f"Ход {eval_data['move_count']} | Оценка: `{eval_data['eval_str']}` | "
            f"Лучший: `{eval_data['best_move']}`"
            f"{clock_line}\n\n"
            f"🧠 {commentary}")


# ─── ТАБЛИЦА ОЧКОВ ────────────────────────────────────────────
async def calculate_standings() -> tuple[dict[str, float], int]:
    """Собрать очки всех игроков по всем сыгранным раундам.
    Возвращает (points, rounds_played)."""
    points: dict[str, float] = {name: 0.0 for name in PLAYER_NAMES_RU.values()}
    rounds_played = 0
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        for rid, rname in KNOWN_ROUND_IDS:
            try:
                r = await client.get(
                    f"https://lichess.org/api/broadcast/round/{rid}.pgn",
                    headers={"User-Agent": "CandidatesBot/1.0"}
                )
                if r.status_code != 200 or not r.text.strip():
                    continue
                games = split_pgn(r.text)
                finished = [g for g in games if is_game_finished(g)]
                if not finished:
                    continue
                # Считаем тур сыгранным если все его партии завершены
                if len(finished) == len(games):
                    rounds_played += 1
                for pgn in finished:
                    gd = pgn_to_game_data(pgn)
                    w = gd["white"]["username"]
                    b = gd["black"]["username"]
                    res = gd.get("result", "*")
                    if res == "1-0":
                        points[w] = points.get(w, 0) + 1.0
                    elif res == "0-1":
                        points[b] = points.get(b, 0) + 1.0
                    elif res == "1/2-1/2":
                        points[w] = points.get(w, 0) + 0.5
                        points[b] = points.get(b, 0) + 0.5
            except Exception as e:
                print(f"Standings error {rname}: {e}")
    return points, rounds_played


async def send_standings(bot: Bot, chat_id: str | int):
    """Отправить текущую таблицу очков в чат."""
    try:
        points, rounds_played = await calculate_standings()
    except Exception as e:
        await bot.send_message(chat_id=chat_id, text=f"Ошибка получения таблицы: {e}")
        return

    # Медали и позиции
    medals = ["🥇", "🥈", "🥉"]
    sorted_players = sorted(points.items(), key=lambda x: -x[1])

    lines = []
    prev_pts = None
    rank = 0
    display_rank = 0
    for name, pts in sorted_players:
        rank += 1
        if pts != prev_pts:
            display_rank = rank
        prev_pts = pts
        medal = medals[display_rank - 1] if display_rank <= 3 else f"{display_rank}."
        # Форматируем очки: 2.0 → "2", 2.5 → "2.5"
        pts_str = str(int(pts)) if pts == int(pts) else str(pts)
        lines.append(f"{medal} *{name}* — {pts_str}")

    rounds_str = f"после {rounds_played} туров" if rounds_played else "турнир ещё не начался"
    msg = f"📊 *Таблица Кандидатов 2026* ({rounds_str})\n\n" + "\n".join(lines)
    await bot.send_message(chat_id=chat_id, text=msg, parse_mode="Markdown")


async def cmd_standings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/standings — текущая таблица очков."""
    bot = context.bot
    chat_id = update.effective_chat.id
    await send_standings(bot, chat_id)


# ─── ГЛАВНЫЙ ЦИКЛ ─────────────────────────────────────────────
async def monitoring_loop(bot: Bot):
    """Основной цикл мониторинга партий."""
    print("✅ Мониторинг запущен. Слежу за турниром претендентов 2026...")

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
            # и не все партии уже завершены (раунд не прошлый).
            # Если бот перезапустился в середине тура (ходов уже > 5) — тихо помечаем
            # раунд как объявленный, чтобы не дублировать анонс после деплоя.
            # Кешируем move_count и is_finished один раз на весь цикл
            pgn_move_counts  = {p: count_moves_pgn(p) for p in games_pgn}
            pgn_is_finished  = {p: is_game_finished(p) for p in games_pgn}

            games_started = [p for p in games_pgn if pgn_move_counts[p] > 0]
            if (games_started
                    and round_id not in announced_rounds
                    and not all(pgn_is_finished[p] for p in games_pgn)):
                announced_rounds.add(round_id)
                max_moves = max(pgn_move_counts[p] for p in games_started)
                if max_moves <= 5:
                    await send_round_start(bot, round_name, games_pgn)
                else:
                    print(f"Раунд {round_name} уже в процессе ({max_moves} ходов) — анонс пропущен (рестарт?)")

            for pgn in games_pgn:
                game_id   = pgn_game_id(pgn)
                game_data = pgn_to_game_data(pgn)
                finished  = pgn_is_finished[pgn]   # из кеша — не парсим PGN повторно

                eval_data = evaluate_position(pgn)
                if not eval_data:
                    continue

                # Запоминаем момент первого обнаружения партии.
                # Используем move_count из уже посчитанного eval_data — без лишнего вызова.
                if game_id not in game_start_times:
                    game_start_times[game_id] = now
                    # Бот мог перезапуститься в середине партии — помечаем уже
                    # пройденные этапы по числу ходов, чтобы не дублировать сообщения
                    mc = eval_data["move_count"]
                    if mc > 12:
                        games_15min_done.add(game_id)
                    sent = games_pulse_sent.setdefault(game_id, set())
                    if mc > 20:
                        sent.add(3600)
                    if mc > 38:
                        sent.add(7200)
                    if mc > 52:
                        sent.add(10800)

                prev = seen_games.get(game_id)
                event_type = None

                # Не отправлять ничего пока партия не началась (ход 0 = игроки ещё не сели)
                if eval_data["move_count"] == 0:
                    seen_games[game_id] = eval_data
                    games_baseline_eval.setdefault(game_id, eval_data)
                    continue

                # 1. Конец партии — отправить один раз
                # prev is not None убрано: бот должен сообщать о завершении
                # даже если перезапустился во время партии
                if finished and game_id not in games_over_sent:
                    games_over_sent.add(game_id)
                    event_type = "game_over"

                # 2. Новая партия — первое обнаружение с ходами
                elif (prev is None or prev.get("move_count", 0) == 0) and not finished:
                    event_type = "new_game"

                # 3. Резкое изменение оценки — сравниваем с базовой линией + кулдаун 5 ходов
                else:
                    baseline = games_baseline_eval.get(game_id, prev)
                    if baseline:
                        swing = eval_data["eval_num"] - baseline["eval_num"]
                        if abs(swing) >= EVAL_SWING_THRESHOLD:
                            last_swing = games_swing_move.get(game_id, 0)
                            if eval_data["move_count"] - last_swing >= 5:
                                games_swing_move[game_id] = eval_data["move_count"]
                                # Определяем: упущенное преимущество или нет
                                base_ev = baseline["eval_num"]
                                curr_ev = eval_data["eval_num"]
                                if abs(base_ev) >= 1.5 and abs(curr_ev) < 0.8:
                                    # Было большое преимущество, стало ~ровно
                                    event_type = "eval_swing_missed"
                                    eval_data["baseline_eval_num"] = base_ev
                                else:
                                    event_type = "eval_swing"

                if event_type:
                    clock_info = analyze_clocks(pgn)
                    commentary = get_gm_commentary(game_data, eval_data, event_type, clock_info)
                    msg = format_event_msg(game_data, eval_data, event_type, commentary, clock_info)
                    await send_update_with_photo(bot, msg, pgn)
                    # Разбор переломного момента теперь включён в итоги тура (send_round_summary)
                    # Обновляем базовую линию только при отправке уведомления
                    games_baseline_eval[game_id] = eval_data

                seen_games[game_id] = eval_data

                # ── 15-минутный анализ дебюта ──────────────────
                elapsed = now - game_start_times.get(game_id, now)
                if (elapsed >= OPENING_STATUS_DELAY
                        and game_id not in games_15min_done
                        and eval_data["move_count"] >= 5
                        and not finished):
                    games_15min_done.add(game_id)
                    await send_15min_status(bot, game_data, pgn)

                # ── Пульс-апдейты через 60/120/180 мин ─────────
                if not finished:
                    sent_pulses = games_pulse_sent.setdefault(game_id, set())
                    for interval in PULSE_INTERVALS:
                        if elapsed >= interval and interval not in sent_pulses:
                            sent_pulses.add(interval)
                            label = PULSE_LABELS[interval]
                            # Передаём уже посчитанные eval_data и clock_info — Stockfish не нужен повторно
                            pulse_clocks = clock_info if event_type else analyze_clocks(pgn)
                            await send_pulse_update(bot, game_data, pgn, label,
                                                    eval_data=eval_data, clock_info=pulse_clocks)

            # ── Итоговый разбор тура ─────────────────────────
            if (games_pgn
                    and round_id not in round_summary_done
                    and len(games_pgn) >= 2
                    and all(pgn_is_finished[p] for p in games_pgn)):  # используем кеш
                round_summary_done.add(round_id)
                await send_round_summary(bot, round_name, games_pgn)

        except Exception as e:
            print(f"Loop error: {e}")

        await asyncio.sleep(POLL_INTERVAL_SECONDS)


async def main():
    # Приложение с поддержкой команд
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("standings", cmd_standings))

    bot = app.bot

    async with app:
        await app.start()
        await app.updater.start_polling(drop_pending_updates=True)
        print("✅ Бот запущен (мониторинг + команды)")
        # Параллельно запускаем мониторинг
        await monitoring_loop(bot)
        # monitoring_loop бесконечный — до сюда не доходим при штатной работе
        await app.updater.stop()
        await app.stop()


if __name__ == "__main__":
    asyncio.run(main())
