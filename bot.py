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
from commentary_prompts import build_prompt, SYSTEM_PROMPT, get_position_analysis
from tournaments_config import load_tournaments, get_active_tournaments

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

# Кулдаун между eval_swing постами на одной партии (в полных ходах).
# Перекрывается из tournaments.yaml params.eval_swing_cooldown_moves.
EVAL_SWING_COOLDOWN_MOVES = 5

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
# Объединённый словарь: Open + Women — имена не пересекаются
PLAYER_NAMES_RU = {
    # Open
    "Caruana":        "Каруана",
    "Nakamura":       "Накамура",
    "Giri":           "Гири",
    "Praggnanandhaa": "Прагг",
    "Sindarov":       "Синдаров",
    "Wei":            "Вэй И",
    "Esipenko":       "Есипенко",
    "Bluebaum":       "Блюбаум",
    # Women
    "Zhu":            "Чжу Цзиньэр",
    "Tan":            "Тань Чжунъи",
    "Goryachkina":    "Горячкина",
    "Muzychuk":       "Музычук",
    "Assaubayeva":    "Ассаубаева",
    "Lagno":          "Лагно",
    "Deshmukh":       "Дешмук",
    "Vaishali":       "Вайшали",
    "Rameshbabu":     "Вайшали",
}

# Множества русских имён для фильтрации таблиц
OPEN_PLAYERS_RU = {"Каруана", "Накамура", "Гири", "Прагг", "Синдаров", "Вэй И", "Есипенко", "Блюбаум"}
WOMEN_PLAYERS_RU = {"Чжу Цзиньэр", "Тань Чжунъи", "Горячкина", "Музычук", "Ассаубаева", "Лагно", "Дешмук", "Вайшали"}

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

# ═══ ЖЕНСКИЙ ТУРНИР ПРЕТЕНДЕНТОВ 2026 ═════════════════════════
# Облегчённый мониторинг: превью тура → пульс 1ч → пульс 2ч → итоги
WOMEN_BROADCAST_ID = "xj4qM8Nw"   # ID серии (не раунда!)

# Хардкод раундов — fallback если discover_women_rounds() не сработает.
# Round 10 и далее будут найдены автоматически через discover_women_rounds().
WOMEN_KNOWN_ROUND_IDS = [
    ("diPdGkEA", "Round 1"),
    ("EMkf0c6e", "Round 2"),
    ("2qEm9CH3", "Round 3"),
    ("MDv2BlCp", "Round 4"),
    ("VAELuM6E", "Round 5"),
    ("hmGcNp3P", "Round 6"),
    ("QqBr2Kpr", "Round 7"),
    ("R0BP4Jy4", "Round 8"),
    ("6Ukl08Ir", "Round 9"),
    ("Es2IjSwE", "Round 10"),
    ("fDNFUpG9", "Round 11"),
    ("u3pemMHq", "Round 12"),
    ("o7DgltDn", "Round 13"),
    ("dllZX7eJ", "Round 14"),
]

# Расписание совпадает с Open (тот же день, то же время старта)
WOMEN_ROUND_SCHEDULE = dict(ROUND_SCHEDULE)

# Пульс-апдейты для женского турнира: только 1ч и 2ч (без 3ч)
WOMEN_PULSE_INTERVALS = [3600, 7200]
WOMEN_PULSE_LABELS    = {3600: "1 час", 7200: "2 часа"}

# ─── ПРОФИЛИ ТУРНИРОВ ─────────────────────────────────────────
# Декларативное описание, какие триггеры активны для каждого турнира.
# Используется как для текущих Кандидатов 2026, так и для будущих турниров
# (Grand Swiss, Кубок мира и т.д.) — достаточно добавить новый профиль.
#
# Поля:
#   preview, pulse_1h, pulse_2h, turning_point, round_summary  — булевые флаги
#   final_standings_with_places  — на последнем туре итоговый пост выводит
#       1, 2, 3 места вместо обычной таблицы очков
#   tiebreak_rules  — порядок критериев тай-брейка (для мест 2+); первое
#       место решается отдельной плей-офф партией по регламенту FIDE 2026
#       (две рапид-партии 15+10 при делёжке двумя игроками; круговой турнир
#       при делёжке 3–6; см. fide.com/candidates-play-off-introduced/).
#   total_rounds  — общее число туров, чтобы понимать, когда «последний»
TOURNAMENT_PROFILES = {
    "open": {
        "preview":            True,
        "pulse_1h":           False,   # мужской — облегчённый режим
        "pulse_2h":           False,   # без пульсов
        "new_game":           False,   # без отдельных постов на каждую партию
        "eval_swing":         False,   # без промежуточных оценок
        "opening_15min":      False,   # без 15-мин анализа дебюта
        "turning_point":      True,
        "round_summary":      True,
        "final_standings_with_places": True,
        "tiebreak_rules":     ["playoff_first", "sb", "wins", "h2h"],
        "total_rounds":       14,
        "display_name":       "Турнир претендентов",
        "qualifies_for":      "матч за звание чемпиона мира 2027",
        "emoji":              "🏆",
    },
    "women": {
        "preview":            True,
        "pulse_1h":           True,    # женский — полный цикл
        "pulse_2h":           True,
        "new_game":           True,    # отдельный пост при старте каждой партии
        "eval_swing":         True,    # оповещения при резких изменениях оценки
        "opening_15min":      True,    # 15-мин анализ дебюта
        "turning_point":      True,
        "round_summary":      True,
        "final_standings_with_places": True,
        "tiebreak_rules":     ["playoff_first", "sb", "wins", "h2h"],
        "total_rounds":       14,
        "display_name":       "Женский турнир претендентов",
        "qualifies_for":      "матч за звание чемпионки мира 2027",
        "emoji":              "♛",
    },
}

# ─── YAML OVERRIDE: подменяем Open-константы из tournaments.yaml ──────
# Если в tournaments.yaml есть активный сегодня турнир (отличный от женского
# Кандидатов, у которого свой код-путь) — подставляем его данные в Open-слот.
# Это делает bot.py универсальным: меняем YAML → бот покрывает новый турнир.
#
# Хардкод-константы выше остаются как fallback на случай ошибки чтения YAML.
def _build_round_schedule(profile: dict) -> dict:
    """Распределить туры профиля по дням от start_date, пропуская rest_days.
    Время старта берётся из profile['params']['round_start_utc'] (HH:MM, UTC).
    Возвращает {round_name: datetime_utc}."""
    h, m = map(int, str(profile["params"].get("round_start_utc", "12:30")).split(":"))
    rest = set(profile["rest_days"])
    cur = profile["start_date"]
    schedule = {}
    for _, round_name in profile["round_ids"]:
        while cur.isoformat() in rest:
            cur = cur + datetime.timedelta(days=1)
        schedule[round_name] = datetime.datetime(
            cur.year, cur.month, cur.day, h, m, tzinfo=_UTC
        )
        cur = cur + datetime.timedelta(days=1)
    return schedule


_main_profile = None    # активный YAML-профиль для Open-слота (доступен ниже)
_active_secondaries: list[tuple[str, dict]] = []   # secondary-турниры (Phase 2)
try:
    _YAML_CFG = load_tournaments()
    _today_utc = datetime.datetime.now(_UTC).date()
    _active_yaml = get_active_tournaments(_YAML_CFG, _today_utc)
    # Берём первый активный turnier, который НЕ женские Кандидаты И с
    # coverage_tier=primary. Старые профили без coverage_tier по умолчанию
    # primary (обратная совместимость через .get(..., "primary")).
    _main_profile = next(
        (p for tid, p in _active_yaml.items()
         if tid != "women_candidates_2026"
         and p.get("coverage_tier", "primary") == "primary"),
        None,
    )
    # Secondaries — отдельный лёгкий цикл (secondary_monitoring_step ниже).
    _active_secondaries = [
        (tid, p) for tid, p in _active_yaml.items()
        if p.get("coverage_tier") == "secondary"
    ]
    if _active_secondaries:
        _names = [tid for tid, _ in _active_secondaries]
        print(f"[YAML] Secondary активны: {_names}")
    if _main_profile:
        print(f"[YAML] Активный турнир в Open-слоте: "
              f"{_main_profile['display_name']} ({_main_profile['id']})")
        LICHESS_BROADCAST_ID = _main_profile["broadcast_id"] or LICHESS_BROADCAST_ID
        KNOWN_ROUND_IDS      = list(_main_profile["round_ids"])
        REST_DAYS            = set(_main_profile["rest_days"])
        ROUND_SCHEDULE       = _build_round_schedule(_main_profile)
        # Имена игроков — добавляем поверх существующего словаря
        for _surname, _info in _main_profile["players"].items():
            PLAYER_NAMES_RU[_surname] = _info["ru"]
            if _info.get("chess_com"):
                PLAYER_CHESS_COM[_surname] = _info["chess_com"]
        OPEN_PLAYERS_RU = {info["ru"] for info in _main_profile["players"].values()}
        # Профиль алгоритмов
        TOURNAMENT_PROFILES["open"].update({
            **_main_profile["algorithms"],
            "display_name":  _main_profile["display_name"],
            "emoji":         _main_profile["emoji"],
            "qualifies_for": _main_profile["qualifies_for"],
            "total_rounds":  _main_profile["total_rounds"],
        })
        # Пульс-интервалы — перекрываем даже пустым списком,
        # чтобы профиль с pulse_intervals: [] реально отключал пульсы
        # (старая проверка `if list:` пропускала пустой список и оставляла
        # хардкод [3600,7200,10800] — пульсы шли несмотря на профиль).
        if "pulse_intervals" in _main_profile["params"]:
            PULSE_INTERVALS = list(_main_profile["params"]["pulse_intervals"])
        # Порог eval_swing
        if "eval_swing_threshold" in _main_profile["params"]:
            EVAL_SWING_THRESHOLD = float(_main_profile["params"]["eval_swing_threshold"])
        # Кулдаун между eval_swing постами (в ходах) — снижает шум
        if "eval_swing_cooldown_moves" in _main_profile["params"]:
            EVAL_SWING_COOLDOWN_MOVES = int(_main_profile["params"]["eval_swing_cooldown_moves"])
        print(f"[YAML] broadcast={LICHESS_BROADCAST_ID}, "
              f"rounds={len(KNOWN_ROUND_IDS)}, "
              f"algos_on={sum(_main_profile['algorithms'].values())}/10")
    else:
        print("[YAML] нет активного турнира на сегодня — остаюсь на хардкоде")
