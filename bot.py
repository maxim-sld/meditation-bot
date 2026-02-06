import asyncio
import os
import asyncpg
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.types import (
    Message,
    LabeledPrice,
    PreCheckoutQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from aiogram.filters import CommandStart


BOT_TOKEN = os.getenv("BOT_TOKEN")
PAY_TOKEN = os.getenv("PAY_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

bot = Bot(BOT_TOKEN)
dp = Dispatcher()

db: asyncpg.Pool | None = None


# ================= DB =================

async def init_db():
    global db
    db = await asyncpg.create_pool(DATABASE_URL)

    async with db.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                telegram_id BIGINT PRIMARY KEY,
                paid BOOLEAN DEFAULT FALSE
            )
        """)


async def set_paid(user_id: int):
    async with db.acquire() as conn:
        await conn.execute("""
            INSERT INTO users (telegram_id, paid)
            VALUES ($1, TRUE)
            ON CONFLICT (telegram_id)
            DO UPDATE SET paid = TRUE
        """, user_id)


async def is_paid(user_id: int) -> bool:
    async with db.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT paid FROM users WHERE telegram_id=$1",
            user_id,
        )
        return bool(row and row["paid"])


# ================= START =================

@dp.message(CommandStart())
async def start(message: Message):
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💳 Купить полный доступ", callback_data="buy")]
        ]
    )

    await message.answer(
        "Добро пожаловать ✨\n\n"
        "Чтобы открыть все медитации — нажмите кнопку ниже 👇",
        reply_markup=kb,
    )


# ================= BUY =================

@dp.callback_query(F.data == "buy")
async def buy(callback):
    prices = [LabeledPrice(label="Доступ ко всем медитациям", amount=19900)]

    await bot.send_invoice(
        chat_id=callback.from_user.id,
        title="Полный доступ к медитациям",
        description="Разблокирует все медитации навсегда",
        payload="meditation_access",
        provider_token=PAY_TOKEN,
        currency="RUB",
        prices=prices,
        start_parameter="buy_access",
    )


# ================= PRE CHECKOUT =================

@dp.pre_checkout_query()
async def pre_checkout(pre_checkout_q: PreCheckoutQuery):
    await bot.answer_pre_checkout_query(pre_checkout_q.id, ok=True)


# ================= SUCCESS =================

@dp.message(F.successful_payment)
async def successful_payment(message: Message):
    user_id = message.from_user.id

    await set_paid(user_id)

    await message.answer(
        "Оплата прошла успешно! 🎉\n\n"
        "Теперь все медитации открыты."
    )


# ================= API =================

async def check_paid(request):
    user_id = request.query.get("user_id")

    if not user_id:
        return web.json_response({"paid": False})

    paid = await is_paid(int(user_id))

    return web.json_response({"paid": paid})


async def start_web_server():
    app = web.Application()
    app.router.add_get("/check", check_paid)

    port = int(os.environ.get("PORT", 8080))

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()


# ================= MAIN =================

async def main():
    await init_db()
    await start_web_server()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
