import asyncio
import html
import logging
import os
import re
import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import aiohttp
import asyncpg
from aiohttp import web
from aiogram import Bot, Dispatcher, F, types
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, KeyboardButton, ReplyKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

# ================= Настройки (из переменных окружения Render) =================
BOT_TOKEN = os.environ["BOT_TOKEN"]
DATABASE_URL = os.environ["DATABASE_URL"]
ADMIN_ID = int(os.environ["ADMIN_ID"])
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "@Pavelmanager")
SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME", "@PavelSupport")
PRICE_STARS = int(os.getenv("PRICE_STARS", "600"))
CATIGRAM_API_URL = os.getenv("CATIGRAM_API_URL", "http://bot.catup.lol")
LINK_BASE = os.getenv("LINK_BASE", "https://tg.catup.lol")
PORT = int(os.getenv("PORT", "10000"))
# ==============================================================================

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("giveaway")

session = AiohttpSession(api=TelegramAPIServer.from_base(CATIGRAM_API_URL), timeout=20)
bot = Bot(token=BOT_TOKEN, session=session,
          default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())
esc = html.escape


async def quick_answer(callback: types.CallbackQuery, *args, **kwargs):
    """answerCallbackQuery иногда подвисает на сервере Catugram — не ждём его дольше 3 секунд."""
    try:
        await asyncio.wait_for(callback.answer(*args, **kwargs), timeout=3)
    except Exception as e:
        log.warning("callback.answer не прошёл: %r", e)


class GiveawayForm(StatesGroup):
    mode = State()
    winners = State()
    prize_type = State()
    prize_details = State()
    code_word = State()
    conditions = State()


class JoinForm(StatesGroup):
    code = State()


class DonateForm(StatesGroup):
    amount = State()


class AdminForm(StatesGroup):
    give = State()
    revoke = State()
    broadcast = State()
    confirm = State()


# ============================== База данных ==============================
def clean_dsn(dsn: str) -> str:
    p = urlsplit(dsn)
    q = [(k, v) for k, v in parse_qsl(p.query) if k != "channel_binding"]
    return urlunsplit((p.scheme, p.netloc, p.path, urlencode(q), p.fragment))


async def get_db_pool():
    return await asyncpg.create_pool(
        clean_dsn(DATABASE_URL), min_size=1, max_size=5,
        statement_cache_size=0, max_inactive_connection_lifetime=60)


