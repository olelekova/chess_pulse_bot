# Как добавить новый турнир за 2 минуты

Все турниры описаны декларативно в `tournaments.yaml`. Никакого Python-кода править не нужно.

## Шаги

**1. Найди broadcast на Lichess.** Открой страницу турнира на lichess.org/broadcast/...
Из URL вытащи `broadcast_id` (последняя часть пути серии).

**2. Скопируй блок `_template`** из `tournaments.yaml` под новым именем:

```yaml
tournaments:
  # ...existing tournaments...

  fide_grand_swiss_2026:
    active: true
    display_name: "FIDE Grand Swiss"
    emoji: "🏆"
    qualifies_for: "выход в Турнир претендентов 2027"
    start_date: "2026-09-04"
    end_date:   "2026-09-15"
    total_rounds: 11
    rest_days: ["2026-09-09"]

    lichess:
      broadcast_id: "AbCdEfGh"
      round_ids: []                # пусто = autodiscover подтянет
      autodiscover_rounds: true

    algorithms:
      preview: true
      new_game: false
      pulse: false
      eval_swing: true
      opening_15min: false
      turning_point: true
      round_summary: true
      game_over_post: true
      final_standings_with_places: true
      hourly_recap: false

    params:
      pulse_intervals: []          # без пульсов
      eval_swing_threshold: 1.5    # для опен-турнира можно поднять порог

    players:
      Caruana:    { ru: "Каруана" }
      Nakamura:   { ru: "Накамура" }
      # ... только тех, кого хочешь по-русски называть
```

**3. Выбери алгоритмы апдейтов.** Доступны:

| Алгоритм | Когда срабатывает | Зачем |
|---|---|---|
| `preview` | за час до старта тура | анонс пар |
| `new_game` | при старте каждой партии | отдельный пост |
| `pulse` | через 1ч / 2ч / 3ч после старта | оценка + что было |
| `eval_swing` | при `\|Δeval\| ≥ threshold` | ключевые ошибки |
| `opening_15min` | через 15 мин после старта | оценка дебюта |
| `turning_point` | после партии | разбор перелома |
| `round_summary` | после всех партий тура | итог + таблица |
| `game_over_post` | сразу как партия завершена | счёт + ссылка |
| `final_standings_with_places` | в итоге последнего тура | 1/2/3 места |
| `hourly_recap` | каждый час во время тура | дайджест по всем партиям |

Можно использовать **пресеты** (внизу `tournaments.yaml`):

- `full` — всё включено (как женский Кандидатов)
- `light` — превью + переломы + итоги (как мужской Кандидатов)
- `minimal` — только итоги тура и финальные места
- `hourly` — экспериментальный, с часовыми обзорами

Применить пресет:
```bash
python tournaments_config.py --apply-preset full fide_grand_swiss_2026
```

**4. Провалидируй:**
```bash
python tournaments_config.py --validate
```
Покажет все турниры, статус, и сколько алгоритмов включено.

**5. Посмотри активные сегодня:**
```bash
python tournaments_config.py --active
```

**6. Закоммить и задеплой.** Бот сам подхватит новый турнир, как только наступит `start_date`.

---

## Когда турнир заканчивается

Просто оставь `active: true`. Бот автоматически перестанет работать с ним через 2 дня после `end_date` (хвост нужен, чтобы успели уйти посты с финальными местами).

Если хочешь принудительно выключить — поставь `active: false`.

---

## Параметры (`params`) — числовые пороги

| Параметр | Default | Что меняет |
|---|---|---|
| `poll_interval_seconds` | 300 | как часто бот опрашивает Lichess |
| `eval_swing_threshold` | 1.2 | порог для алгоритма `eval_swing` (в пешках) |
| `novelty_move_threshold` | 15 | при `opening_15min`: «новинка», если дебют кончился до этого хода |
| `opening_status_delay` | 900 | задержка перед `opening_15min` (сек) |
| `pulse_intervals` | `[3600, 7200, 10800]` | моменты для `pulse` от старта партии (сек) |

Локальные `params` у турнира перебивают `defaults`. Не указанные — берутся из `defaults`.

---

## Тай-брейки

Для `final_standings_with_places` бот использует `tiebreak_rules` (по умолчанию из `defaults`):
`["playoff_first", "sb", "wins", "h2h"]` — плей-офф для 1-го места, потом Зоннеборн-Бергер, число побед, личная встреча. Поменяй под регламент конкретного турнира.

---

## Что осталось

`tournaments.yaml` + `tournaments_config.py` — это **декларация и загрузчик**. Сам `bot.py` пока ещё читает старые `TOURNAMENT_PROFILES` / `KNOWN_ROUND_IDS` / `ROUND_SCHEDULE` напрямую. Чтобы переключить его на YAML, нужно один раз заменить эти константы вызовом `load_tournaments()` — это отдельный коммит, чтобы не сломать работающий бот в разгар Кандидатов.

После переключения:
- добавление нового турнира = один блок в YAML;
- никаких правок Python.