except Exception as _e:
    print(f"[YAML] ошибка загрузки tournaments.yaml: {_e!r} — остаюсь на хардкоде")

# ─── СОСТОЯНИЕ (Open) ─────────────────────────────────────────
seen_games          = {}   # game_id → последний eval_data
games_baseline_eval = {}   # game_id → eval_data на момент последнего уведомления (базовая линия для swing)
game_start_times    = {}   # game_id → timestamp первого обнаружения
games_15min_done    = set()
games_over_sent     = set()  # game_id → уже отправили game_over
games_swing_move    = {}   # game_id → ход последнего eval_swing (кулдаун)
announced_rounds    = set()  # round_id → объявили старт тура
pre_announced_rounds = set()  # round_name → отправили превью до начала тура
round_summary_done  = set()  # round_id → уже отправили итог тура
games_pulse_sent    = {}   # game_id → set(секунд) уже отправленных пульс-апдейтов
games_eval_history  = {}   # game_id → list[(move_count, eval_num)] — история оценок для контекста

# ─── СОСТОЯНИЕ (Women) ───────────────────────────────────────
w_game_start_times     = {}      # game_id → timestamp
w_games_over_sent      = set()   # game_id → game_over отправлен
w_announced_rounds     = set()   # round_id → анонс раунда
w_pre_announced_rounds = set()   # round_name → превью
w_round_summary_done   = set()   # round_id → итог тура
w_games_pulse_sent     = {}      # game_id → set(секунд)
w_last_discover_ts     = 0.0    # timestamp последнего discover_women_rounds()
w_seen_games           = {}      # game_id → последний eval_data
w_games_baseline_eval  = {}      # game_id → eval_data (базовая линия для swing)
w_games_swing_move     = {}      # game_id → ход последнего eval_swing (кулдаун)
w_games_15min_done     = set()   # game_id → 15-мин анализ отправлен
w_games_eval_history   = {}      # game_id → [(move, eval), ...]

# ─── СОСТОЯНИЕ (Secondary turниры) ─────────────────────────────
# Лёгкий пайплайн: один пост в день (daily_digest) + финальные места.
# В отличие от open/women, нет per-game state — только per-day.
secondary_digest_sent     : dict[str, set[str]]              = {}  # tid → {ISO date уже посланы}
secondary_round_ids_cache : dict[str, list[tuple[str, str]]] = {}  # tid → [(round_id, round_name)]
secondary_first_seen      : set[str]                          = set()  # tid → ретроспектива пропущена

# ─── LICHESS API ──────────────────────────────────────────────
async def get_active_round_id() -> tuple[str | None, str | None]:
    """Найти активный раунд Open турнира через PGN endpoint.
    JSON /api/broadcast/round/{id} возвращает 404 — используем только .pgn endpoint.
    Идём от последнего раунда к первому.
    Приоритет: раунд с незавершёнными партиями. Если такого нет — последний
    завершённый раунд (чтобы бот мог отправить game_over и итоги тура).
    Если турнир полностью завершён и итоги отправлены — возвращает (None, None)."""

    # Если последний раунд уже обработан (итоги отправлены) — турнир окончен
    last_rid, last_rname = KNOWN_ROUND_IDS[-1]
    if last_rid in round_summary_done:
        return None, None

    # Если сейчас больше 2 дней после последнего запланированного тура — турнир окончен
    last_scheduled = ROUND_SCHEDULE.get(last_rname)
    if last_scheduled:
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        if now_utc > last_scheduled + datetime.timedelta(days=2):
            print(f"Турнир завершён (прошло >2 дней после {last_rname}). Бот в режиме ожидания.")
            return None, None

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
    # Всё завершено, ничего не нашли — турнир окончен
    print("Турнир завершён, все раунды обработаны.")
    return None, None


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
                # Пропускаем первый ход — часы часто включают время на рассадку/подготовку
                if i > 0 and spent > longest_secs and spent > 0:
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
                # Пропускаем первый ход — часы часто включают время на рассадку/подготовку
                if i > 0 and spent > longest_secs and spent > 0:
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
    """Собрать dict переломного момента по индексу хода.

    followup_san: 5 полуходов ПОСЛЕ переломного — нужны Claude'у, чтобы
    описать «как соперник этим воспользовался» вместо общих слов про
    «решающую перестройку на другом участке доски». Без этого комментарий
    обрывается на самом интересном месте.
    """
    eval_idx = causing_idx + 1  # evals[causing_idx+1] = оценка ПОСЛЕ этого хода
    move_num = causing_idx // 2 + 1
    color = "белых" if causing_idx % 2 == 0 else "чёрных"
    san = san_list[causing_idx]
    eval_before = evals[causing_idx]
    eval_after = evals[eval_idx]

    board_at_tp = game.board()
    for j in range(causing_idx + 1):
        board_at_tp.push(moves[j])

    # Следующие 5 полуходов с их оценками — даём Claude конкретику,
    # чтобы он мог рассказать, что было после ошибки/находки.
    followup_san = []
    followup_evals = []
    for k in range(causing_idx + 1, min(causing_idx + 6, len(san_list))):
        followup_san.append(san_list[k])
        ev_after_k = evals[k + 1] if k + 1 < len(evals) else None
        if ev_after_k is not None:
            followup_evals.append(ev_after_k)

    return {
        "move_index": causing_idx + 1,
        "move_num": move_num,
        "color": color,
        "san": san,
        "eval_before": f"{eval_before:+.2f}",
        "eval_after": f"{eval_after:+.2f}",
        "swing": abs(eval_after - eval_before),
        "fen": board_at_tp.fen(),
        "followup_san": followup_san,
        "followup_evals": followup_evals,
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
                        mate_in = sc.mate()
                        # Далёкий мат = большое преимущество, не 99.0
                        # (иначе свинги между "мат в 15" и "+3" выглядят огромными)
                        if abs(mate_in) <= 5:
                            ev = 99.0 if mate_in > 0 else -99.0
                        else:
                            ev = 10.0 if mate_in > 0 else -10.0
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


def analyze_pawn_structure(fen: str) -> str:
    """Анализ пешечной структуры из FEN: проходные, изолированные, сдвоенные.
    Возвращает читаемый текст для промпта."""
    try:
        board = chess.Board(fen)
    except Exception:
        return ""

    files = "abcdefgh"
    file_idx = {f: i for i, f in enumerate(files)}

    # Собираем пешки по цветам: {file_index: [rank, ...]}
    w_pawns: dict[int, list[int]] = {}  # файл → список рядов
    b_pawns: dict[int, list[int]] = {}
    for sq in chess.SQUARES:
        piece = board.piece_at(sq)
        if piece is None or piece.piece_type != chess.PAWN:
            continue
        f = chess.square_file(sq)
        r = chess.square_rank(sq)
        if piece.color == chess.WHITE:
            w_pawns.setdefault(f, []).append(r)
        else:
            b_pawns.setdefault(f, []).append(r)

    def find_passed(own: dict[int, list[int]], opp: dict[int, list[int]], is_white: bool) -> list[str]:
        """Проходная пешка: на её файле и соседних файлах нет вражеских пешек впереди."""
        passed = []
        for f, ranks in own.items():
            adj_files = [af for af in [f - 1, f, f + 1] if 0 <= af <= 7]
            for r in ranks:
                blocked = False
                for af in adj_files:
                    for opp_r in opp.get(af, []):
                        if is_white and opp_r > r:
                            blocked = True
                        elif not is_white and opp_r < r:
                            blocked = True
                if not blocked:
                    sq_name = files[f] + str(r + 1)
                    passed.append(sq_name)
        return sorted(passed)

    def find_isolated(own: dict[int, list[int]]) -> list[str]:
        """Изолированная пешка: на соседних файлах нет своих пешек."""
        isolated = []
        for f, ranks in own.items():
            has_neighbor = any(af in own for af in [f - 1, f + 1] if 0 <= af <= 7)
            if not has_neighbor:
                for r in ranks:
                    isolated.append(files[f] + str(r + 1))
        return sorted(isolated)

    def find_doubled(own: dict[int, list[int]]) -> list[str]:
        """Сдвоенные пешки: 2+ пешки на одном файле."""
        doubled = []
        for f, ranks in own.items():
            if len(ranks) >= 2:
                doubled.append(files[f])
        return sorted(doubled)

    lines = []
    w_passed = find_passed(w_pawns, b_pawns, True)
    b_passed = find_passed(b_pawns, w_pawns, False)
    w_isolated = find_isolated(w_pawns)
    b_isolated = find_isolated(b_pawns)
    w_doubled = find_doubled(w_pawns)
    b_doubled = find_doubled(b_pawns)

    if w_passed:
        lines.append(f"Проходные белых: {', '.join(w_passed)}")
    if b_passed:
        lines.append(f"Проходные чёрных: {', '.join(b_passed)}")
    if not w_passed and not b_passed:
        lines.append("Проходных пешек нет")
    if w_isolated:
        lines.append(f"Изолированные белых: {', '.join(w_isolated)}")
    if b_isolated:
        lines.append(f"Изолированные чёрных: {', '.join(b_isolated)}")
    if w_doubled:
        lines.append(f"Сдвоенные белых: файлы {', '.join(w_doubled)}")
    if b_doubled:
        lines.append(f"Сдвоенные чёрных: файлы {', '.join(b_doubled)}")

    # Открытые и полуоткрытые линии
    open_files = []
    w_semi_open = []  # нет белой пешки, есть чёрная
    b_semi_open = []  # нет чёрной пешки, есть белая
    for f_idx in range(8):
        has_w = f_idx in w_pawns
        has_b = f_idx in b_pawns
        fname = files[f_idx]
        if not has_w and not has_b:
            open_files.append(fname)
        elif not has_w and has_b:
            w_semi_open.append(fname)
        elif has_w and not has_b:
            b_semi_open.append(fname)
    if open_files:
        lines.append(f"Открытые линии: {', '.join(open_files)}")
    if w_semi_open:
        lines.append(f"Полуоткрытые для белых: {', '.join(w_semi_open)}")
    if b_semi_open:
        lines.append(f"Полуоткрытые для чёрных: {', '.join(b_semi_open)}")

    return "\n".join(lines)


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
            mate_in = score.mate()
            abs_mate = abs(mate_in)
            if abs_mate <= 5:
                eval_num = 99.0 if mate_in > 0 else -99.0
                eval_str = f"Мат в {mate_in}"
            else:
                # Далёкий мат — показываем как большое преимущество, не "мат в X"
                eval_num = 10.0 if mate_in > 0 else -10.0
                eval_str = "решающий перевес" if mate_in > 0 else "решающий перевес чёрных"
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
def _trim_to_sentence(text: str) -> str:
    """Если текст обрезан на полуслове (нет точки/!/? в конце) — откатить до последнего предложения."""
    text = text.strip()
    if not text:
        return text
    if text[-1] in ".!?»":
        return text
    last_period = max(text.rfind(". "), text.rfind("! "), text.rfind("? "),
                     text.rfind("."), text.rfind("!"), text.rfind("?"))
    if last_period > len(text) // 3:  # не обрезать слишком коротко
        return text[:last_period + 1]
    return text


def get_gm_commentary(game_data: dict, eval_data: dict, event_type: str,
                      clock_info: dict | None = None,
                      eval_history: list | None = None,
                      opening_info: dict | None = None) -> str:
    client = Anthropic(api_key=ANTHROPIC_API_KEY)
    white = game_data["white"]["username"]
    black = game_data["black"]["username"]

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
        ev = eval_data["eval_num"]
        result_str = game_data.get("result", "*")

        missed_note = ""
        if event_type == "eval_swing_missed":
            base_ev = eval_data.get("baseline_eval_num", 0)
            missed_side = white if base_ev > 0 else black
            missed_note = f"\nВАЖНО: {missed_side} упустил(а) серьёзное преимущество! Позиция выровнялась. Укажи кто и как упустил."
        elif event_type == "eval_swing":
            if abs(ev) >= 2.0:
                if ev >= 2.0:
                    missed_note = f"\nСитуация критическая — у белых ({white}) серьёзное преимущество."
                else:
                    missed_note = f"\nСитуация критическая — у чёрных ({black}) серьёзное преимущество."

        # История оценок для game_over
        history_note = ""
        if event_type == "game_over" and eval_history:
            max_ev = max(h[1] for h in eval_history)
            min_ev = min(h[1] for h in eval_history)
            if max_ev >= 1.5:
                max_move = next(h[0] for h in eval_history if h[1] == max_ev)
                history_note += f"\nВ какой-то момент (ход ~{max_move}) у белых ({white}) было серьёзное преимущество."
            if min_ev <= -1.5:
                min_move = next(h[0] for h in eval_history if h[1] == min_ev)
                history_note += f"\nВ какой-то момент (ход ~{min_move}) у чёрных ({black}) было серьёзное преимущество."
            result_ru = {"1-0": f"1-0 ({white})", "0-1": f"0-1 ({black})",
                         "1/2-1/2": "½-½"}.get(result_str, "результат неизвестен")
            if max_ev >= 1.5 and abs(ev) < 0.8 and result_str == "1/2-1/2":
                history_note += f"\nВАЖНО: преимущество белых было упущено — партия завершилась вничью. Укажи это!"
            if min_ev <= -1.5 and abs(ev) < 0.8 and result_str == "1/2-1/2":
                history_note += f"\nВАЖНО: преимущество чёрных было упущено — партия завершилась вничью. Укажи это!"
            if max_ev >= 1.5 and result_str == "0-1":
                history_note += f"\nИнтересно: у белых было преимущество, но выиграли чёрные — произошёл перелом!"
            if min_ev <= -1.5 and result_str == "1-0":
                history_note += f"\nИнтересно: у чёрных было преимущество, но выиграли белые — произошёл перелом!"

        system, user = build_prompt(event_type,
            white=white, black=black,
            move_count=eval_data['move_count'],
            best_move=eval_data['best_move'],
            moves_san=eval_data.get('moves_san', []),
            fen=eval_data.get("fen", ""),
            eval_num=ev,
            time_note=time_note,
            missed_note=missed_note,
            history_note=history_note,
            opening_info=opening_info,
            result_str=result_str,   # game_over: явно указать кто выиграл,
                                     # иначе Claude гадает по позиции и путает
        )
        max_tokens = 350

    else:
        system, user = build_prompt(event_type,
            white=white, black=black,
            move_count=eval_data['move_count'],
            best_move=eval_data['best_move'],
            moves_san=eval_data.get('moves_san', []),
            fen=eval_data.get("fen", ""),
            time_note=time_note,
            opening_info=opening_info,
        )
        max_tokens = 220

    r = client.messages.create(
        model="claude-sonnet-4-6", max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}]
    )
    return _trim_to_sentence(r.content[0].text)


