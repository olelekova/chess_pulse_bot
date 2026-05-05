"""
tournaments_config.py — загрузчик и валидатор tournaments.yaml.

Использование из bot.py:

    from tournaments_config import load_tournaments, get_active_tournaments

    CONFIG = load_tournaments()                  # все профили + defaults
    ACTIVE = get_active_tournaments(CONFIG)      # только те, что идут сегодня

    for tid, profile in ACTIVE.items():
        if profile["algorithms"]["pulse"]:
            ...

Поля profile (после нормализации):
    id, display_name, emoji, qualifies_for,
    start_date, end_date (date),
    total_rounds, rest_days (set[str]),
    broadcast_id, round_ids (list[(rid, name)]), autodiscover_rounds,
    algorithms (dict[str, bool]),
    params (dict с числовыми порогами — слиты с defaults),
    players (dict[surname] = {ru, chess_com})

CLI:
    python tournaments_config.py --validate
    python tournaments_config.py --active
    python tournaments_config.py --apply-preset full women_candidates_2026
"""

from __future__ import annotations

import argparse
import datetime
import sys
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError as e:
    raise SystemExit("pip install pyyaml") from e


CONFIG_PATH = Path(__file__).parent / "tournaments.yaml"

# Канонический список ключей, которые могут стоять в algorithms.
# Любой неизвестный ключ → ошибка валидации (защита от опечаток).
KNOWN_ALGORITHMS = {
    "preview",
    "new_game",
    "pulse",
    "eval_swing",
    "opening_15min",
    "turning_point",
    "round_summary",
    "game_over_post",
    "final_standings_with_places",
    "hourly_recap",
}

REQUIRED_FIELDS = {
    "display_name",
    "start_date",
    "end_date",
    "total_rounds",
    "lichess",
    "algorithms",
}


