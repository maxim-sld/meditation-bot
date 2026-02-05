import json
import logging
from aiogram import Bot, Dispatcher, types
from aiogram.types import LabeledPrice
from aiogram.filters import CommandStart
from aiogram import F
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.enums import ContentType
import asyncio

BOT_TOKEN = "8361965118:AAGs96ijjC7og3_-uHr5B0rzaa1Mcx52V5Q"
PROVIDER_TOKEN = "381764678:TEST:164912"  # тестовый ЮKassa

logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

DB_FILE = "users.json"


def load_users():
    try:
        with open(DB_FILE, "r") as f:
            return json.load(f)
    except:
        return {}


def save_users(data):
    with open(DB_FILE, "w") as f:
        json.dump(data, f)


@dp.message(CommandStart())
async def start(message: types.Message):
    kb = InlineKeyboardBuilder()
    kb.button(text="💳 Купить доступ", callback_data="buy")

    await message.answer(
        "Добро пожаловать в медитации 🧘\n\n"
        "Полный доступ ко всем медитациям — **199 ₽**",
        reply_markup=kb.as_markup(),
        parse_mode="Markdown",
    )


@dp.callback_query(F.data == "buy")
async def buy(callback: types.CallbackQuery):
    prices = [LabeledPrice(label="Доступ к медитациям", amount=19900)]

    await bot.send_invoice(
        chat_id=callback.from_user.id,
        title="Доступ к медитациям",
        description="Полный доступ ко всем медитациям",
        provider_token=PROVIDER_TOKEN,
        currency="RUB",
        prices=prices,
        payload="meditation_access",
    )


@dp.pre_checkout_query()
async def pre_checkout(pre_checkout_q: types.PreCheckoutQuery):
    await bot.answer_pre_checkout_query(pre_checkout_q.id, ok=True)


@dp.message(F.content_type == ContentType.SUCCESSFUL_PAYMENT)
async def successful_payment(message: types.Message):
    users = load_users()
    users[str(message.from_user.id)] = {"paid": True}
    save_users(users)

    await message.answer(
        "✅ Оплата прошла успешно!\n"
        "Теперь у тебя есть доступ ко всем медитациям."
    )

from aiohttp import web

async def check_paid(request):
    user_id = request.query.get("user_id")
    users = load_users()

    if user_id in users and users[user_id].get("paid"):
        return web.json_response({"paid": True})
    return web.json_response({"paid": False})


async def start_web_server():
    app = web.Application()
    app.router.add_get("/check", check_paid)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", 8080)
    await site.start()


async def main():
    await start_web()
    await dp.start_polling(bot)



if __name__ == "__main__":
    asyncio.run(main())

