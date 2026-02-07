import asyncio
import os
import asyncpg
import jwt
import hashlib
from datetime import datetime, timedelta
from typing import Optional
from aiohttp import web
import aiohttp_cors
import uuid

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, PreCheckoutQuery, LabeledPrice
from aiogram.filters import CommandStart
from aiogram.filters.command import CommandObject

# Для работы с S3
try:
    import aioboto3
    from aiobotocore.config import AioConfig
    S3_AVAILABLE = True
except ImportError:
    S3_AVAILABLE = False
    print("Warning: aioboto3 not installed. S3 functionality will be disabled.")

BOT_TOKEN = os.getenv("BOT_TOKEN")
PAY_TOKEN = os.getenv("PAY_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "change_me_in_production")
JWT_SECRET = os.getenv("JWT_SECRET", "super_secret_jwt_key_change_me")

# Yandex Cloud S3 конфигурация
S3_ENDPOINT_URL = os.getenv("S3_ENDPOINT_URL", "https://storage.yandexcloud.net")
S3_ACCESS_KEY_ID = os.getenv("S3_ACCESS_KEY_ID")
S3_SECRET_ACCESS_KEY = os.getenv("S3_SECRET_ACCESS_KEY")
S3_BUCKET_NAME = os.getenv("S3_BUCKET_NAME")
S3_REGION = os.getenv("S3_REGION", "ru-central1")

# Проверяем обязательные переменные
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN environment variable is required")
if not DATABASE_URL:
    raise ValueError("DATABASE_URL environment variable is required")

bot = Bot(BOT_TOKEN)
dp = Dispatcher()
db: asyncpg.Pool

# ================= S3 UTILS =================

async def upload_to_s3(file_data: bytes, file_name: str, content_type: str = "audio/mpeg") -> str:
    """Загружает файл в Yandex Cloud S3 и возвращает публичный URL"""
    if not S3_AVAILABLE:
        raise RuntimeError("aioboto3 is not installed")
    
    if not all([S3_ACCESS_KEY_ID, S3_SECRET_ACCESS_KEY, S3_BUCKET_NAME]):
        raise RuntimeError("S3 credentials not configured")
    
    try:
        session = aioboto3.Session()
        async with session.client(
            service_name='s3',
            endpoint_url=S3_ENDPOINT_URL,
            aws_access_key_id=S3_ACCESS_KEY_ID,
            aws_secret_access_key=S3_SECRET_ACCESS_KEY,
            region_name=S3_REGION
        ) as s3:
            # Генерируем уникальное имя файла
            unique_filename = f"{uuid.uuid4()}_{file_name}"
            
            await s3.put_object(
                Bucket=S3_BUCKET_NAME,
                Key=unique_filename,
                Body=file_data,
                ContentType=content_type,
                ACL='public-read'
            )
            
            # Формируем публичный URL
            public_url = f"{S3_ENDPOINT_URL}/{S3_BUCKET_NAME}/{unique_filename}"
            return public_url
    except Exception as e:
        print(f"Error uploading to S3: {e}")
        raise

async def delete_from_s3(file_url: str):
    """Удаляет файл из S3"""
    if not S3_AVAILABLE or not all([S3_ACCESS_KEY_ID, S3_SECRET_ACCESS_KEY, S3_BUCKET_NAME]):
        return
    
    try:
        # Извлекаем имя файла из URL
        if 'storage.yandexcloud.net' not in file_url:
            return
        
        file_name = file_url.split('/')[-1]
        
        session = aioboto3.Session()
        async with session.client(
            service_name='s3',
            endpoint_url=S3_ENDPOINT_URL,
            aws_access_key_id=S3_ACCESS_KEY_ID,
            aws_secret_access_key=S3_SECRET_ACCESS_KEY,
            region_name=S3_REGION
        ) as s3:
            await s3.delete_object(
                Bucket=S3_BUCKET_NAME,
                Key=file_name
            )
    except Exception as e:
        print(f"Error deleting from S3: {e}")

# ================= DB INIT =================

async def init_db():
    global db
    db = await asyncpg.create_pool(DATABASE_URL)
    
    print("Database connection established")

    async with db.acquire() as conn:
        try:
            # Создаем таблицы по очереди
            tables = [
                ("users", """
                    CREATE TABLE IF NOT EXISTS users (
                        id BIGSERIAL PRIMARY KEY,
                        telegram_id BIGINT UNIQUE NOT NULL,
                        created_at TIMESTAMP DEFAULT NOW()
                    )
                """),
                ("meditations", """
                    CREATE TABLE IF NOT EXISTS meditations (
                        id BIGSERIAL PRIMARY KEY,
                        title TEXT NOT NULL,
                        description TEXT,
                        audio_url TEXT,
                        duration_sec INT DEFAULT 0,
                        is_free BOOLEAN DEFAULT FALSE,
                        created_at TIMESTAMP DEFAULT NOW()
                    )
                """),
                ("subscription_plans", """
                    CREATE TABLE IF NOT EXISTS subscription_plans (
                        id BIGSERIAL PRIMARY KEY,
                        title TEXT NOT NULL,
                        price INT NOT NULL,
                        duration_days INT NOT NULL,
                        is_active BOOLEAN DEFAULT TRUE,
                        created_at TIMESTAMP DEFAULT NOW()
                    )
                """),
                ("subscriptions", """
                    CREATE TABLE IF NOT EXISTS subscriptions (
                        id BIGSERIAL PRIMARY KEY,
                        user_id BIGINT REFERENCES users(id),
                        plan_id BIGINT REFERENCES subscription_plans(id),
                        expires_at TIMESTAMP,
                        created_at TIMESTAMP DEFAULT NOW()
                    )
                """),
                ("admin_users", """
                    CREATE TABLE IF NOT EXISTS admin_users (
                        id SERIAL PRIMARY KEY,
                        username VARCHAR(50) UNIQUE NOT NULL,
                        password_hash VARCHAR(255) NOT NULL,
                        created_at TIMESTAMP DEFAULT NOW()
                    )
                """)
            ]
            
            for table_name, sql in tables:
                try:
                    await conn.execute(sql)
                    print(f"Table '{table_name}' checked/created")
                except Exception as e:
                    print(f"Error creating table '{table_name}': {e}")
            
            # Добавляем администратора по умолчанию
            try:
                password_hash = hashlib.sha256("admin123".encode()).hexdigest()
                await conn.execute("""
                    INSERT INTO admin_users (username, password_hash)
                    VALUES ('admin', $1)
                    ON CONFLICT (username) DO NOTHING
                """, password_hash)
                print("Default admin user checked/created")
            except Exception as e:
                print(f"Error creating admin user: {e}")
                
        except Exception as e:
            print(f"Database initialization error: {e}")
            raise

# ================= AUTH UTILS =================

def create_jwt(username: str) -> str:
    payload = {
        "sub": username,
        "exp": datetime.utcnow() + timedelta(hours=24),
        "iat": datetime.utcnow()
    }
    return jwt.encode(payload, JWT_SECRET, algorithm="HS256")

def verify_jwt(token: str) -> Optional[dict]:
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
        return payload
    except jwt.ExpiredSignatureError:
        return None
    except jwt.InvalidTokenError:
        return None
@web.middleware
async def auth_middleware(request: web.Request, handler):
    # Разрешаем OPTIONS запросы (preflight для CORS)
    if request.method == "OPTIONS":
        return web.Response(
            status=200,
            headers={
                'Access-Control-Allow-Origin': '*',
                'Access-Control-Allow-Methods': 'GET, POST, PUT, DELETE, OPTIONS',
                'Access-Control-Allow-Headers': 'Authorization, Content-Type',
                'Access-Control-Allow-Credentials': 'true',
            }
        )
    
    # Пропускаем публичные эндпоинты
    public_paths = ['/meditations', '/access', '/plans', '/admin/login', '/health']
    if any(request.path.startswith(path) for path in public_paths):
        response = await handler(request)
        # Добавляем CORS заголовки
        response.headers['Access-Control-Allow-Origin'] = '*'
        response.headers['Access-Control-Allow-Credentials'] = 'true'
        return response
    
    # Проверяем токен для админских путей
    if request.path.startswith('/admin'):
        auth_header = request.headers.get('Authorization')
        if not auth_header or not auth_header.startswith('Bearer '):
            response = web.json_response({'error': 'Unauthorized'}, status=401)
            response.headers['Access-Control-Allow-Origin'] = '*'
            response.headers['Access-Control-Allow-Credentials'] = 'true'
            return response
        
        token = auth_header.split(' ')[1]
        payload = verify_jwt(token)
        if not payload:
            response = web.json_response({'error': 'Invalid token'}, status=401)
            response.headers['Access-Control-Allow-Origin'] = '*'
            response.headers['Access-Control-Allow-Credentials'] = 'true'
            return response
    
    # Обрабатываем запрос
    response = await handler(request)
    # Добавляем CORS заголовки
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Credentials'] = 'true'
    return response

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

        if not plan:
            raise ValueError("Plan not found")

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
async def start(message: Message, command: CommandObject = None):
    try:
        await get_or_create_user(message.from_user.id)
        
        # Обработка deep link для покупки
        if command and command.args and command.args.startswith("buy_"):
            try:
                plan_id = int(command.args.split("_")[1])
                
                async with db.acquire() as conn:
                    plan = await conn.fetchrow(
                        "SELECT title, price FROM subscription_plans WHERE id=$1 AND is_active=TRUE",
                        plan_id,
                    )
                
                if plan:
                    await bot.send_invoice(
                        chat_id=message.from_user.id,
                        title=plan["title"],
                        description="Подписка на медитации",
                        payload=f"plan_{plan_id}",
                        provider_token=PAY_TOKEN,
                        currency="RUB",
                        prices=[LabeledPrice(label=plan["title"], amount=plan["price"])],
                    )
                    return
            
            except (ValueError, IndexError) as e:
                print(f"Error processing buy command: {e}")
        
        await message.answer("Открой Mini App и выбери медитацию ✨")
    except Exception as e:
        print(f"Error in start command: {e}")
        await message.answer("Произошла ошибка. Попробуйте позже.")

@dp.message(F.text.startswith("💳 Купить"))
async def buy_plan(message: Message):
    try:
        plan_id = int(message.text.split("#")[1])
    except (IndexError, ValueError):
        await message.answer("Ошибка: неверный формат команды")
        return

    async with db.acquire() as conn:
        plan = await conn.fetchrow(
            "SELECT title, price FROM subscription_plans WHERE id=$1 AND is_active=TRUE",
            plan_id,
        )

    if not plan:
        await message.answer("Тариф не найден")
        return

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
    try:
        user_id = await get_or_create_user(message.from_user.id)

        payload = message.successful_payment.invoice_payload
        plan_id = int(payload.split("_")[1])

        await give_subscription(user_id, plan_id)

        await message.answer("Подписка активирована 🎉")
    except Exception as e:
        print(f"Error processing payment: {e}")
        await message.answer("Ошибка при активации подписки. Обратитесь в поддержку.")

# ================= PUBLIC API =================

async def api_meditations(request):
    try:
        rows = await db.fetch(
            "SELECT id, title, description, audio_url, duration_sec, is_free FROM meditations ORDER BY created_at DESC"
        )
        return web.json_response([dict(r) for r in rows])
    except Exception as e:
        print(f"Error in api_meditations: {e}")
        return web.json_response({"error": "Internal server error"}, status=500)

async def api_access(request):
    try:
        telegram_id = request.query.get("user_id")
        meditation_id = request.query.get("meditation_id")
        
        if not telegram_id or not meditation_id:
            return web.json_response({"error": "Missing parameters"}, status=400)
        
        telegram_id = int(telegram_id)
        meditation_id = int(meditation_id)
    except ValueError:
        return web.json_response({"error": "Invalid parameters"}, status=400)
    
    try:
        user_id = await get_or_create_user(telegram_id)

        async with db.acquire() as conn:
            free = await conn.fetchval(
                "SELECT is_free FROM meditations WHERE id=$1",
                meditation_id,
            )

        if free:
            return web.json_response({"access": True})

        return web.json_response({"access": await has_active_subscription(user_id)})
    except Exception as e:
        print(f"Error in api_access: {e}")
        return web.json_response({"error": "Internal server error"}, status=500)

async def api_plans(request):
    try:
        rows = await db.fetch(
            "SELECT id, title, price, duration_days FROM subscription_plans WHERE is_active=TRUE ORDER BY price"
        )
        return web.json_response([dict(r) for r in rows])
    except Exception as e:
        print(f"Error in api_plans: {e}")
        return web.json_response({"error": "Internal server error"}, status=500)

# ================= ADMIN AUTH =================

async def api_admin_login(request):
    try:
        data = await request.json()
        username = data.get("username")
        password = data.get("password")
        
        if not username or not password:
            return web.json_response({"error": "Missing credentials"}, status=400)
        
        password_hash = hashlib.sha256(password.encode()).hexdigest()
        
        async with db.acquire() as conn:
            user = await conn.fetchrow(
                "SELECT id, username FROM admin_users WHERE username=$1 AND password_hash=$2",
                username, password_hash
            )
        
        if not user:
            return web.json_response({"error": "Invalid credentials"}, status=401)
        
        token = create_jwt(username)
        return web.json_response({
            "token": token,
            "user": {"id": user["id"], "username": user["username"]}
        })
    except Exception as e:
        print(f"Error in api_admin_login: {e}")
        return web.json_response({"error": "Internal server error"}, status=500)

async def api_admin_verify(request):
    auth_header = request.headers.get('Authorization')
    if not auth_header or not auth_header.startswith('Bearer '):
        return web.json_response({'error': 'No token'}, status=401)
    
    token = auth_header.split(' ')[1]
    payload = verify_jwt(token)
    
    if not payload:
        return web.json_response({'error': 'Invalid token'}, status=401)
    
    return web.json_response({"valid": True, "username": payload["sub"]})

# ================= ADMIN MEDITATIONS =================

async def api_admin_all_meditations(request):
    try:
        rows = await db.fetch("SELECT * FROM meditations ORDER BY id")
        return web.json_response([dict(r) for r in rows])
    except Exception as e:
        print(f"Error in api_admin_all_meditations: {e}")
        return web.json_response({"error": "Internal server error"}, status=500)

async def api_admin_create_meditation(request):
    try:
        if not S3_AVAILABLE or not all([S3_ACCESS_KEY_ID, S3_SECRET_ACCESS_KEY, S3_BUCKET_NAME]):
            return web.json_response({"error": "S3 storage is not configured"}, status=500)
        
        reader = await request.multipart()
        
        fields = {}
        file_data = None
        file_name = None
        content_type = None
        
        async for part in reader:
            if part.name == 'file':
                file_name = part.filename
                if not file_name or not file_name.endswith('.mp3'):
                    return web.json_response({"error": "Only MP3 files allowed"}, status=400)
                
                content_type = part.headers.get('Content-Type', 'audio/mpeg')
                file_data = await part.read()
                
                if len(file_data) > 50 * 1024 * 1024:  # 50MB limit
                    return web.json_response({"error": "File too large (max 50MB)"}, status=400)
                
            else:
                fields[part.name] = await part.text()
        
        if not file_data:
            return web.json_response({"error": "No file provided"}, status=400)
        
        # Загружаем файл в S3
        audio_url = await upload_to_s3(file_data, file_name, content_type)
        
        # Преобразуем типы
        is_free = fields.get('is_free', '').lower() == 'true'
        
        async with db.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO meditations (title, description, audio_url, is_free)
                VALUES ($1, $2, $3, $4)
                """,
                fields.get('title', '').strip(),
                fields.get('description', '').strip(),
                audio_url,
                is_free
            )
        
        return web.json_response({"ok": True})
    except Exception as e:
        print(f"Error in api_admin_create_meditation: {e}")
        return web.json_response({"error": str(e)}, status=500)

async def api_admin_update_meditation(request):
    try:
        meditation_id = int(request.match_info['id'])
        data = await request.json()
        
        async with db.acquire() as conn:
            await conn.execute(
                """
                UPDATE meditations 
                SET title=$1, description=$2, is_free=$3
                WHERE id=$4
                """,
                data.get('title', '').strip(),
                data.get('description', '').strip(),
                data.get('is_free', False),
                meditation_id
            )
        
        return web.json_response({"ok": True})
    except Exception as e:
        print(f"Error in api_admin_update_meditation: {e}")
        return web.json_response({"error": str(e)}, status=500)

async def api_admin_delete_meditation(request):
    try:
        meditation_id = int(request.match_info['id'])
        
        async with db.acquire() as conn:
            # Получаем URL аудио для удаления файла из S3
            audio_url = await conn.fetchval(
                "SELECT audio_url FROM meditations WHERE id=$1",
                meditation_id
            )
            
            # Удаляем запись из БД
            await conn.execute(
                "DELETE FROM meditations WHERE id=$1",
                meditation_id
            )
        
        # Удаляем файл из S3, если он существует
        if audio_url and 'storage.yandexcloud.net' in audio_url:
            await delete_from_s3(audio_url)
        
        return web.json_response({"ok": True})
    except Exception as e:
        print(f"Error in api_admin_delete_meditation: {e}")
        return web.json_response({"error": str(e)}, status=500)

# ================= ADMIN PLANS =================

async def api_admin_all_plans(request):
    try:
        rows = await db.fetch("SELECT * FROM subscription_plans ORDER BY id")
        return web.json_response([dict(r) for r in rows])
    except Exception as e:
        print(f"Error in api_admin_all_plans: {e}")
        return web.json_response({"error": "Internal server error"}, status=500)

async def api_admin_create_plan(request):
    try:
        data = await request.json()
        
        if not data.get("title") or not data.get("price") or not data.get("duration_days"):
            return web.json_response({"error": "Missing required fields"}, status=400)
        
        await db.execute(
            "INSERT INTO subscription_plans (title, price, duration_days) VALUES ($1,$2,$3)",
            data["title"],
            int(data["price"]),
            int(data["duration_days"]),
        )

        return web.json_response({"ok": True})
    except Exception as e:
        print(f"Error in api_admin_create_plan: {e}")
        return web.json_response({"error": str(e)}, status=500)

async def api_admin_toggle_plan(request):
    try:
        plan_id = int(request.match_info["id"])

        await db.execute(
            "UPDATE subscription_plans SET is_active = NOT is_active WHERE id=$1",
            plan_id,
        )

        return web.json_response({"ok": True})
    except Exception as e:
        print(f"Error in api_admin_toggle_plan: {e}")
        return web.json_response({"error": str(e)}, status=500)

# ================= ADMIN SUBSCRIPTIONS =================

async def api_admin_subscriptions(request):
    try:
        rows = await db.fetch("""
            SELECT s.*, u.telegram_id, p.title as plan_title
            FROM subscriptions s
            JOIN users u ON s.user_id = u.id
            JOIN subscription_plans p ON s.plan_id = p.id
            WHERE s.expires_at > NOW()
            ORDER BY s.expires_at DESC
        """)
        return web.json_response([dict(r) for r in rows])
    except Exception as e:
        print(f"Error in api_admin_subscriptions: {e}")
        return web.json_response({"error": "Internal server error"}, status=500)

# ================= HEALTH CHECK =================

async def api_health(request):
    return web.json_response({
        "status": "ok", 
        "timestamp": datetime.utcnow().isoformat(),
        "services": {
            "database": "connected",
            "s3": "configured" if S3_AVAILABLE and S3_ACCESS_KEY_ID else "not_configured"
        }
    })

# ================= WEB =================

# УДАЛИТЕ импорт aiohttp_cors:
# import aiohttp_cors  # <-- УДАЛИТЬ эту строку

# ... остальной код остается ...

async def start_web():
    app = web.Application(middlewares=[auth_middleware])
    
    # Публичные эндпоинты
    app.router.add_get("/meditations", api_meditations)
    app.router.add_get("/access", api_access)
    app.router.add_get("/plans", api_plans)
    app.router.add_get("/health", api_health)
    
    # Аутентификация
    app.router.add_post("/admin/login", api_admin_login)
    app.router.add_get("/admin/verify", api_admin_verify)
    
    # Админские эндпоинты
    app.router.add_get("/admin/meditations", api_admin_all_meditations)
    app.router.add_post("/admin/meditation", api_admin_create_meditation)
    app.router.add_put("/admin/meditation/{id}", api_admin_update_meditation)
    app.router.add_delete("/admin/meditation/{id}", api_admin_delete_meditation)
    
    app.router.add_get("/admin/plans", api_admin_all_plans)
    app.router.add_post("/admin/plans", api_admin_create_plan)
    app.router.add_post("/admin/plans/{id}/toggle", api_admin_toggle_plan)
    
    app.router.add_get("/admin/subscriptions", api_admin_subscriptions)

    port = int(os.environ.get("PORT", 8080))
    
    print(f"Starting web server on port {port}")
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()

# ================= MAIN =================

async def main():
    print("Starting application...")
    
    # Инициализация базы данных
    print("Initializing database...")
    await init_db()
    print("Database initialized successfully")
    
    # Запуск веб-сервера
    print("Starting web server...")
    await start_web()
    print("Web server started successfully")
    
    # Запуск бота
    print("Starting bot polling...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())