async def init_db(pool):
    async with pool.acquire() as conn:
        await conn.execute("CREATE TABLE IF NOT EXISTS creators (user_id BIGINT PRIMARY KEY)")
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS giveaways (
                id SERIAL PRIMARY KEY,
                creator_id BIGINT NOT NULL,
                kind TEXT NOT NULL,
                prize TEXT NOT NULL,
                conditions TEXT,
                code TEXT,
                status TEXT NOT NULL DEFAULT 'active',
                next_ticket INT NOT NULL DEFAULT 1,
                winner_id BIGINT,
                winner_username TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )""")
        await conn.execute("ALTER TABLE giveaways ADD COLUMN IF NOT EXISTS code TEXT")
        await conn.execute("ALTER TABLE giveaways ADD COLUMN IF NOT EXISTS winners INT NOT NULL DEFAULT 1")
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS giveaway_participants (
                giveaway_id INT NOT NULL REFERENCES giveaways(id) ON DELETE CASCADE,
                user_id BIGINT NOT NULL,
                username TEXT,
                ticket_number INT NOT NULL,
                won BOOLEAN NOT NULL DEFAULT FALSE,
                PRIMARY KEY (giveaway_id, user_id),
                UNIQUE (giveaway_id, ticket_number)
            )""")
        await conn.execute("ALTER TABLE giveaway_participants ADD COLUMN IF NOT EXISTS place INT")
        await conn.execute("CREATE TABLE IF NOT EXISTS sub_ack (user_id BIGINT PRIMARY KEY)")
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS payments (
                charge_id TEXT PRIMARY KEY,
                user_id BIGINT NOT NULL,
                payload TEXT NOT NULL,
                amount INT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )""")
        await conn.execute("INSERT INTO creators (user_id) VALUES ($1) ON CONFLICT DO NOTHING", ADMIN_ID)


# --- кэш, чтобы не ходить в базу на каждое сообщение (ускоряет ответы) ---
_creator_cache: dict = {}   # user_id -> (значение, до какого времени верно)
_ack_cache: set = set()     # кто уже подтвердил подписку
_me_cache = None


async def get_me_cached():
    global _me_cache
    if _me_cache is None:
        _me_cache = await bot.get_me()
    return _me_cache


async def is_creator(pool, user_id) -> bool:
    if user_id == ADMIN_ID:
        return True
    now = time.monotonic()
    hit = _creator_cache.get(user_id)
    if hit and hit[1] > now:
        return hit[0]
    async with pool.acquire() as conn:
        val = await conn.fetchval("SELECT 1 FROM creators WHERE user_id=$1", user_id) is not None
    _creator_cache[user_id] = (val, now + 30)
    return val


async def add_creator(pool, user_id) -> bool:
    """True, если доступ уже был."""
    async with pool.acquire() as conn:
        res = await conn.execute("INSERT INTO creators (user_id) VALUES ($1) ON CONFLICT DO NOTHING", user_id)
    _creator_cache.pop(user_id, None)
    return res.endswith(" 0")


async def get_giveaway(pool, gid):
    async with pool.acquire() as conn:
        return await conn.fetchrow("SELECT * FROM giveaways WHERE id=$1", gid)


async def add_participant(pool, gid, user_id, username):
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.fetchval("SELECT id FROM giveaways WHERE id=$1 FOR UPDATE", gid)
            existing = await conn.fetchval(
                "SELECT ticket_number FROM giveaway_participants WHERE giveaway_id=$1 AND user_id=$2",
                gid, user_id)
            if existing:
                return existing, False
            ticket = await conn.fetchval(
                "UPDATE giveaways SET next_ticket = next_ticket + 1 WHERE id=$1 RETURNING next_ticket - 1", gid)
            await conn.execute(
                "INSERT INTO giveaway_participants (giveaway_id, user_id, username, ticket_number) "
                "VALUES ($1,$2,$3,$4)", gid, user_id, username, ticket)
            return ticket, True


# ============================== Утилиты ==============================
def mention(user_id, username) -> str:
    if username:
        return f"@{esc(username)}"
    return f'<a href="tg://user?id={user_id}">ID {user_id}</a>'


def parse_arg(args, prefix):
    m = re.fullmatch(rf"{prefix}_(\d+)", args or "")
    return int(m.group(1)) if m else None


def norm_code(s: str) -> str:
    return " ".join((s or "").split()).casefold()


MEDALS = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]
MAX_WINNERS = 10
MAX_NFT_PER_PLACE = 10


def ru_people(n: int) -> str:
    return "человека" if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14) else "человек"

REQUIRED_CHANNELS = ["PavelBet", "plushpepeholders"]

BTN_NEW = "➕ Создать конкурс"
BTN_MY = "📋 Мои конкурсы"
BTN_FINISH = "🏁 Итоги"
BTN_REROLL = "🔄 Переролл"
BTN_ID = "🆔 Мой ID"
BTN_DONATE = "💖 Донат"
BTN_BUY = f"⭐️ Купить доступ ({PRICE_STARS})"
BTN_ADMIN = "💬 Написать админу"
BTN_NFT = "🖼 Доступ за NFT"
BTN_GIVE = "🔑 Выдать доступ"
BTN_REVOKE = "🚫 Забрать доступ"
BTN_LIST = "👥 Доступы"
BTN_STATS = "📈 Статистика"
BTN_ALL = "🗂 Все конкурсы"
BTN_PAY = "💰 Платежи"
BTN_BCAST = "📣 Рассылка"


def menu_kb(user_id: int, creator: bool) -> ReplyKeyboardMarkup:
    if creator:
        rows = [[BTN_NEW, BTN_MY], [BTN_FINISH, BTN_REROLL], [BTN_ID, BTN_DONATE]]
        if user_id == ADMIN_ID:
            rows += [[BTN_GIVE, BTN_REVOKE], [BTN_LIST, BTN_STATS], [BTN_ALL, BTN_PAY], [BTN_BCAST]]
    else:
        rows = [[BTN_BUY, BTN_NFT], [BTN_DONATE, BTN_ID], [BTN_ADMIN]]
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=t) for t in r] for r in rows], resize_keyboard=True)


async def handle_ping(request):
    return web.Response(text="Bot is alive!")


async def start_dummy_server():
    app = web.Application()
    app.router.add_get("/", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()


async def set_main_menu(bot: Bot):
    await bot.set_my_commands([BotCommand(command="start", description="Главное меню")])


# ============================== Старт / участие ==============================
@dp.message(Command("myid"))
async def cmd_myid(message: types.Message):
    await message.answer(f"🆔 Твой ID: <code>{message.from_user.id}</code>")


async def begin_join(message: types.Message, state: FSMContext, user: types.User, gid: int):
    pool = dp["pool"]
    g = await get_giveaway(pool, gid)
    if not g or g["kind"] != "normal":
        await message.answer("⚠️ Розыгрыш не найден.")
        return
    if g["status"] != "active":
        await message.answer("🔒 Этот конкурс уже завершён.")
        return
    async with pool.acquire() as conn:
        ticket = await conn.fetchval(
            "SELECT ticket_number FROM giveaway_participants WHERE giveaway_id=$1 AND user_id=$2",
            gid, user.id)
    if ticket:
        await message.answer(f"ℹ️ Вы уже зарегистрированы. Ваш номер: <b>#{ticket}</b>")
        return
    if not g["code"]:  # старые конкурсы без кодового слова
        t, _ = await add_participant(pool, gid, user.id, user.username)
        await message.answer(f"🎉 Вы успешно участвуете! Ваш номер: <b>#{t}</b>")
        return
    await state.set_state(JoinForm.code)
    await state.update_data(join_gid=gid)
    await message.answer(f"🔑 Конкурс <b>#{gid}</b>\nВведите кодовое слово из поста/канала:")


async def has_ack(pool, user_id: int) -> bool:
    if user_id in _ack_cache:
        return True
    async with pool.acquire() as conn:
        ok = await conn.fetchval("SELECT 1 FROM sub_ack WHERE user_id=$1", user_id) is not None
    if ok:
        _ack_cache.add(user_id)
    return ok


async def send_gate(message: types.Message, args):
    kb = InlineKeyboardBuilder()
    for name in REQUIRED_CHANNELS:
        kb.button(text=f"📢 @{name}", url=f"{LINK_BASE}/{name}")
    kb.button(text="✅ Я подписался", callback_data=f"subok:{args or ''}")
    kb.adjust(1)
    chans = "\n".join(f"• @{n}" for n in REQUIRED_CHANNELS)
    await message.answer(f"📢 Чтобы пользоваться ботом, подпишитесь на наши каналы:\n\n{chans}\n\n"
                         f"После подписки нажмите кнопку ниже 👇", reply_markup=kb.as_markup())


@dp.message(Command("start"))
async def cmd_start(message: types.Message, command: CommandObject, state: FSMContext):
    await state.clear()
    user = message.from_user
    if user.id != ADMIN_ID and not await has_ack(dp["pool"], user.id):
        await send_gate(message, command.args)
        return
    await process_start(message, state, user, command.args)


@dp.callback_query(F.data.startswith("subok"))
async def sub_ok(callback: types.CallbackQuery, state: FSMContext):
    await quick_answer(callback)
    await state.clear()
    args = callback.data.split(":", 1)[1] if ":" in callback.data else ""
    async with dp["pool"].acquire() as conn:
        await conn.execute("INSERT INTO sub_ack (user_id) VALUES ($1) ON CONFLICT DO NOTHING", callback.from_user.id)
    _ack_cache.add(callback.from_user.id)
    await process_start(callback.message, state, callback.from_user, args or None)


async def claim_fast(message: types.Message, user: types.User, gid: int):
    """Фаст-клик: первые N человек получают призы по местам (без гонок)."""
    pool = dp["pool"]
    async with pool.acquire() as conn:
        async with conn.transaction():
            g = await conn.fetchrow(
                "SELECT creator_id, prize, winners, winner_id FROM giveaways "
                "WHERE id=$1 AND kind='fast' FOR UPDATE", gid)
            if not g:
                res = ("missing",)
            elif g["creator_id"] == user.id:
                res = ("creator",)
            else:
                n = g["winners"] or 1
                mine = await conn.fetchval(
                    "SELECT place FROM giveaway_participants WHERE giveaway_id=$1 AND user_id=$2", gid, user.id)
                taken = await conn.fetchval("SELECT count(*) FROM giveaway_participants WHERE giveaway_id=$1", gid)
                if taken == 0 and g["winner_id"] is not None:
                    taken = 1  # старый фаст-клик (до обновления)
                if mine:
                    res = ("mine", mine)
                elif g["winner_id"] == user.id:
                    res = ("mine", 1)
                elif taken >= n:
                    res = ("full",)
                else:
                    place = taken + 1
                    await conn.execute(
                        "INSERT INTO giveaway_participants (giveaway_id, user_id, username, ticket_number, won, place) "
                        "VALUES ($1,$2,$3,$4,TRUE,$4)", gid, user.id, user.username, place)
                    if place == 1:
                        await conn.execute("UPDATE giveaways SET winner_id=$2, winner_username=$3 WHERE id=$1",
                                           gid, user.id, user.username)
                    if place >= n:
                        await conn.execute("UPDATE giveaways SET status='finished' WHERE id=$1", gid)
                    res = ("win", place, g["prize"], n, g["creator_id"])

    kind = res[0]
    if kind == "missing":
        await message.answer("⚠️ Розыгрыш не найден.")
    elif kind == "creator":
        await message.answer("🙅 Создатель не может участвовать в своём фаст-клике.")
    elif kind == "mine":
        await message.answer(f"ℹ️ Вы уже забрали приз в этом розыгрыше ({res[1]} место).")
    elif kind == "full":
        await message.answer("❌ К сожалению, все призы уже забрали!")
    else:
        _, place, prize, n, creator_id = res
        prizes = prize.split(" | ") if n > 1 else [prize]
        mine_prize = prizes[min(place, len(prizes)) - 1]
        if n > 1:
            head = f"⚡️ <b>ПОЗДРАВЛЯЕМ! ТЫ ЗАНЯЛ {MEDALS[place - 1]} {place} МЕСТО!</b>"
            note = f"⚡️ Фаст-клик #{gid}: {place} место — {mention(user.id, user.username)} (<code>{user.id}</code>)"
        else:
            head = "⚡️ <b>ПОЗДРАВЛЯЕМ! ТЫ БЫЛ ПЕРВЫМ!</b>"
            note = f"⚡️ Фаст-клик #{gid} выиграл {mention(user.id, user.username)} (<code>{user.id}</code>)"
        await message.answer(f"{head}\n\nПриз: <b>{esc(mine_prize)}</b>")
        try:
            await bot.send_message(creator_id, note)
        except TelegramAPIError:
            pass


async def process_start(message: types.Message, state: FSMContext, user: types.User, args):
    pool = dp["pool"]

    gid = parse_arg(args, "fast")
    if gid is not None:
        await claim_fast(message, user, gid)
        return

    gid = parse_arg(args, "join")
    if gid is not None:
        await begin_join(message, state, user, gid)
        return

    creator = await is_creator(pool, user.id)
    if creator:
        text = "👋 Привет, Создатель!\nВыбирай действие в меню внизу 👇"
    else:
        text = ("🔒 Создавать конкурсы могут только пользователи с доступом.\n"
                "Участвовать в конкурсах можно бесплатно — по ссылке из поста.\n\nМеню внизу 👇")
    await message.answer(text, reply_markup=menu_kb(user.id, creator))
    if not creator:
        await send_access_choice(message)


# ============================== Кнопки меню ==============================
# Эти обработчики стоят выше остальных, поэтому нажатие кнопки сбрасывает любой диалог.
@dp.message(F.text == BTN_NEW)
async def btn_new(message: types.Message, state: FSMContext):
    await state.clear()
    await cmd_new_giveaway(message, state)


@dp.message(F.text == BTN_MY)
async def btn_my(message: types.Message, state: FSMContext):
    await state.clear()
    await cmd_my(message)


@dp.message(F.text.in_({BTN_FINISH, BTN_REROLL}))
async def btn_draw_list(message: types.Message, state: FSMContext):
    await state.clear()
    pool = dp["pool"]
    if not await is_creator(pool, message.from_user.id):
        return
    reroll = message.text == BTN_REROLL
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT g.id, g.prize, (SELECT count(*) FROM giveaway_participants p WHERE p.giveaway_id=g.id) AS cnt "
            "FROM giveaways g WHERE g.creator_id=$1 AND g.kind='normal' AND g.status=$2 "
            "ORDER BY g.id DESC LIMIT 10",
            message.from_user.id, "finished" if reroll else "active")
    if not rows:
        await message.answer("Нет завершённых конкурсов для реролла." if reroll else "Нет активных конкурсов.")
        return
    kb = InlineKeyboardBuilder()
    mode = "reroll" if reroll else "finish"
    for r in rows:
        kb.button(text=f"#{r['id']} · {r['prize'][:22]} · {r['cnt']} уч.", callback_data=f"draw_{mode}_{r['id']}")
    kb.adjust(1)
    await message.answer("Выберите конкурс для реролла:" if reroll else "Выберите конкурс для подведения итогов:",
                         reply_markup=kb.as_markup())


@dp.callback_query(F.data.regexp(r"^draw_(finish|reroll)_(\d+)$"))
async def draw_callback(callback: types.CallbackQuery):
    _, mode, gid = callback.data.split("_")
    await quick_answer(callback)
    if not await is_creator(dp["pool"], callback.from_user.id):
        return
    await run_draw(callback.message, callback.from_user.id, int(gid), reroll=(mode == "reroll"))


@dp.message(F.text == BTN_ID)
async def btn_id(message: types.Message, state: FSMContext):
    await state.clear()
    await cmd_myid(message)


@dp.message(F.text == BTN_DONATE)
async def btn_donate(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("Введите количество звёзд для пожертвования (только цифры):")
    await state.set_state(DonateForm.amount)


@dp.message(F.text == BTN_BUY)
async def btn_buy(message: types.Message, state: FSMContext):
    await state.clear()
    if await is_creator(dp["pool"], message.from_user.id):
        await message.answer("✅ У вас уже есть доступ.")
        return
    await bot.send_invoice(
        chat_id=message.from_user.id, title="Доступ к боту", description="Покупка доступа (один раз, навсегда)",
        payload=f"access_{message.from_user.id}", currency="XTR",
        prices=[types.LabeledPrice(label="Доступ", amount=PRICE_STARS)], provider_token="")


nft_notified: set = set()


async def send_access_choice(message: types.Message):
    kb = InlineKeyboardBuilder()
    kb.button(text=f"⭐️ Оплатить {PRICE_STARS} звёзд", callback_data="buy_access")
    kb.button(text="🖼 Оплатить NFT", callback_data="buy_nft")
    kb.adjust(1)
    await message.answer("🔑 <b>Доступ к созданию конкурсов</b> (оплата один раз, навсегда).\n"
                         "Выберите способ:", reply_markup=kb.as_markup())


async def send_nft_instructions(message: types.Message, user: types.User):
    kb = InlineKeyboardBuilder()
    kb.button(text=f"💬 Написать {SUPPORT_USERNAME}", url=f"{LINK_BASE}/{SUPPORT_USERNAME.lstrip('@')}")
    await message.answer(
        "🖼 <b>Оплата доступа через NFT</b>\n\n"
        f"1️⃣ Нажмите в боте кнопку «{BTN_ID}» и скопируйте свой ID\n"
        f"2️⃣ Отправьте NFT на аккаунт {esc(SUPPORT_USERNAME)}\n"
        f"3️⃣ <b>Обязательно</b> напишите там, что это оплата <b>для доступа к боту</b>, "
        f"и пришлите свой ID из бота\n\n"
        "⏳ Вам ответят в течение 1 часа.", reply_markup=kb.as_markup())
    if user.id not in nft_notified:
        nft_notified.add(user.id)
        try:
            await bot.send_message(ADMIN_ID, f"🖼 {mention(user.id, user.username)} (<code>{user.id}</code>) "
                                             f"выбрал оплату доступа через NFT.")
        except TelegramAPIError:
            pass


@dp.callback_query(F.data == "buy_nft")
async def buy_nft_callback(callback: types.CallbackQuery):
    await quick_answer(callback)
    await send_nft_instructions(callback.message, callback.from_user)


@dp.message(F.text == BTN_NFT)
async def btn_nft(message: types.Message, state: FSMContext):
    await state.clear()
    if await is_creator(dp["pool"], message.from_user.id):
        await message.answer("✅ У вас уже есть доступ.")
        return
    await send_nft_instructions(message, message.from_user)


@dp.message(F.text == BTN_ADMIN)
async def btn_admin(message: types.Message, state: FSMContext):
    await state.clear()
    kb = InlineKeyboardBuilder()
    kb.button(text=f"💬 {ADMIN_USERNAME}", url=f"{LINK_BASE}/{ADMIN_USERNAME.lstrip('@')}")
    await message.answer("Напишите админу:", reply_markup=kb.as_markup())


# --- кнопки админа ---
@dp.message(F.text == BTN_GIVE)
async def btn_give(message: types.Message, state: FSMContext):
    await state.clear()
    if message.from_user.id != ADMIN_ID:
        return
    await message.answer("Отправьте ID пользователя цифрами (он узнаёт его кнопкой «🆔 Мой ID»).\n"
                         "Чтобы отменить — нажмите любую кнопку меню.")
    await state.set_state(AdminForm.give)


@dp.message(F.text == BTN_REVOKE)
async def btn_revoke(message: types.Message, state: FSMContext):
    await state.clear()
    if message.from_user.id != ADMIN_ID:
        return
    await message.answer("Отправьте ID пользователя, у которого забрать доступ.\n"
                         "Чтобы отменить — нажмите любую кнопку меню.")
    await state.set_state(AdminForm.revoke)


@dp.message(F.text == BTN_LIST)
async def btn_list(message: types.Message, state: FSMContext):
    await state.clear()
    if message.from_user.id == ADMIN_ID:
        await list_creators(message)


# --- админ: статистика, конкурсы, платежи, рассылка ---
@dp.message(F.text == BTN_STATS)
async def btn_stats(message: types.Message, state: FSMContext):
    await state.clear()
    if message.from_user.id != ADMIN_ID:
        return
    async with dp["pool"].acquire() as conn:
        creators = await conn.fetchval("SELECT count(*) FROM creators")
        users = await conn.fetchval(
            "SELECT count(*) FROM (SELECT user_id FROM sub_ack UNION SELECT user_id FROM creators "
            "UNION SELECT user_id FROM giveaway_participants) t")
        g = await conn.fetchrow(
            "SELECT count(*) FILTER (WHERE kind='normal') AS n, count(*) FILTER (WHERE kind='fast') AS f, "
            "count(*) FILTER (WHERE status='active') AS a, count(*) FILTER (WHERE status='finished') AS d "
            "FROM giveaways")
        parts = await conn.fetchval("SELECT count(*) FROM giveaway_participants")
        p = await conn.fetchrow(
            "SELECT count(*) FILTER (WHERE payload LIKE 'access%') AS ac, "
            "coalesce(sum(amount) FILTER (WHERE payload LIKE 'access%'),0) AS asum, "
            "count(*) FILTER (WHERE payload LIKE 'donate%') AS dc, "
            "coalesce(sum(amount) FILTER (WHERE payload LIKE 'donate%'),0) AS dsum FROM payments")
    await message.answer(
        "📈 <b>Статистика</b>\n\n"
        f"👥 Пользователей: <b>{users}</b>\n"
        f"🔑 С доступом: <b>{creators}</b>\n"
        f"🎁 Конкурсов: <b>{g['n'] + g['f']}</b> (обычных {g['n']}, фаст-кликов {g['f']})\n"
        f"🟢 Активных: {g['a']} · 🔴 завершённых: {g['d']}\n"
        f"🎟 Участий всего: <b>{parts}</b>\n\n"
        f"⭐️ Куплено доступов: <b>{p['ac']}</b> ({p['asum']} ⭐)\n"
        f"💖 Донатов: <b>{p['dc']}</b> ({p['dsum']} ⭐)")


@dp.message(F.text == BTN_ALL)
async def btn_all(message: types.Message, state: FSMContext):
    await state.clear()
    if message.from_user.id != ADMIN_ID:
        return
    async with dp["pool"].acquire() as conn:
        rows = await conn.fetch(
            "SELECT g.id, g.kind, g.prize, g.status, "
            "(SELECT count(*) FROM giveaway_participants p WHERE p.giveaway_id=g.id) AS cnt "
            "FROM giveaways g ORDER BY g.id DESC LIMIT 15")
    if not rows:
        await message.answer("Конкурсов пока нет.")
        return
    kb = InlineKeyboardBuilder()
    for r in rows:
        k = "⚡️" if r["kind"] == "fast" else "📊"
        st = "🟢" if r["status"] == "active" else "🔴"
        extra = f" · {r['cnt']} уч." if r["kind"] == "normal" else ""
        kb.button(text=f"{k}{st} #{r['id']} · {r['prize'][:18]}{extra}", callback_data=f"adm_g_{r['id']}")
    kb.adjust(1)
    await message.answer("🗂 Последние 15 конкурсов. Нажмите, чтобы открыть:", reply_markup=kb.as_markup())


@dp.callback_query(F.data.regexp(r"^adm_g_\d+$"))
async def adm_game_details(callback: types.CallbackQuery):
    await quick_answer(callback)
    if callback.from_user.id != ADMIN_ID:
        return
    gid = int(callback.data.rsplit("_", 1)[1])
    g = await get_giveaway(dp["pool"], gid)
    if not g:
        await callback.message.answer("⚠️ Конкурс не найден (возможно, удалён).")
        return
    async with dp["pool"].acquire() as conn:
        cnt = await conn.fetchval("SELECT count(*) FROM giveaway_participants WHERE giveaway_id=$1", gid)
    fast = g["kind"] == "fast"
    lines = [f"<b>#{gid}</b> · {'⚡️ Фаст-клик' if fast else '📊 Обычный'} · "
             f"{'🟢 активен' if g['status'] == 'active' else '🔴 завершён'}",
             f"🎁 {esc(g['prize'])}",
             f"👤 Создатель: <code>{g['creator_id']}</code>",
             f"📅 {g['created_at']:%d.%m.%Y %H:%M} UTC"]
    if not fast:
        lines.append(f"📌 Условия: {esc(g['conditions'] or '—')}")
        lines.append(f"🔑 Кодовое слово: {('<code>' + esc(g['code']) + '</code>') if g['code'] else '—'}")
        lines.append(f"🎟 Участников: <b>{cnt}</b>")
    async with dp["pool"].acquire() as conn:
        wrows = await conn.fetch("SELECT user_id, username, place FROM giveaway_participants "
                                 "WHERE giveaway_id=$1 AND place IS NOT NULL ORDER BY place", gid)
    if wrows:
        lines.append("🏆 Победители:")
        lines += [f"{MEDALS[min(r['place'], 10) - 1]} {r['place']} — {mention(r['user_id'], r['username'])} "
                  f"(<code>{r['user_id']}</code>)" for r in wrows]
    elif g["winner_id"]:
        lines.append(f"🏆 Победитель: {mention(g['winner_id'], g['winner_username'])} (<code>{g['winner_id']}</code>)")
    kb = InlineKeyboardBuilder()
    if not fast:
        kb.button(text="👥 Участники", callback_data=f"adm_p_{gid}")
    kb.button(text="🗑 Удалить конкурс", callback_data=f"adm_d_{gid}")
    kb.adjust(1)
    await callback.message.answer("\n".join(lines), reply_markup=kb.as_markup())


@dp.callback_query(F.data.regexp(r"^adm_p_\d+$"))
async def adm_participants(callback: types.CallbackQuery):
    await quick_answer(callback)
    if callback.from_user.id != ADMIN_ID:
        return
    gid = int(callback.data.rsplit("_", 1)[1])
    async with dp["pool"].acquire() as conn:
        rows = await conn.fetch("SELECT user_id, username, ticket_number, won FROM giveaway_participants "
                                "WHERE giveaway_id=$1 ORDER BY ticket_number LIMIT 60", gid)
        total = await conn.fetchval("SELECT count(*) FROM giveaway_participants WHERE giveaway_id=$1", gid)
    if not rows:
        await callback.message.answer("Участников пока нет.")
        return
    lines = [f"#{r['ticket_number']} — {mention(r['user_id'], r['username'])} (<code>{r['user_id']}</code>)"
             f"{' 🏆' if r['won'] else ''}" for r in rows]
    if total > len(rows):
        lines.append(f"… и ещё {total - len(rows)}")
    await callback.message.answer(f"👥 Участники конкурса #{gid} ({total}):\n" + "\n".join(lines))


@dp.callback_query(F.data.regexp(r"^adm_d_\d+$"))
async def adm_delete_ask(callback: types.CallbackQuery):
    await quick_answer(callback)
    if callback.from_user.id != ADMIN_ID:
        return
    gid = int(callback.data.rsplit("_", 1)[1])
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Да, удалить", callback_data=f"adm_dy_{gid}")
    kb.button(text="❌ Отмена", callback_data="adm_dn")
    kb.adjust(1)
    await callback.message.answer(f"Удалить конкурс #{gid} вместе со всеми участниками? Это нельзя отменить.",
                                  reply_markup=kb.as_markup())


@dp.callback_query(F.data.regexp(r"^adm_dy_\d+$"))
async def adm_delete_do(callback: types.CallbackQuery):
    await quick_answer(callback)
    if callback.from_user.id != ADMIN_ID:
        return
    gid = int(callback.data.rsplit("_", 1)[1])
    async with dp["pool"].acquire() as conn:
        await conn.execute("DELETE FROM giveaways WHERE id=$1", gid)
    await callback.message.answer(f"🗑 Конкурс #{gid} удалён.")


@dp.callback_query(F.data == "adm_dn")
async def adm_delete_cancel(callback: types.CallbackQuery):
    await quick_answer(callback, "Отменено")


@dp.message(F.text == BTN_PAY)
async def btn_pay(message: types.Message, state: FSMContext):
    await state.clear()
    if message.from_user.id != ADMIN_ID:
        return
    async with dp["pool"].acquire() as conn:
        rows = await conn.fetch("SELECT user_id, payload, amount, created_at FROM payments "
                                "ORDER BY created_at DESC LIMIT 15")
    if not rows:
        await message.answer("Платежей пока нет.")
        return
    lines = [f"{'⭐️ доступ' if r['payload'].startswith('access') else '💖 донат'} · {r['amount']} ⭐ · "
             f"<code>{r['user_id']}</code> · {r['created_at']:%d.%m %H:%M}" for r in rows]
    await message.answer("💰 Последние платежи (время UTC):\n" + "\n".join(lines))


@dp.message(F.text == BTN_BCAST)
async def btn_bcast(message: types.Message, state: FSMContext):
    await state.clear()
    if message.from_user.id != ADMIN_ID:
        return
    await message.answer("📣 Отправьте текст рассылки (получат все, кто пользовался ботом).\n"
                         "Чтобы отменить — нажмите любую кнопку меню.")
    await state.set_state(AdminForm.broadcast)


@dp.message(AdminForm.broadcast, F.text)
async def admin_broadcast_text(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await state.update_data(bc_text=message.text)
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Отправить всем", callback_data="bc_yes")
    kb.button(text="❌ Отмена", callback_data="bc_no")
    kb.adjust(1)
    await message.answer(f"Предпросмотр:\n\n{esc(message.text)}", reply_markup=kb.as_markup())
    await state.set_state(AdminForm.confirm)


@dp.callback_query(AdminForm.confirm, F.data == "bc_no")
async def bc_cancel(callback: types.CallbackQuery, state: FSMContext):
    await quick_answer(callback, "Отменено")
    await state.clear()


async def run_broadcast(message: types.Message, text: str):
    async with dp["pool"].acquire() as conn:
        ids = [r["user_id"] for r in await conn.fetch(
            "SELECT user_id FROM sub_ack UNION SELECT user_id FROM creators "
            "UNION SELECT user_id FROM giveaway_participants")]
    ok = fail = 0
    for uid in ids:
        try:
            await bot.send_message(uid, esc(text))
            ok += 1
        except TelegramAPIError:
            fail += 1
        await asyncio.sleep(0.05)
    await message.answer(f"📣 Рассылка завершена: доставлено <b>{ok}</b>, не доставлено {fail}.")


@dp.callback_query(AdminForm.confirm, F.data == "bc_yes")
async def bc_send(callback: types.CallbackQuery, state: FSMContext):
    await quick_answer(callback)
    if callback.from_user.id != ADMIN_ID:
        return
    text = (await state.get_data()).get("bc_text")
    await state.clear()
    if not text:
        return
    await callback.message.answer("⏳ Рассылка началась…")
    asyncio.create_task(run_broadcast(callback.message, text))


@dp.message(AdminForm.give)
async def admin_give(message: types.Message, state: FSMContext):
    txt = (message.text or "").strip()
    if message.from_user.id != ADMIN_ID or not txt.isdigit():
        await message.answer("⚠️ Отправьте ID цифрами:")
        return
    await state.clear()
    await grant_access(message, int(txt))


@dp.message(AdminForm.revoke)
async def admin_revoke(message: types.Message, state: FSMContext):
    txt = (message.text or "").strip()
    if message.from_user.id != ADMIN_ID or not txt.isdigit():
        await message.answer("⚠️ Отправьте ID цифрами:")
        return
    await state.clear()
    await revoke_access(message, int(txt))


# ============================== Оплата / донаты ==============================
@dp.callback_query(F.data == "donate_stars")
async def start_donate(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.answer("Введите количество звёзд для пожертвования (только цифры):")
    await state.set_state(DonateForm.amount)
    await quick_answer(callback)


@dp.message(DonateForm.amount)
async def process_donate_amount(message: types.Message, state: FSMContext):
    txt = (message.text or "").strip()
    if not txt.isdigit() or not (0 < int(txt) <= 100000):
        await message.answer("⚠️ Введите корректное число:")
        return
    amount = int(txt)
    await bot.send_invoice(
        chat_id=message.from_user.id, title="Донат", description=f"Пожертвование {amount} звёзд",
        payload=f"donate_{message.from_user.id}", currency="XTR",
        prices=[types.LabeledPrice(label="Пожертвование", amount=amount)], provider_token="")
    await state.clear()


@dp.callback_query(F.data == "buy_access")
async def buy_access_callback(callback: types.CallbackQuery):
    await bot.send_invoice(
        chat_id=callback.from_user.id, title="Доступ к боту", description="Покупка доступа",
        payload=f"access_{callback.from_user.id}", currency="XTR",
        prices=[types.LabeledPrice(label="Доступ", amount=PRICE_STARS)], provider_token="")
    await quick_answer(callback)


@dp.pre_checkout_query()
async def pre_checkout_handler(q: types.PreCheckoutQuery):
    await bot.answer_pre_checkout_query(q.id, ok=True)


@dp.message(F.successful_payment)
async def payment_success(message: types.Message):
    pool = dp["pool"]
    sp = message.successful_payment
    async with pool.acquire() as conn:
        inserted = await conn.execute(
            "INSERT INTO payments (charge_id, user_id, payload, amount) VALUES ($1,$2,$3,$4) ON CONFLICT DO NOTHING",
            sp.telegram_payment_charge_id, message.from_user.id, sp.invoice_payload, sp.total_amount)
    if inserted.endswith(" 0"):
        return
    if sp.invoice_payload.startswith("donate_"):
        await message.answer("💖 Спасибо за поддержку!")
    else:
        await add_creator(pool, message.from_user.id)
        await message.answer("🎉 Оплата успешна! Доступ открыт навсегда. Меню внизу 👇",
                             reply_markup=menu_kb(message.from_user.id, True))


# ============================== Админ: выдача доступа ==============================
async def grant_access(message: types.Message, target: int):
    if await add_creator(dp["pool"], target):
        await message.answer(f"ℹ️ У пользователя <code>{target}</code> уже есть доступ.")
        return
    await message.answer(f"✅ Доступ навсегда выдан пользователю <code>{target}</code>!")
    try:
        await bot.send_message(target, f"🎉 Администратор {esc(ADMIN_USERNAME)} выдал вам бесплатный доступ навсегда!\n"
                                       f"Меню внизу 👇", reply_markup=menu_kb(target, True))
    except TelegramAPIError:
        await message.answer("(Человеку не удалось отправить уведомление — пусть сам напишет боту /start.)")


async def revoke_access(message: types.Message, target: int):
    if target == ADMIN_ID:
        await message.answer("❌ Нельзя забрать доступ у админа.")
        return
    async with dp["pool"].acquire() as conn:
        await conn.execute("DELETE FROM creators WHERE user_id=$1", target)
    _creator_cache.pop(target, None)
    await message.answer(f"✅ Доступ у <code>{target}</code> отозван.")


async def list_creators(message: types.Message):
    async with dp["pool"].acquire() as conn:
        rows = await conn.fetch("SELECT user_id FROM creators ORDER BY user_id")
    await message.answer("👥 Есть доступ:\n" + "\n".join(f"<code>{r['user_id']}</code>" for r in rows))


@dp.message(Command("give_access"))
async def cmd_give_access(message: types.Message, command: CommandObject):
    if message.from_user.id == ADMIN_ID and command.args and command.args.strip().isdigit():
        await grant_access(message, int(command.args.strip()))


@dp.message(Command("revoke_access"))
async def cmd_revoke_access(message: types.Message, command: CommandObject):
    if message.from_user.id == ADMIN_ID and command.args and command.args.strip().isdigit():
        await revoke_access(message, int(command.args.strip()))


@dp.message(Command("creators"))
async def cmd_creators(message: types.Message):
    if message.from_user.id == ADMIN_ID:
        await list_creators(message)


# ============================== Итоги / реролл ==============================
def winner_line(r, place: int, prize: str, many: bool) -> str:
    head = f"{MEDALS[place - 1]} {place} место — " if many else "🏆 "
    return f"{head}{mention(r['user_id'], r['username'])} (Билет #{r['ticket_number']})\n🎁 {esc(prize)}"


async def run_draw(message: types.Message, user_id: int, gid: int, reroll: bool, place: int = None):
    pool = dp["pool"]
    g = await get_giveaway(pool, gid)
    if not g or g["kind"] != "normal":
        await message.answer("❌ Конкурс не найден.")
        return
    if g["creator_id"] != user_id and user_id != ADMIN_ID:
        await message.answer("❌ Это не ваш конкурс.")
        return
    n = g["winners"] or 1
    many = n > 1
    prizes = g["prize"].split(" | ") if many else [g["prize"]]
    prizes += [prizes[-1]] * (n - len(prizes))  # на случай старых данных

    if not reroll and g["status"] == "finished":
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT user_id, username, ticket_number, place FROM giveaway_participants "
                "WHERE giveaway_id=$1 AND place IS NOT NULL ORDER BY place", gid)
        if rows:
            body = "\n".join(winner_line(r, r["place"], prizes[r["place"] - 1], many) for r in rows)
        else:
            body = "🏆 " + (mention(g["winner_id"], g["winner_username"]) if g["winner_id"] else "—")
        await message.answer(f"ℹ️ Конкурс #{gid} уже завершён.\n{body}\n\nЗамена победителя: кнопка «🔄 Переролл».")
        return
    if reroll and g["status"] != "finished":
        await message.answer(f"ℹ️ Сначала завершите конкурс: кнопка «🏁 Итоги».")
        return
    if reroll and many and place is None:
        kb = InlineKeyboardBuilder()
        for i in range(1, n + 1):
            kb.button(text=f"{MEDALS[i - 1]} Заменить {i} место", callback_data=f"rrp_{gid}_{i}")
        kb.adjust(1)
        await message.answer("Какое место переротить?", reply_markup=kb.as_markup())
        return
    if reroll and not many:
        place = 1
    if reroll and not (1 <= (place or 0) <= n):
        return

    async with pool.acquire() as conn:
        async with conn.transaction():
            picked = await conn.fetch(
                "SELECT user_id, username, ticket_number FROM giveaway_participants "
                "WHERE giveaway_id=$1 AND NOT won ORDER BY random() LIMIT $2", gid, 1 if reroll else n)
            places = [place] if reroll else list(range(1, len(picked) + 1))
            if picked and reroll:
                await conn.execute("UPDATE giveaway_participants SET place=NULL WHERE giveaway_id=$1 AND place=$2",
                                   gid, place)
            for r, pl in zip(picked, places):
                await conn.execute("UPDATE giveaway_participants SET won=TRUE, place=$3 "
                                   "WHERE giveaway_id=$1 AND user_id=$2", gid, r["user_id"], pl)
            if picked:
                await conn.execute("UPDATE giveaways SET status='finished' WHERE id=$1", gid)
                for r, pl in zip(picked, places):
                    if pl == 1:
                        await conn.execute("UPDATE giveaways SET winner_id=$2, winner_username=$3 WHERE id=$1",
                                           gid, r["user_id"], r["username"])
    if not picked:
        await message.answer(f"❌ Нет подходящих участников в конкурсе #{gid}.")
        return

    if reroll:
        title = f"🔄 Новый победитель{f' ({place} место)' if many else ''}"
    else:
        title = "🎉 Победители розыгрыша" if many else "🎉 Победитель розыгрыша"
    body = "\n".join(winner_line(r, pl, prizes[pl - 1], many) for r, pl in zip(picked, places))
    extra = ""
    if not reroll and len(picked) < n:
        extra = f"\n\n⚠️ Участников меньше, чем призов: выбрано {len(picked)} из {n}."
    await message.answer(f"{title} #{gid}:\n{body}{extra}")
    for r, pl in zip(picked, places):
        try:
            await bot.send_message(r["user_id"], f"🏆 Вы победили в конкурсе #{gid}!\nПриз: {esc(prizes[pl - 1])}\n"
                                                  f"Свяжитесь с организатором.")
        except TelegramAPIError:
            pass


@dp.callback_query(F.data.regexp(r"^rrp_\d+_\d+$"))
async def reroll_place_callback(callback: types.CallbackQuery):
    await quick_answer(callback)
    _, gid, place = callback.data.split("_")
    if not await is_creator(dp["pool"], callback.from_user.id):
        return
    await run_draw(callback.message, callback.from_user.id, int(gid), reroll=True, place=int(place))


@dp.message(Command("finish"))
async def cmd_finish(message: types.Message, command: CommandObject):
    if not await is_creator(dp["pool"], message.from_user.id):
        return
    if not command.args or not command.args.strip().isdigit():
        await message.answer("⚠️ Использование: <code>/finish ID</code>")
        return
    await run_draw(message, message.from_user.id, int(command.args.strip()), reroll=False)


@dp.message(Command("reroll"))
async def cmd_reroll(message: types.Message, command: CommandObject):
    if not await is_creator(dp["pool"], message.from_user.id):
        return
    if not command.args or not command.args.strip().isdigit():
        await message.answer("⚠️ Использование: <code>/reroll ID</code>")
        return
    await run_draw(message, message.from_user.id, int(command.args.strip()), reroll=True)


@dp.message(Command("my"))
async def cmd_my(message: types.Message):
    pool = dp["pool"]
    if not await is_creator(pool, message.from_user.id):
        return
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT g.id, g.kind, g.prize, g.status, "
            "(SELECT count(*) FROM giveaway_participants p WHERE p.giveaway_id=g.id) AS cnt "
            "FROM giveaways g WHERE creator_id=$1 ORDER BY id DESC LIMIT 15", message.from_user.id)
    if not rows:
        await message.answer("У вас пока нет конкурсов.")
        return
    lines = []
    for r in rows:
        kind = "⚡️" if r["kind"] == "fast" else "📊"
        st = "🟢" if r["status"] == "active" else "🔴"
        extra = f" · {r['cnt']} уч." if r["kind"] == "normal" else ""
        lines.append(f"{kind}{st} <b>#{r['id']}</b> — {esc(r['prize'])}{extra}")
    await message.answer("\n".join(lines))


# ============================== Создание конкурса ==============================
@dp.message(Command("new_giveaway"))
async def cmd_new_giveaway(message: types.Message, state: FSMContext):
    if not await is_creator(dp["pool"], message.from_user.id):
        await message.answer("❌ У вас нет доступа! Купить доступ или написать админу — /start")
        return
    kb = InlineKeyboardBuilder()
    kb.button(text="📊 Обычный конкурс", callback_data="mode_normal")
    kb.button(text="⚡️ Фаст-Клик", callback_data="mode_fast")
    kb.adjust(1)
    await message.answer("Выберите формат:", reply_markup=kb.as_markup())
    await state.set_state(GiveawayForm.mode)


async def ask_prize_type(message: types.Message, state: FSMContext):
    kb = InlineKeyboardBuilder()
    kb.button(text="🖼 NFT", callback_data="type_nft")
    kb.button(text="🎁 Stars", callback_data="type_regular")
    kb.adjust(1)
    await message.answer("Выберите тип приза:", reply_markup=kb.as_markup())
    await state.set_state(GiveawayForm.prize_type)


def prize_prompt(prize_type: str, place: int, total: int) -> str:
    suffix = f" для {place} места" if total > 1 else ""
    if prize_type == "NFT":
        return (f"Отправьте ссылку на NFT{suffix}.\n"
                f"Можно несколько (до {MAX_NFT_PER_PLACE}) — каждую с новой строки в одном сообщении:")
    return f"Введите количество звёзд цифрами{suffix}:"


@dp.callback_query(GiveawayForm.mode, F.data.in_(["mode_normal", "mode_fast"]))
async def process_mode(callback: types.CallbackQuery, state: FSMContext):
    await quick_answer(callback)
    await state.update_data(mode="fast" if callback.data == "mode_fast" else "normal", prizes=[])
    kb = InlineKeyboardBuilder()
    for i in range(1, MAX_WINNERS + 1):
        kb.button(text=str(i), callback_data=f"winners_{i}")
    kb.adjust(5)
    await callback.message.answer(f"Сколько будет победителей? (от 1 до {MAX_WINNERS})",
                                  reply_markup=kb.as_markup())
    await state.set_state(GiveawayForm.winners)


@dp.callback_query(GiveawayForm.winners, F.data.regexp(r"^winners_\d+$"))
async def process_winners(callback: types.CallbackQuery, state: FSMContext):
    await quick_answer(callback)
    n = int(callback.data.split("_")[1])
    if not 1 <= n <= MAX_WINNERS:
        return
    await state.update_data(winners=n, prizes=[])
    await ask_prize_type(callback.message, state)


@dp.callback_query(GiveawayForm.prize_type, F.data.in_(["type_nft", "type_regular"]))
async def process_prize_type(callback: types.CallbackQuery, state: FSMContext):
    ptype = "NFT" if callback.data == "type_nft" else "Stars"
    await state.update_data(prize_type=ptype)
    data = await state.get_data()
    await callback.message.answer(prize_prompt(ptype, 1, data.get("winners", 1)))
    await state.set_state(GiveawayForm.prize_details)
    await quick_answer(callback)


@dp.message(GiveawayForm.prize_details)
async def process_prize_details(message: types.Message, state: FSMContext):
    data = await state.get_data()
    txt = (message.text or "").strip()
    if data["prize_type"] == "Stars":
        if not txt.isdigit() or int(txt) <= 0:
            await message.answer("⚠️ Введите только цифры:")
            return
        prize = f"{txt} Stars"
    else:
        pat = r"(?:https?://)?tg\.catup\.lol/nft/([A-Za-z0-9_-]+)/?"
        found = re.findall(pat, txt, re.IGNORECASE)
        leftover = re.sub(pat, "", txt, flags=re.IGNORECASE).strip(" ,;\n\t\r")
        if not found or leftover:
            await message.answer("⚠️ Нужны ссылки на NFT вида:\n"
                                 "<code>tg.catup.lol/nft/lolpop-14</code>\n"
                                 f"Можно несколько (до {MAX_NFT_PER_PLACE}), каждую с новой строки.")
            return
        urls = list(dict.fromkeys(f"https://tg.catup.lol/nft/{name}" for name in found))
        if len(urls) > MAX_NFT_PER_PLACE:
            await message.answer(f"⚠️ Максимум {MAX_NFT_PER_PLACE} NFT на одно место. Отправьте не больше:")
            return
        used = " ".join(data.get("prizes", []))
        dup = [u for u in urls if u in used]
        if dup:
            await message.answer(f"⚠️ Эта ссылка уже указана для другого места: {esc(dup[0])}\nОтправьте другие:")
            return
        prize = f"NFT ({urls[0]})" if len(urls) == 1 else f"NFT ×{len(urls)} ({', '.join(urls)})"

    n = data.get("winners", 1)
    prizes = list(data.get("prizes", []))
    prizes.append(prize)
    if len(" | ".join(prizes)) > 2800:
        await message.answer("⚠️ Слишком длинный список призов для одного поста. "
                             "Уменьшите число NFT или победителей и начните заново: «➕ Создать конкурс».")
        await state.clear()
        return
    if len(prizes) < n:
        await state.update_data(prizes=prizes)
        await message.answer(prize_prompt(data["prize_type"], len(prizes) + 1, n))
        return
    full_prize = " | ".join(prizes)

    if data["mode"] == "fast":
        async with dp["pool"].acquire() as conn:
            gid = await conn.fetchval(
                "INSERT INTO giveaways (creator_id, kind, prize, winners) VALUES ($1,'fast',$2,$3) RETURNING id",
                message.from_user.id, full_prize, n)
        me = await get_me_cached()
        link = f"{LINK_BASE}/{me.username}?start=fast_{gid}"
        if n > 1:
            prize_block = f"🎁 Призы (первые {n} {ru_people(n)}):\n" + "\n".join(
                f"{MEDALS[i]} {i + 1} место: {esc(p)}" for i, p in enumerate(prizes))
        else:
            prize_block = f"Приз: {esc(prizes[0])}"
        await message.answer("Готово! Перешлите это сообщение в канал 👇")
        await message.answer(f"⚡️ <b>Фаст-клик розыгрыш #{gid}!</b>\n{prize_block}\n\n"
                             f'👉 <a href="{esc(link)}">Забрать приз</a>')
        await state.clear()
    else:
        await state.update_data(prizes=prizes, prize=full_prize)
        kb = InlineKeyboardBuilder()
        kb.button(text="⏭ Без кодового слова", callback_data="skip_code")
        await message.answer("Введите <b>кодовое слово</b> (необязательно). Спрячьте его в своём канале — "
                             "участники должны найти его и ввести в боте.\n"
                             "Или нажмите кнопку, чтобы участвовать в один клик:",
                             reply_markup=kb.as_markup())
        await state.set_state(GiveawayForm.code_word)


async def ask_conditions(message: types.Message, state: FSMContext, has_code: bool):
    kb = InlineKeyboardBuilder()
    kb.button(text="⏭ Пропустить", callback_data="skip_conditions")
    if has_code:
        text = ("Напишите условия: где искать кодовое слово "
                "(например: «Кодовое слово в закрепе канала @PavelBet») — или нажмите кнопку:")
    else:
        text = ("Напишите условия участия "
                "(например: «Подпишись на @PavelBet и нажми Участвовать») — или нажмите кнопку:")
    await message.answer(text, reply_markup=kb.as_markup())
    await state.set_state(GiveawayForm.conditions)


@dp.callback_query(GiveawayForm.code_word, F.data == "skip_code")
async def skip_code_callback(callback: types.CallbackQuery, state: FSMContext):
    await quick_answer(callback)
    await state.update_data(code=None)
    await ask_conditions(callback.message, state, has_code=False)


@dp.message(GiveawayForm.code_word)
async def process_code_word(message: types.Message, state: FSMContext):
    code = " ".join((message.text or "").split())
    if not (2 <= len(code) <= 64):
        await message.answer("⚠️ Кодовое слово должно быть от 2 до 64 символов (или нажмите «Без кодового слова»):")
        return
    await state.update_data(code=code)
    await ask_conditions(message, state, has_code=True)


@dp.callback_query(GiveawayForm.conditions, F.data == "skip_conditions")
async def skip_conditions_callback(callback: types.CallbackQuery, state: FSMContext):
    await quick_answer(callback)
    await finish_creating_giveaway(callback.message, callback.from_user.id, state, "")


@dp.message(GiveawayForm.conditions)
async def process_conditions(message: types.Message, state: FSMContext):
    await finish_creating_giveaway(message, message.from_user.id, state, (message.text or "").strip())


async def finish_creating_giveaway(message: types.Message, creator_id: int, state: FSMContext, conditions: str):
    data = await state.get_data()
    code = data.get("code")
    conditions = conditions or ("найдите кодовое слово и введите его в боте" if code
                                else "нажмите «Участвовать»")
    async with dp["pool"].acquire() as conn:
        gid = await conn.fetchval(
            "INSERT INTO giveaways (creator_id, kind, prize, conditions, code, winners) "
            "VALUES ($1,'normal',$2,$3,$4,$5) RETURNING id",
            creator_id, data["prize"], conditions, code, data.get("winners", 1))
    me = await get_me_cached()
    link = f"{LINK_BASE}/{me.username}?start=join_{gid}"
    await message.answer("Готово! Перешлите это сообщение в канал 👇")
    prizes = data["prize"].split(" | ")
    if len(prizes) > 1:
        prize_block = "🎁 Призы:\n" + "\n".join(f"{MEDALS[i]} {i + 1} место: {esc(p)}" for i, p in enumerate(prizes))
    else:
        prize_block = f"🎁 Приз: {esc(prizes[0])}"
    await message.answer(f"🎉 <b>РОЗЫГРЫШ #{gid}</b> 🎉\n\n{prize_block}\n"
                         f"📌 Условия: {esc(conditions)}\n\n"
                         f'👉 <a href="{esc(link)}">🎉 Участвовать</a>')
    if code:
        await message.answer(f"🔑 Кодовое слово: <code>{esc(code)}</code>\n"
                             f"⚠️ Не публикуйте его в посте с розыгрышем — спрячьте в канале.\n\n"
                             f"Итоги: кнопка «🏁 Итоги» в меню.")
    else:
        await message.answer("ℹ️ Конкурс без кодового слова: участие в один клик.\n"
                             "Итоги: кнопка «🏁 Итоги» в меню.")
    await state.clear()


# ============================== Ввод кодового слова ==============================
@dp.message(JoinForm.code, F.text, ~F.text.startswith("/"))
async def join_code(message: types.Message, state: FSMContext):
    pool = dp["pool"]
    gid = (await state.get_data()).get("join_gid")
    g = await get_giveaway(pool, gid) if gid else None
    if not g or g["status"] != "active":
        await state.clear()
        await message.answer("🔒 Этот конкурс недоступен или уже завершён.")
        return
    if norm_code(message.text) != norm_code(g["code"]):
        await message.answer("❌ Неверное кодовое слово. Найдите его в канале и попробуйте ещё раз:")
        return
    await state.clear()
    ticket, is_new = await add_participant(pool, gid, message.from_user.id, message.from_user.username)
    if is_new:
        await message.answer(f"🎉 Верно! Вы участвуете. Ваш номер: <b>#{ticket}</b>")
    else:
        await message.answer(f"ℹ️ Вы уже зарегистрированы. Ваш номер: <b>#{ticket}</b>")


# ============================== Устойчивость ==============================
@dp.errors()
async def on_error(event: types.ErrorEvent):
    log.exception("Ошибка в обработчике: %r", event.exception)
    upd = event.update
    msg = upd.message or (upd.callback_query.message if upd.callback_query else None)
    if msg:
        try:
            await msg.answer("⚠️ Временная ошибка, попробуйте ещё раз.")
        except Exception:
            pass
    return True


async def keep_alive():
    """Render (бесплатный) засыпает без входящих запросов — пингуем сами себя."""
    url = os.getenv("RENDER_EXTERNAL_URL")
    if not url:
        return
    async with aiohttp.ClientSession() as sess:
        while True:
            await asyncio.sleep(300)
            try:
                await sess.get(url, timeout=aiohttp.ClientTimeout(total=15))
            except Exception as e:
                log.warning("keep_alive: %s", e)


# ============================== Запуск ==============================
async def main():
    pool = await get_db_pool()
    await init_db(pool)
    dp["pool"] = pool
    await start_dummy_server()
    asyncio.create_task(keep_alive())
    await set_main_menu(bot)
    log.info("Бот запущен!")
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
