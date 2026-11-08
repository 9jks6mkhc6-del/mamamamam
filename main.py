"""
Птичка + Кейсы — Telegram бот + мини-апп + админ-панель.
aiogram 3 + FastAPI + SQLite. Всё в одном файле.

Запуск:  python main.py
Переменные окружения (или поменяй константы ниже):
  BOT_TOKEN   — токен от @BotFather
  ADMIN_IDS   — id админов через запятую, например 123456,789012
  WEBAPP_URL  — публичный https-адрес (BotHost даёт домен, галочка «Использовать домен»)
  PORT        — порт (по умолчанию 3000)

Что внутри:
  * Птичка между колоннами: выплата = ставка × колонны × pillar_pct / 100
  * Кейсы (платные и бесплатный ежедневный), призы падают в инвентарь,
    их можно продать за ⭐ или запросить выдачу подарком у админа
  * Реферальная программа: пригласившему ref_pct % от чистого проигрыша приглашённого
  * Админка /admin: статистика, кейсы и шансы, шансы игроков, выдача баланса,
    бан, рассылка, заявки на вывод и на выдачу подарков, настройки
"""
import asyncio
import hashlib
import hmac
import json
import logging
import os
import random
import sys
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from html import escape
from urllib.parse import parse_qsl

import aiosqlite
import uvicorn
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
    WebAppInfo,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

# ============================ НАСТРОЙКИ ============================
BOT_TOKEN = os.getenv("BOT_TOKEN", "PASTE_TOKEN_HERE")
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x}
WEBAPP_URL = os.getenv("WEBAPP_URL", "https://your-domain.example")
PORT = int(os.getenv("PORT", "3000"))
DB_PATH = os.getenv("DB_PATH", "birdgame.db")

# ---- сложность птички: чем больше колонн, тем быстрее и уже проход ----
SPEED_BASE = 170    # px/сек в начале
SPEED_STEP = 3      # +px/сек за каждую пройденную колонну
SPEED_MAX = 290     # потолок скорости
GAP_BASE = 150      # просвет между колоннами в начале (px)
GAP_STEP = 1        # на сколько px сужается просвет с каждой колонной
GAP_MIN = 110       # минимальный просвет
SPACING = 255       # расстояние между колоннами (px)
FIRST_DIST = 444    # путь до первой колонны (px)

# настройки по умолчанию (потом меняются в админке)
DEFAULTS = {
    "min_bet": "10",
    "max_bet": "1000",
    "pillar_pct": "10",      # % от ставки за каждую колонну
    "min_withdraw": "50",
    "max_pillars": "200",
    "ref_pct": "10",         # % от прибыли бота с приглашённого, который получает пригласивший
    "gift_min": "25",        # с какой стоимости приз можно запросить подарком (а не продать)
    "maintenance": "0",      # 1 = технические работы
}
SETTING_NAMES = {
    "min_bet": "Мин. ставка (⭐)",
    "max_bet": "Макс. ставка (⭐)",
    "pillar_pct": "% от ставки за колонну",
    "min_withdraw": "Мин. вывод (⭐)",
    "max_pillars": "Макс. колонн за игру",
    "ref_pct": "Реферальный % ",
    "gift_min": "Мин. стоимость для выдачи подарком (⭐)",
    "maintenance": "Тех. работы (1/0)",
}

# стартовые кейсы: (эмодзи, название, цена, кулдаун_часов, [(эмодзи, название, стоимость, шанс%)])
SEED_CASES = [
    ("🎁", "Ежедневный", 0, 24, [
        ("🪙", "Монетка", 0.1, 99.99),
        ("⭐", "Звёздочка", 0.5, 0),
        ("🍬", "Леденец", 1, 0),
        ("🧸", "Мишка", 5, 0),
        ("🌹", "Роза", 25, 0),
        ("🏆", "Кубок", 100, 0),
        ("💎", "NFT-подарок", 500, 0.01),
    ]),
    ("📦", "Стартовый", 10, 0, [
        ("🪙", "Монетка", 2, 43), ("🧸", "Мишка", 5, 30), ("🌹", "Роза", 10, 14),
        ("🎂", "Торт", 20, 9), ("🏆", "Кубок", 50, 3.5), ("💎", "NFT-подарок", 200, 0.5),
    ]),
    ("🥈", "Серебряный", 50, 0, [
        ("🧸", "Мишка", 10, 42), ("🌹", "Роза", 25, 28), ("🎂", "Торт", 50, 17),
        ("🏆", "Кубок", 100, 9), ("💍", "Кольцо", 250, 3.7), ("💎", "NFT-подарок", 1000, 0.3),
    ]),
    ("🥇", "Золотой", 250, 0, [
        ("🌹", "Роза", 50, 40), ("🎂", "Торт", 125, 28), ("🏆", "Кубок", 250, 17),
        ("💍", "Кольцо", 500, 10), ("👑", "Корона", 1000, 4.5), ("💎", "NFT-подарок", 5000, 0.5),
    ]),
]

if ":" not in BOT_TOKEN:
    sys.exit("Укажи BOT_TOKEN (переменная окружения или константа в начале файла)")

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("birdgame")

bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())
router = Router()
admin = Router()
admin.message.filter(F.from_user.id.in_(ADMIN_IDS))
admin.callback_query.filter(F.from_user.id.in_(ADMIN_IDS))
dp.include_router(admin)
dp.include_router(router)

db: aiosqlite.Connection = None  # type: ignore
SETTINGS: dict = {}
BOT_USERNAME = ""
RNG = random.SystemRandom()
LOCKS = defaultdict(asyncio.Lock)


def sf(key: str) -> float:
    return float(SETTINGS[key])


def fmt(x) -> str:
    s = f"{float(x):.2f}".rstrip("0").rstrip(".")
    return s or "0"


def fmt4(x) -> str:
    s = f"{float(x):.4f}".rstrip("0").rstrip(".")
    return s or "0"


# ---- защита от накрутки птички: минимально возможное время прохождения N колонн
def speed_at(score: int) -> float:
    return min(SPEED_MAX, SPEED_BASE + SPEED_STEP * score)


def min_time(n: int) -> float:
    if n <= 0:
        return 0.0
    t = FIRST_DIST / speed_at(0)
    for k in range(2, n + 1):
        t += SPACING / speed_at(k - 1)
    return t * 0.9


def max_pillars_by_time(elapsed: float, cap: int) -> int:
    n = 0
    while n < cap and min_time(n + 1) <= elapsed:
        n += 1
    return n


# ============================ БАЗА ============================
async def init_db():
    global db
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    await db.executescript(
        """
        CREATE TABLE IF NOT EXISTS users(
            id INTEGER PRIMARY KEY, username TEXT, name TEXT,
            balance REAL DEFAULT 0, games INTEGER DEFAULT 0, best INTEGER DEFAULT 0,
            total_bet REAL DEFAULT 0, total_won REAL DEFAULT 0,
            banned INTEGER DEFAULT 0, created REAL,
            ref_by INTEGER, ref_earned REAL DEFAULT 0,
            ref_net REAL DEFAULT 0, ref_hwm REAL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS games(
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, bet REAL,
            pillars INTEGER DEFAULT 0, payout REAL DEFAULT 0, status TEXT,
            started REAL, finished REAL);
        CREATE TABLE IF NOT EXISTS withdrawals(
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, amount REAL,
            status TEXT DEFAULT 'pending', created REAL);
        CREATE TABLE IF NOT EXISTS payments(
            charge_id TEXT PRIMARY KEY, user_id INTEGER, amount INTEGER, created REAL);
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS cases(
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, emoji TEXT,
            price REAL DEFAULT 0, cooldown_h REAL DEFAULT 0,
            active INTEGER DEFAULT 1, sort INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS prizes(
            id INTEGER PRIMARY KEY AUTOINCREMENT, case_id INTEGER, name TEXT, emoji TEXT,
            value REAL, weight REAL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS inv(
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, name TEXT, emoji TEXT,
            value REAL, status TEXT DEFAULT 'own', created REAL);
        CREATE TABLE IF NOT EXISTS case_opens(
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, case_id INTEGER,
            prize_id INTEGER, price REAL, value REAL, created REAL);
        CREATE TABLE IF NOT EXISTS user_odds(
            user_id INTEGER, prize_id INTEGER, mult REAL,
            PRIMARY KEY(user_id, prize_id));
        """
    )
    # миграция для старых баз
    for col, ddl in (("ref_by", "INTEGER"), ("ref_earned", "REAL DEFAULT 0"),
                     ("ref_net", "REAL DEFAULT 0"), ("ref_hwm", "REAL DEFAULT 0")):
        try:
            await db.execute(f"ALTER TABLE users ADD COLUMN {col} {ddl}")
        except Exception:
            pass
    for k, v in DEFAULTS.items():
        await db.execute("INSERT OR IGNORE INTO settings VALUES(?,?)", (k, v))
    cnt = await one("SELECT COUNT(*) n FROM cases")
    if cnt["n"] == 0:
        for i, (emoji, name, price, cd, prizes) in enumerate(SEED_CASES):
            cur = await db.execute(
                "INSERT INTO cases(name, emoji, price, cooldown_h, sort) VALUES(?,?,?,?,?)",
                (name, emoji, price, cd, i))
            for pe, pn, pv, pw in prizes:
                await db.execute(
                    "INSERT INTO prizes(case_id, name, emoji, value, weight) VALUES(?,?,?,?,?)",
                    (cur.lastrowid, pn, pe, pv, pw))
    await db.commit()
    rows = await db.execute_fetchall("SELECT key, value FROM settings")
    SETTINGS.update({r["key"]: r["value"] for r in rows})


async def one(sql, args=()):
    cur = await db.execute(sql, args)
    return await cur.fetchone()


async def notify(uid: int, text: str):
    try:
        await bot.send_message(uid, text)
    except Exception:
        pass


async def notify_admins(text: str, kb=None):
    for a in ADMIN_IDS:
        try:
            await bot.send_message(a, text, reply_markup=kb)
        except Exception:
            pass


async def ref_bonus(uid: int, delta: float):
    """
    Пригласившему — ref_pct % от ЧИСТОЙ прибыли бота с этого игрока.
    delta = результат одной игры для бота (ставка - выплата): плюс, если игрок проиграл,
    минус, если выиграл. Выигрыши игрока гасят прошлые проигрыши, а бонус платится только
    когда чистая прибыль выходит на новый максимум. Так нельзя накрутить бонус
    через второй аккаунт, а реферер никогда не получит больше ref_pct % от чистого проигрыша.
    """
    row = await one("SELECT ref_by, ref_net, ref_hwm FROM users WHERE id=?", (uid,))
    if not row or not row["ref_by"]:
        return
    net = (row["ref_net"] or 0.0) + delta
    hwm = row["ref_hwm"] or 0.0
    bonus = round(max(0.0, net - hwm) * sf("ref_pct") / 100, 2)
    if bonus < 0.01:
        await db.execute("UPDATE users SET ref_net=? WHERE id=?", (net, uid))
        return
    await db.execute("UPDATE users SET ref_net=?, ref_hwm=? WHERE id=?", (net, max(hwm, net), uid))
    await db.execute(
        "UPDATE users SET balance=ROUND(balance+?,2), ref_earned=ROUND(ref_earned+?,2) WHERE id=?",
        (bonus, bonus, row["ref_by"]))


# ============================ АВТОРИЗАЦИЯ МИНИ-АППА ============================
def verify_init(init_data: str) -> dict:
    try:
        data = dict(parse_qsl(init_data, keep_blank_values=True))
        got = data.pop("hash")
        check = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        calc = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calc, got):
            raise ValueError("hash")
        if time.time() - int(data.get("auth_date", 0)) > 86400:
            raise ValueError("expired")
        return json.loads(data["user"])
    except Exception:
        raise HTTPException(401, "Открой игру через бота")


async def auth(init_data: str):
    u = verify_init(init_data)
    await db.execute(
        "INSERT INTO users(id, username, name, created) VALUES(?,?,?,?) "
        "ON CONFLICT(id) DO UPDATE SET username=excluded.username, name=excluded.name",
        (u["id"], u.get("username"), u.get("first_name", ""), time.time()),
    )
    await db.commit()
    row = await one("SELECT * FROM users WHERE id=?", (u["id"],))
    if row["banned"]:
        raise HTTPException(403, "Ты заблокирован")
    return row


# ============================ API ============================
@asynccontextmanager
async def lifespan(app: FastAPI):
    global BOT_USERNAME
    await init_db()
    try:
        await bot.delete_webhook()  # если раньше стоял вебхук, polling иначе не заработает
        BOT_USERNAME = (await bot.get_me()).username or ""
    except Exception as e:
        log.error("Не удалось связаться с Telegram (проверь BOT_TOKEN): %s", e)
    task = asyncio.create_task(dp.start_polling(bot, handle_signals=False))
    log.info("Бот запущен: @%s", BOT_USERNAME)
    yield
    task.cancel()
    await bot.session.close()
    await db.close()


app = FastAPI(lifespan=lifespan)


class StartReq(BaseModel):
    bet: float


class FinishReq(BaseModel):
    game_id: int
    pillars: int


class AmountReq(BaseModel):
    amount: int


class CaseReq(BaseModel):
    case_id: int


class InvReq(BaseModel):
    inv_id: int


@app.get("/", response_class=HTMLResponse)
async def index():
    cfg = {"speedBase": SPEED_BASE, "speedStep": SPEED_STEP, "speedMax": SPEED_MAX,
           "gapBase": GAP_BASE, "gapStep": GAP_STEP, "gapMin": GAP_MIN, "spacing": SPACING}
    return HTMLResponse(INDEX_HTML.replace("__CFG__", json.dumps(cfg)))


@app.get("/health")
async def health():
    return {"ok": True}


@app.get("/api/me")
async def api_me(x_init_data: str = Header("")):
    u = await auth(x_init_data)
    top = await db.execute_fetchall(
        "SELECT name, username, best FROM users WHERE best>0 AND banned=0 ORDER BY best DESC LIMIT 10"
    )
    invited = await one("SELECT COUNT(*) n FROM users WHERE ref_by=?", (u["id"],))
    return {
        "id": u["id"],
        "balance": u["balance"],
        "best": u["best"],
        "settings": {k: float(SETTINGS[k]) for k in
                     ("min_bet", "max_bet", "pillar_pct", "min_withdraw", "ref_pct", "gift_min")},
        "maintenance": SETTINGS["maintenance"] == "1",
        "top": [{"name": r["name"] or r["username"] or "Игрок", "best": r["best"]} for r in top],
        "ref": {
            "link": f"https://t.me/{BOT_USERNAME}?start=ref_{u['id']}" if BOT_USERNAME else "",
            "count": invited["n"],
            "earned": u["ref_earned"] or 0,
        },
    }


# ---------------- птичка ----------------
@app.post("/api/start")
async def api_start(r: StartReq, x_init_data: str = Header("")):
    u = await auth(x_init_data)
    if SETTINGS["maintenance"] == "1":
        raise HTTPException(503, "Технические работы, зайди позже")
    bet = round(r.bet, 2)
    if bet < sf("min_bet") or bet > sf("max_bet"):
        raise HTTPException(400, f"Ставка от {fmt(sf('min_bet'))} до {fmt(sf('max_bet'))} ⭐")
    # незавершённая игра = проигрыш (ставка уже списана)
    await db.execute(
        "UPDATE games SET status='lost', finished=? WHERE user_id=? AND status='active'",
        (time.time(), u["id"]),
    )
    cur = await db.execute(
        "UPDATE users SET balance=ROUND(balance-?,2), total_bet=ROUND(total_bet+?,2) "
        "WHERE id=? AND balance>=?",
        (bet, bet, u["id"], bet),
    )
    if cur.rowcount == 0:
        await db.commit()
        raise HTTPException(400, "Недостаточно звёзд")
    cur = await db.execute(
        "INSERT INTO games(user_id, bet, status, started) VALUES(?,?,'active',?)",
        (u["id"], bet, time.time()),
    )
    await db.commit()
    row = await one("SELECT balance FROM users WHERE id=?", (u["id"],))
    return {"game_id": cur.lastrowid, "balance": row["balance"]}


@app.post("/api/finish")
async def api_finish(r: FinishReq, x_init_data: str = Header("")):
    u = await auth(x_init_data)
    g = await one(
        "SELECT * FROM games WHERE id=? AND user_id=? AND status='active'",
        (r.game_id, u["id"]),
    )
    if not g:
        raise HTTPException(400, "Игра не найдена")
    elapsed = time.time() - g["started"]
    n = max(0, r.pillars)
    n = min(n, max_pillars_by_time(elapsed, int(sf("max_pillars"))))
    payout = round(g["bet"] * n * sf("pillar_pct") / 100, 2)
    cur = await db.execute(
        "UPDATE games SET status='done', pillars=?, payout=?, finished=? "
        "WHERE id=? AND status='active'",
        (n, payout, time.time(), g["id"]),
    )
    if cur.rowcount:
        await db.execute(
            "UPDATE users SET balance=ROUND(balance+?,2), games=games+1, "
            "total_won=ROUND(total_won+?,2), best=MAX(best,?) WHERE id=?",
            (payout, payout, n, u["id"]),
        )
        await ref_bonus(u["id"], g["bet"] - payout)
    await db.commit()
    row = await one("SELECT balance FROM users WHERE id=?", (u["id"],))
    return {"pillars": n, "payout": payout, "bet": g["bet"], "balance": row["balance"]}


