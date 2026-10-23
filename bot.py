import asyncio
import random
import asyncpg
from aiohttp import web
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
ADMIN_ID = 1780243247  # Твой главный Telegram ID
ADMIN_USERNAME = "@Pavelmanager"

PRICE_STARS = 600  # Цена доступа

# Подключение к Neon PostgreSQL
DATABASE_URL = "postgresql://neondb_owner:npg_pWVDXMBAHT90@ep-jolly-heart-b5c4ag91-pooler.c-7.us-east-2.aws.neon.tech/neondb?sslmode=require&channel_binding=require"

CATIGRAM_API_URL = "http://bot.catup.lol"
# =============================================

catigram_server = TelegramAPIServer.from_base(CATIGRAM_API_URL)
session = AiohttpSession(api=catigram_server)
bot = Bot(token=BOT_TOKEN, session=session)
dp = Dispatcher(storage=MemoryStorage())

# --- Состояния ---
class GiveawayForm(StatesGroup):
    mode = State()          
    prize_type = State()    
    prize_details = State() 
    channel_username = State()
    conditions = State()    

class DonateForm(StatesGroup):
    amount = State()

# --- База данных ---
async def get_db_pool():
    return await asyncpg.create_pool(DATABASE_URL)

async def init_db(pool):
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS creators (
                user_id BIGINT PRIMARY KEY
            );
        """)
        # Теперь храним порядковый номер (ticket_number)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS participants (
                giveaway_id INT,
                user_id BIGINT,
                username TEXT,
                ticket_number INT,
                PRIMARY KEY (giveaway_id, user_id)
            );
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS giveaway_channels (
                giveaway_id INT PRIMARY KEY,
                channel_id TEXT
            );
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS fast_clicks (
                giveaway_id INT PRIMARY KEY,
                creator_id BIGINT,
                winner_id BIGINT,
                winner_username TEXT,
                prize_details TEXT
            );
        """)
        await conn.execute("INSERT INTO creators (user_id) VALUES ($1) ON CONFLICT DO NOTHING;", ADMIN_ID)

async def is_creator(pool, user_id):
    if user_id == ADMIN_ID:
        return True
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT user_id FROM creators WHERE user_id = $1", user_id)
        return row is not None

async def add_creator(pool, user_id):
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT user_id FROM creators WHERE user_id = $1", user_id)
        exists = row is not None
        await conn.execute("INSERT INTO creators (user_id) VALUES ($1) ON CONFLICT DO NOTHING;", user_id)
        return exists

async def add_participant_with_ticket(pool, giveaway_id, user_id, username):
    async with pool.acquire() as conn:
        # Проверяем, не участвует ли уже
        existing = await conn.fetchrow(
            "SELECT ticket_number FROM participants WHERE giveaway_id = $1 AND user_id = $2",
            giveaway_id, user_id
        )
        if existing:
            return existing["ticket_number"], False  # Уже в базе, возвращаем его номер

        # Узнаем последний номер и прибавляем 1
        max_ticket = await conn.fetchval(
            "SELECT MAX(ticket_number) FROM participants WHERE giveaway_id = $1",
            giveaway_id
        )
        next_ticket = (max_ticket or 0) + 1

        await conn.execute(
            "INSERT INTO participants (giveaway_id, user_id, username, ticket_number) VALUES ($1, $2, $3, $4);",
            giveaway_id, user_id, username or "Без username", next_ticket
        )
        return next_ticket, True

async def create_fast_click(pool, giveaway_id, creator_id, prize_details):
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO fast_clicks (giveaway_id, creator_id, prize_details) VALUES ($1, $2, $3);",
            giveaway_id, creator_id, prize_details
        )

async def claim_fast_click(pool, giveaway_id, user_id, username):
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT creator_id, winner_id, prize_details FROM fast_clicks WHERE giveaway_id = $1", giveaway_id)
        if not row:
            return "not_found", None, None
        
        creator_id, winner_id, prize_details = row["creator_id"], row["winner_id"], row["prize_details"]
        if winner_id is not None:
            return "already_claimed", prize_details, creator_id
        
        await conn.execute(
            "UPDATE fast_clicks SET winner_id = $1, winner_username = $2 WHERE giveaway_id = $3",
            user_id, username or "Без username", giveaway_id
        )
        return "win", prize_details, creator_id

