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
from aiogram.enums import ChatMemberStatus, ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand
from aiogram.utils.keyboard import InlineKeyboardBuilder

# ================= Настройки (из переменных окружения Render) =================
BOT_TOKEN = os.environ["BOT_TOKEN"]
DATABASE_URL = os.environ["DATABASE_URL"]
ADMIN_ID = int(os.environ["ADMIN_ID"])
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "@Pavelmanager")
PRICE_STARS = int(os.getenv("PRICE_STARS", "600"))
CATIGRAM_API_URL = os.getenv("CATIGRAM_API_URL", "http://bot.catup.lol")
LINK_BASE = os.getenv("LINK_BASE", "https://tg.catup.lol")
PORT = int(os.getenv("PORT", "10000"))  # Render сам задаёт PORT
# ==============================================================================

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("giveaway")

session = AiohttpSession(api=TelegramAPIServer.from_base(CATIGRAM_API_URL))
bot = Bot(
    token=BOT_TOKEN,
    session=session,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)
dp = Dispatcher(storage=MemoryStorage())

esc = html.escape
OK_STATUSES = {ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR}


@dp.update.outer_middleware()
async def log_updates(handler, event, data):
    log.info("update: %s", getattr(event, "event_type", "?"))
    return await handler(event, data)


class GiveawayForm(StatesGroup):
    mode = State()
    prize_type = State()
    prize_details = State()
    channel_username = State()
    conditions = State()


class DonateForm(StatesGroup):
    amount = State()


# ============================== База данных ==============================
def clean_dsn(dsn: str) -> str:
    """asyncpg не понимает channel_binding из строки Neon — убираем."""
    p = urlsplit(dsn)
    q = [(k, v) for k, v in parse_qsl(p.query) if k != "channel_binding"]
    return urlunsplit((p.scheme, p.netloc, p.path, urlencode(q), p.fragment))


async def get_db_pool():
    # statement_cache_size=0 обязателен для pooler-эндпоинта Neon (pgbouncer)
    return await asyncpg.create_pool(
        clean_dsn(DATABASE_URL),
        min_size=1,
        max_size=5,
        statement_cache_size=0,
        max_inactive_connection_lifetime=60,
    )