async def get_opening_analysis(game_data: dict, opening_info: dict,
                                white_rep: list, black_rep: list) -> str:
    client = Anthropic(api_key=ANTHROPIC_API_KEY)
    white = game_data["white"]["username"]
    black = game_data["black"]["username"]

    system, user = build_prompt("opening_analysis",
        white=white, black=black,
        opening=opening_info.get('opening') or opening_info.get('eco') or 'неизвестен',
        eco=opening_info.get('eco', '—'),
        first_moves=opening_info.get('first_moves', []),
        white_time=opening_info.get('white_time_remaining', '?'),
        black_time=opening_info.get('black_time_remaining', '?'),
        white_rep=white_rep,
        black_rep=black_rep,
    )

    r = client.messages.create(
        model="claude-sonnet-4-6", max_tokens=280,
        system=system,
        messages=[{"role": "user", "content": user}]
    )
    return _trim_to_sentence(r.content[0].text)


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

    # Конкретное продолжение партии (ходы соперника после переломного).
    # Формат: «28.Bxh6 (+1.4) Kg7 (+1.5) 29.Qd2 (+1.7) …». Так Claude
    # видит и сами ходы, и динамику оценки — и описание не сваливается
    # в общие слова про «решающую перестройку».
    followup_line = ""
    f_san = tp.get("followup_san", [])
    f_evals = tp.get("followup_evals", [])
    if f_san:
        # Полуход за ходом, добавляя номер хода и оценку, если есть.
        # causing_idx тут восстанавливаем из move_index/color: первый ply
        # в followup всегда сделан противоположным цветом.
        first_idx = tp["move_index"]  # первый ply followup
        parts = []
        for i, san in enumerate(f_san):
            ply = first_idx + i
            mv_n = ply // 2 + 1
            sep = "." if ply % 2 == 0 else "..."
            ev_str = ""
            if i < len(f_evals):
                ev_str = f" ({f_evals[i]:+.2f})"
            parts.append(f"{mv_n}{sep}{san}{ev_str}")
        followup_line = " ".join(parts)

    system, user = build_prompt("turning_point",
        white=white, black=black,
        result_ru=result_ru,
        what_happened=what_happened,
        fen=tp.get("fen", ""),
        followup_line=followup_line,
    )

    r = client.messages.create(
        # 450 вместо 300 — теперь промпт просит 3 конкретных предложения
        # с разбором followup-ходов, а не 2 общих. На 300 тексты обрезались
        # ровно перед «как именно белые конвертировали».
        model="claude-sonnet-4-6", max_tokens=450,
        system=system,
        messages=[{"role": "user", "content": user}]
    )
    return _trim_to_sentence(r.content[0].text)


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
    Включает результаты, переломные моменты и таблицу — всё одним текстовым сообщением.

    После итогов и таблицы — отдельный пост 🔍 *Разбор* с диаграммой
    для самой драматичной партии тура (если turning_point: true в YAML).
    """
    results_lines = []
    results_for_claude = []
    loop = asyncio.get_event_loop()

    # Кандидат «лучшая партия тура» — с максимальным swing-ом (по чувствительности
    # переломных моментов). Используется ниже для send_game_analysis.
    best_swing = 0.0
    best_pgn: str | None = None

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

        # Кандидат на «лучшую партию тура»: максимальный swing среди всех
        # переломных моментов любой партии. Кешированный find_turning_points
        # значит, что Stockfish уже отработал — выбор бесплатный.
        for tp in tps:
            if tp.get("swing", 0.0) > best_swing:
                best_swing = tp["swing"]
                best_pgn = pgn

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

    # Claude-разбор с голосом Полгар+Сейраван
    client = Anthropic(api_key=ANTHROPIC_API_KEY)
    # Достаём из активного YAML-профиля: список женщин в составе
    # (для смешанных турниров вроде Sigeman, где играет одна Чжу),
    # отображаемое имя и хэштег.
    _female = []
    _display = "турнира претендентов 2026"
    _hashtag = "#турнир_претендентов"
    if _main_profile:
        _female = [info["ru"] for info in _main_profile["players"].values()
                   if info.get("gender") == "f"]
        _display = _main_profile["display_name"] or _display
        _hashtag = _main_profile["hashtag"] or _hashtag
    system, user = build_prompt("round_summary",
        round_name=round_name,
        results_for_claude=results_for_claude,
        standings_text=standings_text,
        is_women=False,
        female_players=_female,
        tournament_display=_display,
        hashtag=_hashtag,
    )

    r = client.messages.create(
        model="claude-sonnet-4-6", max_tokens=700,
        system=system,
        messages=[{"role": "user", "content": user}]
    )
    analysis = _trim_to_sentence(r.content[0].text)
    # Убрать случайные markdown-заголовки если Claude всё же добавил
    analysis = re.sub(r'^#+\s+', '', analysis, flags=re.MULTILINE)

    results_block = "\n".join(results_lines)
    msg = (f"🏁 *{round_name} — итоги*\n\n"
           f"{results_block}\n\n"
           f"{analysis}")
    await send_update(bot, msg)
    # Автоматически показать таблицу очков после итогов тура.
    # На последнем туре — финальный пост с местами вместо обычной таблицы.
    await asyncio.sleep(2)
    sent_final = await send_final_post_if_last_round(
        bot, TELEGRAM_CHAT_ID,
        TOURNAMENT_PROFILES["open"], round_name, KNOWN_ROUND_IDS,
        OPEN_PLAYERS_RU,
    )
    if not sent_final:
        await send_standings(bot, TELEGRAM_CHAT_ID)

    # 🔍 Разбор лучшей партии тура — отдельным постом с диаграммой.
    # Один пост на тур, не на каждую партию (избегаем спама).
    # Минимальный порог swing 1.5 — иначе все партии «спокойные» и разбор
    # будет натянутым; в таком случае пропускаем разбор вовсе.
    if (TOURNAMENT_PROFILES["open"].get("turning_point", True)
            and best_pgn is not None
            and best_swing >= 1.5):
        await asyncio.sleep(2)
        try:
            best_data = pgn_to_game_data(best_pgn)
            await send_game_analysis(bot, best_data, best_pgn)
        except Exception as e:
            print(f"send_game_analysis (best of round) error: {e}")


async def get_round_preview(pairs: list[tuple[str, str]]) -> str:
    """Claude генерирует превью тура: стиль игроков и чего ожидать.

    Без блока «История встреч»: турниры не всегда круговые, и счёт «в этом
    турнире» либо отсутствует (первая встреча пары), либо тривиален
    (одна партия → 1:0 / 0:1 / ½:½). Историю классических встреч из
    внешних баз тянуть ненадёжно (скрейп, рейт-лимиты, риск 403 в момент
    отправки превью), поэтому в превью держимся стиля и интриги.
    """
    client = Anthropic(api_key=ANTHROPIC_API_KEY)

    pairs_lines = [f"• {w} (белые) – {b} (чёрные)" for w, b in pairs]
    pairs_text = "\n".join(pairs_lines)

    system, user = build_prompt("round_preview",
        pairs_text=pairs_text,
        num_pairs=len(pairs),
    )

    r = client.messages.create(
        model="claude-sonnet-4-6", max_tokens=800,
        system=system,
        messages=[{"role": "user", "content": user}]
    )
    return _trim_to_sentence(r.content[0].text)


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

    # Превью + стэндинги тянем параллельно (оба сетевые)
    preview = ""
    points: dict[str, float] = {}
    rounds_played = 0
    try:
        preview, (points, rounds_played) = await asyncio.gather(
            get_round_preview(pairs),
            calculate_standings(),
        )
    except Exception as e:
        print(f"send_round_start data error: {e}")

    pairs_lines = "\n".join([f"• *{w}* — *{b}*" for w, b in pairs])

    # Турнирный контекст перед туром — однострочный список «Имя N» через запятую.
    # Пропускаем на 1-м туре (rounds_played == 0): таблица из нулей бесполезна.
    standings_block = ""
    if rounds_played > 0 and points:
        items = []
        for name, pts in sorted(
            ((n, p) for n, p in points.items() if n in OPEN_PLAYERS_RU),
            key=lambda x: (-x[1], x[0]),
        ):
            pts_s = str(int(pts)) if pts == int(pts) else str(pts)
            items.append(f"{name} {pts_s}")
        if items:
            standings_block = (
                f"📊 *Положение перед {round_name}:* "
                f"{', '.join(items)}\n\n"
            )

    preview_block = f"🔮 *Чего ждать в туре:*\n{preview}\n\n" if preview else ""

    # Заголовок берём из активного профиля (TOURNAMENT_PROFILES["open"] подменяется
    # из tournaments.yaml на старте, см. блок YAML OVERRIDE выше). Так Sigeman будет
    # «🇸🇪 *TePe Sigeman & Co 2026 — Round 5*», а не «🏆 *Турнир Претендентов — ...*».
    _open_profile = TOURNAMENT_PROFILES.get("open", {})
    title_emoji = _open_profile.get("emoji", "🏆")
    title_name  = _open_profile.get("display_name", "Турнир претендентов")

    msg = (f"{title_emoji} *{title_name} — {round_name}*\n"
           f"_{today}_"
           f"{time_line}\n"
           f"{standings_block}"
           f"Пары тура:\n{pairs_lines}\n\n"
           f"{preview_block}"
           f"Слежу за всеми партиями 📡")
    await send_update(bot, msg)


async def check_pre_round_announcement(bot: Bot):
    """Проверяет расписание и отправляет анонс за ~30 мин до начала тура.
    Пары берём из PGN на Lichess (обычно доступны заранее)."""
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    for round_name, start_utc in ROUND_SCHEDULE.items():
        if round_name in pre_announced_rounds:
            continue
        # Ищём раунд, который начнётся через 10–35 минут
        delta = (start_utc - now_utc).total_seconds()
        if not (600 <= delta <= 2100):  # 10–35 min
            continue
        # Нашли подходящий раунд — ищем его round_id
        rid = None
        for r_id, r_name in KNOWN_ROUND_IDS:
            if r_name == round_name:
                rid = r_id
                break
        if not rid:
            continue
        # Пробуем получить PGN (пары могут быть уже выставлены)
        try:
            games_pgn, _ = await get_round_pgns(rid)
            if games_pgn:
                pre_announced_rounds.add(round_name)
                announced_rounds.add(rid)  # не дублировать потом
                await send_round_start(bot, round_name, games_pgn)
                print(f"Предварительный анонс {round_name} отправлен ({int(delta//60)} мин до старта)")
            else:
                print(f"PGN для {round_name} пока недоступен, повторю позже")
        except Exception as e:
            print(f"Ошибка предварительного анонса {round_name}: {e}")


async def send_pulse_update(bot: Bot, game_data: dict, pgn: str, label: str,
                           eval_data: dict | None = None, clock_info: dict | None = None,
                           tag: str = "📍"):
    """Короткий пульс-апдейт: оценка + часы + 2 предложения Claude.
    eval_data и clock_info можно передать извне чтобы не гонять Stockfish повторно.
    tag: эмодзи-префикс (📍 для Open, ♛ для Women)."""
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

    system, user = build_prompt("pulse",
        white=white, black=black, label=label,
        move_count=eval_data['move_count'],
        eval_str=eval_data['eval_str'],
        moves_san=eval_data.get('moves_san', []),
        fen=fen,
        clock_white=wr, clock_black=br,
        time_note=time_note,
    )

    r = client.messages.create(
        model="claude-sonnet-4-6", max_tokens=150,
        system=system,
        messages=[{"role": "user", "content": user}]
    )
    commentary = _trim_to_sentence(r.content[0].text)

    msg = (f"{tag} *{white} — {black}* | {label}\n"
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
                     commentary: str, clock_info: dict | None = None,
                     women: bool = False) -> str:
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
            думал = "думала" if women else "думал"
            move_sep = "..." if lt["color"] == "black" else "."
            clock_line += (f"\n🤔 Дольше всего {думал}: *{thinker}* — "
                           f"ход {lt['move_num']}{move_sep}{lt['san']} ({clock_info['longest_str']})")

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


# ─── ФИНАЛЬНАЯ ТАБЛИЦА С МЕСТАМИ (для последнего тура) ────────
async def build_final_standings(round_ids: list[tuple[str, str]],
                                 players_ru: set[str] | None = None
                                 ) -> list[dict]:
    """Вычислить финальные места с учётом тай-брейков после всех туров.

    Проходит по PGN каждого тура, собирает:
      points  — очки игрока
      wins    — число побед (3-й критерий тай-брейка)
      sb      — Sonneborn-Berger (2-й критерий; сумма очков побеждённых
                соперников + половина очков соперников, с которыми ничья)
      h2h     — словарь личных встреч h2h[a][b] = очки a против b
                (4-й критерий)

    Возвращает список словарей:
      {place, name, points, wins, sb, h2h_note, tied_group_size}

    Места при равенстве очков:
      1 место: в реальности решается плей-офф (рапид 15+10) — поэтому при
               делёжке первого места всем «первым» присваиваем место 1
               и помечаем tied_group_size > 1 (в посте напишем про тай-брейк).
      Прочие места: порядок SB → wins → h2h (per FIDE regulations 2026).
    """
    points:   dict[str, float]                = {}
    wins:     dict[str, int]                  = {}
    opponents: dict[str, list[tuple[str, float]]] = {}  # name → [(opp, score)]
    h2h:      dict[str, dict[str, float]]     = {}

    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        for rid, _rname in round_ids:
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
                    w = gd["white"]["username"]
                    b = gd["black"]["username"]
                    res = gd.get("result", "*")
                    if res not in ("1-0", "0-1", "1/2-1/2"):
                        continue
                    for p in (w, b):
                        points.setdefault(p, 0.0)
                        wins.setdefault(p, 0)
                        opponents.setdefault(p, [])
                        h2h.setdefault(p, {})
                    h2h[w].setdefault(b, 0.0)
                    h2h[b].setdefault(w, 0.0)
                    if res == "1-0":
                        points[w] += 1.0; wins[w] += 1
                        h2h[w][b] += 1.0
                        opponents[w].append((b, 1.0))
                        opponents[b].append((w, 0.0))
                    elif res == "0-1":
                        points[b] += 1.0; wins[b] += 1
                        h2h[b][w] += 1.0
                        opponents[w].append((b, 0.0))
                        opponents[b].append((w, 1.0))
                    else:
                        points[w] += 0.5; points[b] += 0.5
                        h2h[w][b] += 0.5; h2h[b][w] += 0.5
                        opponents[w].append((b, 0.5))
                        opponents[b].append((w, 0.5))
            except Exception as e:
                print(f"[build_final_standings] {rid}: {e}")

    if players_ru:
        # Доверяем PGN-именам; players_ru нужен только для фильтрации мусора.
        pass

    # Sonneborn-Berger: сумма (очки побеждённых) + 0.5*(очки тех, с кем ничья)
    sb: dict[str, float] = {}
    for p, opps in opponents.items():
        s = 0.0
        for opp, score in opps:
            if score == 1.0:
                s += points.get(opp, 0.0)
            elif score == 0.5:
                s += 0.5 * points.get(opp, 0.0)
        sb[p] = s

    # Сортировка: очки (desc) → SB (desc) → wins (desc)
    names = list(points.keys())
    names.sort(key=lambda n: (-points[n], -sb.get(n, 0.0), -wins.get(n, 0)))

    # Простановка мест.
    # Для 1-го места по регламенту FIDE 2026: равенство по ОЧКАМ означает
    # плей-офф (рапид), а SB/wins/h2h НЕ разрешают делёжку за 1 место.
    # Для остальных мест применяются SB → wins → h2h как тай-брейки.
    result = []
    max_points = points[names[0]] if names else 0
    leaders_group = [n for n in names if points[n] == max_points]
    # 1-е место — все, кто делит лидерство по очкам (помечаем tied_size)
    for n in leaders_group:
        result.append({
            "place":           1,
            "name":            n,
            "points":          points[n],
            "wins":            wins.get(n, 0),
            "sb":              sb.get(n, 0.0),
            "tied_group_size": len(leaders_group),
            "h2h":             h2h.get(n, {}),
        })
    # Для остальных — стандартное "dense" правило: одинаковое место
    # получают игроки с равными (points, sb, wins, h2h_sum).
    remaining = [n for n in names if n not in leaders_group]
    i = 0
    base_place = len(leaders_group) + 1
    while i < len(remaining):
        j = i
        ref = (points[remaining[i]], sb.get(remaining[i], 0.0),
               wins.get(remaining[i], 0))
        while j < len(remaining):
            cur = (points[remaining[j]], sb.get(remaining[j], 0.0),
                   wins.get(remaining[j], 0))
            if cur == ref:
                j += 1
            else:
                break
        place = base_place + i
        tied_size = j - i
        for k in range(i, j):
            n = remaining[k]
            result.append({
                "place":           place,
                "name":            n,
                "points":          points[n],
                "wins":            wins.get(n, 0),
                "sb":              sb.get(n, 0.0),
                "tied_group_size": tied_size,
                "h2h":             h2h.get(n, {}),
            })
        i = j

    return result


def format_final_post(profile: dict,
                      standings: list[dict],
                      round_name: str) -> str:
    """Собрать текст итогового поста последнего тура с местами.

    Отдельный блок про тай-брейк за 1 место, если лидеры делят очки."""
    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    lines = []
    for row in standings:
        place = row["place"]
        name  = row["name"]
        pts   = row["points"]
        pts_s = str(int(pts)) if pts == int(pts) else str(pts)
        marker = medals.get(place, f"{place}.")
        extra = f"  _(побед {row['wins']}, СБ {row['sb']:.2f})_" if place > 1 else ""
        lines.append(f"{marker} *{name}* — {pts_s}{extra}")

    # Делёжка 1 места → тай-брейк плей-офф
    leaders = [r for r in standings if r["place"] == 1]
    if len(leaders) == 1:
        champion = leaders[0]["name"]
        # Женский турнир — "чемпионка", мужской — "чемпион"
        title = "Чемпионка" if profile.get("emoji") == "♛" else "Чемпион"
        final_block = (f"🏆 *{title}* — {champion}\n"
                       f"Квалификация: {profile.get('qualifies_for', '')}.")
    else:
        tied_names = ", ".join(l["name"] for l in leaders)
        if len(leaders) == 2:
            tb = ("*Тай-брейк за 1 место:* две партии в рапид (15'+10\"); "
                  "при равенстве — блиц.")
        elif 3 <= len(leaders) <= 6:
            tb = ("*Тай-брейк за 1 место:* круговой турнир в рапид "
                  "между участницами делёжки.")
        else:
            tb = ("*Тай-брейк за 1 место:* круговой турнир в блиц "
                  "(10'+5\") между всеми делящими очки.")
        final_block = (f"⚖️ *1 место делят:* {tied_names}\n{tb}")

    header = f"{profile['emoji']} *{profile['display_name']} — итоги турнира*"
    body = "\n".join(lines)
    return f"{header}\n\n{body}\n\n{final_block}"


async def send_final_post_if_last_round(bot: Bot,
                                         chat_id: str | int,
                                         profile: dict,
                                         round_name: str,
                                         round_ids: list[tuple[str, str]],
                                         players_ru: set[str] | None = None
                                         ) -> bool:
    """Если round_name соответствует последнему туру профиля, отправить
    финальный пост с местами и вернуть True. Иначе вернуть False."""
    try:
        m = re.search(r'(\d+)', round_name or "")
        round_num = int(m.group(1)) if m else -1
    except Exception:
        round_num = -1
    if round_num != profile.get("total_rounds"):
        return False
    try:
        standings = await build_final_standings(round_ids, players_ru)
        if not standings:
            return False
        msg = format_final_post(profile, standings, round_name)
        await bot.send_message(chat_id=chat_id, text=msg, parse_mode="Markdown")
        return True
    except Exception as e:
        print(f"[send_final_post_if_last_round] ошибка: {e}")
        return False


# ─── ТАБЛИЦА ОЧКОВ ────────────────────────────────────────────
async def calculate_standings() -> tuple[dict[str, float], int]:
    """Собрать очки игроков Open турнира по всем сыгранным раундам.
    Возвращает (points, rounds_played)."""
    points: dict[str, float] = {name: 0.0 for name in OPEN_PLAYERS_RU}
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

    # «после 1 туров» режет глаз; используем ordinal, чтобы не возиться с
    # склонением «тур / тура / туров»
    rounds_str = f"после {rounds_played}-го тура" if rounds_played else "турнир ещё не начался"

    # Заголовок берём из активного профиля (см. блок YAML OVERRIDE).
    # Раньше тут был хардкод «Таблица Кандидатов 2026», который оставался
    # и для Sigeman, и для любого другого нового турнира.
    _open_profile = TOURNAMENT_PROFILES.get("open", {})
    emoji = _open_profile.get("emoji", "📊")
    name  = _open_profile.get("display_name", "Турнир претендентов")
    msg = f"{emoji} *Таблица — {name}* ({rounds_str})\n\n" + "\n".join(lines)
    await bot.send_message(chat_id=chat_id, text=msg, parse_mode="Markdown")


async def cmd_standings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/standings — текущая таблица очков."""
    bot = context.bot
    chat_id = update.effective_chat.id
    await send_standings(bot, chat_id)