# ─── ЗАГРУЗКА ─────────────────────────────────────────────────────────
def load_tournaments(path: Path | str = CONFIG_PATH) -> dict[str, Any]:
    """Прочитать YAML, провалидировать, вернуть нормализованный конфиг.

    Возвращает: {
        "defaults": {...},
        "algorithm_catalog": {...},
        "presets": {...},
        "tournaments": { "tournament_id": profile_dict, ... }
    }

    Профили с именами, начинающимися на "_", пропускаются (это шаблоны).
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Не найден файл конфигурации: {path}")
    with path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    defaults = raw.get("defaults", {}) or {}
    catalog = raw.get("algorithm_catalog", {}) or {}
    presets = raw.get("presets", {}) or {}
    raw_tournaments = raw.get("tournaments", {}) or {}

    tournaments: dict[str, dict] = {}
    for tid, profile in raw_tournaments.items():
        if tid.startswith("_"):
            continue   # шаблон
        if profile is None:
            continue
        normalized = _normalize_profile(tid, profile, defaults)
        tournaments[tid] = normalized

    return {
        "defaults": defaults,
        "algorithm_catalog": catalog,
        "presets": presets,
        "tournaments": tournaments,
    }


def _normalize_profile(tid: str, raw: dict, defaults: dict) -> dict:
    """Привести raw-профиль из YAML в удобный для bot.py вид."""
    missing = REQUIRED_FIELDS - set(raw.keys())
    if missing:
        raise ValueError(f"[{tid}] нет обязательных полей: {sorted(missing)}")

    # Даты
    start = _to_date(raw["start_date"], f"{tid}.start_date")
    end = _to_date(raw["end_date"], f"{tid}.end_date")
    if end < start:
        raise ValueError(f"[{tid}] end_date < start_date")
    rest_days = {str(d) for d in (raw.get("rest_days") or [])}

    # Алгоритмы — провалидировать ключи
    algos = dict(raw["algorithms"] or {})
    unknown = set(algos) - KNOWN_ALGORITHMS
    if unknown:
        raise ValueError(
            f"[{tid}] неизвестные алгоритмы: {sorted(unknown)}.\n"
            f"  Допустимые: {sorted(KNOWN_ALGORITHMS)}"
        )
    # Все известные, но не указанные → False
    algos = {k: bool(algos.get(k, False)) for k in KNOWN_ALGORITHMS}

    # Параметры — слить с defaults, локальные перебивают
    params = dict(defaults)
    params.update(raw.get("params") or {})
    # Если algorithms.pulse выкл., но pulse_intervals не пуст — это нормально,
    # просто не сработает. Если включён, а интервалы пусты — предупреждение.
    if algos["pulse"] and not params.get("pulse_intervals"):
        print(f"[warn] {tid}: pulse включён, но pulse_intervals пуст — апдейтов не будет")

    # Lichess
    lichess = raw.get("lichess") or {}
    broadcast_id = lichess.get("broadcast_id") or ""
    round_ids_raw = lichess.get("round_ids") or []
    if not broadcast_id and not round_ids_raw:
        raise ValueError(
            f"[{tid}] нужен либо lichess.broadcast_id, либо хотя бы один round_ids"
        )
    round_ids = []
    for item in round_ids_raw:
        if not (isinstance(item, list) and len(item) == 2):
            raise ValueError(
                f"[{tid}] каждый элемент lichess.round_ids должен быть [id, name]: {item!r}"
            )
        round_ids.append((str(item[0]), str(item[1])))
    autodiscover = bool(lichess.get("autodiscover_rounds", True))

    # Игроки
    players = {}
    for surname, info in (raw.get("players") or {}).items():
        if isinstance(info, str):
            players[surname] = {"ru": info, "chess_com": ""}
        elif isinstance(info, dict):
            players[surname] = {
                "ru": info.get("ru", surname),
                "chess_com": info.get("chess_com", ""),
            }
        else:
            raise ValueError(f"[{tid}] players[{surname}] неверного типа: {type(info)}")

    return {
        "id": tid,
        "active":               bool(raw.get("active", True)),
        "display_name":         raw["display_name"],
        "emoji":                raw.get("emoji", "♟️"),
        "qualifies_for":        raw.get("qualifies_for", ""),
        "start_date":           start,
        "end_date":             end,
        "total_rounds":         int(raw["total_rounds"]),
        "rest_days":            rest_days,
        "broadcast_id":         broadcast_id,
        "round_ids":            round_ids,
        "autodiscover_rounds":  autodiscover,
        "algorithms":           algos,
        "params":               params,
        "players":              players,
        "tiebreak_rules":       raw.get("tiebreak_rules", params.get("tiebreak_rules", [])),
    }


def _to_date(v: Any, label: str) -> datetime.date:
    if isinstance(v, datetime.date):
        return v
    if isinstance(v, str):
        try:
            return datetime.date.fromisoformat(v)
        except ValueError as e:
            raise ValueError(f"{label}: '{v}' не ISO-дата (YYYY-MM-DD)") from e
    raise ValueError(f"{label}: ожидалась дата, получили {type(v).__name__}")


# ─── ВЫБОР АКТИВНЫХ ───────────────────────────────────────────────────
def get_active_tournaments(config: dict, today: datetime.date | None = None) -> dict:
    """Вернуть только те профили, которые активны на сегодня:
       active=true И start_date ≤ today ≤ end_date+2 (хвост на пост-турнирные посты).
    """
    if today is None:
        today = datetime.datetime.now(datetime.timezone.utc).date()
    result = {}
    for tid, profile in config["tournaments"].items():
        if not profile["active"]:
            continue
        # +2 дня после end_date — чтобы успеть отправить итоги последнего тура
        if profile["start_date"] <= today <= profile["end_date"] + datetime.timedelta(days=2):
            result[tid] = profile
    return result


# ─── ПРИМЕНИТЬ ПРЕСЕТ ─────────────────────────────────────────────────
def apply_preset_to_yaml(preset_name: str, tournament_id: str,
                         path: Path | str = CONFIG_PATH) -> None:
    """Заменить блок algorithms у указанного турнира на пресет.
    Пишет YAML обратно. Простая замена — комментарии в YAML потеряются.
    """
    path = Path(path)
    with path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    presets = raw.get("presets", {})
    if preset_name not in presets:
        raise SystemExit(f"Нет пресета '{preset_name}'. Доступны: {list(presets)}")
    if tournament_id not in raw.get("tournaments", {}):
        raise SystemExit(f"Нет турнира '{tournament_id}'")
    raw["tournaments"][tournament_id]["algorithms"] = dict(presets[preset_name])
    with path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(raw, f, allow_unicode=True, sort_keys=False)
    print(f"[ok] Применён пресет '{preset_name}' к '{tournament_id}'")


# ─── CLI ──────────────────────────────────────────────────────────────
def _cli() -> int:
    parser = argparse.ArgumentParser(description="tournaments.yaml — валидация и инспекция")
    parser.add_argument("--validate", action="store_true",
                        help="прочитать и провалидировать YAML, выйти")
    parser.add_argument("--active", action="store_true",
                        help="показать активные на сегодня турниры")
    parser.add_argument("--list-algos", action="store_true",
                        help="каталог всех алгоритмов")
    parser.add_argument("--apply-preset", nargs=2, metavar=("PRESET", "TOURNAMENT"),
                        help="применить пресет к турниру")
    parser.add_argument("--config", default=str(CONFIG_PATH))
    args = parser.parse_args()

    cfg = load_tournaments(args.config)

    if args.apply_preset:
        apply_preset_to_yaml(args.apply_preset[0], args.apply_preset[1], args.config)
        return 0

    if args.list_algos:
        for k, info in cfg["algorithm_catalog"].items():
            on_in = [tid for tid, p in cfg["tournaments"].items() if p["algorithms"].get(k)]
            print(f"  {k:32}  {info.get('title', '')}")
            print(f"    when: {info.get('when', '')}")
            print(f"    on:   {on_in or '—'}")
        return 0

    if args.active:
        today = datetime.datetime.now(datetime.timezone.utc).date()
        active = get_active_tournaments(cfg, today)
        print(f"Сегодня {today}, активных турниров: {len(active)}")
        for tid, p in active.items():
            on = [k for k, v in p["algorithms"].items() if v]
            print(f"  • {tid} ({p['display_name']}) — алгоритмы: {', '.join(on) or 'нет'}")
        return 0

    if args.validate or True:   # default action
        print(f"[ok] YAML валиден. Турниров: {len(cfg['tournaments'])}")
        for tid, p in cfg["tournaments"].items():
            on = sum(p["algorithms"].values())
            status = "active" if p["active"] else "inactive"
            print(f"  • {tid:30} {status:8} {p['start_date']}..{p['end_date']}  "
                  f"алгоритмов вкл: {on}/{len(KNOWN_ALGORITHMS)}")
        return 0


if __name__ == "__main__":
    sys.exit(_cli())