async def init_db(pool):
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS creators (user_id BIGINT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS giveaways (
                id SERIAL PRIMARY KEY,
                creator_id BIGINT NOT NULL,
                kind TEXT NOT NULL,               -- normal / fast
                prize TEXT NOT NULL,
                conditions TEXT,
                channel_id TEXT,
                channel_username TEXT,
                status TEXT NOT NULL DEFAULT 'active',  -- active / finished
                next_ticket INT NOT NULL DEFAULT 1,
                winner_id BIGINT,
                winner_username TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            );
            CREATE TABLE IF NOT EXISTS giveaway_participants (
                giveaway_id INT NOT NULL REFERENCES giveaways(id) ON DELETE CASCADE,
                user_id BIGINT NOT NULL,
                username TEXT,
                ticket_number INT NOT NULL,
                won BOOLEAN NOT NULL DEFAULT FALSE,
                PRIMARY KEY (giveaway_id, user_id),
                UNIQUE (giveaway_id, ticket_number)
            );
            CREATE TABLE IF NOT EXISTS known_channels (
                chat_id BIGINT PRIMARY KEY,
                title TEXT,
                username TEXT,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            );
            CREATE TABLE IF NOT EXISTS payments (
                charge_id TEXT PRIMARY KEY,
                user_id BIGINT NOT NULL,
                payload TEXT NOT NULL,
                amount INT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            );
        """)
        await conn.execute("ALTER TABLE giveaways ALTER COLUMN channel_id TYPE TEXT USING channel_id::text")
        await conn.execute(
            "INSERT INTO creators (user_id) VALUES ($1) ON CONFLICT DO NOTHING", ADMIN_ID
        )


async def is_creator(pool, user_id) -> bool:
    if user_id == ADMIN_ID:
        return True
    async with pool.acquire() as conn:
        return await conn.fetchval("SELECT 1 FROM creators WHERE user_id=$1", user_id) is not None


async def add_creator(pool, user_id) -> bool:
    """Возвращает True, если доступ уже был."""
    async with pool.acquire() as conn:
        res = await conn.execute(
            "INSERT INTO creators (user_id) VALUES ($1) ON CONFLICT DO NOTHING", user_id
        )
        return res.endswith(" 0")


async def get_giveaway(pool, gid):
    async with pool.acquire() as conn:
        return await conn.fetchrow("SELECT * FROM giveaways WHERE id=$1", gid)


async def add_participant(pool, gid, user_id, username):
    """Атомарно выдаёт следующий номер билета (без гонок)."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.fetchval("SELECT id FROM giveaways WHERE id=$1 FOR UPDATE", gid)
            existing = await conn.fetchval(
                "SELECT ticket_number FROM giveaway_participants WHERE giveaway_id=$1 AND user_id=$2",
                gid, user_id,
            )
            if existing:
                return existing, False
            ticket = await conn.fetchval(
                "UPDATE giveaways SET next_ticket = next_ticket + 1 WHERE id=$1 "
                "RETURNING next_ticket - 1", gid,
            )
            await conn.execute(
                "INSERT INTO giveaway_participants (giveaway_id, user_id, username, ticket_number) "
                "VALUES ($1,$2,$3,$4)", gid, user_id, username, ticket,
            )
            return ticket, True


# ============================== Утилиты ==============================
def mention(user_id, username) -> str:
    if username:
        return f"@{esc(username)}"
    return f'<a href="tg://user?id={user_id}">ID {user_id}</a>'


def parse_arg(args, prefix):
    m = re.fullmatch(rf"{prefix}_(\d+)", args or "")
    return int(m.group(1)) if m else None


def norm_chat(ref):
    return int(ref) if re.fullmatch(r"-?\d+", str(ref)) else ref


async def is_subscribed(channel_id, user_id: int) -> bool:
    """Бросает TelegramAPIError, если проверить нельзя (бот не админ канала и т.п.)."""
    member = await bot.get_chat_member(chat_id=norm_chat(channel_id), user_id=user_id)
    if member.status in OK_STATUSES:
        return True
    if member.status == ChatMemberStatus.RESTRICTED:
        return bool(getattr(member, "is_member", False))
    return False


# ============================== Мини веб-сервер для Render ==============================
async def handle_ping(request):
    return web.Response(text="Bot is alive!")


async def start_dummy_server():
    app = web.Application()
    app.router.add_get("/", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()


async def set_main_menu(bot: Bot):
    await bot.set_my_commands([
        BotCommand(command="start", description="Главное меню"),
        BotCommand(command="new_giveaway", description="Создать новый конкурс"),
        BotCommand(command="my", description="Мои конкурсы"),
        BotCommand(command="finish", description="Итоги: /finish ID"),
        BotCommand(command="reroll", description="Переролл: /reroll ID"),
        BotCommand(command="myid", description="Узнать свой Telegram ID"),
    ])


# ============================== Старт / участие ==============================
@dp.message(Command("myid"))
async def cmd_myid(message: types.Message):
    await message.answer(f"🆔 Твой Telegram ID: <code>{message.from_user.id}</code>")


async def do_join(user: types.User, gid: int, reply):
    pool = dp["pool"]
    g = await get_giveaway(pool, gid)
    if not g or g["kind"] != "normal":
        await reply("⚠️ Розыгрыш не найден.")
        return
    if g["status"] != "active":
        await reply("🔒 Этот конкурс уже завершён.")
        return

    if g["channel_id"]:
        try:
            ok = await is_subscribed(g["channel_id"], user.id)
        except TelegramAPIError as e:
            log.warning("Не могу проверить подписку (giveaway %s): %s", gid, e)
            await reply("⚠️ Не могу проверить подписку. Сообщите организатору: бот должен быть админом канала.")
            return
        if not ok:
            kb = InlineKeyboardBuilder()
            if g["channel_username"].startswith("@"):
                kb.button(text="📢 Открыть канал", url=f"{LINK_BASE}/{g['channel_username'].lstrip('@')}")
            kb.button(text="✅ Я подписался", callback_data=f"check_{gid}")
            kb.adjust(1)
            await reply(f"❌ Сначала подпишитесь на {esc(g['channel_username'])}, затем нажмите кнопку.",
                        reply_markup=kb.as_markup())
            return

    ticket, is_new = await add_participant(pool, gid, user.id, user.username)
    if is_new:
        await reply(f"🎉 Вы успешно участвуете! Ваш номер: <b>#{ticket}</b>")
    else:
        await reply(f"ℹ️ Вы уже зарегистрированы. Ваш номер: <b>#{ticket}</b>")


@dp.callback_query(F.data.startswith("check_"))
async def check_sub_callback(callback: types.CallbackQuery):
    gid = parse_arg(callback.data, "check")
    await callback.answer()
    if gid is not None:
        await do_join(callback.from_user, gid, callback.message.answer)


@dp.message(Command("start"))
async def cmd_start(message: types.Message, command: CommandObject, state: FSMContext):
    await state.clear()
    pool = dp["pool"]
    user = message.from_user
    args = command.args

    gid = parse_arg(args, "fast")
    if gid is not None:
        async with pool.acquire() as conn:
            g = await conn.fetchrow("SELECT creator_id, prize FROM giveaways WHERE id=$1 AND kind='fast'", gid)
            if not g:
                await message.answer("⚠️ Розыгрыш не найден.")
                return
            if g["creator_id"] == user.id:
                await message.answer("🙅 Создатель не может участвовать в своём фаст-клике.")
                return
            # Атомарно: только один пользователь сможет выставить winner_id
            won = await conn.fetchrow(
                "UPDATE giveaways SET winner_id=$2, winner_username=$3, status='finished' "
                "WHERE id=$1 AND winner_id IS NULL RETURNING prize, creator_id",
                gid, user.id, user.username,
            )
        if won:
            await message.answer(f"⚡️ <b>ПОЗДРАВЛЯЕМ! ТЫ БЫЛ ПЕРВЫМ!</b>\n\nПриз: <b>{esc(won['prize'])}</b>")
            try:
                await bot.send_message(
                    won["creator_id"],
                    f"⚡️ Фаст-клик #{gid} выиграл {mention(user.id, user.username)} (<code>{user.id}</code>)",
                )
            except TelegramAPIError:
                pass
        else:
            await message.answer("❌ К сожалению, этот приз уже забрали!")
        return

    gid = parse_arg(args, "join")
    if gid is not None:
        await do_join(user, gid, message.answer)
        return

    if await is_creator(pool, user.id):
        kb = InlineKeyboardBuilder()
        kb.button(text="💖 Поддержать автора", callback_data="donate_stars")
        await message.answer("👋 Привет, Создатель!\nИспользуй /new_giveaway для создания конкурса.",
                             reply_markup=kb.as_markup())
    else:
        kb = InlineKeyboardBuilder()
        kb.button(text=f"⭐️ Купить доступ — {PRICE_STARS}", callback_data="buy_access")
        kb.button(text="💖 Пожертвовать звёзды", callback_data="donate_stars")
        kb.button(text=f"💬 Написать {ADMIN_USERNAME}", url=f"https://t.me/{ADMIN_USERNAME.lstrip('@')}")
        kb.adjust(1)
        await message.answer("🔒 Доступ закрыт. Оплатите звёзды или напишите админу.", reply_markup=kb.as_markup())


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
        prices=[types.LabeledPrice(label="Пожертвование", amount=amount)], provider_token="",
    )
    await state.clear()


@dp.callback_query(F.data == "buy_access")
async def buy_access_callback(callback: types.CallbackQuery):
    await bot.send_invoice(
        chat_id=callback.from_user.id, title="Доступ к боту", description="Покупка доступа",
        payload=f"access_{callback.from_user.id}", currency="XTR",
        prices=[types.LabeledPrice(label="Доступ", amount=PRICE_STARS)], provider_token="",
    )
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
            "INSERT INTO payments (charge_id, user_id, payload, amount) VALUES ($1,$2,$3,$4) "
            "ON CONFLICT DO NOTHING",
            sp.telegram_payment_charge_id, message.from_user.id, sp.invoice_payload, sp.total_amount,
        )
    if inserted.endswith(" 0"):  # повторная доставка того же платежа
        return
    if sp.invoice_payload.startswith("donate_"):
        await message.answer("💖 Спасибо за поддержку!")
    else:
        await add_creator(pool, message.from_user.id)
        await message.answer("🎉 Оплата успешна! Доступ открыт. Используйте /new_giveaway")