# ---------------- кейсы ----------------
@app.get("/api/cases")
async def api_cases(x_init_data: str = Header("")):
    u = await auth(x_init_data)
    cs = await db.execute_fetchall("SELECT * FROM cases WHERE active=1 ORDER BY sort, id")
    ps = await db.execute_fetchall("SELECT * FROM prizes ORDER BY value")
    by = defaultdict(list)
    for p in ps:
        by[p["case_id"]].append(p)
    last_rows = await db.execute_fetchall(
        "SELECT case_id, MAX(created) m FROM case_opens WHERE user_id=? GROUP BY case_id", (u["id"],))
    last = {r["case_id"]: r["m"] for r in last_rows}
    now = time.time()
    out = []
    for c in cs:
        pr = by[c["id"]]
        tot = sum(p["weight"] for p in pr) or 1
        left = 0
        if c["cooldown_h"] > 0 and c["id"] in last:
            left = max(0, int(last[c["id"]] + c["cooldown_h"] * 3600 - now))
        out.append({
            "id": c["id"], "name": c["name"], "emoji": c["emoji"], "price": c["price"],
            "cooldown_h": c["cooldown_h"], "left": left,
            "prizes": [{"id": p["id"], "name": p["name"], "emoji": p["emoji"],
                        "value": p["value"], "pct": p["weight"] / tot * 100} for p in pr],
        })
    return {"cases": out}


@app.post("/api/case/open")
async def api_case_open(r: CaseReq, x_init_data: str = Header("")):
    u = await auth(x_init_data)
    if SETTINGS["maintenance"] == "1":
        raise HTTPException(503, "Технические работы, зайди позже")
    async with LOCKS[u["id"]]:
        case = await one("SELECT * FROM cases WHERE id=? AND active=1", (r.case_id,))
        if not case:
            raise HTTPException(404, "Кейс недоступен")
        prizes = await db.execute_fetchall("SELECT * FROM prizes WHERE case_id=?", (case["id"],))
        if not prizes or sum(p["weight"] for p in prizes) <= 0:
            raise HTTPException(400, "Кейс пока пуст")
        price = case["price"]
        now = time.time()
        if case["cooldown_h"] > 0:
            last = await one("SELECT MAX(created) m FROM case_opens WHERE user_id=? AND case_id=?",
                             (u["id"], case["id"]))
            if last["m"] and now < last["m"] + case["cooldown_h"] * 3600:
                raise HTTPException(400, "Кейс ещё не доступен, подожди таймер")
        # персональные множители шансов действуют только на бесплатные кейсы
        mults = {}
        if price == 0:
            rows = await db.execute_fetchall("SELECT prize_id, mult FROM user_odds WHERE user_id=?", (u["id"],))
            mults = {x["prize_id"]: x["mult"] for x in rows}
        ws = [max(0.0, p["weight"] * mults.get(p["id"], 1.0)) for p in prizes]
        if sum(ws) <= 0:
            ws = [p["weight"] for p in prizes]
        prize = RNG.choices(prizes, weights=ws)[0]
        if price > 0:
            cur = await db.execute(
                "UPDATE users SET balance=ROUND(balance-?,2), total_bet=ROUND(total_bet+?,2) "
                "WHERE id=? AND balance>=?", (price, price, u["id"], price))
            if cur.rowcount == 0:
                await db.commit()
                raise HTTPException(400, "Недостаточно звёзд")
        await db.execute(
            "INSERT INTO case_opens(user_id, case_id, prize_id, price, value, created) VALUES(?,?,?,?,?,?)",
            (u["id"], case["id"], prize["id"], price, prize["value"], now))
        cur = await db.execute(
            "INSERT INTO inv(user_id, name, emoji, value, created) VALUES(?,?,?,?,?)",
            (u["id"], prize["name"], prize["emoji"], prize["value"], now))
        inv_id = cur.lastrowid
        await ref_bonus(u["id"], price - prize["value"])
        await db.commit()
        row = await one("SELECT balance FROM users WHERE id=?", (u["id"],))
        left = int(case["cooldown_h"] * 3600) if case["cooldown_h"] > 0 else 0
        return {"prize": {"id": prize["id"], "name": prize["name"], "emoji": prize["emoji"],
                          "value": prize["value"]},
                "inv_id": inv_id, "balance": row["balance"], "left": left}


# ---------------- инвентарь ----------------
@app.get("/api/inv")
async def api_inv(x_init_data: str = Header("")):
    u = await auth(x_init_data)
    rows = await db.execute_fetchall(
        "SELECT id, name, emoji, value, status FROM inv WHERE user_id=? AND status IN ('own','pending') "
        "ORDER BY value DESC, id DESC LIMIT 300", (u["id"],))
    return {"items": [dict(r) for r in rows]}


@app.post("/api/inv/sell")
async def api_inv_sell(r: InvReq, x_init_data: str = Header("")):
    u = await auth(x_init_data)
    it = await one("SELECT * FROM inv WHERE id=? AND user_id=? AND status='own'", (r.inv_id, u["id"]))
    if not it:
        raise HTTPException(400, "Предмет не найден")
    cur = await db.execute("UPDATE inv SET status='sold' WHERE id=? AND status='own'", (it["id"],))
    if cur.rowcount:
        await db.execute(
            "UPDATE users SET balance=ROUND(balance+?,2), total_won=ROUND(total_won+?,2) WHERE id=?",
            (it["value"], it["value"], u["id"]))
    await db.commit()
    row = await one("SELECT balance FROM users WHERE id=?", (u["id"],))
    return {"balance": row["balance"]}


@app.post("/api/inv/sell_all")
async def api_inv_sell_all(x_init_data: str = Header("")):
    u = await auth(x_init_data)
    rows = await db.execute_fetchall("SELECT id, value FROM inv WHERE user_id=? AND status='own'", (u["id"],))
    total = 0.0
    for it in rows:
        cur = await db.execute("UPDATE inv SET status='sold' WHERE id=? AND status='own'", (it["id"],))
        if cur.rowcount:
            total += it["value"]
    total = round(total, 2)
    if total:
        await db.execute(
            "UPDATE users SET balance=ROUND(balance+?,2), total_won=ROUND(total_won+?,2) WHERE id=?",
            (total, total, u["id"]))
    await db.commit()
    row = await one("SELECT balance FROM users WHERE id=?", (u["id"],))
    return {"balance": row["balance"], "sold": total}


@app.post("/api/inv/withdraw")
async def api_inv_withdraw(r: InvReq, x_init_data: str = Header("")):
    u = await auth(x_init_data)
    it = await one("SELECT * FROM inv WHERE id=? AND user_id=? AND status='own'", (r.inv_id, u["id"]))
    if not it:
        raise HTTPException(400, "Предмет не найден")
    if it["value"] < sf("gift_min"):
        raise HTTPException(400, f"Подарком можно вывести от {fmt(sf('gift_min'))} ⭐, остальное продай")
    cur = await db.execute("UPDATE inv SET status='pending' WHERE id=? AND status='own'", (it["id"],))
    await db.commit()
    if cur.rowcount:
        who = f"@{u['username']}" if u["username"] else escape(u["name"] or "")
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Выдано", callback_data=f"gw:ok:{it['id']}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"gw:no:{it['id']}"),
        ]])
        await notify_admins(
            f"🎁 Запрос на выдачу подарка #{it['id']}\nИгрок: {who} (<code>{u['id']}</code>)\n"
            f"Подарок: {it['emoji']} <b>{escape(it['name'])}</b> ({fmt(it['value'])} ⭐)", kb)
    return {"ok": True}


# ---------------- пополнение / вывод ----------------
@app.post("/api/invoice")
async def api_invoice(r: AmountReq, x_init_data: str = Header("")):
    u = await auth(x_init_data)
    if not 1 <= r.amount <= 10000:
        raise HTTPException(400, "Сумма от 1 до 10000 ⭐")
    link = await bot.create_invoice_link(
        title="Пополнение баланса",
        description=f"Пополнение на {r.amount} ⭐",
        payload=f"topup:{u['id']}",
        provider_token="",
        currency="XTR",
        prices=[LabeledPrice(label="Пополнение", amount=r.amount)],
    )
    return {"link": link}


@app.post("/api/withdraw")
async def api_withdraw(r: AmountReq, x_init_data: str = Header("")):
    u = await auth(x_init_data)
    if r.amount < sf("min_withdraw"):
        raise HTTPException(400, f"Минимальный вывод {fmt(sf('min_withdraw'))} ⭐")
    cur = await db.execute(
        "UPDATE users SET balance=ROUND(balance-?,2) WHERE id=? AND balance>=?",
        (r.amount, u["id"], r.amount),
    )
    if cur.rowcount == 0:
        await db.commit()
        raise HTTPException(400, "Недостаточно звёзд")
    cur = await db.execute(
        "INSERT INTO withdrawals(user_id, amount, created) VALUES(?,?,?)",
        (u["id"], r.amount, time.time()),
    )
    await db.commit()
    wid = cur.lastrowid
    who = f"@{u['username']}" if u["username"] else escape(u["name"] or "")
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Выплачено", callback_data=f"wd:ok:{wid}"),
        InlineKeyboardButton(text="❌ Отклонить", callback_data=f"wd:no:{wid}"),
    ]])
    await notify_admins(
        f"💸 Заявка на вывод #{wid}\nИгрок: {who} (<code>{u['id']}</code>)\n"
        f"Сумма: <b>{r.amount} ⭐</b>", kb)
    return {"ok": True}


# ============================ БОТ: ПОЛЬЗОВАТЕЛЬ ============================
@router.message(CommandStart())
async def cmd_start(m: Message, command: CommandObject):
    uid = m.from_user.id
    arg = command.args or ""
    ref = int(arg[4:]) if arg.startswith("ref_") and arg[4:].isdigit() else None
    exists = await one("SELECT 1 FROM users WHERE id=?", (uid,))
    if not exists:
        await db.execute(
            "INSERT INTO users(id, username, name, created) VALUES(?,?,?,?)",
            (uid, m.from_user.username, m.from_user.first_name, time.time()))
        if ref and ref != uid and await one("SELECT 1 FROM users WHERE id=?", (ref,)):
            await db.execute("UPDATE users SET ref_by=? WHERE id=?", (ref, uid))
            await notify(ref, "👥 По твоей ссылке пришёл новый игрок!")
    else:
        await db.execute("UPDATE users SET username=?, name=? WHERE id=?",
                         (m.from_user.username, m.from_user.first_name, uid))
    await db.commit()
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🐦 Играть", web_app=WebAppInfo(url=WEBAPP_URL))
    ]])
    await m.answer(
        "🐦 <b>Птичка и кейсы</b>\n\n"
        f"• Птичка: каждая колонна даёт <b>{fmt(sf('pillar_pct'))}%</b> от ставки\n"
        "• Кейсы с подарками и ежедневный бесплатный кейс 🎁\n"
        f"• Приглашай друзей и получай <b>{fmt(sf('ref_pct'))}%</b> от их чистого проигрыша\n\n"
        f"Мин. ставка: {fmt(sf('min_bet'))} ⭐ · вывод от {fmt(sf('min_withdraw'))} ⭐",
        reply_markup=kb,
    )


@router.pre_checkout_query()
async def pre_checkout(q: PreCheckoutQuery):
    await q.answer(ok=True)


@router.message(F.successful_payment)
async def paid(m: Message):
    p = m.successful_payment
    try:
        await db.execute(
            "INSERT INTO payments VALUES(?,?,?,?)",
            (p.telegram_payment_charge_id, m.from_user.id, p.total_amount, time.time()),
        )
    except aiosqlite.IntegrityError:
        return
    await db.execute(
        "INSERT OR IGNORE INTO users(id, username, name, created) VALUES(?,?,?,?)",
        (m.from_user.id, m.from_user.username, m.from_user.first_name, time.time()),
    )
    await db.execute(
        "UPDATE users SET balance=ROUND(balance+?,2) WHERE id=?",
        (p.total_amount, m.from_user.id),
    )
    await db.commit()
    await m.answer(f"✅ Баланс пополнен на {p.total_amount} ⭐")


# ============================ БОТ: АДМИНКА ============================
class AdminS(StatesGroup):
    give = State()
    ban = State()
    cast = State()
    find = State()
    setting = State()
    case_new = State()
    case_price = State()
    case_cd = State()
    prize_add = State()
    prize_pct = State()
    prize_val = State()
    odds_user = State()
    odds_mult = State()


def btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def kb_of(rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="📊 Статистика", callback_data="a:stats")
    b.button(text="🎁 Кейсы и шансы", callback_data="a:cases")
    b.button(text="🎲 Шансы игрока", callback_data="a:odds")
    b.button(text="📤 Заявки на вывод", callback_data="a:wd")
    b.button(text="💰 Выдать/списать", callback_data="a:give")
    b.button(text="🔎 Игрок", callback_data="a:find")
    b.button(text="🚫 Бан/разбан", callback_data="a:ban")
    b.button(text="📣 Рассылка", callback_data="a:cast")
    b.button(text="⚙️ Настройки", callback_data="a:set")
    b.adjust(2)
    return b.as_markup()


def back_kb() -> InlineKeyboardMarkup:
    return kb_of([[btn("⬅️ В админку", "a:menu")]])


async def edit(c: CallbackQuery, text: str, mk=None):
    try:
        await c.message.edit_text(text, reply_markup=mk)
    except Exception:
        pass