async def send_women_standings(bot: Bot, chat_id: str | int):
    """Отправить текущую таблицу женского турнира."""
    try:
        points, rounds_played = await women_calculate_standings()
    except Exception as e:
        await bot.send_message(chat_id=chat_id, text=f"Ошибка получения таблицы: {e}")
        return

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
        pts_str = str(int(pts)) if pts == int(pts) else str(pts)
        lines.append(f"{medal} *{name}* — {pts_str}")

    rounds_str = f"после {rounds_played} туров" if rounds_played else "турнир ещё не начался"
    msg = f"♛ *Таблица Претенденток 2026* ({rounds_str})\n\n" + "\n".join(lines)
    await bot.send_message(chat_id=chat_id, text=msg, parse_mode="Markdown")


async def cmd_standings_women(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/standings_women — таблица женского турнира."""
    bot = context.bot
    chat_id = update.effective_chat.id
    await send_women_standings(bot, chat_id)


# ═══ ЖЕНСКИЙ ТУРНИР — ОБЛЕГЧЁННЫЙ МОНИТОРИНГ ══════════════════

async def get_women_active_round_id() -> tuple[str | None, str | None]:
    """Найти активный раунд женского турнира (аналог get_active_round_id)."""
    # Проверяем: если >2 дней после последнего тура — турнир окончен
    if WOMEN_KNOWN_ROUND_IDS:
        last_rid, last_rname = WOMEN_KNOWN_ROUND_IDS[-1]
        if last_rid in w_round_summary_done:
            return None, None
        last_scheduled = WOMEN_ROUND_SCHEDULE.get(last_rname)
        if last_scheduled:
            now_utc = datetime.datetime.now(datetime.timezone.utc)
            if now_utc > last_scheduled + datetime.timedelta(days=2):
                print(f"[Women] Турнир завершён (прошло >2 дней после {last_rname}).")
                return None, None

    latest_finished = None
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        for rid, rname in reversed(WOMEN_KNOWN_ROUND_IDS):
            try:
                r = await client.get(
                    f"https://lichess.org/api/broadcast/round/{rid}.pgn",
                    headers={"User-Agent": "CandidatesBot/1.0"}
                )
                if r.status_code == 200 and r.text.strip():
                    games = split_pgn(r.text)
                    if not games:
                        continue
                    started = [g for g in games if count_moves_pgn(g) > 0]
                    if not started:
                        continue
                    finished_count = sum(1 for g in games if is_game_finished(g))
                    if finished_count < len(games):
                        return rid, rname
                    if latest_finished is None:
                        latest_finished = (rid, rname)
                elif r.status_code == 404:
                    continue
            except Exception:
                pass
    if latest_finished:
        return latest_finished
    return None, None


async def discover_women_rounds():
    """Найти недостающие раунды женского турнира через Lichess Broadcast API.
    Lichess GET /api/broadcast/{seriesId} → JSON {tour: {...}, rounds: [...]}.
    Имена раундов приходят на русском: "Раунд 1", "Раунд 2" и т.д.
    Вызывается при старте бота и периодически из monitoring_loop."""
    import json as _json
    known_ids = {rid for rid, _ in WOMEN_KNOWN_ROUND_IDS}
    found_new = False
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            r = await client.get(
                f"https://lichess.org/api/broadcast/{WOMEN_BROADCAST_ID}",
                headers={"User-Agent": "CandidatesBot/1.0", "Accept": "application/json"}
            )
            print(f"[Women] discover_rounds: HTTP {r.status_code}, {len(r.text)} bytes")
            if r.status_code != 200:
                return
            data = _json.loads(r.text)
            rounds_list = data.get("rounds", [])
            for rd in rounds_list:
                rid = rd.get("id", "")
                rname = rd.get("name", "")  # "Раунд 1", "Раунд 12" и т.д.
                if not rid or rid in known_ids or rid == WOMEN_BROADCAST_ID:
                    continue
                # Нормализуем имя: "Раунд 12" → "Round 12" для совместимости
                m = re.search(r'(\d+)', rname)
                if m:
                    rname = f"Round {m.group(1)}"
                WOMEN_KNOWN_ROUND_IDS.append((rid, rname))
                known_ids.add(rid)
                found_new = True
                print(f"[Women] Обнаружен новый раунд: {rname} ({rid})")
            if found_new:
                def round_sort_key(item):
                    m = re.search(r'(\d+)', item[1])
                    return int(m.group(1)) if m else 999
                WOMEN_KNOWN_ROUND_IDS.sort(key=round_sort_key)
            print(f"[Women] Всего раундов: {len(WOMEN_KNOWN_ROUND_IDS)}")
    except Exception as e:
        print(f"[Women] discover_rounds error: {e}")


async def women_calculate_standings() -> tuple[dict[str, float], int]:
    """Таблица женского турнира."""
    points: dict[str, float] = {name: 0.0 for name in WOMEN_PLAYERS_RU}
    rounds_played = 0
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        for rid, _ in WOMEN_KNOWN_ROUND_IDS:
            try:
                r = await client.get(
                    f"https://lichess.org/api/broadcast/round/{rid}.pgn",
                    headers={"User-Agent": "CandidatesBot/1.0"}
                )
                if r.status_code != 200 or not r.text.strip():
                    continue
                games = split_pgn(r.text)
                all_finished = all(is_game_finished(g) for g in games)
                if not all_finished:
                    continue
                rounds_played += 1
                for pgn in games:
                    gd = pgn_to_game_data(pgn)
                    w, b = gd["white"]["username"], gd["black"]["username"]
                    res = gd.get("result", "*")
                    points.setdefault(w, 0.0)
                    points.setdefault(b, 0.0)
                    if res == "1-0":
                        points[w] += 1.0
                    elif res == "0-1":
                        points[b] += 1.0
                    elif res == "1/2-1/2":
                        points[w] += 0.5
                        points[b] += 0.5
            except Exception:
                pass
    return points, rounds_played


async def send_women_round_summary(bot: Bot, round_name: str, games_pgn: list[str]):
    """Итоги тура женского турнира — тот же формат, но с меткой ♛."""
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

        tps = await loop.run_in_executor(None, find_turning_points, pgn)
        tp_descs = []
        for tp in tps:
            ev_before = float(tp['eval_before'])
            ev_after = float(tp['eval_after'])
            delta = ev_after - ev_before
            side = tp['color']
            move_san = f"{tp['move_num']}. {tp['san']}" if side == "белых" else f"{tp['move_num']}...{tp['san']}"
            who = w if side == "белых" else b
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

    try:
        points, rounds_played = await women_calculate_standings()
        sorted_pts = sorted(points.items(), key=lambda x: -x[1])
        standings_text = ", ".join(f"{n} {p}" for n, p in sorted_pts if p > 0)
    except Exception:
        standings_text = "(таблица недоступна)"

    client = Anthropic(api_key=ANTHROPIC_API_KEY)
    system, user = build_prompt("round_summary",
        round_name=round_name,
        results_for_claude=results_for_claude,
        standings_text=standings_text,
        is_women=True,
    )

    r = client.messages.create(
        model="claude-sonnet-4-6", max_tokens=700,
        system=system,
        messages=[{"role": "user", "content": user}]
    )
    analysis = _trim_to_sentence(r.content[0].text)
    analysis = re.sub(r'^#+\s+', '', analysis, flags=re.MULTILINE)

    results_block = "\n".join(results_lines)
    msg = (f"♛ *{round_name} — итоги*\n\n"
           f"{results_block}\n\n"
           f"{analysis}")
    await send_update(bot, msg)
    # Автоматически показать таблицу очков после итогов тура.
    # На последнем туре — финальный пост с местами вместо обычной таблицы.
    await asyncio.sleep(2)
    sent_final = await send_final_post_if_last_round(
        bot, TELEGRAM_CHAT_ID,
        TOURNAMENT_PROFILES["women"], round_name, WOMEN_KNOWN_ROUND_IDS,
        WOMEN_PLAYERS_RU,
    )
    if not sent_final:
        await send_women_standings(bot, TELEGRAM_CHAT_ID)


# ═══════════════════════════════════════════════════════════════════════
# SECONDARY TOURNAMENTS — дайджест-пайплайн для GCT и пр.
# ═══════════════════════════════════════════════════════════════════════
# Один пост в день (daily_digest) со всеми результатами + таблицей + сюжетом.
# Не претендует на open-слот, не тревожит per-game логику Sigeman/Кандидатов.
#
# Поток:
#   secondary_monitoring_step → _process_secondary_tournament(per tid)
#     → _discover_secondary_rounds (Lichess /api/broadcast/{tour_id})
#     → _group_secondary_rounds_by_date (читаем [Date] из PGN)
#     → для каждой не-отправленной даты: send_daily_digest
#
# При первом обнаружении турнира (например Poland Rapid в середине) все
# прошедшие даты помечаются как «уже отправлены», чтобы не было ретро-флуда.

_RU_MONTHS = ["", "января", "февраля", "марта", "апреля", "мая", "июня",
              "июля", "августа", "сентября", "октября", "ноября", "декабря"]


def _format_ru_date(date_str: str) -> str:
    """'2026-05-06' → '6 мая'."""
    try:
        d = datetime.date.fromisoformat(date_str)
        return f"{d.day} {_RU_MONTHS[d.month]}"
    except Exception:
        return date_str


async def _discover_secondary_rounds(tid: str, profile: dict) -> list[tuple[str, str]]:
    """Подтянуть список (round_id, round_name) для secondary-турнира с Lichess.
    Кешируется по tid — повторные вызовы не дёргают Lichess.
    """
    if tid in secondary_round_ids_cache:
        return secondary_round_ids_cache[tid]

    broadcast_id = profile.get("broadcast_id") or ""
    rounds: list[tuple[str, str]] = []
    if not broadcast_id:
        # Заглушка — Lichess ещё не опубликовал трансляцию
        secondary_round_ids_cache[tid] = []
        return []

    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            r = await client.get(
                f"https://lichess.org/api/broadcast/{broadcast_id}",
                headers={"User-Agent": "CandidatesBot/1.0", "Accept": "application/json"}
            )
            if r.status_code == 200:
                data = r.json()
                for rd in data.get("rounds", []):
                    rid = rd.get("id", "")
                    rname = rd.get("name", "") or f"Round {len(rounds) + 1}"
                    if rid:
                        rounds.append((rid, rname))
                print(f"[Secondary {tid}] discover: {len(rounds)} раундов")
            else:
                print(f"[Secondary {tid}] discover HTTP {r.status_code}")
    except Exception as e:
        print(f"[Secondary {tid}] discover error: {e}")

    secondary_round_ids_cache[tid] = rounds
    return rounds


async def _group_secondary_rounds_by_date(round_ids: list[tuple[str, str]]) -> dict[str, list[dict]]:
    """Качаем PGN всех раундов и группируем по [Date] из PGN-тегов.
    Возвращает {ISO date: [{round_id, round_name, pgns, all_finished}, ...]}.
    """
    out: dict[str, list[dict]] = {}
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        for rid, rname in round_ids:
            try:
                r = await client.get(
                    f"https://lichess.org/api/broadcast/round/{rid}.pgn",
                    headers={"User-Agent": "CandidatesBot/1.0"}
                )
                if r.status_code != 200 or not r.text.strip():
                    continue
                pgns = split_pgn(r.text)
                if not pgns:
                    continue
                # Дата раунда — из PGN-тега первой партии
                m = re.search(r'\[Date "(\d{4})\.(\d{2})\.(\d{2})"\]', pgns[0])
                if not m:
                    continue
                date_str = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
                all_finished = all(is_game_finished(p) for p in pgns)
                out.setdefault(date_str, []).append({
                    "round_id":   rid,
                    "round_name": rname,
                    "pgns":       pgns,
                    "all_finished": all_finished,
                })
            except Exception as e:
                print(f"[Secondary] fetch round {rid}: {e}")
    return out


async def _calc_standings_secondary(round_ids: list[tuple[str, str]]) -> dict[str, float]:
    """Сводные очки игроков по всем раундам (не только сегодняшним).
    Имена нормализуются через PLAYER_NAMES_RU; для secondary-турниров без
    маппинга — остаются латиницей как в PGN.
    """
    points: dict[str, float] = {}
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        for rid, _ in round_ids:
            try:
                r = await client.get(
                    f"https://lichess.org/api/broadcast/round/{rid}.pgn",
                    headers={"User-Agent": "CandidatesBot/1.0"}
                )
                if r.status_code != 200:
                    continue
                for pgn in split_pgn(r.text):
                    if not is_game_finished(pgn):
                        continue
                    gd = pgn_to_game_data(pgn)
                    w = gd["white"]["username"]
                    b = gd["black"]["username"]
                    res = gd.get("result", "*")
                    if res == "1-0":
                        points[w] = points.get(w, 0.0) + 1.0
                    elif res == "0-1":
                        points[b] = points.get(b, 0.0) + 1.0
                    elif res == "1/2-1/2":
                        points[w] = points.get(w, 0.0) + 0.5
                        points[b] = points.get(b, 0.0) + 0.5
            except Exception as e:
                print(f"[Secondary] standings round {rid}: {e}")
    return points


def _format_points_compact(points: dict[str, float], top_n: int = 0) -> str:
    """Компактная строка таблицы: 'Caruana 5, Gukesh 4½, …'.
    top_n=0 → все игроки с очками > 0.
    """
    items = []
    for name, pts in sorted(points.items(), key=lambda x: (-x[1], x[0])):
        if pts <= 0:
            continue
        # Половинки красивее как ½
        if pts == int(pts):
            pts_s = str(int(pts))
        elif pts - int(pts) == 0.5:
            pts_s = f"{int(pts)}½" if pts >= 1 else "½"
        else:
            pts_s = str(pts)
        items.append(f"{name} {pts_s}")
        if top_n and len(items) >= top_n:
            break
    return ", ".join(items)


async def _claude_secondary_storyline(profile: dict, date_str: str,
                                       results_text: str,
                                       standings_text: str) -> str:
    """2–3 предложения от Claude про сюжет дня. Особый акцент — на Гукеша
    и Синдарова (оба играют матч за чемпиона мира 2026)."""
    if not ANTHROPIC_API_KEY:
        return ""
    client = Anthropic(api_key=ANTHROPIC_API_KEY)

    user = f"""Игровой день в {profile['display_name']} ({_format_ru_date(date_str)}).

Результаты дня (формат: ход_партии — результат):
{results_text}

Текущая турнирная таблица: {standings_text}

Особый контекст: Гукеш (Индия) и Синдаров (Узбекистан) играют здесь и
оба будут в матче за звание чемпиона мира 2026. Если кто-то из них
отметился ярко (победил, проиграл, или вышел в лидеры) — выдели это.

2–3 предложения о сюжете дня. НЕ перечисляй партии — они уже выше.
НЕ изобретай результатов и игроков, которых нет в данных.
В конце поставь хэштег: {profile.get('hashtag', '#chess')}"""

    try:
        r = client.messages.create(
            model="claude-sonnet-4-6", max_tokens=300,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user}]
        )
        return _trim_to_sentence(r.content[0].text)
    except Exception as e:
        print(f"[Secondary] storyline error: {e}")
        return ""


async def send_daily_digest(bot: Bot, profile: dict, date_str: str,
                             rounds_for_date: list[dict],
                             round_ids_all: list[tuple[str, str]]) -> None:
    """Отправить дайджест дня для secondary-турнира.

    Формат: заголовок (флаг + название + дата) → результаты дня →
    компактная таблица → 2–3 предложения сюжета от Claude → хэштег.
    """
    # Собираем строки результатов
    results_lines = []
    results_text_for_claude = []
    for r in rounds_for_date:
        for pgn in r["pgns"]:
            if not is_game_finished(pgn):
                continue
            gd = pgn_to_game_data(pgn)
            w = gd["white"]["username"]
            b = gd["black"]["username"]
            res = gd.get("result", "*")
            if res == "1-0":
                emoji = "⚪️"
            elif res == "0-1":
                emoji = "⚫️"
            elif res == "1/2-1/2":
                emoji = "🤝"
                res = "½-½"
            else:
                emoji = "·"
            results_lines.append(f"{emoji} {res}  *{w} – {b}*  ({r['round_name']})")
            results_text_for_claude.append(f"{w} {res} {b} ({r['round_name']})")

    if not results_lines:
        print(f"[Secondary {profile.get('id', '?')}] {date_str}: нет завершённых партий, пропуск")
        return

    # Таблица — по всем раундам, не только сегодняшним
    points = await _calc_standings_secondary(round_ids_all)
    standings_text = _format_points_compact(points) or "(пока нет очков)"

    # Сюжет дня — отдельным пассом Claude
    storyline = await _claude_secondary_storyline(
        profile, date_str,
        "\n".join(results_text_for_claude),
        standings_text,
    )

    title = f"{profile['emoji']} *{profile['display_name']} — итоги дня ({_format_ru_date(date_str)})*"
    msg_parts = [title, "", "Результаты:", *results_lines, "",
                 f"📊 *Положение:* {standings_text}"]
    if storyline:
        msg_parts.extend(["", storyline])

    msg = "\n".join(msg_parts)
    await send_update(bot, msg)
    print(f"[Secondary {profile.get('id', '?')}] {date_str}: digest отправлен")


async def _process_secondary_tournament(bot: Bot, tid: str, profile: dict,
                                        today_date: datetime.date) -> None:
    """Один шаг обработки одного secondary-турнира."""
    # Первое обнаружение — пометить все прошедшие даты как «уже посланы»,
    # чтобы не выкатить ретро-флуд при включении бота посреди турнира.
    if tid not in secondary_first_seen:
        secondary_first_seen.add(tid)
        sent = secondary_digest_sent.setdefault(tid, set())
        cur = profile["start_date"]
        while cur < today_date:
            sent.add(cur.isoformat())
            cur += datetime.timedelta(days=1)
        if sent:
            print(f"[Secondary {tid}] init: {len(sent)} прошлых дней помечены как отправленные")

    sent = secondary_digest_sent.setdefault(tid, set())

    round_ids = await _discover_secondary_rounds(tid, profile)
    if not round_ids:
        return

    rounds_by_date = await _group_secondary_rounds_by_date(round_ids)
    for date_str in sorted(rounds_by_date.keys()):
        if date_str in sent:
            continue
        try:
            day_date = datetime.date.fromisoformat(date_str)
        except ValueError:
            continue
        if day_date > today_date:
            continue   # на всякий — не обрабатываем будущее
        rounds_for_date = rounds_by_date[date_str]
        if not all(r["all_finished"] for r in rounds_for_date):
            continue   # не все партии этого дня доиграны

        try:
            await send_daily_digest(bot, profile, date_str,
                                    rounds_for_date, round_ids)
            sent.add(date_str)
        except Exception as e:
            print(f"[Secondary {tid} {date_str}] digest error: {e}")


async def secondary_monitoring_step(bot: Bot, now: float) -> None:
    """Один шаг мониторинга всех активных secondary-турниров.
    Вызывается из monitoring_loop. Тихо ничего не делает, если active
    secondaries нет."""
    if not _active_secondaries:
        return
    today_date = datetime.datetime.now(_UTC).date()
    for tid, profile in _active_secondaries:
        try:
            await _process_secondary_tournament(bot, tid, profile, today_date)
        except Exception as e:
            print(f"[Secondary {tid}] step error: {e}")


# ═══════════════════════════════════════════════════════════════════════


async def women_monitoring_step(bot: Bot, now: float):
    """Один шаг облегчённого мониторинга женского турнира.
    Вызывается из monitoring_loop каждые 5 минут.
    Обрабатывает: превью тура, пульсы (1ч, 2ч), итоги тура."""
    global w_last_discover_ts
    try:
        # Периодически обновляем список раундов (раз в 6 часов)
        if now - w_last_discover_ts > 21600:
            w_last_discover_ts = now
            await discover_women_rounds()

        round_id, round_name = await get_women_active_round_id()
        if not round_id:
            return

        games_pgn, pgn_debug = await get_round_pgns(round_id)
        print(f"[Women] {round_name} ({round_id}): {pgn_debug}")

        if not games_pgn:
            return

        pgn_move_counts = {p: count_moves_pgn(p) for p in games_pgn}
        pgn_is_finished = {p: is_game_finished(p) for p in games_pgn}

        # ── Анонс нового раунда (одно сообщение с парами) ──
        games_started = [p for p in games_pgn if pgn_move_counts[p] > 0]
        if (games_started
                and round_id not in w_announced_rounds
                and not all(pgn_is_finished[p] for p in games_pgn)):
            w_announced_rounds.add(round_id)
            max_moves = max(pgn_move_counts[p] for p in games_started)
            if max_moves <= 5:
                # Простой анонс без Claude — список пар
                pairs_lines = []
                for pgn in games_pgn:
                    gd = pgn_to_game_data(pgn)
                    w, b = gd["white"]["username"], gd["black"]["username"]
                    pairs_lines.append(f"• {w} — {b}")
                msg = f"♛ *{round_name} Women — начало*\n\n" + "\n".join(pairs_lines)
                await send_update(bot, msg)
            else:
                print(f"[Women] {round_name} уже в процессе ({max_moves} ходов) — анонс пропущен")

        # ── Полный цикл событий для каждой партии ──
        women_prof = TOURNAMENT_PROFILES["women"]
        for pgn in games_pgn:
            game_id = "w_" + pgn_game_id(pgn)   # префикс чтобы не путать с Open
            game_data = pgn_to_game_data(pgn)
            finished = pgn_is_finished[pgn]
            mc = pgn_move_counts[pgn]

            if mc == 0:
                w_seen_games[game_id] = None
                continue

            # Оценка позиции через Stockfish
            eval_data = evaluate_position(pgn)
            if not eval_data:
                continue

            # Запоминаем время старта
            if game_id not in w_game_start_times:
                scheduled_dt = WOMEN_ROUND_SCHEDULE.get(round_name)
                if scheduled_dt and mc > 3:
                    w_game_start_times[game_id] = scheduled_dt.timestamp()
                else:
                    w_game_start_times[game_id] = now
                # Помечаем пройденные пульсы и 15min по реальному времени
                real_elapsed = now - w_game_start_times[game_id]
                sent = w_games_pulse_sent.setdefault(game_id, set())
                for interval in WOMEN_PULSE_INTERVALS:
                    if real_elapsed > interval + 300:
                        sent.add(interval)
                if mc > 12 or real_elapsed > OPENING_STATUS_DELAY:
                    w_games_15min_done.add(game_id)

            prev = w_seen_games.get(game_id)
            event_type = None

            # 1. Конец партии
            if finished and game_id not in w_games_over_sent:
                w_games_over_sent.add(game_id)
                event_type = "game_over"

            # 2. Новая партия — первое обнаружение с ходами
            elif women_prof.get("new_game", False) and (prev is None or (isinstance(prev, dict) and prev.get("move_count", 0) == 0)) and not finished:
                event_type = "new_game"

            # 3. Резкое изменение оценки
            elif women_prof.get("eval_swing", False) and isinstance(prev, dict):
                baseline = w_games_baseline_eval.get(game_id, prev)
                if baseline:
                    swing = eval_data["eval_num"] - baseline["eval_num"]
                    if abs(swing) >= EVAL_SWING_THRESHOLD:
                        last_swing = w_games_swing_move.get(game_id, 0)
                        if eval_data["move_count"] - last_swing >= EVAL_SWING_COOLDOWN_MOVES:
                            w_games_swing_move[game_id] = eval_data["move_count"]
                            base_ev = baseline["eval_num"]
                            curr_ev = eval_data["eval_num"]
                            if abs(base_ev) >= 1.5 and abs(curr_ev) < 0.8:
                                event_type = "eval_swing_missed"
                                eval_data["baseline_eval_num"] = base_ev
                            else:
                                event_type = "eval_swing"

            if event_type:
                clock_info = analyze_clocks(pgn)
                hist = w_games_eval_history.get(game_id)
                op_info = extract_opening_info(pgn)
                commentary = get_gm_commentary(game_data, eval_data, event_type, clock_info,
                                               eval_history=hist, opening_info=op_info)
                msg = format_event_msg(game_data, eval_data, event_type, commentary, clock_info, women=True)
                msg = "♛ " + msg   # метка женского турнира
                await send_update_with_photo(bot, msg, pgn)
                w_games_baseline_eval[game_id] = eval_data

            w_seen_games[game_id] = eval_data
            w_games_baseline_eval.setdefault(game_id, eval_data)
            # История оценок для контекста game_over
            hist = w_games_eval_history.setdefault(game_id, [])
            hist.append((eval_data["move_count"], eval_data["eval_num"]))

            # ── 15-минутный анализ дебюта ──────────────────
            elapsed = now - w_game_start_times.get(game_id, now)
            if (women_prof.get("opening_15min", False)
                    and elapsed >= OPENING_STATUS_DELAY
                    and game_id not in w_games_15min_done
                    and eval_data["move_count"] >= 5
                    and not finished):
                w_games_15min_done.add(game_id)
                await send_15min_status(bot, game_data, pgn)

            # ── Пульс-апдейты (1ч, 2ч) ──────────────────
            if not finished:
                sent_pulses = w_games_pulse_sent.setdefault(game_id, set())
                for interval in WOMEN_PULSE_INTERVALS:
                    if elapsed >= interval and interval not in sent_pulses:
                        sent_pulses.add(interval)
                        label = WOMEN_PULSE_LABELS[interval]
                        clock_info = analyze_clocks(pgn)
                        await send_pulse_update(
                            bot, game_data, pgn, label,
                            eval_data=eval_data, clock_info=clock_info,
                            tag="♛"
                        )

        # ── Итоги тура — когда все партии завершены ──
        if (games_pgn
                and round_id not in w_round_summary_done
                and len(games_pgn) >= 2
                and all(pgn_is_finished[p] for p in games_pgn)):
            w_round_summary_done.add(round_id)
            await send_women_round_summary(bot, round_name, games_pgn)

    except Exception as e:
        print(f"[Women] Loop error: {e}")


# ─── ГЛАВНЫЙ ЦИКЛ ─────────────────────────────────────────────
async def monitoring_loop(bot: Bot):
    """Основной цикл мониторинга партий."""
    print("✅ Мониторинг запущен. Слежу за турниром претендентов 2026 (Open + Women)...")

    while True:
        try:
            now = time.time()

            # ── Предварительный анонс тура (за ~30 мин до старта) ──
            await check_pre_round_announcement(bot)

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

                # Запоминаем момент старта партии.
                # При перезапуске бота используем реальное время из расписания,
                # а не момент обнаружения — иначе пульсы теряются.
                if game_id not in game_start_times:
                    scheduled_dt = ROUND_SCHEDULE.get(round_name)
                    if scheduled_dt and eval_data["move_count"] > 3:
                        # Партия уже в процессе — используем реальное время старта
                        game_start_times[game_id] = scheduled_dt.timestamp()
                    else:
                        game_start_times[game_id] = now
                    # Бот мог перезапуститься — помечаем уже пройденные этапы
                    # по реально прошедшему времени (не по числу ходов!)
                    real_elapsed = now - game_start_times[game_id]
                    mc = eval_data["move_count"]
                    if mc > 12 or real_elapsed > OPENING_STATUS_DELAY:
                        games_15min_done.add(game_id)
                    sent = games_pulse_sent.setdefault(game_id, set())
                    for interval in PULSE_INTERVALS:
                        if real_elapsed > interval + 300:
                            # +300 сек (5 мин) буфер: если мы чуть позже —
                            # пульс уже точно был отправлен ранее
                            sent.add(interval)

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
                            if eval_data["move_count"] - last_swing >= EVAL_SWING_COOLDOWN_MOVES:
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

                # Проверяем профиль — мужской турнир может не отправлять некоторые события.
                # Внимание: games_over_sent уже добавлен выше, так что выключение
                # game_over_post НЕ ломает детект «все партии тура завершены»
                # (round_summary триггерится по pgn_is_finished, а не по этой ветке).
                open_prof = TOURNAMENT_PROFILES["open"]
                send_event = False
                if event_type == "game_over":
                    send_event = open_prof.get("game_over_post", True)
                elif event_type == "new_game":
                    send_event = open_prof.get("new_game", True)
                elif event_type in ("eval_swing", "eval_swing_missed"):
                    # Поздняя стадия партии (>60 ходов) — eval_swing уже излишен,
                    # game_over/round_summary всё равно подведут итог. Срезаем шум.
                    if eval_data["move_count"] > 60:
                        send_event = False
                    else:
                        send_event = open_prof.get("eval_swing", True)

                if event_type and send_event:
                    clock_info = analyze_clocks(pgn)
                    hist = games_eval_history.get(game_id)
                    op_info = extract_opening_info(pgn)
                    commentary = get_gm_commentary(game_data, eval_data, event_type, clock_info,
                                                   eval_history=hist, opening_info=op_info)
                    msg = format_event_msg(game_data, eval_data, event_type, commentary, clock_info)
                    await send_update_with_photo(bot, msg, pgn)
                if event_type:
                    # Обновляем базовую линию при любом событии (даже если не отправляли)
                    games_baseline_eval[game_id] = eval_data

                seen_games[game_id] = eval_data
                # Записываем историю оценок для контекста game_over
                hist = games_eval_history.setdefault(game_id, [])
                hist.append((eval_data["move_count"], eval_data["eval_num"]))

                # ── 15-минутный анализ дебюта ──────────────────
                elapsed = now - game_start_times.get(game_id, now)
                if (open_prof.get("opening_15min", True)
                        and elapsed >= OPENING_STATUS_DELAY
                        and game_id not in games_15min_done
                        and eval_data["move_count"] >= 5
                        and not finished):
                    games_15min_done.add(game_id)
                    await send_15min_status(bot, game_data, pgn)

                # ── Пульс-апдейты через 60/120/180 мин ─────────
                # Гейтим на алгоритм-флаге профиля (раньше не гейтилось — пульсы
                # шли даже при pulse: false в YAML). И PULSE_INTERVALS должен
                # быть непустым — пресет с pulse_intervals: [] = выключено.
                if (not finished
                        and open_prof.get("pulse", True)
                        and PULSE_INTERVALS):
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

        # ── Женский турнир — облегчённый мониторинг ──
        try:
            await women_monitoring_step(bot, time.time())
        except Exception as e:
            print(f"[Women] Step error: {e}")

        # ── Secondary-турниры — дайджест-пайплайн (Phase 2) ──
        # Всё, что в YAML с coverage_tier: secondary и active: true.
        # Один пост в день на турнир, не на каждый тур. Тихо ничего не
        # делает, если active secondaries нет.
        try:
            await secondary_monitoring_step(bot, time.time())
        except Exception as e:
            print(f"[Secondary] Step error: {e}")

        await asyncio.sleep(POLL_INTERVAL_SECONDS)


async def main():
    # Приложение с поддержкой команд
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("standings", cmd_standings))
    app.add_handler(CommandHandler("standings_women", cmd_standings_women))

    bot = app.bot

    async with app:
        await app.start()
        await app.updater.start_polling(drop_pending_updates=True)
        # Автодетект раундов женского турнира при старте
        await discover_women_rounds()
        print(f"✅ Бот запущен (Open + Women, {len(WOMEN_KNOWN_ROUND_IDS)} women rounds)")
        # Параллельно запускаем мониторинг
        await monitoring_loop(bot)
        # monitoring_loop бесконечный — до сюда не доходим при штатной работе
        await app.updater.stop()
        await app.stop()


if __name__ == "__main__":
    asyncio.run(main())