@dp.message(Command("give_access"))
async def cmd_give_access(message: types.Message, command: CommandObject):
    if message.from_user.id != ADMIN_ID:
        return
    if not command.args or not command.args.strip().isdigit():
        await message.answer("Использование: <code>/give_access ID</code>")
        return
    target = int(command.args.strip())
    if await add_creator(dp["pool"], target):
        await message.answer(f"ℹ️ У пользователя <code>{target}</code> уже был доступ.")
        return
    await message.answer(f"✅ Доступ выдан пользователю <code>{target}</code>!")
    try:
        await bot.send_message(target, f"🎉 Администратор {esc(ADMIN_USERNAME)} выдал вам бесплатный доступ! "
                                       f"Используйте /new_giveaway")
    except TelegramAPIError:
        pass


@dp.message(Command("revoke_access"))
async def cmd_revoke_access(message: types.Message, command: CommandObject):
    if message.from_user.id != ADMIN_ID:
        return
    if not command.args or not command.args.strip().isdigit() or int(command.args) == ADMIN_ID:
        await message.answer("Использование: <code>/revoke_access ID</code>")
        return
    async with dp["pool"].acquire() as conn:
        await conn.execute("DELETE FROM creators WHERE user_id=$1", int(command.args))
    await message.answer("✅ Доступ отозван.")


