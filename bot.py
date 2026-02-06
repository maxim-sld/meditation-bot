import asyncio
import os
import asyncpg
from datetime import datetime, timedelta
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, LabeledPrice, PreCheckoutQuery
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
            telegram_id BIGINT UNIQUE NOT NULL,
            created_at TIMESTAMP DEFAULT NOW()
        );

        CREATE TABLE IF NOT EXISTS packages (
            id BIGSERIAL PRIMARY KEY,
            title TEXT NOT NULL,
            description TEXT,
            price INT NOT NULL,
            created_at TIMESTAMP DEFAULT NOW()
        );

        CREATE TABLE IF NOT EXISTS meditations (
            id BIGSERIAL PRIMARY KEY,
            package_id BIGINT REFERENCES packages(id) ON DELETE CASCADE,
            title TEXT NOT NULL,
            description TEXT,
            audio_url TEXT NOT NULL,
            duration_sec INT,
            is_free BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMP DEFAULT NOW()
        );

        CREATE TABLE IF NOT EXISTS purchases (
            id BIGSERIAL PRIMARY KEY,
            user_id BIGINT REFERENCES users(id) ON DELETE CASCADE,
            package_id BIGINT REFERENCES packages(id) ON DELETE CASCADE,
            created_at TIMESTAMP DEFAULT NOW(),
            UNIQUE(user_id, package_id)
        );

        CREATE TABLE IF NOT EXISTS subscriptions (
            id BIGSERIAL PRIMARY KEY,
            user_id BIGINT REFERENCES users(id) ON DELETE CASCADE,
            plan TEXT NOT NULL,
            expires_at TIMESTAMP NOT NULL,
            created_at TIMESTAMP DEFAULT NOW()
        );

        CREATE TABLE IF NOT EXISTS listens (
            id BIGSERIAL PRIMARY KEY,
            user_id BIGINT REFERENCES users(id) ON DELETE CASCADE,
            meditation_id BIGINT REFERENCES meditations(id) ON DELETE CASCADE,
            seconds_listened INT,
            completed BOOLEAN,
            created_at TIMESTAMP DEFAULT NOW()
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


# ================= ACCESS =================

async def give_lifetime_access(user_id: int):
    async with db.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO subscriptions (user_id, plan, expires_at)
            VALUES ($1, 'lifetime', '2100-01-01')
            """,
            user_id,
        )


async def has_access(user_id: int, meditation_id: int) -> bool:
    async with db.acquire() as conn:

        # бесплатная медитация
        free = await conn.fetchval(
            "SELECT is_free FROM meditations WHERE id=$1",
            meditation_id,
        )
        if free:
            return True

        # активная подписка
        sub = await conn.fetchval(
            """
            SELECT 1 FROM subscriptions
            WHERE user_id=$1 AND expires_at > NOW()
            """,
            user_id,
        )
        if sub:
            return True

        # покупка пакета
        pkg = await conn.fetchval(
            "SELECT package_id FROM meditations WHERE id=$1",
            meditation_id,
        )

        bought = await conn.fetchval(
            """
            SELECT 1 FROM purchases
            WHERE user_id=$1 AND package_id=$2
            """,
            user_id,
            pkg,
        )

        return bool(bought)


# ================= BOT =================

@dp.message(CommandStart())
async def start(message: Message):
    await get_or_create_user(message.from_user.id)
    await message.answer("Добро пожаловать ✨\nОткрой Mini App для медитаций.")


@dp.pre_checkout_query()
async def pre_checkout(q: PreCheckoutQuery):
    await bot.answer_pre_checkout_query(q.id, ok=True)


@dp.message(F.successful_payment)
async def successful_payment(message: Message):
    user_id = await get_or_create_user(message.from_user.id)
    await give_lifetime_access(user_id)

    await message.answer("Оплата прошла успешно 🎉\nДоступ открыт навсегда.")


# ================= API =================

async def api_me(request):
    telegram_id = int(request.query["user_id"])
    user_id = await get_or_create_user(telegram_id)
    return web.json_response({"user_id": user_id})


async def api_meditations(request):
    rows = await db.fetch(
        """
        SELECT id, title, description, audio_url, duration_sec, is_free
        FROM meditations
        ORDER BY id
        """
    )
    return web.json_response([dict(r) for r in rows])


async def api_access(request):
    telegram_id = int(request.query["user_id"])
    meditation_id = int(request.query["meditation_id"])

    user_id = await get_or_create_user(telegram_id)
    access = await has_access(user_id, meditation_id)

    return web.json_response({"access": access})


async def api_listen(request):
    data = await request.json()

    telegram_id = int(data["user_id"])
    meditation_id = int(data["meditation_id"])
    seconds = int(data["seconds"])
    completed = bool(data["completed"])

    user_id = await get_or_create_user(telegram_id)

    await db.execute(
        """
        INSERT INTO listens (user_id, meditation_id, seconds_listened, completed)
        VALUES ($1,$2,$3,$4)
        """,
        user_id,
        meditation_id,
        seconds,
        completed,
    )

    return web.json_response({"ok": True})


# ================= WEB =================

async def start_web():
    app = web.Application()
    app.router.add_get("/me", api_me)
    app.router.add_get("/meditations", api_meditations)
    app.router.add_get("/access", api_access)
    app.router.add_post("/listen", api_listen)

    port = int(os.environ.get("PORT", 8080))

    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()


# ================= MAIN =================

async def main():
    await init_db()   # ← авто-создание таблиц
    await start_web()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
