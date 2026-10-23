import asyncio
import html
import logging
import os
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

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
PRICE_STARS = int(os.getenv("PRICE_STARS", "600"))
CATIGRAM_API_URL = os.getenv("CATIGRAM_API_URL", "http://bot.catup.lol")
LINK_BASE = os.getenv("LINK_BASE", "https://tg.catup.lol")
PORT = int(os.getenv("PORT", "10000"))
# ==============================================================================

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("giveaway")

session = AiohttpSession(api=TelegramAPIServer.from_base(CATIGRAM_API_URL))
bot = Bot(token=BOT_TOKEN, session=session,
          default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())
esc = html.escape


class GiveawayForm(StatesGroup):
    mode = State()
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
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS payments (
                charge_id TEXT PRIMARY KEY,
                user_id BIGINT NOT NULL,
                payload TEXT NOT NULL,
                amount INT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )""")
        await conn.execute("INSERT INTO creators (user_id) VALUES ($1) ON CONFLICT DO NOTHING", ADMIN_ID)


async def is_creator(pool, user_id) -> bool:
    if user_id == ADMIN_ID:
        return True
    async with pool.acquire() as conn:
        return await conn.fetchval("SELECT 1 FROM creators WHERE user_id=$1", user_id) is not None


async def add_creator(pool, user_id) -> bool:
    """True, если доступ уже был."""
    async with pool.acquire() as conn:
        res = await conn.execute("INSERT INTO creators (user_id) VALUES ($1) ON CONFLICT DO NOTHING", user_id)
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


BTN_NEW = "➕ Создать конкурс"
BTN_MY = "📋 Мои конкурсы"
BTN_FINISH = "🏁 Итоги"
BTN_REROLL = "🔄 Переролл"
BTN_ID = "🆔 Мой ID"
BTN_DONATE = "💖 Донат"
BTN_BUY = f"⭐️ Купить доступ ({PRICE_STARS})"
BTN_ADMIN = "💬 Написать админу"
BTN_GIVE = "🔑 Выдать доступ"
BTN_REVOKE = "🚫 Забрать доступ"
BTN_LIST = "👥 Доступы"


def menu_kb(user_id: int, creator: bool) -> ReplyKeyboardMarkup:
    if creator:
        rows = [[BTN_NEW, BTN_MY], [BTN_FINISH, BTN_REROLL], [BTN_ID, BTN_DONATE]]
        if user_id == ADMIN_ID:
            rows += [[BTN_GIVE, BTN_REVOKE], [BTN_LIST]]
    else:
        rows = [[BTN_BUY, BTN_DONATE], [BTN_ID, BTN_ADMIN]]
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


async def begin_join(message: types.Message, state: FSMContext, gid: int):
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
            gid, message.from_user.id)
    if ticket:
        await message.answer(f"ℹ️ Вы уже зарегистрированы. Ваш номер: <b>#{ticket}</b>")
        return
    if not g["code"]:  # старые конкурсы без кодового слова
        t, _ = await add_participant(pool, gid, message.from_user.id, message.from_user.username)
        await message.answer(f"🎉 Вы успешно участвуете! Ваш номер: <b>#{t}</b>")
        return
    await state.set_state(JoinForm.code)
    await state.update_data(join_gid=gid)
    await message.answer(f"🔑 Конкурс <b>#{gid}</b>\nВведите кодовое слово из поста/канала:")


@dp.message(Command("start"))
async def cmd_start(message: types.Message, command: CommandObject, state: FSMContext):
    await state.clear()
    pool = dp["pool"]
    user = message.from_user
    args = command.args

    gid = parse_arg(args, "fast")
    if gid is not None:
        async with pool.acquire() as conn:
            g = await conn.fetchrow("SELECT creator_id FROM giveaways WHERE id=$1 AND kind='fast'", gid)
            if not g:
                await message.answer("⚠️ Розыгрыш не найден.")
                return
            if g["creator_id"] == user.id:
                await message.answer("🙅 Создатель не может участвовать в своём фаст-клике.")
                return
            won = await conn.fetchrow(
                "UPDATE giveaways SET winner_id=$2, winner_username=$3, status='finished' "
                "WHERE id=$1 AND winner_id IS NULL RETURNING prize, creator_id",
                gid, user.id, user.username)
        if won:
            await message.answer(f"⚡️ <b>ПОЗДРАВЛЯЕМ! ТЫ БЫЛ ПЕРВЫМ!</b>\n\nПриз: <b>{esc(won['prize'])}</b>")
            try:
                await bot.send_message(
                    won["creator_id"],
                    f"⚡️ Фаст-клик #{gid} выиграл {mention(user.id, user.username)} (<code>{user.id}</code>)")
            except TelegramAPIError:
                pass
        else:
            await message.answer("❌ К сожалению, этот приз уже забрали!")
        return

    gid = parse_arg(args, "join")
    if gid is not None:
        await begin_join(message, state, gid)
        return

    creator = await is_creator(pool, user.id)
    if creator:
        text = "👋 Привет, Создатель!\nВыбирай действие в меню внизу 👇"
    else:
        text = ("🔒 Создавать конкурсы могут только пользователи с доступом.\n"
                "Участвовать в конкурсах можно бесплатно — по ссылке из поста.\n\nМеню внизу 👇")
    await message.answer(text, reply_markup=menu_kb(user.id, creator))


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
    await callback.answer()
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
    await callback.answer()


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
    await callback.answer()


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
async def run_draw(message: types.Message, user_id: int, gid: int, reroll: bool):
    pool = dp["pool"]
    g = await get_giveaway(pool, gid)
    if not g or g["kind"] != "normal":
        await message.answer("❌ Конкурс не найден.")
        return
    if g["creator_id"] != user_id and user_id != ADMIN_ID:
        await message.answer("❌ Это не ваш конкурс.")
        return
    if not reroll and g["status"] == "finished":
        w = mention(g["winner_id"], g["winner_username"]) if g["winner_id"] else "—"
        await message.answer(f"ℹ️ Конкурс #{gid} уже завершён. Победитель: 🏆 {w}\n"
                             f"Для нового победителя: <code>/reroll {gid}</code>")
        return
    if reroll and g["status"] != "finished":
        await message.answer(f"ℹ️ Сначала завершите конкурс: <code>/finish {gid}</code>")
        return

    async with pool.acquire() as conn:
        async with conn.transaction():
            winner = await conn.fetchrow(
                "SELECT user_id, username, ticket_number FROM giveaway_participants "
                "WHERE giveaway_id=$1 AND NOT won ORDER BY random() LIMIT 1", gid)
            if winner:
                await conn.execute("UPDATE giveaway_participants SET won=TRUE WHERE giveaway_id=$1 AND user_id=$2",
                                   gid, winner["user_id"])
                await conn.execute("UPDATE giveaways SET status='finished', winner_id=$2, winner_username=$3 "
                                   "WHERE id=$1", gid, winner["user_id"], winner["username"])
    if not winner:
        await message.answer(f"❌ Нет подходящих участников в конкурсе #{gid}.")
        return

    title = "🔄 Новый победитель" if reroll else "🎉 Победитель розыгрыша"
    await message.answer(f"{title} #{gid}:\n🏆 {mention(winner['user_id'], winner['username'])} "
                         f"(Билет #{winner['ticket_number']})\n🎁 Приз: {esc(g['prize'])}")
    try:
        await bot.send_message(winner["user_id"], f"🏆 Вы победили в конкурсе #{gid}!\nПриз: {esc(g['prize'])}\n"
                                                  f"Свяжитесь с организатором.")
    except TelegramAPIError:
        pass


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
    kb.button(text="📊 Обычный конкурс (кодовое слово)", callback_data="mode_normal")
    kb.button(text="⚡️ Фаст-Клик", callback_data="mode_fast")
    kb.adjust(1)
    await message.answer("Выберите формат:", reply_markup=kb.as_markup())
    await state.set_state(GiveawayForm.mode)


@dp.callback_query(GiveawayForm.mode, F.data.in_(["mode_normal", "mode_fast"]))
async def process_mode(callback: types.CallbackQuery, state: FSMContext):
    await state.update_data(mode="fast" if callback.data == "mode_fast" else "normal")
    kb = InlineKeyboardBuilder()
    kb.button(text="🖼 NFT", callback_data="type_nft")
    kb.button(text="🎁 Stars", callback_data="type_regular")
    kb.adjust(1)
    await callback.message.answer("Выберите тип приза:", reply_markup=kb.as_markup())
    await state.set_state(GiveawayForm.prize_type)
    await callback.answer()


@dp.callback_query(GiveawayForm.prize_type, F.data.in_(["type_nft", "type_regular"]))
async def process_prize_type(callback: types.CallbackQuery, state: FSMContext):
    is_nft = callback.data == "type_nft"
    await state.update_data(prize_type="NFT" if is_nft else "Stars")
    await callback.message.answer("Отправьте ссылку на NFT:" if is_nft else "Введите количество звёзд цифрами:")
    await state.set_state(GiveawayForm.prize_details)
    await callback.answer()


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
        m = re.fullmatch(r"(?:https?://)?tg\.catup\.lol/nft/([A-Za-z0-9_-]+)/?", txt, re.IGNORECASE)
        if not m:
            await message.answer("⚠️ Нужна ссылка на NFT вида:\n"
                                 "<code>tg.catup.lol/nft/lolpop-14</code>")
            return
        prize = f"NFT (https://tg.catup.lol/nft/{m.group(1)})"

    if data["mode"] == "fast":
        async with dp["pool"].acquire() as conn:
            gid = await conn.fetchval(
                "INSERT INTO giveaways (creator_id, kind, prize) VALUES ($1,'fast',$2) RETURNING id",
                message.from_user.id, prize)
        me = await bot.get_me()
        link = f"{LINK_BASE}/{me.username}?start=fast_{gid}"
        await message.answer("Готово! Перешлите это сообщение в канал 👇")
        await message.answer(f"⚡️ <b>Фаст-клик розыгрыш #{gid}!</b>\nПриз: {esc(prize)}\n\n"
                             f'👉 <a href="{esc(link)}">Забрать приз</a>')
        await state.clear()
    else:
        await state.update_data(prize=prize)
        await message.answer("Введите <b>кодовое слово</b>. Спрячьте его в своём канале (пост, закреп, описание) — "
                             "участники должны его найти и ввести в боте:")
        await state.set_state(GiveawayForm.code_word)


@dp.message(GiveawayForm.code_word)
async def process_code_word(message: types.Message, state: FSMContext):
    code = " ".join((message.text or "").split())
    if not (2 <= len(code) <= 64):
        await message.answer("⚠️ Кодовое слово должно быть от 2 до 64 символов:")
        return
    await state.update_data(code=code)
    kb = InlineKeyboardBuilder()
    kb.button(text="⏭ Пропустить", callback_data="skip_conditions")
    await message.answer("Напишите условия: где искать кодовое слово "
                         "(например: «Кодовое слово в закрепе канала @PavelBet») — или нажмите кнопку:",
                         reply_markup=kb.as_markup())
    await state.set_state(GiveawayForm.conditions)


@dp.callback_query(GiveawayForm.conditions, F.data == "skip_conditions")
async def skip_conditions_callback(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    await finish_creating_giveaway(callback.message, callback.from_user.id, state, "")


@dp.message(GiveawayForm.conditions)
async def process_conditions(message: types.Message, state: FSMContext):
    await finish_creating_giveaway(message, message.from_user.id, state, (message.text or "").strip())


async def finish_creating_giveaway(message: types.Message, creator_id: int, state: FSMContext, conditions: str):
    data = await state.get_data()
    conditions = conditions or "найдите кодовое слово и введите его в боте"
    async with dp["pool"].acquire() as conn:
        gid = await conn.fetchval(
            "INSERT INTO giveaways (creator_id, kind, prize, conditions, code) "
            "VALUES ($1,'normal',$2,$3,$4) RETURNING id",
            creator_id, data["prize"], conditions, data["code"])
    me = await bot.get_me()
    link = f"{LINK_BASE}/{me.username}?start=join_{gid}"
    await message.answer("Готово! Перешлите это сообщение в канал 👇")
    await message.answer(f"🎉 <b>РОЗЫГРЫШ #{gid}</b> 🎉\n\n🎁 Приз: {esc(data['prize'])}\n"
                         f"📌 Условия: {esc(conditions)}\n\n"
                         f'👉 <a href="{esc(link)}">🎉 Участвовать</a>')
    await message.answer(f"🔑 Кодовое слово: <code>{esc(data['code'])}</code>\n"
                         f"⚠️ Не публикуйте его в посте с розыгрышем — спрячьте в канале.\n\n"
                         f"Итоги: <code>/finish {gid}</code>")
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


# ============================== Запуск ==============================
async def main():
    pool = await get_db_pool()
    await init_db(pool)
    dp["pool"] = pool
    await start_dummy_server()
    await set_main_menu(bot)
    log.info("Бот запущен!")
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