# ============================== Итоги / реролл ==============================
async def run_draw(message: types.Message, gid: int, reroll: bool):
    pool = dp["pool"]
    g = await get_giveaway(pool, gid)
    if not g or g["kind"] != "normal":
        await message.answer("❌ Конкурс не найден.")
        return
    if g["creator_id"] != message.from_user.id and message.from_user.id != ADMIN_ID:
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
        rows = await conn.fetch(
            "SELECT user_id, username, ticket_number FROM giveaway_participants "
            "WHERE giveaway_id=$1 AND NOT won ORDER BY random()", gid)

    winner = None
    for r in rows:
        if g["channel_id"]:
            try:
                if not await is_subscribed(g["channel_id"], r["user_id"]):
                    continue  # отписался — пропускаем
            except TelegramAPIError as e:
                log.warning("Проверка подписки при розыгрыше #%s: %s", gid, e)
                await message.answer("⚠️ Не могу проверить подписки. Бот должен быть админом канала.")
                return
        winner = r
        break

    if not winner:
        await message.answer(f"❌ Нет подходящих участников в конкурсе #{gid}.")
        return

    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("UPDATE giveaway_participants SET won=TRUE WHERE giveaway_id=$1 AND user_id=$2",
                               gid, winner["user_id"])
            await conn.execute("UPDATE giveaways SET status='finished', winner_id=$2, winner_username=$3 "
                               "WHERE id=$1", gid, winner["user_id"], winner["username"])

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
    await run_draw(message, int(command.args.strip()), reroll=False)


