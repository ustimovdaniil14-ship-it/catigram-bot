import asyncio
import random
import sqlite3
from aiogram import Bot, Dispatcher, F, types
from aiogram.client.telegram import TelegramAPIServer
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.filters import Command, CommandObject
from aiogram.utils.keyboard import InlineKeyboardBuilder

# ================= Настройки =================
BOT_TOKEN = "1780243771:Gb_eq0_l0bqdqxSG5aU1zTnkyVSUjHnETPp"
ADMIN_ID = 1780243771
ADMIN_USERNAME = "@imsexy"

CATIGRAM_API_URL = "http://bot.catup.lol"
# =============================================

# Настройка API Catigram
catigram_server = TelegramAPIServer.from_base(CATIGRAM_API_URL)
session = AiohttpSession(api=catigram_server)
bot = Bot(token=BOT_TOKEN, session=session)
dp = Dispatcher()

# --- База данных (SQLite) ---
def init_db():
    conn = sqlite3.connect("bot_data.db")
    cursor = conn.cursor()
    cursor.execute("CREATE TABLE IF NOT EXISTS creators (user_id INTEGER PRIMARY KEY)")
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS participants (
            giveaway_id INTEGER,
            user_id INTEGER,
            username TEXT,
            PRIMARY KEY (giveaway_id, user_id)
        )
    """)
    conn.commit()
    conn.close()

def is_creator(user_id):
    if user_id == ADMIN_ID:
        return True
    conn = sqlite3.connect("bot_data.db")
    cursor = conn.cursor()
    cursor.execute("SELECT user_id FROM creators WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    return row is not None

def add_creator(user_id):
    conn = sqlite3.connect("bot_data.db")
    cursor = conn.cursor()
    try:
        cursor.execute("INSERT INTO creators VALUES (?)", (user_id,))
        conn.commit()
    except sqlite3.IntegrityError:
        pass
    conn.close()

def add_participant(giveaway_id, user_id, username):
    conn = sqlite3.connect("bot_data.db")
    cursor = conn.cursor()
    try:
        cursor.execute("INSERT INTO participants VALUES (?, ?, ?)", (giveaway_id, user_id, username or "Без username"))
        conn.commit()
        added = True
    except sqlite3.IntegrityError:
        added = False
    conn.close()
    return added

# --- Хэндлеры ---

# Команда /myid для проверки своего ID
@dp.message(Command("myid"))
async def cmd_myid(message: types.Message):
    await message.answer(f"🆔 Твой Telegram ID: `{message.from_user.id}`", parse_mode="Markdown")

# Команда /start
@dp.message(Command("start"))
async def cmd_start(message: types.Message, command: CommandObject):
    user_id = message.from_user.id
    args = command.args

    if args and args.startswith("join_"):
        giveaway_id = args.split("_")[1]
        success = add_participant(giveaway_id, user_id, message.from_user.username)
        
        if success:
            await message.answer(
                "🎉 **Ты участвуешь в розыгрыше!**\n"
                "Твой номер участника сохранён ботом."
            )
        else:
            await message.answer("⚠️ Вы уже зарегистрированы в этом розыгрыше!")
        return

    if is_creator(user_id):
        await message.answer(
            "👋 **Привет, Создатель!**\n\n"
            "У тебя есть доступ к созданию розыгрышей.\n"
            "Чтобы создать пост с конкурсом, используй команду:\n"
            "`/new_giveaway <название>`"
        )
    else:
        builder = InlineKeyboardBuilder()
        builder.button(text="⭐️ Купить доступ — 1000 ⭐️", callback_data="buy_access")
        builder.button(text=f"💬 Написать {ADMIN_USERNAME}", url=f"https://t.me/{ADMIN_USERNAME.replace('@', '')}")
        builder.adjust(1)

        await message.answer(
            "🔒 **Доступ закрыт**\n\n"
            f"Чтобы получить доступ к созданию и публикации розыгрышей, напиши администратору {ADMIN_USERNAME} в личные сообщения и отправь ему ⭐️ 1000.\n\n"
            "После проверки оплаты администратор вручную выдаст тебе доступ. До подтверждения оплаты функции бота останутся закрыты.",
            reply_markup=builder.as_markup()
        )

# Покупка доступа за Звёзды
@dp.callback_query(F.data == "buy_access")
async def buy_access_callback(callback: types.CallbackQuery):
    prices = [types.LabeledPrice(label="Доступ к созданию розыгрышей", amount=1000)]
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
    add_creator(message.from_user.id)
    await message.answer("🎉 **Оплата прошла успешно!** Теперь тебе доступно создание розыгрышей.")

# Ручная выдача доступа админом
@dp.message(Command("give_access"))
async def cmd_give_access(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    args = message.text.split()
    if len(args) > 1 and args[1].isdigit():
        target_id = int(args[1])
        add_creator(target_id)
        await message.answer(f"✅ Доступ успешно выдан пользователю `{target_id}`!")
        try:
            await bot.send_message(target_id, "🎉 Администратор выдал вам доступ к созданию розыгрышей!")
        except Exception:
            pass

# Создание розыгрыша
@dp.message(Command("new_giveaway"))
async def cmd_new_giveaway(message: types.Message):
    if not is_creator(message.from_user.id):
        await message.answer("❌ У вас нет доступа!")
        return

    title = message.text.replace("/new_giveaway", "").strip() or "Розыгрыш"
    giveaway_id = random.randint(10, 99)

    bot_info = await bot.get_me()
    bot_username = bot_info.username

    text = (
        f"🎁 **{title}**\n\n"
        f"📌 **Условия участия:** Подписка на канал\n\n"
        f"👥 **Участие:** нажми ссылку ниже\n"
        f"https://tg.catup.lol/{bot_username}?start=join_{giveaway_id}\n\n"
        f"🏆 **Победителей:** 1\n"
        f"⏰ **Окончание (UTC):** не указано\n\n"
        f"ℹ️ *Участие засчитывается ботом после перехода по ссылке.*"
    )

    await message.answer(text, parse_mode="Markdown")

# Запуск
async def main():
    init_db()
    print("🤖 Бот запущен!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
