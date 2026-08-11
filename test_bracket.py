"""Оффлайн-тесты bracket-логики (EWC 2026): группировка партий в матчи,
армагеддон, фантомные доски, детект сенсаций, финальные места.

Запуск: python test_bracket.py   (сетевые вызовы не выполняются)
"""
import os
import sys
import types
import asyncio

# ── Стабы тяжёлых зависимостей, чтобы импортировать bot.py без Telegram/Claude ──
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

import bot  # noqa: E402


def mk_pgn(white, black, result, moves="1. e4 e5 2. Nf3 Nc6",
           welo="2700", belo="2650", game_no="01.01"):
    body = f"{moves} {result}" if moves else (result if result != "*" else "*")
    return (f'[Event "Test"]\n[Round "{game_no}"]\n'
            f'[White "{white}"]\n[Black "{black}"]\n[Result "{result}"]\n'
            f'[WhiteElo "{welo}"]\n[BlackElo "{belo}"]\n\n{body}\n')


FAILED = []

def check(label, cond, extra=""):
    status = "ok" if cond else "FAIL"
    print(f"  [{status}] {label}" + (f" — {extra}" if extra and not cond else ""))
    if not cond:
        FAILED.append(label)


print("── 1. Bo2, решённый матч 1½–½ ──")
pgns = [
    mk_pgn("Grischuk, Alexander", "Aravindh, Chithambaram VR.", "1-0", welo="2628", belo="2677"),
    mk_pgn("Aravindh, Chithambaram VR.", "Grischuk, Alexander", "1/2-1/2", welo="2677", belo="2628", game_no="01.02"),
]
ms = bot._bracket_group_matches(pgns)
check("один матч", len(ms) == 1)
m = ms[0]
check("победитель Грищук", m["winner"] == "Грищук", str(m["winner"]))
check("счёт 1½:½", bot._fmt_score(m["score"][m["winner"]]) == "1½")
check("не армагеддон", not m["armageddon"])
check("решающая партия = победа Грищука", m["decisive_game"]["result"] == "1-0")
check("upset при gap=40 (2628 < 2677)", bot._bracket_is_upset(m, 40))
check("не upset при gap=60", not bot._bracket_is_upset(m, 60))

print("── 2. 1-1 + армагеддон вничью → матч берут чёрные ──")
pgns = [
    mk_pgn("Wei, Yi", "Esipenko, Andrey", "1-0", welo="2752", belo="2680"),
    mk_pgn("Esipenko, Andrey", "Wei, Yi", "1-0", welo="2680", belo="2752", game_no="01.02"),
    mk_pgn("Wei, Yi", "Esipenko, Andrey", "1/2-1/2", welo="2752", belo="2680", game_no="01.03"),
]
ms = bot._bracket_group_matches(pgns)
m = ms[0]
check("армагеддон распознан", m["armageddon"])
check("ничья в армагеддоне → победа чёрных (Есипенко)", m["winner"] == "Есипенко", str(m["winner"]))
check("upset: Есипенко (2680) обыграл Вэй И (2752), gap 50", bot._bracket_is_upset(m, 50))
check("решающая = армагеддон", m["decisive_game"]["game_no" if False else "result"] == "1/2-1/2")

print("── 3. Фантомная армагеддон-доска (0 ходов) игнорируется ──")
pgns = [
    mk_pgn("Artemiev, Vladislav", "Maghsoodloo, Parham", "1-0", welo="2653", belo="2705"),
    mk_pgn("Maghsoodloo, Parham", "Artemiev, Vladislav", "1/2-1/2", welo="2705", belo="2653", game_no="01.02"),
    mk_pgn("Artemiev, Vladislav", "Maghsoodloo, Parham", "*", moves="", game_no="01.03"),
]
ms = bot._bracket_group_matches(pgns)
m = ms[0]
check("фантомная доска не попала в матч", len(m["games"]) == 2)
check("победитель Артемьев", m["winner"] == "Артемьев")
check("не армагеддон", not m["armageddon"])
check("upset: Артемьев (2653) обыграл Магсудлу (2705)", bot._bracket_is_upset(m, 50))

print("── 4. Незаконченный матч 1-1 (армагеддон ещё идёт) ──")
pgns = [
    mk_pgn("Le, Quang Liem", "Bortnyk, Olexandr", "0-1", welo="2732", belo="2604"),
    mk_pgn("Bortnyk, Olexandr", "Le, Quang Liem", "0-1", welo="2604", belo="2732", game_no="01.02"),
    mk_pgn("Le, Quang Liem", "Bortnyk, Olexandr", "*", game_no="01.03"),  # арм. с ходами, без результата
]
ms = bot._bracket_group_matches(pgns)
m = ms[0]
check("победителя пока нет", m["winner"] is None)
check("строка матча — «не решён»", "не решён" in bot._bracket_match_line(m))