@dp.message(Command("reroll"))
async def cmd_reroll(message: types.Message, command: CommandObject):
    if not await is_creator(dp["pool"], message.from_user.id):
        return
    if not command.args or not command.args.strip().isdigit():
        await message.answer("⚠️ Использование: <code>/reroll ID</code>")
        return
    await run_draw(message, int(command.args.strip()), reroll=True)


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
        await message.answer("❌ У вас нет доступа!")
        return
    kb = InlineKeyboardBuilder()
    kb.button(text="📊 Обычный конкурс", callback_data="mode_normal")
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
        if not re.fullmatch(r"(https?://)?[A-Za-z0-9.-]+\.[A-Za-z]{2,}(/\S*)?", txt):
            await message.answer("⚠️ Это не ссылка. Отправьте ссылку на NFT, например:\n"
                                 "<code>https://t.me/nft/PlushPepe-1</code>")
            return
        if not txt.startswith("http"):
            txt = "https://" + txt
        prize = f"NFT ({txt})"

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
        await show_channels(message, message.from_user.id)
        await state.set_state(GiveawayForm.channel_username)


async def save_channel(chat: types.Chat):
    async with dp["pool"].acquire() as conn:
        await conn.execute(
            "INSERT INTO known_channels (chat_id, title, username) VALUES ($1,$2,$3) "
            "ON CONFLICT (chat_id) DO UPDATE SET title=$2, username=$3, updated_at=now()",
            chat.id, chat.title, chat.username)


@dp.channel_post()
async def on_channel_post(message: types.Message):
    await save_channel(message.chat)


@dp.my_chat_member()
async def on_my_chat_member(update: types.ChatMemberUpdated):
    if update.chat.type != "channel":
        return
    if update.new_chat_member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR):
        await save_channel(update.chat)
    else:
        async with dp["pool"].acquire() as conn:
            await conn.execute("DELETE FROM known_channels WHERE chat_id=$1", update.chat.id)


async def user_can_use_channel(user_id: int, chat_id: int) -> bool:
    if user_id == ADMIN_ID:
        return True
    try:
        m = await bot.get_chat_member(chat_id=chat_id, user_id=user_id)
        return m.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR)
    except TelegramAPIError:
        return False


async def show_channels(target: types.Message, user_id: int):
    async with dp["pool"].acquire() as conn:
        rows = await conn.fetch("SELECT chat_id, title, username FROM known_channels "
                                "ORDER BY updated_at DESC LIMIT 30")
    kb = InlineKeyboardBuilder()
    shown = 0
    for r in rows:
        if not await user_can_use_channel(user_id, r["chat_id"]):
            continue
        label = f"@{r['username']}" if r["username"] else (r["title"] or str(r["chat_id"]))
        kb.button(text=f"📢 {label}", callback_data=f"pickch_{r['chat_id']}")
        shown += 1
    kb.button(text="🔄 Обновить список", callback_data="refresh_channels")
    kb.adjust(1)
    if shown:
        text = "Выберите канал кнопкой или отправьте <code>@юзернейм</code> / числовой ID канала:"
    else:
        text = ("Отправьте <code>@юзернейм</code> канала (например, <code>@PavelBet</code>) "
                "или его числовой ID.\n\n"
                "⚠️ Бот должен быть <b>администратором</b> этого канала.")
    await target.answer(text, reply_markup=kb.as_markup())


@dp.callback_query(GiveawayForm.channel_username, F.data == "refresh_channels")
async def refresh_channels(callback: types.CallbackQuery):
    await callback.answer()
    await show_channels(callback.message, callback.from_user.id)


