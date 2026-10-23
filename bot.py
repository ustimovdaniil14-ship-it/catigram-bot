import asyncio
import random
import asyncpg
from aiogram import Bot, Dispatcher, F, types
from aiogram.client.telegram import TelegramAPIServer
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.filters import Command, CommandObject
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand

# ================= Настройки =================
BOT_TOKEN = "1780243771:Gb_eq0_l0bqdqxSG5aU1zTnkyVSUjHnETPp"
ADMIN_ID = 1780243771
ADMIN_USERNAME = "@Pavelmanager"

PRICE_STARS = 600  # Цена доступа

# Подключение к Neon PostgreSQL
DATABASE_URL = "postgresql://neondb_owner:npg_pWVDXMBAHT90@ep-jolly-heart-b5c4ag91-pooler.c-7.us-east-2.aws.neon.tech/neondb?sslmode=require&channel_binding=require"

CATIGRAM_API_URL = "http://bot.catup.lol"
# =============================================

# Настройка API Catigram
catigram_server = TelegramAPIServer.from_base(CATIGRAM_API_URL)
session = AiohttpSession(api=catigram_server)
bot = Bot(token=BOT_TOKEN, session=session)
dp = Dispatcher(storage=MemoryStorage())

# --- Состояния конструктора розыгрышей ---
class GiveawayForm(StatesGroup):
    mode = State()          # Обычный или Fast Click
    prize_type = State()    # NFT или Обычный
    prize_details = State() # Детали приза
    conditions = State()    # Условия

# --- База данных (PostgreSQL / Neon) ---
async def get_db_pool():
    return await asyncpg.create_pool(DATABASE_URL)

