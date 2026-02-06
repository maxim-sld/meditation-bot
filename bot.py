import asyncio
import os
import asyncpg
from datetime import datetime, timedelta

from aiohttp import web
import aiohttp_cors

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, PreCheckoutQuery, LabeledPrice
from aiogram.filters import CommandStart


BOT_TOKEN = os.getenv("BOT_TOKEN")
PAY_TOKEN = os.getenv("PAY_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

bot = Bot(BOT_TOKEN)
dp = Dispatcher()
db: asyncpg.Pool


# ================= DB INIT =================

async def init_db():
    global db
    db = await asyncpg.create_pool(DATABASE_URL)

    async with db.acquire() as conn:
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id BIGSERIAL PRIMARY KEY,
            telegram_id BIGINT UNIQUE NOT NULL
        );

        CREATE TABLE IF NOT EXISTS meditations (
            id BIGSERIAL PRIMARY KEY,
            title TEXT NOT NULL,
            description TEXT,
            audio_url TEXT,
            duration_sec INT,
            is_free BOOLEAN DEFAULT FALSE
        );

        CREATE TABLE IF NOT EXISTS subscription_plans (
            id BIGSERIAL PRIMARY KEY,
            title TEXT NOT NULL,
            price INT NOT NULL,
            duration_days INT NOT NULL,
            is_active BOOLEAN DEFAULT TRUE
        );

        CREATE TABLE IF NOT EXISTS subscriptions (
            id BIGSERIAL PRIMARY KEY,
            user_id BIGINT REFERENCES users(id),
            plan_id BIGINT REFERENCES subscription_plans(id),
            expires_at TIMESTAMP
        );
        """)


# ================= USERS =================

async def get_or_create_user(telegram_id: int) -> int:
    async with db.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO users (telegram_id)
            VALUES ($1)
            ON CONFLICT (telegram_id)
            DO UPDATE SET telegram_id = EXCLUDED.telegram_id
            RETURNING id
            """,
            telegram_id,
        )
        return row["id"]


# ================= SUBSCRIPTIONS =================

async def give_subscription(user_id: int, plan_id: int):
    async with db.acquire() as conn:

        plan = await conn.fetchrow(
            "SELECT duration_days FROM subscription_plans WHERE id=$1",
            plan_id,
        )

        expires = datetime.utcnow() + timedelta(days=plan["duration_days"])

        await conn.execute(
            """
            INSERT INTO subscriptions (user_id, plan_id, expires_at)
            VALUES ($1,$2,$3)
            """,
            user_id,
            plan_id,
            expires,
        )


async def has_active_subscription(user_id: int) -> bool:
    async with db.acquire() as conn:
        row = await conn.fetchval(
            "SELECT 1 FROM subscriptions WHERE user_id=$1 AND expires_at > NOW()",
            user_id,
        )
        return bool(row)


# ================= BOT =================

@dp.message(CommandStart())
async def start(message: Message):
    await get_or_create_user(message.from_user.id)
    await message.answer("Открой Mini App и выбери медитацию ✨")


# ---------- BUY ----------

@dp.message(F.text.startswith("💳 Купить"))
async def buy_plan(message: Message):
    plan_id = int(message.text.split("#")[1])

    async with db.acquire() as conn:
        plan = await conn.fetchrow(
            "SELECT title, price FROM subscription_plans WHERE id=$1",
            plan_id,
        )

    await bot.send_invoice(
        chat_id=message.from_user.id,
        title=plan["title"],
        description="Подписка на медитации",
        payload=f"plan_{plan_id}",
        provider_token=PAY_TOKEN,
        currency="RUB",
        prices=[LabeledPrice(label=plan["title"], amount=plan["price"])],
    )


@dp.pre_checkout_query()
async def pre_checkout(q: PreCheckoutQuery):
    await bot.answer_pre_checkout_query(q.id, ok=True)


@dp.message(F.successful_payment)
async def successful_payment(message: Message):
    user_id = await get_or_create_user(message.from_user.id)

    payload = message.successful_payment.invoice_payload
    plan_id = int(payload.split("_")[1])

    await give_subscription(user_id, plan_id)

    await message.answer("Подписка активирована 🎉")


# ================= PUBLIC API =================

async def api_meditations(request):
    rows = await db.fetch(
        "SELECT id, title, description, audio_url, duration_sec, is_free FROM meditations"
    )
    return web.json_response([dict(r) for r in rows])


async def api_access(request):
    telegram_id = int(request.query["user_id"])
    meditation_id = int(request.query["meditation_id"])

    user_id = await get_or_create_user(telegram_id)

    async with db.acquire() as conn:
        free = await conn.fetchval(
            "SELECT is_free FROM meditations WHERE id=$1",
            meditation_id,
        )

    if free:
        return web.json_response({"access": True})

    return web.json_response({"access": await has_active_subscription(user_id)})


async def api_plans(request):
    rows = await db.fetch(
        "SELECT id, title, price, duration_days FROM subscription_plans WHERE is_active=TRUE"
    )
    return web.json_response([dict(r) for r in rows])


# ================= ADMIN =================

async def api_admin_all_plans(request):
    rows = await db.fetch("SELECT * FROM subscription_plans ORDER BY id")
    return web.json_response([dict(r) for r in rows])


async def api_admin_create_plan(request):
    data = await request.json()

    await db.execute(
        "INSERT INTO subscription_plans (title, price, duration_days) VALUES ($1,$2,$3)",
        data["title"],
        int(data["price"]),
        int(data["duration_days"]),
    )

    return web.json_response({"ok": True})


async def api_admin_toggle_plan(request):
    plan_id = int(request.match_info["id"])

    await db.execute(
        "UPDATE subscription_plans SET is_active = NOT is_active WHERE id=$1",
        plan_id,
    )

    return web.json_response({"ok": True})


# ================= WEB =================

async def start_web():
    app = web.Application()

    cors = aiohttp_cors.setup(
        app,
        defaults={"*": aiohttp_cors.ResourceOptions(
            allow_credentials=True,
            expose_headers="*",
            allow_headers="*",
            allow_methods="*",
        )},
    )

    app.router.add_get("/meditations", api_meditations)
    app.router.add_get("/access", api_access)
    app.router.add_get("/plans", api_plans)

    app.router.add_get("/admin/plans", api_admin_all_plans)
    app.router.add_post("/admin/plans", api_admin_create_plan)
    app.router.add_post("/admin/plans/{id}/toggle", api_admin_toggle_plan)

    for route in list(app.router.routes()):
        cors.add(route)

    port = int(os.environ.get("PORT", 8080))

    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()


# ================= MAIN =================

async def main():
    await init_db()
    await start_web()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