async def accept_channel(target: types.Message, state: FSMContext, user_id: int, chat_ref, label: str) -> bool:
    """Проверяет, что бот (и создатель) админы канала, и переходит к условиям."""
    try:
        me = await bot.get_me()
        bm = await bot.get_chat_member(chat_id=chat_ref, user_id=me.id)
        if bm.status not in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR):
            raise ValueError("бот не админ канала")
        if user_id != ADMIN_ID:
            um = await bot.get_chat_member(chat_id=chat_ref, user_id=user_id)
            if um.status not in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR):
                raise ValueError("вы не админ этого канала")
    except (TelegramAPIError, ValueError) as e:
        await target.answer(f"❌ Не получилось: <code>{esc(str(e))}</code>\n"
                            "Проверьте, что бот добавлен админом канала.")
        return False
    await state.update_data(channel_id=str(chat_ref), channel_username=label)
    kb = InlineKeyboardBuilder()
    kb.button(text="⏭ Пропустить (только подписка)", callback_data="skip_conditions")
    await target.answer(f"✅ Канал: <b>{esc(label)}</b>\n\nУкажите дополнительные условия (или нажмите кнопку):",
                        reply_markup=kb.as_markup())
    await state.set_state(GiveawayForm.conditions)
    return True


@dp.callback_query(GiveawayForm.channel_username, F.data.regexp(r"^pickch_-?\d+$"))
async def pick_channel(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    chat_id = int(callback.data.split("_", 1)[1])
    async with dp["pool"].acquire() as conn:
        row = await conn.fetchrow("SELECT title, username FROM known_channels WHERE chat_id=$1", chat_id)
    if not row:
        await callback.message.answer("⚠️ Канал не найден, обновите список.")
        return
    label = f"@{row['username']}" if row["username"] else (row["title"] or str(chat_id))
    await accept_channel(callback.message, state, callback.from_user.id, chat_id, label)


@dp.message(GiveawayForm.channel_username)
async def channel_step_text(message: types.Message, state: FSMContext):
    txt = (message.text or "").strip()
    chat_ref, label = None, None
    if re.fullmatch(r"-?\d{6,}", txt):
        chat_ref, label = int(txt), txt
    elif re.fullmatch(r"@?[A-Za-z][A-Za-z0-9_]{3,31}", txt):
        chat_ref = label = "@" + txt.lstrip("@")
    else:
        await message.answer("⚠️ Отправьте <code>@юзернейм</code> канала или его числовой ID.")
        await show_channels(message, message.from_user.id)
        return
    try:  # красивое название и числовой ID, если сервер это умеет
        chat = await bot.get_chat(chat_id=chat_ref)
        chat_ref = chat.id
        label = f"@{chat.username}" if chat.username else (chat.title or label)
    except TelegramAPIError as e:
        log.info("get_chat(%s) не сработал: %s", chat_ref, e)
    await accept_channel(message, state, message.from_user.id, chat_ref, label)


@dp.callback_query(GiveawayForm.conditions, F.data == "skip_conditions")
async def skip_conditions_callback(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    await finish_creating_giveaway(callback.message, callback.from_user.id, state, "")


@dp.message(GiveawayForm.conditions)
async def process_conditions(message: types.Message, state: FSMContext):
    await finish_creating_giveaway(message, message.from_user.id, state, (message.text or "").strip())


async def finish_creating_giveaway(message: types.Message, creator_id: int, state: FSMContext, extra: str):
    data = await state.get_data()
    conditions = f"Подписка на {data['channel_username']}" + (f"\n{extra}" if extra else "")
    async with dp["pool"].acquire() as conn:
        gid = await conn.fetchval(
            "INSERT INTO giveaways (creator_id, kind, prize, conditions, channel_id, channel_username) "
            "VALUES ($1,'normal',$2,$3,$4,$5) RETURNING id",
            creator_id, data["prize"], conditions, data["channel_id"], data["channel_username"])
    me = await bot.get_me()
    link = f"{LINK_BASE}/{me.username}?start=join_{gid}"
    await message.answer("Готово! Перешлите это сообщение в канал 👇")
    await message.answer(f"🎉 <b>РОЗЫГРЫШ #{gid}</b> 🎉\n\n🎁 Приз: {esc(data['prize'])}\n"
                         f"📌 Условия: {esc(conditions)}\n\n"
                         f'👉 <a href="{esc(link)}">🎉 Участвовать</a>')
    await message.answer(f"Итоги: <code>/finish {gid}</code>")
    await state.clear()


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
