"""Мульти-туровый сценарий EWC: Group Stage A + B + Playoffs (реальная
структура Lichess, обнаруженная 12 авг). Проверяет: покрытие ОБЕИХ групп,
метки этапов с именем tour'а, и что «Upper | Финалы» внутри группы не
принимается за гранд-финал (финальный пост только из Playoffs-tour'а).

Запуск: python test_bracket_multitour.py (офлайн).
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
from test_bracket import mk_pgn

FAILED = []
def check(label, cond, extra=""):
    print(f"  [{'ok' if cond else 'FAIL'}] {label}" + (f" — {extra}" if extra and not cond else ""))
    if not cond:
        FAILED.append(label)

PROFILE = {
    "id": "ewc_2026", "display_name": "Esports World Cup 2026",
    "emoji": "🎮", "hashtag": "#ewc2026",
    "broadcast_id": "", "group_probe_id": "PROBE", "tour_name_exclude": ["Play-in"],
    "bracket_context": "тест", "algorithms": {"round_summary": True,
    "upset_analysis": False, "final_standings_with_places": True},
    "params": {"upset_rating_gap": 50},
}

TOURS = [("GA", "Group Stage | A"), ("GB", "Group Stage | B")]
ROUNDS = {
    "GA": [{"id": "a1", "name": "Upper | Раунд 1", "finished": True, "ongoing": False},
           {"id": "af", "name": "Upper | Финалы", "finished": True, "ongoing": False}],
    "GB": [{"id": "b1", "name": "Upper | Раунд 1", "finished": True, "ongoing": False}],
}
def two_games(w, b):
    return [mk_pgn(w, b, "1-0"), mk_pgn(b, w, "1/2-1/2", game_no="01.02")]
PGNS = {
    "a1": two_games("Carlsen, Magnus", "Duda, Jan-Krzysztof"),
    "af": two_games("Carlsen, Magnus", "Niemann, Hans Moke"),
    "b1": two_games("Nepomniachtchi, Ian", "Bok, Benjamin"),
    "pf": two_games("Carlsen, Magnus", "Nepomniachtchi, Ian"),
    "p3": two_games("Duda, Jan-Krzysztof", "Niemann, Hans Moke"),
}

sent = []
async def fake_resolve(tid, profile, now):
    return list(TOURS)
async def fake_fetch_rounds(bid):
    return [dict(r) for r in ROUNDS.get(bid, [])]
async def fake_get_round_pgns(rid):
    return PGNS.get(rid, []), f"stub {rid}"
async def fake_send_update(b, msg):
    sent.append(msg)
async def fake_storyline(profile, stage, lines):
    return "Сюжет."

bot._bracket_resolve_tours = fake_resolve
bot._bracket_fetch_rounds = fake_fetch_rounds
bot.get_round_pgns = fake_get_round_pgns
bot.send_update = fake_send_update
bot._claude_bracket_storyline = fake_storyline

async def main():
    # Сброс состояния и отключение стартовой амнистии (иначе всё «прошлое»)
    bot.bracket_stage_done.clear(); bot.bracket_final_sent.clear()
    bot.bracket_seen_tours.update(["GA", "GB", "PO"])

    print("── Групповой день: обе группы завершили по этапу ──")
    await bot._process_bracket_tournament(None, "ewc_2026", PROFILE, 0.0)
    check("сразу постов нет — гейт стабилизации", len(sent) == 0, str(len(sent)))
    await bot._process_bracket_tournament(None, "ewc_2026", PROFILE, 700.0)
    check("3 поста итогов (A: 2 этапа, B: 1)", len(sent) == 3, str(len(sent)))
    check("метка группы A в посте", any("Group Stage | A · Upper | Раунд 1" in m for m in sent))
    check("метка группы B в посте", any("Group Stage | B · Upper | Раунд 1" in m for m in sent))
    check("групповые «Финалы» НЕ породили пост с местами 1-4",
          not any("итоги турнира" in m for m in sent))

    print("── Плей-офф появился в группе бродкастов ──")
    TOURS.append(("PO", "Playoffs"))
    ROUNDS["PO"] = [
        {"id": "p3", "name": "Матч за 3-е место", "finished": False, "ongoing": True},
        {"id": "pf", "name": "Гранд-финал", "finished": False, "ongoing": False},
    ]
    sent.clear()
    await bot._process_bracket_tournament(None, "ewc_2026", PROFILE, 1400.0)
    check("плей-офф идёт — финального поста ещё нет",
          not any("итоги турнира" in m for m in sent))

    print("── Гранд-финал и бронза завершены ──")
    for r in ROUNDS["PO"]:
        r["finished"] = True
    sent.clear()
    await bot._process_bracket_tournament(None, "ewc_2026", PROFILE, 2100.0)   # fp store
    await bot._process_bracket_tournament(None, "ewc_2026", PROFILE, 2800.0)   # post
    final = [m for m in sent if "итоги турнира" in m]
    check("финальный пост отправлен", len(final) == 1, str(sent))
    if final:
        check("Карлсен 1-й (из Playoffs, не из групповых «Финалов»)",
              "🥇 *Карлсен*" in final[0] and "🥈 Непомнящий" in final[0], final[0])
        check("бронза: Дуда 3-й", "🥉 Дуда" in final[0])

asyncio.run(main())
print()
if FAILED:
    print(f"❌ ПРОВАЛЕНО: {FAILED}")
    sys.exit(1)
print("✅ Все проверки пройдены")
