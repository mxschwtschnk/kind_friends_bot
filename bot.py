import os
import asyncio
from aiohttp import web

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
import asyncpg

# -------------------------------------------------------------------
# CONFIG
# -------------------------------------------------------------------

BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")  # We'll set this on Render

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is not set")

if not WEBHOOK_URL:
    raise RuntimeError("WEBHOOK_URL is not set")


bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


# -------------------------------------------------------------------
# DATABASE
# -------------------------------------------------------------------

async def init_db():
    global pool
    pool = await asyncpg.create_pool(DATABASE_URL)

    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                telegram_id BIGINT UNIQUE NOT NULL,
                username TEXT,
                is_paused BOOLEAN NOT NULL DEFAULT FALSE,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
        """)


async def get_or_create_user(tg_id, username):
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, is_paused FROM users WHERE telegram_id=$1",
            tg_id
        )
        if row:
            await conn.execute(
                "UPDATE users SET username=$1 WHERE telegram_id=$2",
                username, tg_id
            )
            return row["is_paused"]

        await conn.execute(
            "INSERT INTO users (telegram_id, username) VALUES ($1, $2)",
            tg_id, username
        )
        return False


async def set_pause(tg_id, paused):
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET is_paused=$1 WHERE telegram_id=$2",
            paused, tg_id
        )


async def is_paused(tg_id):
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT is_paused FROM users WHERE telegram_id=$1",
            tg_id
        )
    return row["is_paused"] if row else False


# -------------------------------------------------------------------
# KEYBOARD
# -------------------------------------------------------------------

def main_keyboard(paused):
    button = "▶️ Resume" if paused else "⏸ Pause"

    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📤 Send a link")],
            [KeyboardButton(text="➕ Invite friends"),
             KeyboardButton(text="👥 Friends")],
            [KeyboardButton(text=button)],
            [KeyboardButton(text="ℹ️ Help"),
             KeyboardButton(text="💡 Feedback")]
        ],
        resize_keyboard=True
    )


# -------------------------------------------------------------------
# HANDLERS
# -------------------------------------------------------------------

@dp.message(CommandStart())
async def start(message: types.Message):
    paused = await get_or_create_user(
        message.from_user.id,
        message.from_user.username
    )
    await message.answer(
        "Hi! I’m **Kind Friends** 👋\n"
        "Paste a link or use the menu below.",
        reply_markup=main_keyboard(paused)
    )


@dp.message(F.text == "⏸ Pause")
async def pause(message: types.Message):
    await set_pause(message.from_user.id, True)
    await message.answer(
        "You are now on pause.",
        reply_markup=main_keyboard(True)
    )


@dp.message(F.text == "▶️ Resume")
async def resume(message: types.Message):
    await set_pause(message.from_user.id, False)
    await message.answer(
        "You are active again!",
        reply_markup=main_keyboard(False)
    )


@dp.message()
async def handle_message(message: types.Message):
    txt = message.text

    # Is link?
    if txt.startswith("http://") or txt.startswith("https://"):
        paused = await is_paused(message.from_user.id)
        if paused:
            await message.answer(
                "You are paused. Tap ▶️ Resume to continue."
            )
            return

        await message.answer("Got your link! (MVP placeholder)")
        return

    await message.answer("Send me a link or use the menu buttons.")


# -------------------------------------------------------------------
# AIOHTTP WEB SERVER FOR WEBHOOK
# -------------------------------------------------------------------

async def handle(request):
    data = await request.json()
    update = types.Update(**data)
    await dp.feed_update(bot, update)
    return web.Response()


async def on_startup(app):
    await init_db()
    await bot.set_webhook(WEBHOOK_URL)
    print("Webhook is set:", WEBHOOK_URL)


async def on_shutdown(app):
    await bot.delete_webhook()
    print("Webhook removed")


def main():
    app = web.Application()
    app.router.add_post("/", handle)

    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)

    port = int(os.getenv("PORT", 10000))
    web.run_app(app, host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
