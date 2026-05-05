# Runbook для агента-наполнителя турниров

Этот файл — инструкция для будущего агента, который будет добавлять новые турниры в `tournaments.yaml` без участия человека.

## Цель

По одной строке от пользователя («началcя турнир X, охвати его») — заполнить блок в `tournaments.yaml` и провалидировать. Никакого ручного копи-паста.

---

## Шаг 1. Найти Lichess broadcast tour ID

Ключ ко всему — это `broadcast_id` (8 символов). От него API даёт всё остальное.

### Способ A: Lichess Broadcast API (предпочтительно)

Lichess не индексируется Google глубоко, но у него открытый API.

```javascript
// Список активных/будущих/прошедших трансляций (в браузере или curl)
fetch('https://lichess.org/api/broadcast/top?nb=50', {
  headers: {'Accept': 'application/json'}
})
  .then(r => r.json())
  .then(j => {
    const all = [...j.active, ...j.upcoming,
                 ...(j.past?.currentPageResults || [])];
    const match = all.find(x =>
      x.tour.name.toLowerCase().includes('SEARCH_TERM'));
    return match.tour.id;        // ← вот broadcast_id
  });
```

Структура ответа (упрощённо):
```json
{
  "active":   [{ "tour": {"id": "1X75mYa7", "name": "...", "slug": "..."}, "rounds": [...] }, ...],
  "upcoming": [...],
  "past":     { "currentPageResults": [...] }
}
```

### Способ B: HTML-страница раунда

Если знаем ID любого раунда — открыть `https://lichess.org/broadcast/{slug}/round-N/{roundId}` и в исходнике найти tour ID. Но `top` API проще.

---

## Шаг 2. Получить все раунды по broadcast_id

```javascript
fetch('https://lichess.org/api/broadcast/' + tourId, {
  headers: {'Accept': 'application/json'}
})
  .then(r => r.json())
  .then(j => j.rounds.map(r => ({
    id:       r.id,        // ← round_id для YAML
    name:     r.name,      // "Round 1", "Раунд 1" — отнормализовать
    slug:     r.slug,
    startsAt: r.startsAt,  // unix ms — пригодится для расписания
    finished: r.finished
  })));
```

`name` приходит на языке Accept-Language. Нормализовать на «Round N» (английский), потому что bot.py матчит по этой строке (`announce_rounds`, `KNOWN_ROUND_IDS`).

---

## Шаг 3. Получить состав игроков

Из PGN первого раунда:
```bash
curl -s 'https://lichess.org/api/broadcast/round/{roundId}.pgn' | grep -E '^\[(White|Black)' | sort -u
```

Тэги PGN дают фамилии в формате «Carlsen, Magnus» или «Carlsen». В YAML кладём ключ = фамилия из PGN.

---

## Шаг 4. Сгенерировать русские имена

Эвристика для транслитерации фамилий:
- Если игрок уже есть в других турнирах (`open_candidates_2026`, `women_candidates_2026`) — **взять оттуда** (та же транслитерация).
- Иначе — стандартная транслитерация.

| Латиница | Кириллица | Заметка |
|---|---|---|
| Carlsen | Карлсен | |
| Abdusattorov | Абдусатторов | |
| Erigaisi | Эригайси | |
| Erdogmus / Erdoğmuş | Эрдогмуш | турецкий ğ |
| Foreest / Van Foreest | Ван Форест | оба ключа в YAML |
| Grandelius | Гранделиус | |
| Woodward | Вудворд | |
| Zhu Jiner | Чжу Цзиньэр | китайский pinyin |

В сомнительных случаях — спросить у пользователя.

---

## Шаг 5. Определить даты и расписание

- `start_date` / `end_date` — из tour metadata (`startsAt` первого и последнего раунда) или из tour `description`.
- Туры играются ежедневно или с днями отдыха (rest_days). Видно по `startsAt` соседних раундов: разница > 24ч → день между ними отдыха.

```python
rest_days = []
for prev, curr in zip(rounds, rounds[1:]):
    delta = (curr.startsAt - prev.startsAt) / (1000*86400)
    if delta > 1.5:
        # есть выходной между prev и curr
        rest_days.append(date_after(prev.startsAt))
```

---

## Шаг 6. Выбрать профиль алгоритмов

Эвристика по типу турнира:

| Турнир | Профиль | Почему |
|---|---|---|
| Кандидаты (любой) | `full` | главное событие, эталон |
| Супертурнир ≥ 2700 средний | `light + eval_swing` | мало шуму, но громкие зевки |
| Опен / Swiss | `light` | слишком много партий для пульсов |
| Чемпионат мира матч | `full + game_over_post` | каждая партия — событие |
| Рапид/блиц | `minimal` | контроль слишком быстрый для пульсов |

В сомнительных случаях — `light` (наименее спам-генеративный).

`eval_swing_threshold` подбирается по среднему рейтингу:
- < 2600: 1.2 (чаще ошибаются)
- 2600–2700: 1.3
- > 2700: 1.5

---

## Шаг 7. Записать в YAML и провалидировать

Скопировать `_template`, заполнить, прогнать:
```bash
python tournaments_config.py --validate
```
Валидатор поймает опечатки в ключах алгоритмов. Если ругается — править YAML, не игнорировать.

---

## Полный псевдокод агента

```python
async def add_tournament(query: str):  # query = "tepe sigeman 2026"
    # 1. broadcast_id
    top = await fetch_json('/api/broadcast/top?nb=50')
    candidates = [t for bucket in [top['active'], top['upcoming']] for t in bucket]
    tour = best_match(candidates, query)
    if not tour:
        return ask_user("не нашёл — дай URL")

    # 2. rounds
    detail = await fetch_json(f'/api/broadcast/{tour["tour"]["id"]}')
    rounds = [(r['id'], r['name'], r['startsAt']) for r in detail['rounds']]

    # 3. игроки из PGN первого раунда
    pgn = await fetch_text(f'/api/broadcast/round/{rounds[0][0]}.pgn')
    surnames = parse_pgn_players(pgn)

    # 4. русские имена
    existing = load_existing_player_names()
    players = {s: existing.get(s) or transliterate(s) for s in surnames}
    unsure = [s for s, ru in players.items() if not existing.get(s)]
    if unsure:
        players |= ask_user_to_confirm(unsure)

    # 5. даты + rest_days
    start = date_from_ms(rounds[0][2])
    end   = date_from_ms(rounds[-1][2])
    rest_days = compute_rest_days(rounds)

    # 6. профиль
    avg_rating = avg_rating_from_pgn(pgn)
    profile = choose_profile(tour['tour']['name'], avg_rating)

    # 7. YAML
    new_block = build_yaml_block(tour, rounds, players, start, end,
                                 rest_days, profile)
    append_to_tournaments_yaml(new_block)
    run_validation()
```

---

## Что хранить вне YAML

Ничего критичного не должно дублироваться. Единственный источник истины — `tournaments.yaml`. Логи, состояния партий, время отправки уведомлений — это runtime state, не конфиг (см. `seen_games`, `games_pulse_sent` и т.д. в `bot.py`).

---

## Известные ограничения

- Lichess broadcast API анонимный, но rate-limited (~30 запросов/мин).
- `name` раунда зависит от Accept-Language; всегда форсировать `en` или нормализовать.
- Если у турнира hidden/private broadcast — потребуется auth-токен (мы такие пока не покрываем).
- Tour ID статичный, round ID статичный — кэшировать можно агрессивно. Но `startsAt` могут переноситься (зевок, технический форс-мажор) — стоит обновлять расписание ежедневно.