print("── 5. Раунд с 4 матчами (2 доски × 2 партии на пару) ──")
pgns = []
pairs = [("Grischuk, Alexander", "Aravindh, Chithambaram VR."),
         ("Artemiev, Vladislav", "Maghsoodloo, Parham"),
         ("Le, Quang Liem", "Bortnyk, Olexandr"),
         ("Esipenko, Andrey", "Wei, Yi")]
for a, b in pairs:
    pgns.append(mk_pgn(a, b, "1-0"))
    pgns.append(mk_pgn(b, a, "1/2-1/2", game_no="01.02"))
ms = bot._bracket_group_matches(pgns)
check("4 матча", len(ms) == 4, str(len(ms)))
check("во всех есть победитель", all(m["winner"] for m in ms))

print("── 6. Bo4 (плей-офф QF): 2½–1½ без армагеддона ──")
pgns = [
    mk_pgn("Carlsen, Magnus", "Niemann, Hans Moke", "1-0", welo="2839", belo="2700"),
    mk_pgn("Niemann, Hans Moke", "Carlsen, Magnus", "1-0", welo="2700", belo="2839", game_no="01.02"),
    mk_pgn("Carlsen, Magnus", "Niemann, Hans Moke", "1/2-1/2", welo="2839", belo="2700", game_no="01.03"),
    mk_pgn("Niemann, Hans Moke", "Carlsen, Magnus", "0-1", welo="2700", belo="2839", game_no="01.04"),
]
ms = bot._bracket_group_matches(pgns)
m = ms[0]
check("Карлсен выиграл 2½:1½", m["winner"] == "Карлсен"
      and bot._fmt_score(m["score"]["Карлсен"]) == "2½")
check("чётное число партий → не армагеддон", not m["armageddon"])
check("не upset (фаворит выиграл)", not bot._bracket_is_upset(m, 50))
check("решающая — последняя победа Карлсена (0-1 чёрными)",
      m["decisive_game"]["result"] == "0-1")

print("── 7. Строки постов ──")
line = bot._bracket_match_line(m)
check("строка матча содержит счёт", "2½:1½" in line, line)
check("½ рендер", bot._fmt_score(0.5) == "½" and bot._fmt_score(2.0) == "2")

print("── 8. Финальные места: регэкспы имён раундов ──")
async def fake_rounds_test():
    rounds = [
        {"id": "r1", "name": "Полуфиналы", "finished": True},
        {"id": "r2", "name": "Матч за 3-е место", "finished": True},
        {"id": "r3", "name": "Гранд-финал | Сет 1", "finished": True},
        {"id": "r4", "name": "Гранд-финал | Сет 2", "finished": True},
    ]
    fetched = []
    async def fake_get_round_pgns(rid):
        fetched.append(rid)
        if rid == "r2":   # бронза: Дуда обыграл Со
            return [mk_pgn("Duda, Jan-Krzysztof", "So, Wesley", "1-0"),
                    mk_pgn("So, Wesley", "Duda, Jan-Krzysztof", "1/2-1/2", game_no="01.02")], ""
        if rid in ("r3", "r4"):  # финал: Карлсен взял оба сета
            return [mk_pgn("Carlsen, Magnus", "Nakamura, Hikaru", "1-0"),
                    mk_pgn("Nakamura, Hikaru", "Carlsen, Magnus", "1/2-1/2", game_no="01.02"),
                    mk_pgn("Carlsen, Magnus", "Nakamura, Hikaru", "1-0", game_no="01.03"),
                    mk_pgn("Nakamura, Hikaru", "Carlsen, Magnus", "0-1", game_no="01.04")], ""
        return [], ""
    sent = []
    async def fake_send(bot_obj, msg):
        sent.append(msg)
    orig_fetch, orig_send = bot.get_round_pgns, bot.send_update
    bot.get_round_pgns, bot.send_update = fake_get_round_pgns, fake_send
    try:
        profile = {"display_name": "EWC 2026", "emoji": "🎮", "hashtag": "#ewc2026"}
        ok = await bot._bracket_send_final_places(None, profile, rounds)
    finally:
        bot.get_round_pgns, bot.send_update = orig_fetch, orig_send
    return ok, sent, fetched

ok, sent, fetched = asyncio.run(fake_rounds_test())
check("финальный пост отправлен", ok and len(sent) == 1)
check("места: Карлсен 1-й, Накамура 2-й", "🥇 *Карлсен*" in sent[0] and "🥈 Накамура" in sent[0],
      sent[0] if sent else "")
check("бронза: Дуда 3-й, Со 4-й", "🥉 Дуда" in sent[0] and "4. Уэсли Со" in sent[0])
check("семифинал не принят за финал", "r1" not in fetched)
check("взял ПОСЛЕДНИЙ сет финала (r4)", fetched and fetched[0] == "r4", str(fetched))

print()
if FAILED:
    print(f"❌ ПРОВАЛЕНО: {len(FAILED)}: {FAILED}")
    sys.exit(1)
print("✅ Все проверки пройдены")