async def init_db(pool):
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS creators (
                user_id BIGINT PRIMARY KEY
            );
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS participants (
                giveaway_id INT,
                user_id BIGINT,
                username TEXT,
                PRIMARY KEY (giveaway_id, user_id)
            );
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS fast_clicks (
                giveaway_id INT PRIMARY KEY,
                winner_id BIGINT,
                winner_username TEXT,
                prize_details TEXT
            );
        """)
        # Навсегда заносим главного админа
        await conn.execute("INSERT INTO creators (user_id) VALUES ($1) ON CONFLICT DO NOTHING;", ADMIN_ID)

async def is_creator(pool, user_id):
    if user_id == ADMIN_ID:
        return True
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT user_id FROM creators WHERE user_id = $1", user_id)
        return row is not None

async def add_creator(pool, user_id):
    async with pool.acquire() as conn:
        await conn.execute("INSERT INTO creators (user_id) VALUES ($1) ON CONFLICT DO NOTHING;", user_id)

async def add_participant(pool, giveaway_id, user_id, username):
    async with pool.acquire() as conn:
        try:
            await conn.execute(
                "INSERT INTO participants (giveaway_id, user_id, username) VALUES ($1, $2, $3);",
                giveaway_id, user_id, username or "Без username"
            )
            return True
        except asyncpg.UniqueViolationError:
            return False

async def create_fast_click(pool, giveaway_id, prize_details):
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO fast_clicks (giveaway_id, prize_details) VALUES ($1, $2);",
            giveaway_id, prize_details
        )

async def claim_fast_click(pool, giveaway_id, user_id, username):
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT winner_id, prize_details FROM fast_clicks WHERE giveaway_id = $1", giveaway_id)
        if not row:
            return "not_found", None
        
        winner_id, prize_details = row["winner_id"], row["prize_details"]
        if winner_id is not None:
            return "already_claimed", prize_details
        
        await conn.execute(
            "UPDATE fast_clicks SET winner_id = $1, winner_username = $2 WHERE giveaway_id = $3",
            user_id, username or "Без username", giveaway_id
        )
        return "win", prize_details

# --- Кнопка "Меню" ---
async def set_main_menu(bot: Bot):
    commands = [
        BotCommand(command="start", description="Главное меню / Перезапуск"),
        BotCommand(command="new_giveaway", description="Создать новый конкурс"),
        BotCommand(command="myid", description="Узнать свой Telegram ID"),
    ]
    await bot.set_my_commands(commands)

# --- Хэндлеры ---

@dp.message(Command("myid"))
async def cmd_myid(message: types.Message):
    await message.answer(f"🆔 Твой Telegram ID: `{message.from_user.id}`", parse_mode="Markdown")

@dp.message(Command("start"))
async def cmd_start(message: types.Message, command: CommandObject, state: FSMContext):
    await state.clear()
    pool = dp["pool"]
    user_id = message.from_user.id
    args = command.args

    # Фаст-клик
    if args and args.startswith("fast_"):
        giveaway_id = int(args.split("_")[1])
        status, prize = await claim_fast_click(pool, giveaway_id, user_id, message.from_user.username)
        
        if status == "win":
            await message.answer(
                f"⚡️ **ПОЗДРАВЛЯЕМ! ТЫ БЫЛ ПЕРВЫМ!** ⚡️\n\n"
                f"🎉 Ты успешно забрал приз: **{prize}**!\n"
                f"Напиши администратору {ADMIN_USERNAME} для получения награды."
            )
        elif status == "already_claimed":
            await message.answer("❌ **К сожалению, ты не успел!** Этот приз уже забрал другой участник.")
        else:
            await message.answer("⚠️ Розыгрыш не найден или завершён.")
        return

    # Обычный конкурс
    if args and args.startswith("join_"):
        giveaway_id = int(args.split("_")[1])
        success = await add_participant(pool, giveaway_id, user_id, message.from_user.username)
        
        if success:
            await message.answer(
                "🎉 **Ты участвуешь в розыгрыше!**\n"
                "Твой номер участника сохранён ботом."
            )
        else:
            await message.answer("⚠️ Вы уже зарегистрированы в этом розыгрыше!")
        return

    if await is_creator(pool, user_id):
        await message.answer(
            "👋 **Привет, Создатель!**\n\n"
            "У тебя есть доступ к созданию розыгрышей.\n"
            "Чтобы создать конкурс, используй команду:\n"
            "👉 /new_giveaway"
        )
    else:
        builder = InlineKeyboardBuilder()
        builder.button(text=f"⭐️ Купить доступ — {PRICE_STARS} ⭐️", callback_data="buy_access")
        builder.button(text=f"💬 Написать {ADMIN_USERNAME}", url=f"https://t.me/{ADMIN_USERNAME.replace('@', '')}")
        builder.adjust(1)

        await message.answer(
            "🔒 **Доступ закрыт**\n\n"
            f"Чтобы получить доступ к созданию и публикации розыгрышей, напиши администратору {ADMIN_USERNAME} в личные сообщения или оплатите ⭐️ {PRICE_STARS} кнопкой ниже.\n\n"
            "После оплаты доступ откроется автоматически!",
            reply_markup=builder.as_markup()
        )

# Покупка доступа
@dp.callback_query(F.data == "buy_access")
async def buy_access_callback(callback: types.CallbackQuery):
    prices = [types.LabeledPrice(label="Доступ к созданию розыгрышей", amount=PRICE_STARS)]
    await bot.send_invoice(
        chat_id=callback.from_user.id,
        title="Доступ к боту",
        description="Покупка доступа к созданию и публикации розыгрышей.",
        payload=f"access_{callback.from_user.id}",
        currency="XTR",
        prices=prices,
        provider_token=""
    )
    await callback.answer()

@dp.pre_checkout_query()
async def pre_checkout_handler(pre_checkout_query: types.PreCheckoutQuery):
    await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)

@dp.message(F.successful_payment)
async def payment_success(message: types.Message):
    pool = dp["pool"]
    await add_creator(pool, message.from_user.id)
    await message.answer("🎉 **Оплата прошла успешно!** Теперь тебе доступно создание розыгрышей.")

# Ручная выдача доступа
@dp.message(Command("give_access"))
async def cmd_give_access(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    pool = dp["pool"]
    args = message.text.split()
    if len(args) > 1 and args[1].isdigit():
        target_id = int(args[1])
        await add_creator(pool, target_id)
        await message.answer(f"✅ Доступ успешно выдан пользователю `{target_id}`!")
        try:
            await bot.send_message(target_id, "🎉 Администратор выдал вам доступ к созданию розыгрышей!")
        except Exception:
            pass

# --- Конструктор розыгрышей ---
@dp.message(Command("new_giveaway"))
async def cmd_new_giveaway(message: types.Message, state: FSMContext):
    pool = dp["pool"]
    if not await is_creator(pool, message.from_user.id):
        await message.answer("❌ У вас нет доступа к созданию розыгрышей!")
        return

    builder = InlineKeyboardBuilder()
    builder.button(text="📊 Обычный конкурс", callback_data="mode_normal")
    builder.button(text="⚡️ Фаст-Клик (Кто первый)", callback_data="mode_fast")
    builder.adjust(1)

    await message.answer("🛠 **Создание нового розыгрыша**\n\nВыберите формат проведения:", reply_markup=builder.as_markup())
    await state.set_state(GiveawayForm.mode)

@dp.callback_query(GiveawayForm.mode, F.data.in_(["mode_normal", "mode_fast"]))
async def process_mode(callback: types.CallbackQuery, state: FSMContext):
    mode = "fast" if callback.data == "mode_fast" else "normal"
    await state.update_data(mode=mode)

    builder = InlineKeyboardBuilder()
    builder.button(text="🖼 NFT", callback_data="type_nft")
    builder.button(text="🎁 Обычный приз (Stars / Деньги)", callback_data="type_regular")
    builder.adjust(1)

    await callback.message.answer("Выберите тип приза:", reply_markup=builder.as_markup())
    await state.set_state(GiveawayForm.prize_type)
    await callback.answer()

@dp.callback_query(GiveawayForm.prize_type, F.data.in_(["type_nft", "type_regular"]))
async def process_prize_type(callback: types.CallbackQuery, state: FSMContext):
    prize_type = "NFT" if callback.data == "type_nft" else "Обычный"
    await state.update_data(prize_type=prize_type)

    if prize_type == "NFT":
        await callback.message.answer("🔗 Отправьте ссылку на ваш **NFT**:")
    else:
        await callback.message.answer("✏️ Напишите наименование приза (например: `500 Stars` или `10$`):")

    await state.set_state(GiveawayForm.prize_details)
    await callback.answer()

@dp.message(GiveawayForm.prize_details)
async def process_prize_details(message: types.Message, state: FSMContext):
    pool = dp["pool"]
    await state.update_data(prize_details=message.text.strip())
    user_data = await state.get_data()

    if user_data["mode"] == "fast":
        prize_type = user_data["prize_type"]
        prize_details = user_data["prize_details"]
        
        giveaway_id = random.randint(1000, 9999)
        bot_info = await bot.get_me()
        bot_username = bot_info.username

        await create_fast_click(pool, giveaway_id, f"{prize_type}: {prize_details}")
        fast_link = f"https://tg.catup.lol/{bot_username}?start=fast_{giveaway_id}"

        text = (
            f"⚡️ **ФАСТ-КЛИК РОЗЫГРЫШ!** ⚡️\n\n"
            f"🎁 **Приз:** {prize_details}\n\n"
            f"🚀 **Кто первый нажмёт на ссылку ниже, тот и забирает приз!**\n"
            f"{fast_link}\n\n"
            f"⏱ *Победитель определяется мгновенно.*"
        )

        await message.answer("⚡️ **Ваш Фаст-Клик конкурс готов! Срочно публикуйте его в канале:**")
        await message.answer(text, parse_mode="Markdown")
        await state.clear()
    else:
        await message.answer("📌 **Укажите условия для участия**\n(например: `Подписка на канал @mychannel`):")
        await state.set_state(GiveawayForm.conditions)

@dp.message(GiveawayForm.conditions)
async def process_conditions(message: types.Message, state: FSMContext):
    user_data = await state.get_data()
    prize_type = user_data["prize_type"]
    prize_details = user_data["prize_details"]
    conditions = message.text.strip()

    giveaway_id = random.randint(100, 999)
    bot_info = await bot.get_me()
    bot_username = bot_info.username

    join_link = f"https://tg.catup.lol/{bot_username}?start=join_{giveaway_id}"

    if prize_type == "NFT":
        prize_text = f"🖼 **Приз:** NFT ({prize_details})"
    else:
        prize_text = f"🎁 **Приз:** {prize_details}"

    text = (
        f"🎉 **РОЗЫГРЫШ!** 🎉\n\n"
        f"{prize_text}\n"
        f"📌 **Условия участия:** {conditions}\n\n"
        f"👥 **Для участия нажмите на ссылку ниже:**\n"
        f"{join_link}\n\n"
        f"🏆 **Победителей:** 1\n"
        f"⏰ **Окончание:** По решению организатора\n\n"
        f"ℹ️ *Ваше участие автоматически фиксируется ботом после перехода по ссылке.*"
    )

    await message.answer("✅ **Ваш пост с конкурсом готов! Скопируйте и опубликуйте его в канале:**")
    await message.answer(text, parse_mode="Markdown")
    await state.clear()

# --- Запуск ---
async def main():
    pool = await get_db_pool()
    await init_db(pool)
    dp["pool"] = pool
    
    await set_main_menu(bot)
    print("🤖 Бот запущен с облачной базой Neon PostgreSQL!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
