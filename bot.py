import asyncio
import json
import os
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, LabeledPrice
from aiogram.filters import CommandStart

BOT_TOKEN = os.getenv("BOT_TOKEN")
PAY_TOKEN = os.getenv("PAY_TOKEN")  # ЮKassa TEST

bot = Bot(BOT_TOKEN)
dp = Dispatcher()

USERS_FILE = "users.json"


# ================= USERS =================

def load_users():
    if not os.path.exists(USERS_FILE):
        return {}
    with open(USERS_FILE, "r") as f:
        return json.load(f)


def save_users(data):
    with open(USERS_FILE, "w") as f:
        json.dump(data, f)


# ================= START =================

@dp.message(CommandStart())
async def start(message: Message):
    await message.answer(
        "Добро пожаловать ✨\n\n"
        "Чтобы открыть все медитации — нажмите кнопку ниже 👇",
        reply_markup={
            "inline_keyboard": [
                [{"text": "💳 Купить полный доступ", "callback_data": "buy"}]
            ]
        },
    )


# ================= BUY BUTTON =================

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


# ================= SUCCESSFUL PAYMENT =================

@dp.message(F.successful_payment)
async def successful_payment(message: Message):
    user_id = str(message.from_user.id)

    users = load_users()
    users[user_id] = {"paid": True}
    save_users(users)

    await message.answer("Оплата прошла успешно! 🎉\n\nВсе медитации открыты.")


# ================= API /check =================

async def check_paid(request):
    user_id = request.query.get("user_id")
    users = load_users()

    return web.json_response({"paid": user_id in users})


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
    await start_web_server()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