# --- Веб-сервер-заглушка для Render ---
async def handle_ping(request):
    return web.Response(text="Bot is alive!")

async def start_dummy_server():
    app = web.Application()
    app.router.add_get('/', handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', 10000)
    await site.start()

# --- Главное меню ---
async def set_main_menu(bot: Bot):
    commands = [
        BotCommand(command="start", description="Главное меню / Перезапуск"),
        BotCommand(command="new_giveaway", description="Создать новый конкурс"),
        BotCommand(command="finish", description="Итоги: /finish ID"),
        BotCommand(command="reroll", description="Переролл: /reroll ID"),
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

    if args and args.startswith("fast_"):
        giveaway_id = int(args.split("_")[1])
        status, prize, creator_id = await claim_fast_click(pool, giveaway_id, user_id, message.from_user.username)
        
        if status == "win":
            user_tg = f"@{message.from_user.username}" if message.from_user.username else "Без username"
            await message.answer(
                f"⚡️ **ПОЗДРАВЛЯЕМ! ТЫ БЫЛ ПЕРВЫМ!** ⚡️\n\n"
                f"🎉 Ты успешно забрал приз: **{prize}**!\n"
                f"Организатор скоро свяжется с тобой."
            )
            try:
                await bot.send_message(
                    chat_id=creator_id,
                    text=(
                        f"⚡️ **ФАСТ-КЛИК РОЗЫГРЫШ ЗАВЕРШЁН!** ⚡️\n\n"
                        f"🏆 **Победитель:** {user_tg} (`ID: {user_id}`)\n"
                        f"🎁 **Приз:** {prize}\n"
                        f"📌 **ID конкурса:** `#{giveaway_id}`"
                    ),
                    parse_mode="Markdown"
                )
            except Exception:
                pass
        elif status == "already_claimed":
            await message.answer("❌ **К сожалению, ты не успел!** Этот приз уже забрал другой участник.")
        else:
            await message.answer("⚠️ Розыгрыш не найден или завершён.")
        return

    if args and args.startswith("join_"):
        giveaway_id = int(args.split("_")[1])
        
        async with pool.acquire() as conn:
            ch_row = await conn.fetchrow("SELECT channel_id FROM giveaway_channels WHERE giveaway_id = $1", giveaway_id)
        
        if ch_row:
            channel_id = ch_row["channel_id"]
            try:
                member = await bot.get_chat_member(chat_id=channel_id, user_id=user_id)
                if member.status in ["left", "kicked"]:
                    await message.answer(
                        "❌ **Вы не подписаны на канал!**\n\n"
                        f"Чтобы принять участие, подпишитесь на канал {channel_id} и перейдите по ссылке розыгрыша снова."
                    )
                    return
            except Exception:
                pass

        ticket_number, is_new = await add_participant_with_ticket(pool, giveaway_id, user_id, message.from_user.username)
        if is_new:
            await message.answer(
                f"🎉 **Ты успешно участвуешь в розыгрыше!**\n"
                f"🎟 Твой порядковый номер: **#{ticket_number}**"
            )
        else:
            await message.answer(f"ℹ️ Ты уже зарегистрирован в этом розыгрыше! Твой номер: **#{ticket_number}**")
        return

    if await is_creator(pool, user_id):
        builder = InlineKeyboardBuilder()
        builder.button(text="💖 Поддержать автора (Пожертвование)", callback_data="donate_stars")
        
        await message.answer(
            "👋 **Привет, Создатель!**\n\n"
            "У тебя есть доступ к созданию розыгрышей.\n"
            "👉 /new_giveaway — Создать конкурс\n"
            "👉 `/finish <ID>` — Подвести итоги\n"
            "👉 `/reroll <ID>` — Перевыбрать победителя",
            reply_markup=builder.as_markup()
        )
    else:
        builder = InlineKeyboardBuilder()
        builder.button(text=f"⭐️ Купить доступ — {PRICE_STARS} ⭐️", callback_data="buy_access")
        builder.button(text="💖 Пожертвовать звёзды", callback_data="donate_stars")
        builder.button(text=f"💬 Написать {ADMIN_USERNAME}", url=f"https://t.me/{ADMIN_USERNAME.replace('@', '')}")
        builder.adjust(1)

        await message.answer(
            "🔒 **Доступ закрыт**\n\n"
            f"Чтобы получить доступ к созданию и публикации розыгрышей, напиши администратору {ADMIN_USERNAME} в личные сообщения или оплатите ⭐️ {PRICE_STARS} кнопкой ниже.\n\n"
            "После оплаты доступ откроется автоматически!",
            reply_markup=builder.as_markup()
        )

# --- Донаты ---
@dp.callback_query(F.data == "donate_stars")
async def start_donate(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.answer("💖 **Пожертвование автору**\n\nВведите количество звёзд ⭐️ (**только цифрами**, например: `50`, `100`):")
    await state.set_state(DonateForm.amount)
    await callback.answer()

@dp.message(DonateForm.amount)
async def process_donate_amount(message: types.Message, state: FSMContext):
    text_input = message.text.strip() if message.text else ""
    if not text_input.isdigit() or int(text_input) <= 0:
        await message.answer("⚠️ Ошибка! Введите корректное число звёзд:")
        return

    amount = int(text_input)
    prices = [types.LabeledPrice(label="Пожертвование проекту", amount=amount)]
    
    await bot.send_invoice(
        chat_id=message.from_user.id,
        title="Пожертвование автору 💖",
        description=f"Добровольное пожертвование в размере {amount} Telegram Stars.",
        payload=f"donate_{message.from_user.id}",
        currency="XTR",
        prices=prices,
        provider_token=""
    )
    await state.clear()

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
    payload = message.successful_payment.invoice_payload
    if payload.startswith("donate_"):
        await message.answer("💖 **Огромное спасибо за вашу поддержку!**")
    else:
        await add_creator(pool, message.from_user.id)
        await message.answer("🎉 **Оплата прошла успешно!** Теперь тебе доступно создание розыгрышей.")

@dp.message(Command("give_access"))
async def cmd_give_access(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    pool = dp["pool"]
    args = message.text.split()
    if len(args) > 1 and args[1].isdigit():
        target_id = int(args[1])
        already_had = await add_creator(pool, target_id)
        
        if already_had:
            await message.answer(f"ℹ️ У пользователя `{target_id}` **уже был** бесплатный доступ ранее.")
        else:
            await message.answer(f"✅ Бесплатный доступ **успешно выдан** пользователю `{target_id}`!")
            try:
                await bot.send_message(
                    target_id, 
                    f"🎉 Главный администратор {ADMIN_USERNAME} выдал вам бесплатный доступ к созданию розыгрышей навсегда!\n\n"
                    f"Используйте команду /new_giveaway для создания первого конкурса."
                )
            except Exception:
                pass
    else:
        await message.answer("⚠️ Укажите ID. Пример: `/give_access 123456789`", parse_mode="Markdown")

@dp.message(Command("finish"))
async def cmd_finish(message: types.Message):
    pool = dp["pool"]
    if not await is_creator(pool, message.from_user.id):
        return

    args = message.text.split()
    if len(args) < 2 or not args[1].isdigit():
        await message.answer("⚠️ Укажите ID розыгрыша. Пример: `/finish 123`", parse_mode="Markdown")
        return

    giveaway_id = int(args[1])
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT user_id, username, ticket_number FROM participants WHERE giveaway_id = $1", giveaway_id)

    if not rows:
        await message.answer(f"❌ В розыгрыше `#{giveaway_id}` пока нет участников!")
        return

    winner = random.choice(rows)
    winner_username = f"@{winner['username']}" if winner['username'] != "Без username" else f"ID: {winner['user_id']}"

    await message.answer(
        f"🎉 **ИТОГИ РОЗЫГРЫША #{giveaway_id}** 🎉\n\n"
        f"🏆 **Победитель:** {winner_username} (Номер билета: **#{winner['ticket_number']}**)\n"
        f"👥 Всего участников: {len(rows)}\n\n"
        f"💬 *Скопируйте этот текст и опубликуйте его в своём канале!*",
        parse_mode="Markdown"
    )

@dp.message(Command("reroll"))
async def cmd_reroll(message: types.Message):
    pool = dp["pool"]
    if not await is_creator(pool, message.from_user.id):
        return

    args = message.text.split()
    if len(args) < 2 or not args[1].isdigit():
        await message.answer("⚠️ Укажите ID розыгрыша для реролла. Пример: `/reroll 123`", parse_mode="Markdown")
        return

    giveaway_id = int(args[1])
    async with pool.acquire() as conn:
        if len(args) > 2 and args[2].isdigit():
            old_winner_id = int(args[2])
            await conn.execute("DELETE FROM participants WHERE giveaway_id = $1 AND user_id = $2", giveaway_id, old_winner_id)

        rows = await conn.fetch("SELECT user_id, username, ticket_number FROM participants WHERE giveaway_id = $1", giveaway_id)

    if not rows:
        await message.answer(f"❌ В розыгрыше `#{giveaway_id}` больше нет других участников для реролла!")
        return

    new_winner = random.choice(rows)
    winner_username = f"@{new_winner['username']}" if new_winner['username'] != "Без username" else f"ID: {new_winner['user_id']}"

    await message.answer(
        f"🔄 **ПЕРЕРОЛЛ РОЗЫГРЫША #{giveaway_id}** 🔄\n\n"
        f"🏆 **Новый победитель:** {winner_username} (Номер билета: **#{new_winner['ticket_number']}**)\n"
        f"👥 Осталось участников в пуле: {len(rows)}\n\n"
        f"💬 *Опубликуйте этот результат в канале!*",
        parse_mode="Markdown"
    )

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
    builder.button(text="🎁 Обычный приз (Stars)", callback_data="type_regular")
    builder.adjust(1)

    await callback.message.answer("Выберите тип приза:", reply_markup=builder.as_markup())
    await state.set_state(GiveawayForm.prize_type)
    await callback.answer()

@dp.callback_query(GiveawayForm.prize_type, F.data.in_(["type_nft", "type_regular"]))
async def process_prize_type(callback: types.CallbackQuery, state: FSMContext):
    prize_type = "NFT" if callback.data == "type_nft" else "Обычный"
    await state.update_data(prize_type=prize_type)

    if prize_type == "NFT":
        await callback.message.answer("🔗 Отправьте ссылку на ваш **NFT** (текстом):")
    else:
        await callback.message.answer("🔢 Введите количество звёзд **только цифрами** (например: `500` или `1000`):")

    await state.set_state(GiveawayForm.prize_details)
    await callback.answer()

@dp.message(GiveawayForm.prize_details)
async def process_prize_details(message: types.Message, state: FSMContext):
    user_data = await state.get_data()
    prize_type = user_data.get("prize_type")
    text_input = message.text.strip() if message.text else ""

    if prize_type == "Обычный":
        if not text_input.isdigit():
            await message.answer("⚠️ Ошибка! Нужно ввести **только цифры** (например: `500`). Попробуй еще раз:")
            return
        prize_details = f"{text_input} Stars"
    else:
        if not text_input:
            await message.answer("⚠️ Ссылка не должна быть пустой. Отправьте ссылку на NFT:")
            return
        prize_details = text_input

    await state.update_data(prize_details=prize_details)

    if user_data.get("mode") == "fast":
        pool = dp["pool"]
        giveaway_id = random.randint(1000, 9999)
        bot_info = await bot.get_me()
        bot_username = bot_info.username

        await create_fast_click(pool, giveaway_id, message.from_user.id, f"{prize_type}: {prize_details}")
        fast_link = f"https://tg.catup.lol/{bot_username}?start=fast_{giveaway_id}"

        text = (
            f"⚡️ **ФАСТ-КЛИК РОЗЫГРЫШ!** ⚡️\n\n"
            f"🎁 **Приз:** {prize_details}\n\n"
            f"🚀 Кто первый нажмёт на ссылку ниже, тот и забирает приз!\n\n"
            f"👉 [⚡️ Забрать приз!]({fast_link})\n\n"
            f"⏱ *Победитель определяется мгновенно.*"
        )

        await message.answer(f"⚡️ **Ваш Фаст-Клик конкурс готов! (ID: `{giveaway_id}`) Скопируйте текст в свой канал:**", parse_mode="Markdown")
        await message.answer(text, parse_mode="Markdown")
        await state.clear()
    else:
        await message.answer(
            "📢 **Введите юзернейм вашего канала**\n"
            "(например: `@PavelBet`)\n\n"
            "ℹ️ *Бот должен быть администратором в этом канале для проверки подписок.*"
        )
        await state.set_state(GiveawayForm.channel_username)

@dp.message(GiveawayForm.channel_username)
async def process_channel(message: types.Message, state: FSMContext):
    channel_input = message.text.strip()
    await state.update_data(channel_username=channel_input)

    builder = InlineKeyboardBuilder()
    builder.button(text="⏭ Пропустить (без условий)", callback_data="skip_conditions")
    builder.adjust(1)

    await message.answer(
        "📌 **Укажите условия для участия**\n"
        "(например: `Поставить реакцию на пост`)\n\n"
        "*Подписка на канал проверится автоматически!* Можете указать доп. условия или нажать кнопку ниже:",
        reply_markup=builder.as_markup()
    )
    await state.set_state(GiveawayForm.conditions)

@dp.callback_query(GiveawayForm.conditions, F.data == "skip_conditions")
async def skip_conditions_callback(callback: types.CallbackQuery, state: FSMContext):
    await finish_creating_giveaway(callback.message, state, conditions="Подписка на канал")
    await callback.answer()

@dp.message(GiveawayForm.conditions)
async def process_conditions(message: types.Message, state: FSMContext):
    conditions = message.text.strip() if message.text else "Подписка на канал"
    await finish_creating_giveaway(message, state, conditions=conditions)

async def finish_creating_giveaway(message: types.Message, state: FSMContext, conditions: str):
    pool = dp["pool"]
    user_data = await state.get_data()
    prize_type = user_data.get("prize_type")
    prize_details = user_data.get("prize_details")
    channel_username = user_data.get("channel_username")

    giveaway_id = random.randint(100, 999)
    bot_info = await bot.get_me()
    bot_username = bot_info.username

    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO giveaway_channels (giveaway_id, channel_id) VALUES ($1, $2);",
            giveaway_id, channel_username
        )

    join_link = f"https://tg.catup.lol/{bot_username}?start=join_{giveaway_id}"

    if prize_type == "NFT":
        prize_text = f"🖼 **Приз:** NFT ({prize_details})"
    else:
        prize_text = f"🎁 **Приз:** {prize_details}"

    text = (
        f"🎉 **РОЗЫГРЫШ #{giveaway_id}** 🎉\n\n"
        f"{prize_text}\n"
        f"📌 **Условия участия:** {conditions} (Проверяется автоматически!)\n\n"
        f"🏆 **Победителей:** 1\n"
        f"⏰ **Окончание:** По решению организатора\n\n"
        f"👉 [🎉 Участвовать в розыгрыше]({join_link})"
    )

    await message.answer(
        f"✅ **Ваш пост с конкурсом готов! (ID: `{giveaway_id}`)**\n"
        f"Завершить: `/finish {giveaway_id}` | Переролл: `/reroll {giveaway_id}`\n\n"
        f"Скопируйте текст ниже и вставьте в свой канал (кнопка исчезать не будет):",
        parse_mode="Markdown"
    )
    await message.answer(text, parse_mode="Markdown")
    await state.clear()

async def main():
    pool = await get_db_pool()
    await init_db(pool)
    dp["pool"] = pool
    
    await start_dummy_server()
    
    await set_main_menu(bot)
    print("🤖 Бот запущен!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
