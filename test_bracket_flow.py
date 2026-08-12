"""Интеграционный тест bracket-пайплайна: _process_bracket_tournament.

Проверяет: стартовую амнистию, итоги этапа + сенсацию, ретрай после сбоя.
Запуск: python test_bracket_flow.py (сеть и Telegram застаблены).
"""
import os, sys, types, asyncio

os.environ.setdefault("TELEGRAM_TOKEN", "test")
os.environ.setdefault("TELEGRAM_CHAT_ID", "0")
os.environ.setdefault("ANTHROPIC_API_KEY", "test")

def _stub(name, **attrs):
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules[name] = m
    return m

class _Any:
    def __init__(self, *a, **kw): pass
    def __call__(self, *a, **kw): return self
    def __getattr__(self, item): return _Any()

_stub("cairosvg", svg2png=lambda **kw: b"")
_stub("telegram", Bot=_Any, Update=_Any)
_stub("telegram.ext", Application=_Any, CommandHandler=_Any, ContextTypes=_Any())
sys.modules["telegram.ext"].ContextTypes.DEFAULT_TYPE = object
_stub("anthropic", Anthropic=_Any)
_stub("commentary_prompts", build_prompt=lambda *a, **kw: ("", ""),
      SYSTEM_PROMPT="", get_position_analysis=lambda *a, **kw: "")

import bot
from test_bracket import mk_pgn  # noqa: E402 — реиспользуем фабрику PGN

FAILED = []
def check(label, cond, extra=""):
    print(f"  [{'ok' if cond else 'FAIL'}] {label}" + (f" — {extra}" if extra and not cond else ""))
    if not cond:
        FAILED.append(label)

PROFILE = {
    "id": "ewc_2026", "display_name": "Esports World Cup 2026",
    "emoji": "🎮", "hashtag": "#ewc2026",
    "broadcast_id": "MAINEVENT", "group_probe_id": "", "tour_name_exclude": [],
    "bracket_context": "тестовый контекст",
    "algorithms": {"round_summary": True, "upset_analysis": True,
                   "final_standings_with_places": True},
    "params": {"upset_rating_gap": 50},
}

# Раунд A завершён ещё ДО старта бота; раунд B завершается на 2-м тике.
ROUNDS = [
    {"id": "A", "name": "Группа A | Раунд 1", "startsAt": 1, "finished": True, "ongoing": False},
    {"id": "B", "name": "Группа A | Раунд 2", "startsAt": 2, "finished": False, "ongoing": True},
]
PGNS = {
    "A": [mk_pgn("Carlsen, Magnus", "Bok, Benjamin", "1-0", welo="2839", belo="2600"),
          mk_pgn("Bok, Benjamin", "Carlsen, Magnus", "0-1", welo="2600", belo="2839", game_no="01.02")],
    # B: Лазавик (2620) сенсационно обыгрывает Накамуру (2780) 1-1 + арм (ничья чёрными)
    "B": [mk_pgn("Nakamura, Hikaru", "Lazavik, Denis", "0-1", welo="2780", belo="2620"),
          mk_pgn("Lazavik, Denis", "Nakamura, Hikaru", "0-1", welo="2620", belo="2780", game_no="01.02"),
          mk_pgn("Nakamura, Hikaru", "Lazavik, Denis", "1/2-1/2", welo="2780", belo="2620", game_no="01.03")],
}

sent, analyzed, fail_next_send = [], [], []

async def fake_fetch_rounds(bid):
    return [dict(r) for r in ROUNDS]
async def fake_get_round_pgns(rid):
    return PGNS.get(rid, []), f"stub {rid}"
async def fake_send_update(b, msg):
    if fail_next_send:
        fail_next_send.pop()
        raise RuntimeError("simulated network error")
    sent.append(msg)
async def fake_send_game_analysis(b, gd, pgn):
    analyzed.append(gd)
async def fake_storyline(profile, stage, lines):
    return "Сюжет этапа. #ewc2026"

bot._bracket_fetch_rounds = fake_fetch_rounds
bot.get_round_pgns = fake_get_round_pgns
bot.send_update = fake_send_update
bot.send_game_analysis = fake_send_game_analysis
bot._claude_bracket_storyline = fake_storyline

async def main():
    print("── Тик 1 (t=0): стартовая амнистия (раунд A уже был завершён) ──")
    await bot._process_bracket_tournament(None, "ewc_2026", PROFILE, 0.0)
    check("ретро-постов нет", not sent, str(sent))
    check("раунд A помечен обработанным", "A" in bot.bracket_stage_done["ewc_2026"])

    print("── Тик 2 (t=1000): раунд B завершился — гейт стабилизации, поста ещё нет ──")
    ROUNDS[1]["finished"] = True
    await bot._process_bracket_tournament(None, "ewc_2026", PROFILE, 1000.0)
    check("пост не ушёл (ждём стабилизации состава)", not sent, str(sent))
    check("раунд B не помечен", "B" not in bot.bracket_stage_done["ewc_2026"])

    print("── Тик 3 (t=1700): состав стабилен, но сеть падает ──")
    fail_next_send.append(1)
    await bot._process_bracket_tournament(None, "ewc_2026", PROFILE, 1700.0)
    check("пост не ушёл (сбой)", not sent)
    check("раунд B НЕ помечен — будет ретрай", "B" not in bot.bracket_stage_done["ewc_2026"])

    print("── Тик 4 (t=2400): ретрай успешен ──")
    await bot._process_bracket_tournament(None, "ewc_2026", PROFILE, 2400.0)
    check("итоги этапа отправлены", any("Раунд 2: итоги" in m for m in sent), str(sent))
    check("сенсация отправлена", any("Сенсация" in m for m in sent))
    check("в сенсации Лазавик и армагеддон",
          any("Лазавик" in m and "армагеддон" in m for m in sent if "Сенсация" in m))
    check("разбор решающей партии запущен", len(analyzed) == 1)
    check("раунд B помечен", "B" in bot.bracket_stage_done["ewc_2026"])
    summary = next(m for m in sent if "итоги" in m)
    check("сюжет в посте", "Сюжет этапа" in summary)
    # Ничейный армагеддон: очки 1½:1½, победитель выделен + пометка «арм.»
    check("счёт матча в посте", "1½:1½" in summary and "арм." in summary, summary)

    print("── Тик 5 (t=3100): идемпотентность (ничего нового) ──")
    n = len(sent)
    await bot._process_bracket_tournament(None, "ewc_2026", PROFILE, 3100.0)
    check("дублей нет", len(sent) == n)

asyncio.run(main())
print()
if FAILED:
    print(f"❌ ПРОВАЛЕНО: {FAILED}")
    sys.exit(1)
print("✅ Все проверки пройдены")