@admin.message(Command("admin"))
async def cmd_admin(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("🛠 <b>Админ-панель</b>", reply_markup=admin_kb())


@admin.message(Command("give"))
async def cmd_give(m: Message):
    """/give <id> <сумма>  (минус — списать)"""
    try:
        _, uid, amt = m.text.split()
        await change_balance(m, int(uid), float(amt.replace(",", ".")))
    except ValueError:
        await m.answer("Формат: /give ID сумма")


@admin.callback_query(F.data == "a:menu")
async def a_menu(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await edit(c, "🛠 <b>Админ-панель</b>", admin_kb())
    await c.answer()


@admin.callback_query(F.data == "a:stats")
async def a_stats(c: CallbackQuery):
    users = await one("SELECT COUNT(*) n, COALESCE(SUM(balance),0) b, COALESCE(SUM(ref_earned),0) r FROM users")
    today = await one("SELECT COUNT(*) n FROM users WHERE created>?", (time.time() - 86400,))
    gm = await one("SELECT COUNT(*) n, COALESCE(SUM(bet),0) bet, COALESCE(SUM(payout),0) pay "
                   "FROM games WHERE status IN ('done','lost')")
    cs = await one("SELECT COUNT(*) n, COALESCE(SUM(price),0) p, COALESCE(SUM(value),0) v FROM case_opens")
    pay = await one("SELECT COALESCE(SUM(amount),0) s FROM payments")
    wd = await one("SELECT COUNT(*) n, COALESCE(SUM(amount),0) s FROM withdrawals WHERE status='pending'")
    paid_out = await one("SELECT COALESCE(SUM(amount),0) s FROM withdrawals WHERE status='paid'")
    gifts = await one("SELECT COUNT(*) n FROM inv WHERE status='pending'")
    text = (
        "📊 <b>Статистика</b>\n\n"
        f"👥 Игроков: {users['n']} (+{today['n']} за 24ч)\n"
        f"💼 На балансах игроков: {fmt(users['b'])} ⭐\n"
        f"💳 Всего пополнено: {fmt(pay['s'])} ⭐\n"
        f"📤 Выплачено: {fmt(paid_out['s'])} ⭐\n"
        f"⏳ Ожидает вывода: {wd['n']} шт. на {fmt(wd['s'])} ⭐\n"
        f"🎁 Ожидает выдачи подарков: {gifts['n']}\n\n"
        f"🐦 Птичка: игр {gm['n']}, поставлено {fmt(gm['bet'])} ⭐, "
        f"выплачено {fmt(gm['pay'])} ⭐ → результат {fmt(gm['bet'] - gm['pay'])} ⭐\n"
        f"📦 Кейсы: открытий {cs['n']}, цена {fmt(cs['p'])} ⭐, призов на {fmt(cs['v'])} ⭐ "
        f"→ результат {fmt(cs['p'] - cs['v'])} ⭐\n"
        f"👥 Выплачено рефералам: {fmt(users['r'])} ⭐"
    )
    await edit(c, text, back_kb())
    await c.answer()


# ---- выдача / списание баланса
async def change_balance(m: Message, uid: int, amount: float):
    u = await one("SELECT id FROM users WHERE id=?", (uid,))
    if not u:
        await m.answer("Игрок не найден (он должен хоть раз запустить бота)")
        return
    amount = round(amount, 2)
    await db.execute("UPDATE users SET balance=ROUND(balance+?,2) WHERE id=?", (amount, uid))
    await db.commit()
    row = await one("SELECT balance FROM users WHERE id=?", (uid,))
    await m.answer(f"✅ Баланс {uid}: {'+' if amount >= 0 else ''}{fmt(amount)} ⭐ → теперь {fmt(row['balance'])} ⭐",
                   reply_markup=back_kb())
    if amount > 0:
        await notify(uid, f"🎁 Тебе начислено {fmt(amount)} ⭐")


@admin.callback_query(F.data == "a:give")
async def a_give(c: CallbackQuery, state: FSMContext):
    await state.set_state(AdminS.give)
    await edit(c, "Отправь: <code>ID сумма</code>\nМинус — списать, например <code>123456 -20</code>", back_kb())
    await c.answer()


@admin.message(AdminS.give, F.text)
async def s_give(m: Message, state: FSMContext):
    try:
        uid, amt = m.text.split()
        await change_balance(m, int(uid), float(amt.replace(",", ".")))
        await state.clear()
    except ValueError:
        await m.answer("Не понял. Формат: ID сумма", reply_markup=back_kb())


# ---- информация об игроке
async def user_card(uid: int) -> str:
    u = await one("SELECT * FROM users WHERE id=?", (uid,))
    if not u:
        return "Игрок не найден"
    invited = await one("SELECT COUNT(*) n FROM users WHERE ref_by=?", (uid,))
    odds = await db.execute_fetchall(
        "SELECT o.mult, p.name, p.emoji FROM user_odds o JOIN prizes p ON p.id=o.prize_id WHERE o.user_id=?", (uid,))
    odds_txt = ""
    if odds:
        odds_txt = "\n🎲 Свои шансы: " + ", ".join(f"{o['emoji']}{escape(o['name'])} ×{fmt(o['mult'])}" for o in odds)
    return (
        f"👤 <b>{escape(u['name'] or '—')}</b> @{u['username'] or '—'}\n"
        f"ID: <code>{u['id']}</code>\n"
        f"Баланс: {fmt(u['balance'])} ⭐\n"
        f"Игр: {u['games']} · рекорд: {u['best']} колонн\n"
        f"Поставил: {fmt(u['total_bet'])} ⭐ · выиграл: {fmt(u['total_won'])} ⭐\n"
        f"Пригласил: {invited['n']} · реф. доход: {fmt(u['ref_earned'] or 0)} ⭐"
        f"{' · пришёл от ' + str(u['ref_by']) if u['ref_by'] else ''}\n"
        f"Бан: {'да' if u['banned'] else 'нет'}{odds_txt}"
    )


@admin.callback_query(F.data == "a:find")
async def a_find(c: CallbackQuery, state: FSMContext):
    await state.set_state(AdminS.find)
    await edit(c, "Отправь ID игрока или @username", back_kb())
    await c.answer()


@admin.message(AdminS.find, F.text)
async def s_find(m: Message, state: FSMContext):
    t = m.text.strip().lstrip("@")
    row = await one("SELECT id FROM users WHERE id=? OR username=?",
                    (int(t) if t.isdigit() else -1, t))
    await m.answer(await user_card(row["id"]) if row else "Игрок не найден", reply_markup=back_kb())
    await state.clear()


# ---- бан
@admin.callback_query(F.data == "a:ban")
async def a_ban(c: CallbackQuery, state: FSMContext):
    await state.set_state(AdminS.ban)
    await edit(c, "Отправь ID игрока — переключу бан/разбан", back_kb())
    await c.answer()


@admin.message(AdminS.ban, F.text)
async def s_ban(m: Message, state: FSMContext):
    if not m.text.strip().isdigit():
        await m.answer("Нужен числовой ID", reply_markup=back_kb())
        return
    uid = int(m.text.strip())
    u = await one("SELECT banned FROM users WHERE id=?", (uid,))
    if not u:
        await m.answer("Игрок не найден", reply_markup=back_kb())
    else:
        nb = 0 if u["banned"] else 1
        await db.execute("UPDATE users SET banned=? WHERE id=?", (nb, uid))
        await db.commit()
        await m.answer(f"{'🚫 Забанен' if nb else '✅ Разбанен'}: {uid}", reply_markup=back_kb())
    await state.clear()


# ---- рассылка
@admin.callback_query(F.data == "a:cast")
async def a_cast(c: CallbackQuery, state: FSMContext):
    await state.set_state(AdminS.cast)
    await edit(c, "Отправь текст рассылки (можно с HTML-разметкой)", back_kb())
    await c.answer()


@admin.message(AdminS.cast, F.text)
async def s_cast(m: Message, state: FSMContext):
    await state.clear()
    rows = await db.execute_fetchall("SELECT id FROM users WHERE banned=0")
    await m.answer(f"Начинаю рассылку на {len(rows)} чел...")
    ok = fail = 0
    for r in rows:
        try:
            await bot.send_message(r["id"], m.html_text)
            ok += 1
        except Exception:
            fail += 1
        await asyncio.sleep(0.05)
    await m.answer(f"📣 Готово: доставлено {ok}, не доставлено {fail}", reply_markup=back_kb())


# ---- настройки
def settings_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for k, name in SETTING_NAMES.items():
        b.button(text=f"{name}: {SETTINGS[k]}", callback_data=f"set:{k}")
    b.button(text="⬅️ В админку", callback_data="a:menu")
    b.adjust(1)
    return b.as_markup()


@admin.callback_query(F.data == "a:set")
async def a_set(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await edit(c, "⚙️ <b>Настройки</b>\nНажми на параметр, чтобы изменить", settings_kb())
    await c.answer()


@admin.callback_query(F.data.startswith("set:"))
async def a_set_one(c: CallbackQuery, state: FSMContext):
    key = c.data.split(":")[1]
    await state.set_state(AdminS.setting)
    await state.update_data(key=key)
    await edit(c, f"{SETTING_NAMES[key]}\nСейчас: <b>{SETTINGS[key]}</b>\n\nОтправь новое число:", back_kb())
    await c.answer()


@admin.message(AdminS.setting, F.text)
async def s_setting(m: Message, state: FSMContext):
    key = (await state.get_data())["key"]
    try:
        val = float(m.text.replace(",", "."))
        if val < 0:
            raise ValueError
    except ValueError:
        await m.answer("Нужно положительное число", reply_markup=back_kb())
        return
    SETTINGS[key] = fmt(val)
    await db.execute("UPDATE settings SET value=? WHERE key=?", (SETTINGS[key], key))
    await db.commit()
    await state.clear()
    await m.answer(f"✅ {SETTING_NAMES[key]} = {SETTINGS[key]}", reply_markup=settings_kb())


# ---- заявки на вывод звёзд
@admin.callback_query(F.data == "a:wd")
async def a_wd(c: CallbackQuery):
    rows = await db.execute_fetchall(
        "SELECT w.id, w.user_id, w.amount, u.username FROM withdrawals w "
        "LEFT JOIN users u ON u.id=w.user_id WHERE w.status='pending' ORDER BY w.id LIMIT 10")
    gifts = await db.execute_fetchall(
        "SELECT i.id, i.user_id, i.name, i.emoji, i.value, u.username FROM inv i "
        "LEFT JOIN users u ON u.id=i.user_id WHERE i.status='pending' ORDER BY i.id LIMIT 10")
    if not rows and not gifts:
        await edit(c, "Заявок нет 👌", back_kb())
        await c.answer()
        return
    await edit(c, f"📤 Ожидают: выводов {len(rows)}, подарков {len(gifts)} (по 10 максимум). Заявки ниже.", back_kb())
    for r in rows:
        kb = kb_of([[btn("✅ Выплачено", f"wd:ok:{r['id']}"), btn("❌ Отклонить", f"wd:no:{r['id']}")]])
        await c.message.answer(
            f"#{r['id']} · @{r['username'] or '—'} (<code>{r['user_id']}</code>) · <b>{fmt(r['amount'])} ⭐</b>",
            reply_markup=kb)
    for g in gifts:
        kb = kb_of([[btn("✅ Выдано", f"gw:ok:{g['id']}"), btn("❌ Отклонить", f"gw:no:{g['id']}")]])
        await c.message.answer(
            f"🎁 #{g['id']} · @{g['username'] or '—'} (<code>{g['user_id']}</code>) · "
            f"{g['emoji']} <b>{escape(g['name'])}</b> ({fmt(g['value'])} ⭐)", reply_markup=kb)
    await c.answer()


@admin.callback_query(F.data.startswith("wd:"))
async def wd_action(c: CallbackQuery):
    _, act, wid = c.data.split(":")
    w = await one("SELECT * FROM withdrawals WHERE id=?", (int(wid),))
    if not w or w["status"] != "pending":
        await c.answer("Заявка уже обработана", show_alert=True)
        return
    if act == "ok":
        await db.execute("UPDATE withdrawals SET status='paid' WHERE id=?", (w["id"],))
        await db.commit()
        await notify(w["user_id"], f"✅ Вывод {fmt(w['amount'])} ⭐ выполнен")
        mark = "✅ выплачено"
    else:
        await db.execute("UPDATE withdrawals SET status='rejected' WHERE id=?", (w["id"],))
        await db.execute("UPDATE users SET balance=ROUND(balance+?,2) WHERE id=?", (w["amount"], w["user_id"]))
        await db.commit()
        await notify(w["user_id"], f"❌ Вывод {fmt(w['amount'])} ⭐ отклонён, звёзды вернулись на баланс")
        mark = "❌ отклонено, баланс возвращён"
    await edit(c, f"{c.message.html_text}\n\n<b>{mark}</b>")
    await c.answer()


@admin.callback_query(F.data.startswith("gw:"))
async def gw_action(c: CallbackQuery):
    _, act, iid = c.data.split(":")
    it = await one("SELECT * FROM inv WHERE id=?", (int(iid),))
    if not it or it["status"] != "pending":
        await c.answer("Заявка уже обработана", show_alert=True)
        return
    if act == "ok":
        await db.execute("UPDATE inv SET status='sent' WHERE id=?", (it["id"],))
        await db.commit()
        await notify(it["user_id"], f"🎁 Подарок {it['emoji']} {it['name']} выдан!")
        mark = "✅ выдано"
    else:
        await db.execute("UPDATE inv SET status='own' WHERE id=?", (it["id"],))
        await db.commit()
        await notify(it["user_id"], f"❌ Выдача подарка {it['emoji']} {it['name']} отклонена, он снова в инвентаре")
        mark = "❌ отклонено, подарок вернулся в инвентарь"
    await edit(c, f"{c.message.html_text}\n\n<b>{mark}</b>")
    await c.answer()


# ---- кейсы и шансы
async def set_percent(pid: int, p: float):
    """Ставит приза шанс p %, остальные призы кейса пересчитываются пропорционально до суммы 100."""
    pr = await one("SELECT * FROM prizes WHERE id=?", (pid,))
    if not pr:
        return
    p = min(100.0, max(0.0, p))
    others = await db.execute_fetchall("SELECT id, weight FROM prizes WHERE case_id=? AND id<>?", (pr["case_id"], pid))
    rest = 100.0 - p
    tot = sum(o["weight"] for o in others)
    await db.execute("UPDATE prizes SET weight=? WHERE id=?", (round(p, 6), pid))
    for o in others:
        w = (o["weight"] / tot * rest) if tot > 0 else (rest / len(others))
        await db.execute("UPDATE prizes SET weight=? WHERE id=?", (round(w, 6), o["id"]))
    await db.commit()


async def case_view(cid: int):
    c = await one("SELECT * FROM cases WHERE id=?", (cid,))
    if not c:
        return "Кейс не найден", back_kb()
    ps = await db.execute_fetchall("SELECT * FROM prizes WHERE case_id=? ORDER BY value", (cid,))
    tot = sum(p["weight"] for p in ps) or 1
    ev = sum(p["value"] * p["weight"] for p in ps) / tot
    lines = [f"{p['emoji']} {escape(p['name'])} — {fmt(p['value'])} ⭐ — <b>{fmt4(p['weight'] / tot * 100)}%</b>" for p in ps]
    if c["price"] > 0:
        rtp = ev / c["price"] * 100
        warn = "\n⚠️ <b>Возврат ≥ 100% — кейс убыточен для тебя!</b>" if rtp >= 100 else ""
        econ = f"Средний возврат игроку: <b>{rtp:.1f}%</b> (твоя маржа {100 - rtp:.1f}%){warn}"
    else:
        econ = f"Средняя стоимость приза: <b>{ev:.3f} ⭐</b> за открытие"
    cd = f"раз в {fmt(c['cooldown_h'])} ч" if c["cooldown_h"] > 0 else "без ограничений"
    text = (
        f"{c['emoji']} <b>{escape(c['name'])}</b>\n"
        f"Цена: {'бесплатно' if c['price'] == 0 else fmt(c['price']) + ' ⭐'} · открытие: {cd} · "
        f"{'🟢 включён' if c['active'] else '🔴 выключен'}\n\n"
        + ("\n".join(lines) if lines else "Призов пока нет") + f"\n\n{econ}"
    )
    mk = kb_of([
        [btn("🎯 Призы и шансы", f"cz:{cid}"), btn("➕ Приз", f"pa:{cid}")],
        [btn("💰 Цена", f"cp:{cid}"), btn("⏱ Кулдаун", f"cc:{cid}")],
        [btn("🔴 Выключить" if c["active"] else "🟢 Включить", f"ct:{cid}"), btn("🗑 Удалить", f"cd:{cid}")],
        [btn("⬅️ К кейсам", "a:cases")],
    ])
    return text, mk


async def prize_view(pid: int):
    p = await one("SELECT * FROM prizes WHERE id=?", (pid,))
    if not p:
        return "Приз не найден", back_kb()
    tot = (await one("SELECT SUM(weight) s FROM prizes WHERE case_id=?", (p["case_id"],)))["s"] or 1
    pct = p["weight"] / tot * 100
    text = (f"{p['emoji']} <b>{escape(p['name'])}</b>\nСтоимость: {fmt(p['value'])} ⭐\n"
            f"Шанс: <b>{fmt4(pct)}%</b>\n\n"
            "Кнопки меняют шанс, остальные призы пересчитываются автоматически.")
    mk = kb_of([
        [btn("−1", f"pp:{pid}:-1"), btn("−0.1", f"pp:{pid}:-0.1"), btn("+0.1", f"pp:{pid}:0.1"), btn("+1", f"pp:{pid}:1")],
        [btn("✏️ Задать шанс %", f"pe:{pid}"), btn("💎 Стоимость", f"pv:{pid}")],
        [btn("🗑 Удалить приз", f"pd:{pid}")],
        [btn("⬅️ К призам", f"cz:{p['case_id']}")],
    ])
    return text, mk


@admin.callback_query(F.data == "a:cases")
async def a_cases(c: CallbackQuery, state: FSMContext):
    await state.clear()
    cs = await db.execute_fetchall("SELECT * FROM cases ORDER BY sort, id")
    rows = [[btn(f"{x['emoji']} {x['name']} · {'бесплатно' if x['price'] == 0 else fmt(x['price']) + ' ⭐'}"
                 f"{'' if x['active'] else ' (выкл)'}", f"c:{x['id']}")] for x in cs]
    rows.append([btn("➕ Новый кейс", "cn")])
    rows.append([btn("⬅️ В админку", "a:menu")])
    await edit(c, "🎁 <b>Кейсы</b>\nВыбери кейс, чтобы поменять призы и шансы", kb_of(rows))
    await c.answer()


@admin.callback_query(F.data.startswith("c:"))
async def a_case(c: CallbackQuery, state: FSMContext):
    await state.clear()
    text, mk = await case_view(int(c.data.split(":")[1]))
    await edit(c, text, mk)
    await c.answer()


@admin.callback_query(F.data == "cn")
async def a_case_new(c: CallbackQuery, state: FSMContext):
    await state.set_state(AdminS.case_new)
    await edit(c, "Отправь в формате:\n<code>эмодзи Название | цена | кулдаун в часах</code>\n"
                  "Пример: <code>🥇 Золотой | 250 | 0</code>\nЦена 0 = бесплатный кейс (обычно с кулдауном 24).", back_kb())
    await c.answer()


@admin.message(AdminS.case_new, F.text)
async def s_case_new(m: Message, state: FSMContext):
    try:
        a, price, cd = [x.strip() for x in m.text.split("|")]
        emoji, name = a.split(" ", 1) if " " in a else ("🎁", a)
        cur = await db.execute("INSERT INTO cases(name, emoji, price, cooldown_h, sort) VALUES(?,?,?,?,?)",
                               (name.strip(), emoji, float(price.replace(",", ".")), float(cd.replace(",", ".")), 99))
        await db.commit()
    except Exception:
        await m.answer("Не понял формат. Пример: <code>🥇 Золотой | 250 | 0</code>", reply_markup=back_kb())
        return
    await state.clear()
    text, mk = await case_view(cur.lastrowid)
    await m.answer("✅ Кейс создан, теперь добавь призы.\n\n" + text, reply_markup=mk)


@admin.callback_query(F.data.startswith("cp:"))
async def a_case_price(c: CallbackQuery, state: FSMContext):
    cid = int(c.data.split(":")[1])
    await state.set_state(AdminS.case_price)
    await state.update_data(cid=cid)
    await edit(c, "Отправь новую цену в ⭐ (0 = бесплатный)", kb_of([[btn("⬅️ Назад", f"c:{cid}")]]))
    await c.answer()


@admin.message(AdminS.case_price, F.text)
async def s_case_price(m: Message, state: FSMContext):
    cid = (await state.get_data())["cid"]
    try:
        v = float(m.text.replace(",", "."))
        if v < 0:
            raise ValueError
    except ValueError:
        await m.answer("Нужно число ≥ 0")
        return
    await db.execute("UPDATE cases SET price=? WHERE id=?", (v, cid))
    await db.commit()
    await state.clear()
    text, mk = await case_view(cid)
    await m.answer(text, reply_markup=mk)


@admin.callback_query(F.data.startswith("cc:"))
async def a_case_cd(c: CallbackQuery, state: FSMContext):
    cid = int(c.data.split(":")[1])
    await state.set_state(AdminS.case_cd)
    await state.update_data(cid=cid)
    await edit(c, "Раз в сколько часов можно открывать? (0 = без ограничений, 24 = раз в сутки)",
               kb_of([[btn("⬅️ Назад", f"c:{cid}")]]))
    await c.answer()


@admin.message(AdminS.case_cd, F.text)
async def s_case_cd(m: Message, state: FSMContext):
    cid = (await state.get_data())["cid"]
    try:
        v = float(m.text.replace(",", "."))
        if v < 0:
            raise ValueError
    except ValueError:
        await m.answer("Нужно число ≥ 0")
        return
    await db.execute("UPDATE cases SET cooldown_h=? WHERE id=?", (v, cid))
    await db.commit()
    await state.clear()
    text, mk = await case_view(cid)
    await m.answer(text, reply_markup=mk)


@admin.callback_query(F.data.startswith("ct:"))
async def a_case_toggle(c: CallbackQuery):
    cid = int(c.data.split(":")[1])
    await db.execute("UPDATE cases SET active=1-active WHERE id=?", (cid,))
    await db.commit()
    text, mk = await case_view(cid)
    await edit(c, text, mk)
    await c.answer()


@admin.callback_query(F.data.startswith("cd:"))
async def a_case_del(c: CallbackQuery):
    cid = int(c.data.split(":")[1])
    await edit(c, "Удалить кейс вместе со всеми призами? Предметы, которые уже у игроков, останутся.",
               kb_of([[btn("Да, удалить", f"cdy:{cid}"), btn("Отмена", f"c:{cid}")]]))
    await c.answer()


@admin.callback_query(F.data.startswith("cdy:"))
async def a_case_del_yes(c: CallbackQuery):
    cid = int(c.data.split(":")[1])
    await db.execute("DELETE FROM user_odds WHERE prize_id IN (SELECT id FROM prizes WHERE case_id=?)", (cid,))
    await db.execute("DELETE FROM prizes WHERE case_id=?", (cid,))
    await db.execute("DELETE FROM cases WHERE id=?", (cid,))
    await db.commit()
    await edit(c, "🗑 Кейс удалён", kb_of([[btn("⬅️ К кейсам", "a:cases")]]))
    await c.answer()


@admin.callback_query(F.data.startswith("cz:"))
async def a_prizes(c: CallbackQuery):
    cid = int(c.data.split(":")[1])
    ps = await db.execute_fetchall("SELECT * FROM prizes WHERE case_id=? ORDER BY value", (cid,))
    tot = sum(p["weight"] for p in ps) or 1
    rows = [[btn(f"{p['emoji']} {p['name']} · {fmt(p['value'])} ⭐ · {fmt4(p['weight'] / tot * 100)}%", f"pz:{p['id']}")]
            for p in ps]
    rows.append([btn("➕ Добавить приз", f"pa:{cid}")])
    rows.append([btn("⬅️ К кейсу", f"c:{cid}")])
    await edit(c, "🎯 <b>Призы и шансы</b>\nНажми на приз, чтобы поменять его шанс или стоимость", kb_of(rows))
    await c.answer()


@admin.callback_query(F.data.startswith("pz:"))
async def a_prize(c: CallbackQuery, state: FSMContext):
    await state.clear()
    text, mk = await prize_view(int(c.data.split(":")[1]))
    await edit(c, text, mk)
    await c.answer()


@admin.callback_query(F.data.startswith("pp:"))
async def a_prize_adj(c: CallbackQuery):
    _, pid, delta = c.data.split(":")
    pid = int(pid)
    p = await one("SELECT * FROM prizes WHERE id=?", (pid,))
    if not p:
        await c.answer("Приз не найден", show_alert=True)
        return
    tot = (await one("SELECT SUM(weight) s FROM prizes WHERE case_id=?", (p["case_id"],)))["s"] or 1
    await set_percent(pid, p["weight"] / tot * 100 + float(delta))
    text, mk = await prize_view(pid)
    await edit(c, text, mk)
    await c.answer()


@admin.callback_query(F.data.startswith("pe:"))
async def a_prize_pct(c: CallbackQuery, state: FSMContext):
    pid = int(c.data.split(":")[1])
    await state.set_state(AdminS.prize_pct)
    await state.update_data(pid=pid)
    await edit(c, "Отправь шанс приза в процентах, например <code>0.05</code> или <code>12.5</code>",
               kb_of([[btn("⬅️ Назад", f"pz:{pid}")]]))
    await c.answer()


@admin.message(AdminS.prize_pct, F.text)
async def s_prize_pct(m: Message, state: FSMContext):
    pid = (await state.get_data())["pid"]
    try:
        v = float(m.text.replace(",", ".").replace("%", ""))
        if not 0 <= v <= 100:
            raise ValueError
    except ValueError:
        await m.answer("Нужно число от 0 до 100")
        return
    await set_percent(pid, v)
    await state.clear()
    text, mk = await prize_view(pid)
    await m.answer(text, reply_markup=mk)


@admin.callback_query(F.data.startswith("pv:"))
async def a_prize_val(c: CallbackQuery, state: FSMContext):
    pid = int(c.data.split(":")[1])
    await state.set_state(AdminS.prize_val)
    await state.update_data(pid=pid)
    await edit(c, "Отправь новую стоимость приза в ⭐ (за сколько его можно продать)",
               kb_of([[btn("⬅️ Назад", f"pz:{pid}")]]))
    await c.answer()


@admin.message(AdminS.prize_val, F.text)
async def s_prize_val(m: Message, state: FSMContext):
    pid = (await state.get_data())["pid"]
    try:
        v = float(m.text.replace(",", "."))
        if v < 0:
            raise ValueError
    except ValueError:
        await m.answer("Нужно число ≥ 0")
        return
    await db.execute("UPDATE prizes SET value=? WHERE id=?", (v, pid))
    await db.commit()
    await state.clear()
    text, mk = await prize_view(pid)
    await m.answer(text, reply_markup=mk)


@admin.callback_query(F.data.startswith("pd:"))
async def a_prize_del(c: CallbackQuery):
    pid = int(c.data.split(":")[1])
    p = await one("SELECT case_id FROM prizes WHERE id=?", (pid,))
    if p:
        await db.execute("DELETE FROM user_odds WHERE prize_id=?", (pid,))
        await db.execute("DELETE FROM prizes WHERE id=?", (pid,))
        await db.commit()
    cid = p["case_id"] if p else 0
    await edit(c, "🗑 Приз удалён", kb_of([[btn("⬅️ К кейсу", f"c:{cid}")]]))
    await c.answer()


@admin.callback_query(F.data.startswith("pa:"))
async def a_prize_add(c: CallbackQuery, state: FSMContext):
    cid = int(c.data.split(":")[1])
    await state.set_state(AdminS.prize_add)
    await state.update_data(cid=cid)
    await edit(c, "Отправь в формате:\n<code>эмодзи Название | стоимость | шанс %</code>\n"
                  "Пример: <code>🌹 Роза | 25 | 1.5</code>\nШанс 0 — приз добавится, но выпадать не будет, пока не поднимешь шанс.",
               kb_of([[btn("⬅️ Назад", f"c:{cid}")]]))
    await c.answer()


@admin.message(AdminS.prize_add, F.text)
async def s_prize_add(m: Message, state: FSMContext):
    cid = (await state.get_data())["cid"]
    try:
        a, val, pct = [x.strip() for x in m.text.split("|")]
        emoji, name = a.split(" ", 1) if " " in a else ("🎁", a)
        val = float(val.replace(",", "."))
        pct = float(pct.replace(",", ".").replace("%", ""))
        if val < 0 or not 0 <= pct <= 100:
            raise ValueError
        cur = await db.execute("INSERT INTO prizes(case_id, name, emoji, value, weight) VALUES(?,?,?,?,0)",
                               (cid, name.strip(), emoji, val))
        await db.commit()
        await set_percent(cur.lastrowid, pct)
    except Exception:
        await m.answer("Не понял формат. Пример: <code>🌹 Роза | 25 | 1.5</code>")
        return
    await state.clear()
    text, mk = await case_view(cid)
    await m.answer("✅ Приз добавлен\n\n" + text, reply_markup=mk)


# ---- персональные шансы игрока (только бесплатные кейсы)
@admin.callback_query(F.data == "a:odds")
async def a_odds(c: CallbackQuery, state: FSMContext):
    await state.set_state(AdminS.odds_user)
    await edit(c, "🎲 <b>Шансы игрока</b>\nМожно усилить или ослабить шансы на призы <b>бесплатных</b> кейсов "
                  "(например, ежедневного). Платные кейсы у всех одинаковые.\n\nОтправь ID игрока или @username", back_kb())
    await c.answer()


@admin.message(AdminS.odds_user, F.text)
async def s_odds_user(m: Message, state: FSMContext):
    t = m.text.strip().lstrip("@")
    row = await one("SELECT id FROM users WHERE id=? OR username=?", (int(t) if t.isdigit() else -1, t))
    if not row:
        await m.answer("Игрок не найден", reply_markup=back_kb())
        return
    await state.clear()
    await m.answer(*await odds_view(row["id"]))


async def odds_view(uid: int):
    ps = await db.execute_fetchall(
        "SELECT p.*, c.name cname FROM prizes p JOIN cases c ON c.id=p.case_id WHERE c.price=0 ORDER BY c.id, p.value")
    mine = {r["prize_id"]: r["mult"] for r in
            await db.execute_fetchall("SELECT prize_id, mult FROM user_odds WHERE user_id=?", (uid,))}
    rows = [[btn(f"{p['emoji']} {p['name']} · {fmt(p['value'])} ⭐ · ×{fmt(mine.get(p['id'], 1))}", f"ou:{uid}:{p['id']}")]
            for p in ps]
    rows.append([btn("♻️ Сбросить все", f"or:{uid}")])
    rows.append([btn("⬅️ В админку", "a:menu")])
    text = (await user_card(uid)) + ("\n\nВыбери приз и задай множитель шанса: ×1 обычный, ×10 в 10 раз чаще, "
                                      "×0 не выпадет совсем.") if ps else "Бесплатных кейсов нет"
    return text, kb_of(rows)


@admin.callback_query(F.data.startswith("ou:"))
async def a_odds_pick(c: CallbackQuery, state: FSMContext):
    _, uid, pid = c.data.split(":")
    p = await one("SELECT * FROM prizes WHERE id=?", (int(pid),))
    if not p:
        await c.answer("Приз не найден", show_alert=True)
        return
    await state.set_state(AdminS.odds_mult)
    await state.update_data(uid=int(uid), pid=int(pid))
    await edit(c, f"{p['emoji']} <b>{escape(p['name'])}</b>\nОтправь множитель шанса для игрока <code>{uid}</code>:\n"
                  "1 — обычный, 10 — в 10 раз чаще, 1000 — почти гарантированно, 0 — не выпадет.",
               kb_of([[btn("⬅️ Назад", f"ob:{uid}")]]))
    await c.answer()


@admin.callback_query(F.data.startswith("ob:"))
async def a_odds_back(c: CallbackQuery, state: FSMContext):
    await state.clear()
    text, mk = await odds_view(int(c.data.split(":")[1]))
    await edit(c, text, mk)
    await c.answer()


@admin.message(AdminS.odds_mult, F.text)
async def s_odds_mult(m: Message, state: FSMContext):
    d = await state.get_data()
    try:
        v = float(m.text.replace(",", ".").replace("×", "").replace("x", ""))
        if v < 0 or v > 1e6:
            raise ValueError
    except ValueError:
        await m.answer("Нужно число ≥ 0")
        return
    if v == 1:
        await db.execute("DELETE FROM user_odds WHERE user_id=? AND prize_id=?", (d["uid"], d["pid"]))
    else:
        await db.execute("INSERT OR REPLACE INTO user_odds(user_id, prize_id, mult) VALUES(?,?,?)",
                         (d["uid"], d["pid"], v))
    await db.commit()
    await state.clear()
    await m.answer(*await odds_view(d["uid"]))


@admin.callback_query(F.data.startswith("or:"))
async def a_odds_reset(c: CallbackQuery):
    uid = int(c.data.split(":")[1])
    await db.execute("DELETE FROM user_odds WHERE user_id=?", (uid,))
    await db.commit()
    text, mk = await odds_view(uid)
    await edit(c, text, mk)
    await c.answer("Сброшено")


# ============================ МИНИ-АПП (HTML) ============================
INDEX_HTML = r"""<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no,viewport-fit=cover">
<title>Птичка и кейсы</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Manrope:wght@500;600;700;800&family=Unbounded:wght@500;700;800&display=swap" rel="stylesheet">
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
:root{
  --ink:#060818;--violet:#1a1047;--dusk:#c23a8c;--amber:#ff9a4d;
  --gold:#ffd166;--gold2:#ff9f1c;--mint:#3be8b0;--bad:#ff5c7a;
  --txt:#eef0ff;--mut:#8a90b8;--line:rgba(255,255,255,.1);--glass:rgba(255,255,255,.06);
  --disp:'Unbounded','Manrope',system-ui,sans-serif;
  --body:'Manrope',-apple-system,'Segoe UI',Roboto,sans-serif;
}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html{background:var(--ink)}
body{margin:0;color:var(--txt);font:600 15px/1.45 var(--body);-webkit-user-select:none;user-select:none;
  min-height:100vh;overflow-x:hidden;padding-bottom:calc(96px + env(safe-area-inset-bottom))}
body.playing{padding-bottom:0}
body.playing .top,body.playing nav{display:none}
body.playing #app{padding:0;min-height:100vh;display:flex;flex-direction:column;justify-content:center}
body.playing canvas#cv{border-radius:0;margin:0}
body.playing #result{border-radius:30px 30px 0 0}
.hidden{display:none!important}
.ic{width:1em;height:1em;display:inline-block;vertical-align:-.14em}

/* ---------- небо: фон всего приложения ---------- */
.sky{position:fixed;inset:0;z-index:-2;
  background:
   radial-gradient(130% 55% at 50% 108%,rgba(255,138,92,.34),transparent 62%),
   radial-gradient(90% 45% at 50% 100%,rgba(194,58,140,.32),transparent 72%),
   linear-gradient(180deg,#060818 0%,#0c0a2c 42%,#1a1047 100%)}
.sky i{position:absolute;left:0;top:0;width:2px;height:2px;border-radius:50%;background:#fff;animation:tw 4s ease-in-out infinite alternate}
@keyframes tw{from{opacity:.35}to{opacity:1}}

#app{max-width:460px;margin:0 auto;padding:0 16px}

/* ---------- верхняя панель ---------- */
.top{position:sticky;top:0;z-index:6;display:flex;align-items:center;justify-content:space-between;
  padding:12px 0 10px;margin-bottom:6px;background:linear-gradient(180deg,rgba(6,8,24,.92) 55%,transparent)}
.who{display:flex;align-items:center;gap:10px;min-width:0}
.ava{width:40px;height:40px;border-radius:14px;flex:none;background:linear-gradient(135deg,var(--dusk),var(--amber));
  display:grid;place-items:center;font:800 17px var(--disp);overflow:hidden;color:#fff}
.ava img{width:100%;height:100%;object-fit:cover}
.who .hi{font-size:12px;color:var(--mut);line-height:1.1}
.who .nm{font-weight:800;font-size:15px;max-width:150px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.pill{display:flex;align-items:center;gap:8px;padding:7px 7px 7px 13px;border-radius:999px;border:1px solid var(--line);
  background:var(--glass);color:var(--txt);font:700 15px var(--disp);cursor:pointer}
.pill i{font-style:normal;width:26px;height:26px;border-radius:50%;display:grid;place-items:center;
  background:linear-gradient(135deg,var(--gold),var(--gold2));color:#2b1500;font:900 17px var(--body)}

/* ---------- кнопки ---------- */
.btn{appearance:none;border:0;border-radius:16px;padding:14px 18px;font:800 15px var(--body);color:#fff;cursor:pointer;
  background:var(--glass);box-shadow:inset 0 0 0 1px var(--line);transition:transform .12s,opacity .2s}
.btn:active{transform:scale(.96)}
.btn.gold{background:linear-gradient(135deg,var(--gold),var(--gold2));color:#2b1500;box-shadow:0 10px 26px -8px rgba(255,159,28,.7),inset 0 1px 0 rgba(255,255,255,.55)}
.btn.mint{background:linear-gradient(135deg,#4af0b8,#17b6d4);color:#02241c;box-shadow:0 10px 26px -8px rgba(59,232,176,.6),inset 0 1px 0 rgba(255,255,255,.5)}
.btn.big{width:100%;padding:17px;font-size:17px;margin-top:12px;border-radius:18px}
.btn.sm{padding:9px 14px;font-size:13px;border-radius:12px}
.btn[disabled]{opacity:.42;pointer-events:none}

/* ---------- заголовки ---------- */
.h1{font:800 22px/1.15 var(--disp);margin:6px 0 4px}
.h2{font:700 16px var(--disp);margin:26px 0 12px;display:flex;justify-content:space-between;align-items:baseline}
.h2 span{font:600 13px var(--body);color:var(--mut)}
.muted{color:var(--mut);font-size:13px;line-height:1.5;font-weight:600}
.tab{animation:tabin .35s cubic-bezier(.2,.8,.2,1)}
@keyframes tabin{from{opacity:0;transform:translateY(10px)}}

/* ---------- главная: баланс прямо в небе ---------- */
.bal-hero{padding:14px 0 6px}
.bal-hero .lbl{font-size:13px;color:var(--mut)}
.bal-hero .amt{display:flex;align-items:center;gap:12px;font:800 54px/1.05 var(--disp);margin:6px 0 16px;letter-spacing:-1px}
.bal-hero .amt .ic{font-size:44px;filter:drop-shadow(0 0 16px rgba(255,190,60,.8));animation:pulse 3.4s ease-in-out infinite}
@keyframes pulse{50%{transform:scale(1.08) rotate(6deg)}}
.row2{display:flex;gap:10px}.row2 .btn{flex:1}

.daily{display:flex;align-items:center;gap:14px;margin-top:22px;padding:14px 16px 14px 10px;border-radius:26px;cursor:pointer;
  background:linear-gradient(115deg,rgba(59,232,176,.28),rgba(23,182,212,.10) 70%,transparent);box-shadow:inset 0 0 0 1px rgba(59,232,176,.4);overflow:hidden;position:relative}
.daily .art{width:92px;flex:none;animation:float 3.4s ease-in-out infinite}
.daily h4{margin:0;font:800 17px var(--disp)}
.daily p{margin:4px 0 10px;font-size:13px;color:#bff5e6;line-height:1.35}
.tag{display:inline-block;padding:7px 14px;border-radius:999px;background:var(--mint);color:#02241c;font-weight:800;font-size:13px}
.daily.wait .tag{background:rgba(255,255,255,.14);color:#fff}
@keyframes float{50%{transform:translateY(-7px) rotate(-2.5deg)}}

/* птичка на главной: живая сценка */
.scene{position:relative;height:178px;margin-top:14px;border-radius:30px;overflow:hidden;cursor:pointer;
  background:linear-gradient(180deg,#14083d 0%,#5b1d96 52%,#e0508f 88%,#ff9a5a 100%);box-shadow:inset 0 0 0 1px rgba(255,255,255,.14)}
.scene .moon{position:absolute;right:26px;top:20px;width:34px;height:34px;border-radius:50%;background:#fff3d1;box-shadow:0 0 40px 12px rgba(255,240,200,.35)}
.scene .pipe{position:absolute;width:38px;background:linear-gradient(90deg,#0f7a4a,#3be48a 40%,#22b86a 65%,#0b5e3a);border-radius:6px;animation:slide 5.5s linear infinite}
.scene .pipe::after{content:'';position:absolute;left:-5px;right:-5px;height:16px;background:linear-gradient(90deg,#14804f,#7cf3b3 40%,#22b86a);border-radius:6px}
.scene .p1{top:-4px;height:62px;right:-60px}.scene .p1::after{bottom:-4px}
.scene .p2{bottom:-4px;height:56px;right:-60px}.scene .p2::after{top:-4px}
.scene .p3{top:-4px;height:30px;right:-60px;animation-delay:-2.75s}.scene .p3::after{bottom:-4px}
.scene .p4{bottom:-4px;height:88px;right:-60px;animation-delay:-2.75s}.scene .p4::after{top:-4px}
@keyframes slide{to{transform:translateX(-560px)}}
.scene .bird{position:absolute;left:54px;top:62px;width:46px;animation:bob 1.6s ease-in-out infinite}
@keyframes bob{50%{transform:translateY(-16px) rotate(-8deg)}}
.scene .cap{position:absolute;left:0;right:0;bottom:0;padding:34px 16px 14px;background:linear-gradient(180deg,transparent,rgba(10,5,40,.82));display:flex;justify-content:space-between;align-items:flex-end;gap:10px}
.scene h4{margin:0;font:800 18px var(--disp)}
.scene p{margin:2px 0 0;font-size:12px;color:#ffffffc0}

/* ---------- топ игроков: строки, без карточек ---------- */
.lb .r{display:flex;align-items:center;gap:12px;padding:11px 0;border-bottom:1px solid var(--line)}
.lb .r:last-child{border:0}
.lb .pos{width:28px;font:800 14px var(--disp);color:var(--mut);text-align:center}
.lb .r.m1 .pos,.lb .r.m2 .pos,.lb .r.m3 .pos{font-size:20px}
.lb .n{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-weight:700}
.lb .v{font:800 15px var(--disp);color:var(--gold)}
.lb .v small{font:600 12px var(--body);color:var(--mut)}

/* ---------- птичка: экран ставки ---------- */
.panel{border-radius:26px;padding:18px;background:linear-gradient(180deg,rgba(255,255,255,.08),rgba(255,255,255,.03));box-shadow:inset 0 0 0 1px var(--line)}
.betbox{display:flex;gap:8px;align-items:center;margin:14px 0 10px}
.betbox .in{flex:1;position:relative}
.betbox .in .ic{position:absolute;left:15px;top:50%;transform:translateY(-50%);font-size:24px}
input{width:100%;padding:15px 14px 15px 48px;border-radius:18px;border:2px solid var(--line);background:rgba(4,5,18,.6);color:#fff;font:800 24px var(--disp);outline:none}
input:focus{border-color:var(--gold)}
.mini{width:54px;height:54px;border-radius:18px;font:800 15px var(--disp);flex:none;padding:0}
.chips{display:flex;gap:8px;flex-wrap:wrap}
.chip{padding:9px 14px;border-radius:999px;background:var(--glass);box-shadow:inset 0 0 0 1px var(--line);font:700 14px var(--body);cursor:pointer}
.chip:active,.chip.on{background:var(--gold);color:#2b1500}
.bars{display:flex;align-items:stretch;gap:8px;height:132px;margin:14px 0 0}
.bar{flex:1;display:flex;flex-direction:column;align-items:center;gap:6px}
.bar .a{font:800 11px var(--body);white-space:nowrap}
.bar .tr{flex:1;width:100%;display:flex;align-items:flex-end}
.bar .b{width:100%;border-radius:10px 10px 4px 4px;background:linear-gradient(180deg,#5a5f9c,#2a2e5e);min-height:6px;transition:height .4s cubic-bezier(.2,.8,.2,1)}
.bar.win .b{background:linear-gradient(180deg,#5af5b5,#1597a8)}
.bar .c{font-size:11px;color:var(--mut)}

/* ---------- кейсы на полках ---------- */
.cgrid{display:grid;grid-template-columns:1fr 1fr;gap:26px 14px}
.ccard{position:relative;text-align:center;cursor:pointer;padding-bottom:20px}
.ccard .art{width:118px;margin:0 auto;animation:float 3.8s ease-in-out infinite;position:relative;z-index:1}
.ccard:nth-child(2n) .art{animation-delay:-1.4s}
.ccard::after{content:'';position:absolute;left:-4px;right:-4px;bottom:0;height:3px;border-radius:3px;
  background:linear-gradient(90deg,transparent,var(--cg2),transparent);box-shadow:0 0 26px 6px var(--cg)}
.ccard:active .art{transform:scale(.94)}
.ccard .cn{font:800 15px var(--disp);margin:8px 0 8px}
.cprice{display:inline-flex;gap:6px;align-items:center;padding:7px 14px;border-radius:999px;background:rgba(0,0,0,.4);box-shadow:inset 0 0 0 1px var(--line);font-weight:800;font-size:14px}
.cprice.free{background:var(--mint);color:#02241c;box-shadow:none}
.cprice.wait{background:rgba(255,255,255,.12)}

/* ---------- редкость ---------- */
.t0{--c:#9aa3c7;--cg:rgba(154,163,199,.24)}
.t1{--c:#3ddc84;--cg:rgba(61,220,132,.28)}
.t2{--c:#4c9aff;--cg:rgba(76,154,255,.32)}
.t3{--c:#a66bff;--cg:rgba(166,107,255,.36)}
.t4{--c:#ffa63d;--cg:rgba(255,166,61,.40)}
.t5{--c:#ff4d79;--cg:rgba(255,77,121,.44)}
.pgrid{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin:14px 0 4px}
.pz{border-radius:16px;padding:10px 4px 8px;text-align:center;box-shadow:inset 0 0 0 1px var(--c);
  background:radial-gradient(100% 90% at 50% 0,var(--cg),transparent 78%),rgba(6,8,24,.7)}
.pz .pe{font-size:32px;line-height:1.15;filter:drop-shadow(0 4px 10px var(--cg))}
.pz .pn{font-size:11px;color:var(--mut);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;padding:0 2px}
.pz .pv{font:800 12px var(--disp);color:var(--gold);margin-top:2px}
.pz .pc{font-size:11px;font-weight:800;color:var(--c)}

/* ---------- инвентарь ---------- */
.isum{display:flex;justify-content:space-between;align-items:center;gap:12px;margin:2px 0 16px}
.igrid{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.icard{position:relative;border-radius:22px;padding:16px 10px 14px;text-align:center;cursor:pointer;
  box-shadow:inset 0 0 0 1px var(--c);background:radial-gradient(100% 80% at 50% 0,var(--cg),transparent 78%),rgba(8,10,30,.72)}
.icard:active{transform:scale(.97)}
.icard .pe{font-size:56px;line-height:1.1;filter:drop-shadow(0 8px 16px var(--cg))}
.icard b{display:block;margin-top:6px;font-size:14px}
.icard .v{font:800 14px var(--disp);color:var(--gold);margin-top:4px}
.icard .pend{display:block;width:max-content;margin:0 auto 6px;font-size:11px;padding:3px 10px;border-radius:999px;background:rgba(255,255,255,.16)}
.empty{text-align:center;padding:48px 20px;color:var(--mut)}
.empty .big{font-size:56px;margin-bottom:8px}

/* ---------- профиль ---------- */
.prof{display:flex;align-items:center;gap:14px;margin:6px 0 18px}
.prof .ava{width:64px;height:64px;border-radius:22px;font-size:28px}
.prof .nm{font:800 20px var(--disp)}
.stats{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-bottom:6px}
.stat{padding:12px 6px;text-align:center;border-radius:18px;background:var(--glass);box-shadow:inset 0 0 0 1px var(--line)}
.stat b{display:block;font:800 20px var(--disp)}
.stat span{font-size:12px;color:var(--mut)}
.refbox{border-radius:26px;padding:18px;background:linear-gradient(135deg,rgba(194,58,140,.45),rgba(255,154,77,.22));box-shadow:inset 0 0 0 1px rgba(255,160,120,.35)}
.refbox h3{margin:0 0 4px;font:800 17px var(--disp)}

/* ---------- нижнее меню ---------- */
nav{position:fixed;left:0;right:0;bottom:0;z-index:8;padding:18px 10px calc(8px + env(safe-area-inset-bottom));
  background:linear-gradient(180deg,transparent,rgba(6,8,24,.9) 55%)}
nav .in{display:flex;max-width:460px;margin:0 auto}
nav button{flex:1;border:0;background:none;color:var(--mut);font:700 11px var(--body);padding:6px 0 2px;display:flex;flex-direction:column;align-items:center;gap:4px;cursor:pointer;position:relative}
nav button svg{width:25px;height:25px;fill:none;stroke:currentColor;stroke-width:1.9;stroke-linecap:round;stroke-linejoin:round;transition:transform .2s}
nav button.on{color:#fff}
nav button.on svg{stroke:var(--gold);transform:translateY(-2px)}
nav button.on::before{content:'';position:absolute;top:-8px;width:5px;height:5px;border-radius:50%;background:var(--gold);box-shadow:0 0 12px 3px rgba(255,209,102,.8)}

/* ---------- шторки ---------- */
.bg{position:fixed;inset:0;z-index:30;display:flex;align-items:flex-end;justify-content:center;background:rgba(3,4,12,.72);
  backdrop-filter:blur(6px);-webkit-backdrop-filter:blur(6px);opacity:0;pointer-events:none;transition:opacity .25s}
.bg.on{opacity:1;pointer-events:auto}
.sheet{width:100%;max-width:460px;max-height:92vh;overflow:auto;border-radius:30px 30px 0 0;padding:10px 18px calc(22px + env(safe-area-inset-bottom));
  background:linear-gradient(180deg,#201a52,#0d0b2c);box-shadow:inset 0 1px 0 rgba(255,255,255,.18);transform:translateY(100%);transition:transform .34s cubic-bezier(.2,.9,.2,1)}
.bg.on .sheet{transform:none}
.grab{width:42px;height:5px;border-radius:3px;background:rgba(255,255,255,.25);margin:4px auto 14px}
.sheet h3{margin:0;font:800 20px var(--disp);text-align:center}
.sheet .sub{text-align:center;margin:4px 0 0;color:var(--mut);font-size:13px}
.chestbig{width:150px;margin:6px auto 0;animation:float 3.4s ease-in-out infinite}

/* ---------- рулетка ---------- */
#roul{position:relative;height:150px;overflow:hidden;border-radius:22px;background:#050615;box-shadow:inset 0 0 0 1px var(--line);margin:18px 0 10px}
#roul::before,#roul::after{content:'';position:absolute;top:0;bottom:0;width:70px;z-index:2;pointer-events:none}
#roul::before{left:0;background:linear-gradient(90deg,#050615,transparent)}
#roul::after{right:0;background:linear-gradient(270deg,#050615,transparent)}
#mark{position:absolute;left:50%;top:0;bottom:0;width:3px;margin-left:-1.5px;z-index:3;background:linear-gradient(180deg,transparent,var(--gold) 18%,var(--gold) 82%,transparent);box-shadow:0 0 18px 2px var(--gold)}
.tri{position:absolute;left:50%;margin-left:-9px;z-index:4;width:0;height:0;border:9px solid transparent}
.tri.t{top:0;border-top-color:var(--gold)}.tri.b{bottom:0;border-bottom-color:var(--gold)}
#track{display:flex;height:100%;align-items:center;will-change:transform}
.ri{flex:0 0 100px;margin-right:6px;height:124px;border-radius:16px;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:4px;
  box-shadow:inset 0 0 0 1px var(--c);background:radial-gradient(100% 90% at 50% 0,var(--cg),transparent 78%),#0a0c20}
.ri .pe{font-size:46px;line-height:1}.ri .pv{font:800 12px var(--disp);color:var(--gold)}
.res{text-align:center;position:relative;padding-top:6px}
.res .stage{position:relative;height:190px;display:grid;place-items:center}
.res .rays{position:absolute;width:340px;height:340px;left:50%;top:50%;margin:-170px 0 0 -170px;border-radius:50%;
  background:conic-gradient(from 0deg,var(--cg),transparent 10%,var(--cg) 20%,transparent 30%,var(--cg) 40%,transparent 50%,var(--cg) 60%,transparent 70%,var(--cg) 80%,transparent 90%,var(--cg));
  -webkit-mask-image:radial-gradient(circle,#000 8%,transparent 62%);mask-image:radial-gradient(circle,#000 8%,transparent 62%);animation:spin 9s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
.res .em{position:relative;font-size:96px;line-height:1;filter:drop-shadow(0 0 28px var(--c));animation:pop .6s cubic-bezier(.2,1.4,.4,1)}
@keyframes pop{0%{transform:scale(.2) rotate(-25deg);opacity:0}100%{transform:none;opacity:1}}
.res .nm{font:800 22px var(--disp);color:var(--c)}
.res .vl{font:800 16px var(--disp);color:var(--gold);margin:6px 0 4px}

/* ---------- птичка: игровой экран ---------- */
canvas#cv{width:100%;max-height:calc(100vh - 20px);aspect-ratio:360/560;object-fit:contain;border-radius:22px;display:block;touch-action:none;background:#14083d;margin:10px auto 0}
#game{position:relative}
#result{position:absolute;left:0;right:0;bottom:0;padding:18px;border-radius:30px 30px 22px 22px;background:linear-gradient(180deg,rgba(32,26,82,.96),rgba(10,8,34,.98));
  box-shadow:inset 0 1px 0 rgba(255,255,255,.18),0 -20px 60px rgba(0,0,0,.5);text-align:center;animation:rise .4s cubic-bezier(.2,.9,.2,1)}
@keyframes rise{from{transform:translateY(60px);opacity:0}}
#result .cnt{font:800 54px/1 var(--disp)}
#result .net{font:800 30px var(--disp);margin:6px 0}
.win{color:var(--mint)}.lose{color:var(--bad)}

/* ---------- служебное ---------- */
.sk{height:170px;border-radius:24px;background:linear-gradient(100deg,rgba(255,255,255,.04) 30%,rgba(255,255,255,.10) 50%,rgba(255,255,255,.04) 70%);background-size:200% 100%;animation:skl 1.2s infinite}
@keyframes skl{to{background-position:-200% 0}}
#toast{position:fixed;left:50%;top:12px;transform:translate(-50%,-90px);z-index:70;padding:11px 18px;border-radius:16px;background:rgba(20,16,60,.96);
  box-shadow:inset 0 0 0 1px var(--line),0 10px 30px rgba(0,0,0,.5);font-weight:700;font-size:14px;transition:transform .3s cubic-bezier(.2,.9,.2,1);pointer-events:none}
#toast.on{transform:translate(-50%,0)}
#fx{position:fixed;inset:0;z-index:80;pointer-events:none}
:focus-visible{outline:2px solid var(--gold);outline-offset:2px}
@media (prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
</style></head><body>
<svg width="0" height="0" style="position:absolute" aria-hidden="true"><defs>
<linearGradient id="gstar" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#ffe9a0"/><stop offset="1" stop-color="#ff9f1c"/></linearGradient>
<symbol id="i-star" viewBox="0 0 24 24"><path fill="url(#gstar)" d="M12 2l2.9 6.3 6.9.8-5.1 4.7 1.4 6.8L12 17.3l-6.1 3.3 1.4-6.8L2.2 9.1l6.9-.8z"/></symbol>
</defs></svg>
<div class="sky" id="sky"></div>
<canvas id="fx"></canvas>

<div id="app">
 <header class="top">
  <div class="who"><div class="ava" id="ava">И</div><div><div class="hi">Привет</div><div class="nm" id="uname">Игрок</div></div></div>
  <button class="pill" id="btnTop" aria-label="Пополнить баланс"><svg class="ic"><use href="#i-star"/></svg><span id="bal">0</span><i>+</i></button>
 </header>

 <!-- ГЛАВНАЯ -->
 <section id="tab-home" class="tab">
  <div class="bal-hero">
   <div class="lbl">Твой баланс</div>
   <div class="amt"><svg class="ic"><use href="#i-star"/></svg><span id="heroBal">0</span></div>
   <div class="row2"><button class="btn gold" id="hBtnTop">Пополнить</button><button class="btn" id="hBtnWd">Вывести</button></div>
  </div>
  <div class="daily wait" id="daily"><div class="art" id="dailyArt"></div>
   <div><h4>Ежедневный кейс</h4><p>Бесплатно раз в сутки. Есть шанс выбить NFT.</p><span class="tag" id="dailyTag">Загрузка</span></div></div>
  <div class="scene" id="sceneBird">
   <div class="moon"></div><div class="pipe p1"></div><div class="pipe p2"></div><div class="pipe p3"></div><div class="pipe p4"></div>
   <svg class="bird" viewBox="0 0 60 60"><circle cx="30" cy="30" r="22" fill="#ffc83a"/><ellipse cx="29" cy="40" rx="12" ry="8" fill="#fff3a8" opacity=".6"/><ellipse cx="22" cy="32" rx="10" ry="6" fill="#f97316"/><path d="M48 26l14 5-14 6z" fill="#fb923c"/><circle cx="38" cy="22" r="7" fill="#fff"/><circle cx="40" cy="22" r="3.4" fill="#111"/></svg>
   <div class="cap"><div><h4>Птичка</h4><p id="sceneSub">Каждая колонна приносит звёзды</p></div><button class="btn gold sm">Играть</button></div>
  </div>
  <div class="h2">Лучшие лётчики <span>по колоннам</span></div>
  <div class="lb" id="topList"></div>
 </section>

 <!-- ПТИЧКА -->
 <section id="tab-game" class="tab hidden">
  <div id="gMenu">
   <div class="h1">Птичка между колоннами</div>
   <div class="muted">Чем дальше летишь, тем быстрее колонны и уже проход. Твой рекорд: <b id="best" style="color:#fff">0</b></div>
   <div class="panel" style="margin-top:16px">
    <div class="muted">Ставка</div>
    <div class="betbox">
     <button class="btn mini" id="betHalf" aria-label="Половина">½</button>
     <div class="in"><svg class="ic"><use href="#i-star"/></svg><input id="bet" type="number" inputmode="decimal" value="10" aria-label="Ставка"></div>
     <button class="btn mini" id="betDouble" aria-label="Удвоить">×2</button>
    </div>
    <div class="chips" id="chips"></div>
    <div class="muted" style="margin-top:18px" id="calc"></div>
    <div class="bars" id="bars"></div>
    <button class="btn gold big" id="play">Играть</button>
   </div>
  </div>
  <div id="game" class="hidden">
   <canvas id="cv" width="360" height="560"></canvas>
   <div id="result" class="hidden">
    <div class="muted">Пройдено колонн</div>
    <div class="cnt" id="rp">0</div>
    <div class="muted" style="margin-top:6px">Ставка <b id="rb" style="color:#fff">0</b> ⭐, выплата <b id="rw" style="color:#fff">0</b> ⭐</div>
    <div class="net" id="rn"></div>
    <button class="btn gold big" id="again">Ещё раз</button>
    <button class="btn big" id="tomenu">В меню</button>
   </div>
  </div>
 </section>

 <!-- КЕЙСЫ -->
 <section id="tab-cases" class="tab hidden">
  <div class="h1">Кейсы</div>
  <div class="muted" style="margin-bottom:22px">Выбирай кейс и открывай. Приз можно продать за звёзды или забрать подарком.</div>
  <div class="cgrid" id="cases"></div>
 </section>

 <!-- ИНВЕНТАРЬ -->
 <section id="tab-inv" class="tab hidden">
  <div class="isum"><div><div class="h1">Инвентарь</div><div class="muted" id="invSum">Загрузка</div></div><button class="btn gold sm" id="sellAll">Продать всё</button></div>
  <div class="igrid" id="inv"></div>
 </section>

 <!-- ПРОФИЛЬ -->
 <section id="tab-prof" class="tab hidden">
  <div class="prof"><div class="ava" id="pAva">И</div><div><div class="nm" id="pName">Игрок</div><div class="muted" id="pId"></div></div></div>
  <div class="stats">
   <div class="stat"><b id="stRec">0</b><span>рекорд</span></div>
   <div class="stat"><b id="stRef">0</b><span>друзей</span></div>
   <div class="stat"><b id="stEarn">0</b><span>заработано</span></div>
  </div>
  <div class="h2">Реферальная программа</div>
  <div class="refbox">
   <h3>Зови друзей</h3>
   <div class="muted" style="color:#ffd9c7" id="refTxt"></div>
   <button class="btn gold big" id="refShare">Пригласить друга</button>
   <button class="btn big" id="refCopy" style="margin-top:8px">Скопировать ссылку</button>
  </div>
  <div class="h2">Вывод звёзд</div>
  <div class="panel"><div class="muted" id="wdTxt"></div><button class="btn big" id="btnWd">Вывести</button></div>
  <div class="h2">Правила</div>
  <div class="muted" id="rules"></div>
 </section>
</div>

<nav><div class="in">
 <button data-t="home" class="on"><svg viewBox="0 0 24 24"><path d="M3 11l9-8 9 8v9a1 1 0 0 1-1 1h-5v-6H9v6H4a1 1 0 0 1-1-1z"/></svg>Главная</button>
 <button data-t="game"><svg viewBox="0 0 24 24"><path d="M6 8h12a4 4 0 0 1 4 4v3a3 3 0 0 1-5.2 2L15 15H9l-1.8 2A3 3 0 0 1 2 15v-3a4 4 0 0 1 4-4z"/><path d="M8 10.5v3M6.5 12h3"/><circle cx="16" cy="11.5" r=".6"/><circle cx="18" cy="13" r=".6"/></svg>Птичка</button>
 <button data-t="cases"><svg viewBox="0 0 24 24"><rect x="3" y="8" width="18" height="4" rx="1"/><path d="M12 8v13M5 12v8a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1v-8M7.5 8a2.5 2.5 0 1 1 0-5C10 3 12 8 12 8s2-5 4.5-5a2.5 2.5 0 1 1 0 5"/></svg>Кейсы</button>
 <button data-t="inv"><svg viewBox="0 0 24 24"><path d="M5 8h14l-1 12H6z"/><path d="M9 8V6a3 3 0 0 1 6 0v2"/></svg>Инвентарь</button>
 <button data-t="prof"><svg viewBox="0 0 24 24"><circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/></svg>Профиль</button>
</div></nav>

<!-- пополнение / вывод -->
<div class="bg" id="bg-money"><div class="sheet">
  <div class="grab"></div><h3 id="mt"></h3>
  <div class="betbox"><div class="in"><svg class="ic"><use href="#i-star"/></svg><input id="mi" type="number" inputmode="numeric" aria-label="Сумма"></div></div>
  <div class="chips" id="mchips"></div>
  <button class="btn gold big" id="mok">Подтвердить</button>
  <button class="btn big" id="mcancel" style="margin-top:8px">Отмена</button>
</div></div>

<!-- кейс -->
<div class="bg" id="bg-case"><div class="sheet">
  <div class="grab"></div>
  <div id="cm-info">
    <div class="chestbig" id="cmArt"></div>
    <h3 id="cmTitle"></h3><div class="sub" id="cmSub"></div>
    <div class="pgrid" id="cmPrizes"></div>
    <button class="btn gold big" id="cmOpen">Открыть</button>
    <button class="btn big" id="cmClose" style="margin-top:8px">Закрыть</button>
  </div>
  <div id="cm-spin" class="hidden">
    <h3 id="cmTitle2"></h3>
    <div id="roul"><div id="mark"></div><div class="tri t"></div><div class="tri b"></div><div id="track"></div></div>
    <div class="sub">Крутим…</div>
  </div>
  <div id="cm-res" class="hidden res">
    <div class="stage"><div class="rays"></div><div class="em" id="rsE"></div></div>
    <div class="nm" id="rsN"></div><div class="vl" id="rsV"></div>
    <button class="btn gold big" id="rsSell">Продать</button>
    <button class="btn big" id="rsKeep" style="margin-top:8px">В инвентарь</button>
    <button class="btn mint big" id="rsAgain" style="margin-top:8px">Открыть ещё</button>
  </div>
</div></div>

<!-- предмет -->
<div class="bg" id="bg-item"><div class="sheet">
  <div class="grab"></div>
  <div class="res" id="itBox"><div class="stage" style="height:150px"><div class="rays"></div><div class="em" id="itE" style="font-size:80px"></div></div>
   <div class="nm" id="itN"></div><div class="vl" id="itV"></div></div>
  <button class="btn gold big" id="itSell">Продать</button>
  <button class="btn big" id="itGift" style="margin-top:8px">Вывести подарком</button>
  <button class="btn big" id="itClose" style="margin-top:8px">Закрыть</button>
</div></div>
<div id="toast"></div>

<script>
const CFG=__CFG__;
const tg=window.Telegram.WebApp;tg.ready();tg.expand();
try{tg.setHeaderColor('#060818');tg.setBackgroundColor('#060818')}catch(e){}
if(tg.disableVerticalSwipes)tg.disableVerticalSwipes();
const $=id=>document.getElementById(id);
const show=id=>$(id).classList.remove('hidden'),hide=id=>$(id).classList.add('hidden');
const esc=s=>String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const fm=x=>{const v=Math.round(x*100)/100;const p=String(Math.abs(v)).split('.');return (v<0?'-':'')+p[0].replace(/\B(?=(\d{3})+(?!\d))/g,' ')+(p[1]?'.'+p[1]:'')};
const pctf=x=>{const s=Number(x).toFixed(4).replace(/0+$/,'').replace(/\.$/,'');return s||'0'};
const tier=v=>v<1?0:v<10?1:v<50?2:v<250?3:v<1000?4:5;
const STAR='<svg class="ic"><use href="#i-star"/></svg>';
function fmtT(s){const h=Math.floor(s/3600),m=Math.floor(s%3600/60),c=s%60;return (h?h+':':'')+String(m).padStart(h?2:1,'0')+':'+String(c).padStart(2,'0')}
const DPR=Math.min(2,window.devicePixelRatio||1);
const hap={l:()=>{try{tg.HapticFeedback.impactOccurred('light')}catch(e){}},s:()=>{try{tg.HapticFeedback.selectionChanged()}catch(e){}},
  ok:()=>{try{tg.HapticFeedback.notificationOccurred('success')}catch(e){}},err:()=>{try{tg.HapticFeedback.notificationOccurred('error')}catch(e){}}};
let toastT=0;function toast(t){const e=$('toast');e.textContent=t;e.classList.add('on');clearTimeout(toastT);toastT=setTimeout(()=>e.classList.remove('on'),2000)}

// звёзды на небе
(function(){const sky=$('sky');let n=46;while(n--){const s=document.createElement('i');s.style.left=Math.random()*100+'%';s.style.top=Math.random()*85+'%';
  const z=Math.random()*1.6+.6;s.style.width=s.style.height=z+'px';s.style.animationDelay=(-Math.random()*4)+'s';s.style.animationDuration=(2.5+Math.random()*3)+'s';sky.appendChild(s)}})();

// пользователь
const TU=(tg.initDataUnsafe&&tg.initDataUnsafe.user)||{};
const UNAME=TU.first_name||'Игрок';
function setAva(el){if(TU.photo_url){el.innerHTML='<img alt="" src="'+esc(TU.photo_url)+'">'}else el.textContent=(UNAME[0]||'И').toUpperCase()}
setAva($('ava'));setAva($('pAva'));$('uname').textContent=UNAME;$('pName').textContent=UNAME;

// ---------- арт: сундуки
const KINDS={
 free:{b:['#22b58a','#0d5e57'],l:['#5af0c0','#18a587'],m:['#e8fff6','#6fe3c0'],g:'rgba(59,232,176,.45)'},
 blue:{b:['#4a78ff','#1b3797'],l:['#7ea6ff','#2f58db'],m:['#e0ebff','#8fb3ff'],g:'rgba(76,141,255,.45)'},
 silver:{b:['#98a2c6','#4a5278'],l:['#d3daf0','#7f89ae'],m:['#ffffff','#b4bcd9'],g:'rgba(190,200,235,.38)'},
 gold:{b:['#e8a024','#8c4a08'],l:['#ffdc7a','#e99a1e'],m:['#fff6cf','#f2b73f'],g:'rgba(255,176,46,.48)'},
 ruby:{b:['#e03a62','#7c1233'],l:['#ff7c9b','#d22d57'],m:['#fff0c2','#f0b43a'],g:'rgba(255,77,121,.48)'}};
const kindFor=p=>p===0?'free':p<25?'blue':p<100?'silver':p<500?'gold':'ruby';
let CID=0;
function chest(kind){
  const k=KINDS[kind],id='c'+kind+(++CID)+'_';
  const lg=(n,a)=>'<linearGradient id="'+id+n+'" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="'+a[0]+'"/><stop offset="1" stop-color="'+a[1]+'"/></linearGradient>';
  return '<svg viewBox="0 0 120 116" xmlns="http://www.w3.org/2000/svg"><defs>'+lg('b',k.b)+lg('l',k.l)+lg('m',k.m)+
  '<radialGradient id="'+id+'g"><stop offset="0" stop-color="'+k.g+'"/><stop offset="1" stop-color="rgba(0,0,0,0)"/></radialGradient></defs>'+
  '<circle cx="60" cy="58" r="58" fill="url(#'+id+'g)"/><ellipse cx="60" cy="106" rx="44" ry="6" fill="rgba(0,0,0,.4)"/>'+
  '<rect x="14" y="52" width="92" height="52" rx="9" fill="url(#'+id+'b)"/>'+
  '<path d="M12 60 C12 32 32 20 60 20 C88 20 108 32 108 60 Z" fill="url(#'+id+'l)"/>'+
  '<path d="M22 44 C28 32 42 27 60 27" stroke="rgba(255,255,255,.55)" stroke-width="3" fill="none" stroke-linecap="round"/>'+
  '<rect x="30" y="22" width="9" height="82" rx="3" fill="url(#'+id+'m)" opacity=".95"/><rect x="81" y="22" width="9" height="82" rx="3" fill="url(#'+id+'m)" opacity=".95"/>'+
  '<rect x="12" y="56" width="96" height="7" fill="url(#'+id+'m)"/>'+
  '<circle cx="60" cy="66" r="12" fill="url(#'+id+'m)" stroke="rgba(0,0,0,.25)" stroke-width="1.5"/>'+
  '<circle cx="60" cy="64" r="3.2" fill="#2a1a05"/><rect x="58.6" y="64" width="2.8" height="8" rx="1.2" fill="#2a1a05"/></svg>';
}

// ---------- состояние
let S={min_bet:10,max_bet:1000,pillar_pct:10,min_withdraw:50,ref_pct:10,gift_min:25},ME=null,balance=0,shown=0,lastBet=10,CASES=[],INV=[],curTab='home',cur=null,lastRes=null,spinning=false;
async function api(path,body){
  const r=await fetch('/api/'+path,{method:body!==undefined?'POST':'GET',headers:{'Content-Type':'application/json','X-Init-Data':tg.initData},body:body!==undefined?JSON.stringify(body):undefined});
  const j=await r.json().catch(()=>({}));
  if(!r.ok)throw new Error(typeof j.detail==='string'?j.detail:'Что-то пошло не так, попробуй ещё раз');
  return j;
}
function setBal(b,anim){
  balance=b;const from=shown;shown=b;
  const put=v=>{$('bal').textContent=fm(v);$('heroBal').textContent=fm(v)};
  if(!anim||Math.abs(b-from)<.01){put(b);return}
  const t0=performance.now();(function f(t){const k=Math.min(1,(t-t0)/700),e=1-Math.pow(1-k,3);put(from+(b-from)*e);if(k<1)requestAnimationFrame(f)})(t0);
}

// ---------- вкладки
function go(t){
  curTab=t;
  for(const n of ['home','game','cases','inv','prof'])$('tab-'+n).classList.toggle('hidden',n!==t);
  document.querySelectorAll('nav button').forEach(b=>b.classList.toggle('on',b.dataset.t===t));
  if(t==='cases')loadCases();if(t==='inv')loadInv();refresh();
  window.scrollTo(0,0);hap.s();
}
document.querySelectorAll('nav button').forEach(b=>b.onclick=()=>go(b.dataset.t));
$('sceneBird').onclick=()=>go('game');

// ---------- шторки
const openSheet=id=>$(id).classList.add('on'),closeSheet=id=>$(id).classList.remove('on');
$('bg-money').onclick=e=>{if(e.target.id==='bg-money')closeSheet('bg-money')};
$('bg-case').onclick=e=>{if(e.target.id==='bg-case'&&!spinning)closeSheet('bg-case')};
$('bg-item').onclick=e=>{if(e.target.id==='bg-item')closeSheet('bg-item')};

// ---------- данные
async function refresh(){
  try{
    const j=await api('me');ME=j;S=j.settings;setBal(j.balance);
    $('best').textContent=j.best;$('stRec').textContent=j.best;$('stRef').textContent=j.ref.count;$('stEarn').textContent=fm(j.ref.earned);
    $('pId').textContent='ID '+j.id;
    $('sceneSub').textContent='Каждая колонна даёт '+S.pillar_pct+'% от ставки';
    $('rules').innerHTML='Птичка: ставка от '+fm(S.min_bet)+' до '+fm(S.max_bet)+' ⭐. За каждую колонну платим '+S.pillar_pct+'% от ставки. Врезался — игра окончена, выплата за пройденное.<br>Кейсы: приз попадает в инвентарь, его можно продать или вывести подарком от '+fm(S.gift_min)+' ⭐.<br>Вывод звёзд от '+fm(S.min_withdraw)+' ⭐.'+(j.maintenance?'<br><b>Сейчас технические работы.</b>':'');
    $('topList').innerHTML=j.top.length?j.top.map((t,i)=>'<div class="r m'+(i+1)+'"><div class="pos">'+(i<3?['🥇','🥈','🥉'][i]:i+1)+'</div><div class="n">'+esc(t.name)+'</div><div class="v">'+t.best+' <small>кол.</small></div></div>').join(''):'<div class="muted">Пока никого. Стань первым.</div>';
    $('refTxt').innerHTML='Получай <b>'+S.ref_pct+'%</b> от чистого проигрыша каждого друга, которого пригласишь. Выигрыши друга вычитаются. Бонус сам приходит на баланс.';
    $('wdTxt').textContent='Минимум '+fm(S.min_withdraw)+' ⭐. Заявку обработает админ.';
    renderBet();
    if(!CASES.length)loadCases(true);
  }catch(e){tg.showAlert(e.message)}
}

// ---------- ставка птички
function betVal(){return parseFloat($('bet').value)||0}
function setBet(v){v=Math.max(S.min_bet,Math.min(S.max_bet,Math.round(v*100)/100));$('bet').value=v;renderBet();hap.s()}
function renderBet(){
  const b=betVal(),p=S.pillar_pct,be=Math.ceil(100/p);
  const list=[...new Set([S.min_bet,50,100,250,S.max_bet])].filter(v=>v>=S.min_bet&&v<=S.max_bet).sort((a,c)=>a-c);
  $('chips').innerHTML=list.map(v=>'<div class="chip'+(v===b?' on':'')+'" data-v="'+v+'">'+fm(v)+'</div>').join('');
  $('calc').innerHTML='Колонна даёт <b style="color:#fff">+'+fm(b*p/100)+' ⭐</b>. В плюс уходишь с '+be+'-й колонны. Столбики показывают выплату в звёздах за пройденные колонны.';
  const ns=[5,10,15,20,30,50],mx=b*ns[ns.length-1]*p/100||1;
  $('bars').innerHTML=ns.map(n=>{const a=b*n*p/100;return '<div class="bar'+(a>=b?' win':'')+'"><div class="a">'+fm(a)+'</div><div class="tr"><div class="b" style="height:'+Math.max(5,a/mx*100)+'%"></div></div><div class="c">'+n+' кол.</div></div>'}).join('');
}
$('chips').onclick=e=>{if(e.target.dataset.v)setBet(+e.target.dataset.v)};
$('bet').oninput=renderBet;$('betHalf').onclick=()=>setBet(betVal()/2);$('betDouble').onclick=()=>setBet(betVal()*2);

// ---------- пополнение / вывод
let mMode='';
function openMoney(mode){
  mMode=mode;$('mt').textContent=mode==='topup'?'Пополнить баланс':'Вывести звёзды';
  $('mi').value=mode==='topup'?50:Math.max(S.min_withdraw,0);
  $('mchips').innerHTML=(mode==='topup'?[25,50,100,500]:[]).map(v=>'<div class="chip" data-v="'+v+'">'+v+'</div>').join('');
  $('mok').textContent=mode==='topup'?'Оплатить':'Отправить заявку';openSheet('bg-money');
}
$('mchips').onclick=e=>{if(e.target.dataset.v)$('mi').value=e.target.dataset.v};
['btnTop','hBtnTop'].forEach(i=>$(i).onclick=()=>openMoney('topup'));
['btnWd','hBtnWd'].forEach(i=>$(i).onclick=()=>openMoney('wd'));
$('mcancel').onclick=()=>closeSheet('bg-money');
$('mok').onclick=async()=>{
  const a=parseInt($('mi').value);if(!a||a<1)return;closeSheet('bg-money');
  try{
    if(mMode==='topup'){const j=await api('invoice',{amount:a});tg.openInvoice(j.link,s=>{if(s==='paid'){hap.ok();setTimeout(refresh,1500);setTimeout(refresh,4000)}})}
    else{await api('withdraw',{amount:a});toast('Заявка на вывод отправлена');refresh()}
  }catch(e){tg.showAlert(e.message)}
};
$('refShare').onclick=()=>{if(!ME||!ME.ref.link)return;tg.openTelegramLink('https://t.me/share/url?url='+encodeURIComponent(ME.ref.link)+'&text='+encodeURIComponent('Залетай: птичка со ставками и кейсы с подарками'))};
$('refCopy').onclick=()=>{if(!ME||!ME.ref.link)return;(navigator.clipboard?navigator.clipboard.writeText(ME.ref.link):Promise.reject()).then(()=>toast('Ссылка скопирована')).catch(()=>tg.showAlert(ME.ref.link))};

// ---------- кейсы
async function loadCases(quiet){
  if(!quiet&&!CASES.length)$('cases').innerHTML='<div class="sk"></div><div class="sk"></div>';
  try{const j=await api('cases');CASES=j.cases;renderCases();renderDaily()}catch(e){if(!quiet)tg.showAlert(e.message)}
}
function renderCases(){
  $('cases').innerHTML=CASES.length?CASES.map(c=>{
    const k=kindFor(c.price),free=c.price===0,wait=c.left>0;
    return '<div class="ccard" data-id="'+c.id+'" style="--cg:'+KINDS[k].g+';--cg2:'+KINDS[k].m[1]+'"><div class="art">'+chest(k)+'</div><div class="cn">'+esc(c.name)+'</div><span class="cprice '+(wait?'wait':free?'free':'')+'">'+
      (wait?'<span data-timer="'+c.id+'">'+fmtT(c.left)+'</span>':free?'Бесплатно':STAR+' '+fm(c.price))+'</span></div>'}).join(''):'<div class="empty"><div class="big">📦</div>Кейсов пока нет</div>';
}
function renderDaily(){
  const c=CASES.find(x=>x.price===0);const d=$('daily');
  if(!c){d.classList.add('hidden');return}
  d.classList.remove('hidden');$('dailyArt').innerHTML=chest('free');
  const wait=c.left>0;d.classList.toggle('wait',wait);
  $('dailyTag').innerHTML=wait?'Через <span data-timer="'+c.id+'">'+fmtT(c.left)+'</span>':'Открыть бесплатно';
  d.dataset.id=c.id;
}
$('daily').onclick=()=>{const id=+$('daily').dataset.id;if(id)openCase(id)};
$('cases').onclick=e=>{const el=e.target.closest('.ccard');if(el)openCase(+el.dataset.id)};
setInterval(()=>{
  let ended=false;
  for(const c of CASES){if(c.left>0){c.left--;if(c.left===0)ended=true}}
  if(ended){renderCases();renderDaily()}
  document.querySelectorAll('[data-timer]').forEach(el=>{const c=CASES.find(x=>x.id==el.dataset.timer);if(c&&c.left>0)el.textContent=fmtT(c.left)});
  if(cur&&$('bg-case').classList.contains('on'))updOpenBtn();
},1000);

function updOpenBtn(){
  if(!cur)return;const b=$('cmOpen');
  if(cur.left>0){b.textContent='Доступен через '+fmtT(cur.left);b.disabled=true}
  else{b.innerHTML=cur.price===0?'Открыть бесплатно':'Открыть за '+STAR+' '+fm(cur.price);b.disabled=false}
}
function openCase(id){
  cur=CASES.find(x=>x.id===id);if(!cur)return;
  $('cmArt').innerHTML=chest(kindFor(cur.price));
  $('cmTitle').textContent=cur.name;
  $('cmSub').textContent=cur.price===0?'Бесплатно'+(cur.cooldown_h?', раз в '+cur.cooldown_h+' ч':''):'Вот что может выпасть';
  $('cmPrizes').innerHTML=cur.prizes.slice().sort((a,b)=>b.value-a.value).map(p=>'<div class="pz t'+tier(p.value)+'"><div class="pe">'+esc(p.emoji)+'</div><div class="pn">'+esc(p.name)+'</div><div class="pv">'+fm(p.value)+' ⭐</div><div class="pc">'+pctf(p.pct)+'%</div></div>').join('');
  updOpenBtn();hide('cm-spin');hide('cm-res');show('cm-info');openSheet('bg-case');
}
$('cmClose').onclick=()=>closeSheet('bg-case');
function pickVisual(c){const w=c.prizes.map(p=>Math.max(p.pct,4));let r=Math.random()*w.reduce((a,b)=>a+b,0);for(let i=0;i<w.length;i++){r-=w[i];if(r<=0)return c.prizes[i]}return c.prizes[0]}
$('cmOpen').onclick=doOpen;
$('rsAgain').onclick=()=>{hide('cm-res');openCase(cur.id)};
async function doOpen(){
  if(!cur||spinning)return;const c=cur;$('cmOpen').disabled=true;
  try{
    const j=await api('case/open',{case_id:c.id});
    setBal(j.balance);lastRes=j;c.left=j.left;spinning=true;
    $('cmTitle2').textContent=c.name;hide('cm-info');hide('cm-res');show('cm-spin');
    const N=52,IDX=44,IW=106,items=[];
    for(let i=0;i<N;i++)items.push(i===IDX?j.prize:pickVisual(c));
    const tr=$('track');tr.style.transition='none';tr.style.transform='translateX(0)';
    tr.innerHTML=items.map(p=>'<div class="ri t'+tier(p.value)+'"><div class="pe">'+esc(p.emoji)+'</div><div class="pv">'+fm(p.value)+'</div></div>').join('');
    void tr.offsetWidth;
    const W=$('roul').clientWidth,target=IDX*IW+50-W/2+(Math.random()*44-22);
    tr.style.transition='transform 6s cubic-bezier(.12,.72,.1,1)';tr.style.transform='translateX('+(-target)+'px)';
    hap.l();
    let li=-1;const t0=performance.now();
    (function f(){const m=new DOMMatrix(getComputedStyle(tr).transform);const idx=Math.floor((-m.m41+W/2)/IW);if(idx!==li){li=idx;hap.s()}if(performance.now()-t0<6000)requestAnimationFrame(f)})();
    setTimeout(()=>showRes(j),6200);
  }catch(e){tg.showAlert(e.message);updOpenBtn()}
}
const CONF=['#ffd166','#ff9f1c','#3be8b0','#c23a8c','#4c9aff','#ffffff'];
function showRes(j){
  spinning=false;const p=j.prize,t=tier(p.value);
  $('cm-res').className='res t'+t;
  $('rsE').textContent=p.emoji;$('rsN').textContent=p.name;$('rsV').innerHTML=STAR+' '+fm(p.value);
  $('rsSell').innerHTML='Продать за '+STAR+' '+fm(p.value);$('rsSell').disabled=false;
  $('rsAgain').style.display=(cur&&cur.left<=0)?'':'none';
  hide('cm-spin');show('cm-res');hap.ok();
  if(t>=2)confetti(t>=4?160:70,CONF);
}
$('rsSell').onclick=async()=>{
  try{const j=await api('inv/sell',{inv_id:lastRes.inv_id});setBal(j.balance,true);toast('Продано');closeSheet('bg-case');loadCases(true)}catch(e){tg.showAlert(e.message)}
};
$('rsKeep').onclick=()=>{closeSheet('bg-case');loadCases(true);toast('Приз лежит в инвентаре')};

// ---------- инвентарь
async function loadInv(){
  if(!INV.length)$('inv').innerHTML='<div class="sk"></div><div class="sk"></div>';
  try{const j=await api('inv');INV=j.items;renderInv()}catch(e){tg.showAlert(e.message)}
}
function renderInv(){
  const own=INV.filter(i=>i.status==='own'),sum=own.reduce((a,i)=>a+i.value,0);
  $('invSum').innerHTML=INV.length?INV.length+' шт. на '+STAR+' '+fm(sum):'Здесь будут твои призы';
  $('sellAll').style.display=own.length?'':'none';
  $('inv').innerHTML=INV.length?INV.map(i=>'<div class="icard t'+tier(i.value)+'" data-id="'+i.id+'">'+(i.status==='pending'?'<span class="pend">ждёт выдачи</span>':'')+'<div class="pe">'+esc(i.emoji)+'</div><b>'+esc(i.name)+'</b><div class="v">'+fm(i.value)+' ⭐</div></div>').join(''):'<div class="empty" style="grid-column:1/-1"><div class="big">🎒</div>Пока пусто. Открой кейс и забери первый приз.</div>';
}
let itCur=null;
$('inv').onclick=e=>{
  const el=e.target.closest('.icard');if(!el)return;const it=INV.find(x=>x.id==el.dataset.id);if(!it)return;itCur=it;
  $('itBox').className='res t'+tier(it.value);$('itE').textContent=it.emoji;$('itN').textContent=it.name;$('itV').innerHTML=STAR+' '+fm(it.value);
  const own=it.status==='own';
  $('itSell').style.display=own?'':'none';$('itSell').innerHTML='Продать за '+STAR+' '+fm(it.value);
  $('itGift').style.display=own&&it.value>=S.gift_min?'':'none';
  if(!own)$('itV').innerHTML+=' · ждёт выдачи';
  openSheet('bg-item');
};
$('itClose').onclick=()=>closeSheet('bg-item');
$('itSell').onclick=async()=>{
  try{const j=await api('inv/sell',{inv_id:itCur.id});setBal(j.balance,true);closeSheet('bg-item');toast('Продано');loadInv()}catch(e){tg.showAlert(e.message)}
};
$('itGift').onclick=async()=>{
  try{await api('inv/withdraw',{inv_id:itCur.id});closeSheet('bg-item');tg.showAlert('Запрос отправлен. Админ выдаст подарок и напишет тебе.');loadInv()}catch(e){tg.showAlert(e.message)}
};
$('sellAll').onclick=async()=>{
  try{const j=await api('inv/sell_all',{});setBal(j.balance,true);toast('Продано на '+fm(j.sold)+' ⭐');loadInv()}catch(e){tg.showAlert(e.message)}
};

// ---------- конфетти
const fx=$('fx'),fctx=fx.getContext('2d');let fparts=[],fxRun=false;
function fitFx(){fx.width=innerWidth*DPR;fx.height=innerHeight*DPR;fctx.setTransform(DPR,0,0,DPR,0,0)}
addEventListener('resize',fitFx);fitFx();
function confetti(n,colors){
  for(let i=0;i<n;i++)fparts.push({x:innerWidth/2+(Math.random()-.5)*80,y:innerHeight*.5,vx:(Math.random()-.5)*15,vy:-Math.random()*17-4,s:Math.random()*7+5,r:Math.random()*6,vr:(Math.random()-.5)*.4,c:colors[i%colors.length],l:1});
  if(!fxRun){fxRun=true;requestAnimationFrame(fxLoop)}
}
function fxLoop(){
  fctx.clearRect(0,0,innerWidth,innerHeight);
  fparts=fparts.filter(p=>p.l>0&&p.y<innerHeight+30);
  for(const p of fparts){p.vy+=.45;p.x+=p.vx;p.y+=p.vy;p.vx*=.99;p.r+=p.vr;p.l-=.006;fctx.save();fctx.translate(p.x,p.y);fctx.rotate(p.r);fctx.globalAlpha=Math.min(1,p.l*2);fctx.fillStyle=p.c;fctx.fillRect(-p.s/2,-p.s/4,p.s,p.s/2);fctx.restore()}
  if(fparts.length)requestAnimationFrame(fxLoop);else fxRun=false;
}

// ---------- птичка
const cv=$('cv'),ctx=cv.getContext('2d');
const W=360,H=560,GROUND=60,BX=90,R=14,PW=60,SPACING=CFG.spacing,GRAV=1500,FLAP=-430,DT=1/120;
cv.width=W*DPR;cv.height=H*DPR;
let st=null,raf=0,last=0,acc=0;
const speedAt=s=>Math.min(CFG.speedMax,CFG.speedBase+CFG.speedStep*s);
const gapFor=k=>Math.max(CFG.gapMin,CFG.gapBase-CFG.gapStep*(k-1));
const STARS=Array.from({length:44},()=>({x:Math.random()*W,y:Math.random()*320,r:Math.random()*1.3+.4,p:Math.random()*6}));
function mkPipe(x,k){const gap=gapFor(k);return{x:x,gap:gap,top:80+Math.random()*(H-GROUND-gap-160),passed:false}}
function startGame(id,bet){
  lastBet=bet;
  st={id:id,bet:bet,y:H/2-40,vy:0,t:0,scroll:0,pipes:[0,1,2,3].map(i=>mkPipe(W+100+i*SPACING,i+1)),nextIdx:5,score:0,started:false,over:false,deadT:0,shake:0,flash:0,
    clouds:[0,1,2].map(i=>({x:i*140+30,y:70+Math.random()*190,s:.7+Math.random()*.7})),parts:[],trail:[],tt:0};
  hide('gMenu');hide('result');show('game');document.body.classList.add('playing');
  last=0;acc=0;cancelAnimationFrame(raf);raf=requestAnimationFrame(loop);
}
function flap(){if(!st||st.over)return;st.started=true;st.vy=FLAP}
cv.addEventListener('pointerdown',e=>{e.preventDefault();flap()});
document.addEventListener('keydown',e=>{if(e.code==='Space'){e.preventDefault();flap()}});
function burst(x,y,col,n){for(let i=0;i<n;i++)st.parts.push({x:x,y:y,vx:(Math.random()-.5)*220,vy:(Math.random()-.8)*220,l:.6,c:col})}
function crash(){st.over=true;st.deadT=0;st.shake=.4;st.flash=1;st.vy=-260;burst(BX,st.y,'#ff5c7a',16);hap.err()}
function partsStep(dt){for(const q of st.parts){q.x+=q.vx*dt;q.y+=q.vy*dt;q.vy+=400*dt;q.l-=dt}st.parts=st.parts.filter(q=>q.l>0)}
function step(dt){
  st.t+=dt;
  for(const c of st.clouds){c.x-=(st.started?speedAt(st.score):30)*dt*.25*c.s;if(c.x<-90){c.x=W+70;c.y=60+Math.random()*200}}
  partsStep(dt);
  if(!st.started){st.y=H/2-40+Math.sin(st.t*5)*8;return}
  st.vy+=GRAV*dt;st.y+=st.vy*dt;
  const sp=speedAt(st.score);st.scroll+=sp*dt;
  st.tt+=dt;if(st.tt>.035){st.tt=0;st.trail.push({x:BX-6,y:st.y,l:.38})}
  for(const t of st.trail){t.x-=sp*dt;t.l-=dt}st.trail=st.trail.filter(t=>t.l>0);
  for(const p of st.pipes)p.x-=sp*dt;
  if(st.pipes[0].x<-PW){st.pipes.shift();st.pipes.push(mkPipe(st.pipes[st.pipes.length-1].x+SPACING,st.nextIdx++))}
  for(const p of st.pipes){
    if(!p.passed&&p.x+PW<BX-R){p.passed=true;st.score++;burst(BX,st.y,'#ffd166',10);hap.l()}
    if(BX+R>p.x&&BX-R<p.x+PW&&(st.y-R<p.top||st.y+R>p.top+p.gap)){crash();return}
  }
  if(st.y<R){st.y=R;st.vy=0}
  if(st.y+R>H-GROUND){st.y=H-GROUND-R;crash()}
}
function dieStep(dt){
  st.deadT+=dt;st.t+=dt;st.vy+=GRAV*dt;st.y=Math.min(H-GROUND-R,st.y+st.vy*dt);
  st.shake=Math.max(0,st.shake-dt);st.flash=Math.max(0,st.flash-dt*3);partsStep(dt);
}
function loop(ts){
  if(!st)return;
  if(!last)last=ts;
  acc+=Math.min(.1,(ts-last)/1000);last=ts;
  while(acc>=DT){if(st.over)dieStep(DT);else step(DT);acc-=DT}
  draw();
  if(st.over&&st.deadT>=.6){endGame();return}
  raf=requestAnimationFrame(loop);
}
function rr(x,y,w,h,r){ctx.beginPath();ctx.moveTo(x+r,y);ctx.arcTo(x+w,y,x+w,y+h,r);ctx.arcTo(x+w,y+h,x,y+h,r);ctx.arcTo(x,y+h,x,y,r);ctx.arcTo(x,y,x+w,y,r);ctx.closePath()}
function ridge(off,base,amp,f1,f2,col){ctx.fillStyle=col;ctx.beginPath();ctx.moveTo(-10,H);for(let x=-10;x<=W+10;x+=10){const t=x+off;ctx.lineTo(x,base+Math.sin(t*f1)*amp+Math.sin(t*f2+1.3)*amp*.5)}ctx.lineTo(W+10,H);ctx.closePath();ctx.fill()}
function cloud(c){ctx.fillStyle='rgba(255,190,230,.16)';const s=c.s*22;ctx.beginPath();ctx.arc(c.x,c.y,s,0,7);ctx.arc(c.x+s,c.y+4,s*.8,0,7);ctx.arc(c.x-s,c.y+6,s*.7,0,7);ctx.arc(c.x+s*.2,c.y-s*.5,s*.8,0,7);ctx.fill()}
function pipeBody(x,y,w,h){
  const g=ctx.createLinearGradient(x,0,x+w,0);g.addColorStop(0,'#0f7a4a');g.addColorStop(.35,'#3be48a');g.addColorStop(.62,'#22b86a');g.addColorStop(1,'#0b5e3a');
  ctx.fillStyle=g;ctx.fillRect(x,y,w,h);ctx.fillStyle='rgba(255,255,255,.2)';ctx.fillRect(x+9,y,4,h);
  ctx.fillStyle='rgba(0,0,0,.16)';for(let yy=Math.ceil(y/28)*28;yy<y+h;yy+=28)ctx.fillRect(x,yy,w,2);
}
function pipeCap(x,y){
  const g=ctx.createLinearGradient(x-5,0,x+PW+5,0);g.addColorStop(0,'#14804f');g.addColorStop(.35,'#86f7b8');g.addColorStop(.62,'#2ac577');g.addColorStop(1,'#0b5e3a');
  ctx.fillStyle=g;rr(x-5,y,PW+10,26,7);ctx.fill();ctx.strokeStyle='rgba(0,0,0,.35)';ctx.lineWidth=2;ctx.stroke();
  ctx.fillStyle='rgba(255,255,255,.25)';ctx.fillRect(x+4,y+4,5,18);
}
function draw(){
  ctx.setTransform(DPR,0,0,DPR,0,0);
  let sx=0,sy=0;if(st.shake>0){sx=(Math.random()-.5)*st.shake*34;sy=(Math.random()-.5)*st.shake*34}
  ctx.save();ctx.translate(sx,sy);
  const g=ctx.createLinearGradient(0,0,0,H);g.addColorStop(0,'#0b0b2e');g.addColorStop(.48,'#4b1d8f');g.addColorStop(.84,'#d6468a');g.addColorStop(1,'#ff9a5a');
  ctx.fillStyle=g;ctx.fillRect(-20,-20,W+40,H+40);
  for(const s of STARS){ctx.globalAlpha=.35+.65*Math.abs(Math.sin(st.t*1.5+s.p));ctx.fillStyle='#fff';ctx.beginPath();ctx.arc(s.x,s.y,s.r,0,7);ctx.fill()}ctx.globalAlpha=1;
  const mg=ctx.createRadialGradient(292,92,4,292,92,74);mg.addColorStop(0,'rgba(255,240,200,.95)');mg.addColorStop(.25,'rgba(255,240,200,.25)');mg.addColorStop(1,'rgba(255,240,200,0)');
  ctx.fillStyle=mg;ctx.beginPath();ctx.arc(292,92,74,0,7);ctx.fill();ctx.fillStyle='#fff3d1';ctx.beginPath();ctx.arc(292,92,19,0,7);ctx.fill();
  for(const c of st.clouds)cloud(c);
  ridge(st.scroll*.12,H-GROUND-112,28,.012,.027,'#2c1560');ridge(st.scroll*.25,H-GROUND-64,22,.018,.041,'#1a0c3d');
  for(const p of st.pipes){pipeBody(p.x,0,PW,p.top-26);pipeCap(p.x,p.top-26);pipeCap(p.x,p.top+p.gap);pipeBody(p.x,p.top+p.gap+26,PW,H)}
  const gg=ctx.createLinearGradient(0,H-GROUND,0,H);gg.addColorStop(0,'#3a1a63');gg.addColorStop(1,'#1a0b36');ctx.fillStyle=gg;ctx.fillRect(0,H-GROUND,W,GROUND);
  ctx.fillStyle='#ffd166';ctx.fillRect(0,H-GROUND,W,3);
  ctx.fillStyle='rgba(255,209,102,.18)';const off=-(st.scroll%32);for(let x=off;x<W;x+=32){ctx.beginPath();ctx.moveTo(x,H-GROUND+3);ctx.lineTo(x+16,H-GROUND+3);ctx.lineTo(x+6,H);ctx.lineTo(x-10,H);ctx.fill()}
  for(const t of st.trail){ctx.globalAlpha=Math.max(0,t.l/.38)*.5;ctx.fillStyle='#ffd166';ctx.beginPath();ctx.arc(t.x,t.y,R*.55*(t.l/.38),0,7);ctx.fill()}ctx.globalAlpha=1;
  for(const q of st.parts){ctx.globalAlpha=Math.max(0,q.l/.6);ctx.fillStyle=q.c;ctx.beginPath();ctx.arc(q.x,q.y,3,0,7);ctx.fill()}ctx.globalAlpha=1;
  // птичка
  ctx.save();ctx.translate(BX,st.y);ctx.rotate(Math.max(-.5,Math.min(1.3,st.vy/650)));
  ctx.fillStyle='#f59e0b';ctx.beginPath();ctx.moveTo(-R+2,0);ctx.lineTo(-R-10,-6);ctx.lineTo(-R-7,1);ctx.lineTo(-R-10,7);ctx.closePath();ctx.fill();
  const bg=ctx.createRadialGradient(-4,-6,2,0,0,R+3);bg.addColorStop(0,'#fff3a8');bg.addColorStop(.6,'#ffc83a');bg.addColorStop(1,'#f59e0b');
  ctx.fillStyle=bg;ctx.beginPath();ctx.arc(0,0,R,0,7);ctx.fill();
  ctx.fillStyle='rgba(255,255,255,.5)';ctx.beginPath();ctx.ellipse(2,6,8,5,0,0,7);ctx.fill();
  const wing=Math.sin(st.t*(st.started&&!st.over?26:9));
  ctx.fillStyle='#f97316';ctx.beginPath();ctx.ellipse(-4,2+wing*3,8,4.5+Math.abs(wing)*3,-.4+wing*.3,0,7);ctx.fill();
  ctx.fillStyle='#fb923c';ctx.beginPath();ctx.moveTo(R-2,-2);ctx.lineTo(R+10,2);ctx.lineTo(R-2,6);ctx.fill();
  ctx.fillStyle='#fff';ctx.beginPath();ctx.arc(5,-5,5,0,7);ctx.fill();ctx.fillStyle='#111';ctx.beginPath();ctx.arc(st.over?6:7,-5,st.over?1.6:2.3,0,7);ctx.fill();
  ctx.restore();
  // счёт
  ctx.textAlign='center';ctx.lineJoin='round';ctx.fillStyle='#fff';ctx.strokeStyle='rgba(10,5,40,.7)';ctx.lineWidth=7;
  ctx.font='800 58px Unbounded,Manrope,sans-serif';ctx.strokeText(st.score,W/2,76);ctx.fillText(st.score,W/2,76);
  const w=fm(st.bet*st.score*S.pillar_pct/100)+' ⭐';ctx.font='800 17px Manrope,sans-serif';const tw=ctx.measureText(w).width+26;
  ctx.fillStyle='rgba(10,5,40,.55)';rr(W/2-tw/2,92,tw,28,14);ctx.fill();ctx.fillStyle='#ffd166';ctx.fillText(w,W/2,112);
  ctx.font='700 13px Manrope,sans-serif';ctx.fillStyle='rgba(255,255,255,.85)';ctx.fillText('скорость ×'+(speedAt(st.score)/CFG.speedBase).toFixed(1),W/2,140);
  if(!st.started){ctx.font='800 22px Manrope,sans-serif';ctx.globalAlpha=.6+.4*Math.sin(st.t*4);ctx.fillStyle='#fff';ctx.strokeStyle='rgba(10,5,40,.7)';ctx.lineWidth=6;ctx.strokeText('Тапни, чтобы лететь',W/2,H/2+70);ctx.fillText('Тапни, чтобы лететь',W/2,H/2+70);ctx.globalAlpha=1}
  ctx.restore();
  if(st.flash>0){ctx.fillStyle='rgba(255,92,122,'+(st.flash*.35)+')';ctx.fillRect(0,0,W,H)}
}
async function endGame(){
  const s=st;
  try{
    const j=await api('finish',{game_id:s.id,pillars:s.score});
    setBal(j.balance,true);
    $('rp').textContent=j.pillars;$('rb').textContent=fm(j.bet);$('rw').textContent=fm(j.payout);
    const net=Math.round((j.payout-j.bet)*100)/100;
    $('rn').textContent=(net>=0?'+':'')+fm(net)+' ⭐';$('rn').className='net '+(net>=0?'win':'lose');
    if(net>0){confetti(net>=j.bet*2?140:70,CONF);hap.ok()}
  }catch(e){tg.showAlert(e.message)}
  show('result');
}
$('again').onclick=async()=>{try{const j=await api('start',{bet:lastBet});setBal(j.balance);startGame(j.game_id,lastBet)}catch(e){tg.showAlert(e.message)}};
$('tomenu').onclick=()=>{st=null;hide('game');show('gMenu');document.body.classList.remove('playing');refresh()};
$('play').onclick=async()=>{
  const b=betVal();if(!b)return;
  try{const j=await api('start',{bet:b});setBal(j.balance);startGame(j.game_id,b)}catch(e){tg.showAlert(e.message)}
};
refresh();
</script></body></html>
"""

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")
