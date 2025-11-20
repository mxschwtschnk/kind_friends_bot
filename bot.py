import os
import asyncio
from dataclasses import dataclass

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton

import asyncpg

# -------------------------------------------------------------------
# CONFIG
# -------------------------------------------------------------------

BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set. Please set it in the environment variables.")

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is not set. Please set it in the environment variables.")


bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


# -------------------------------------------------------------------
# DATABASE LAYER
# -------------------------------------------------------------------

@dataclass
class User:
    id: int
    telegram_id: int
    username: str | None
    is_paused: bool


class DB:
    """
    Simple database helper for Kind Friends bot.
    Manages:
    - users table
    - paused state
    """

    def __init__(self, dsn: str):
        self._dsn = dsn
        self.pool: asyncpg.Pool | None = None

    async def connect(self):
        """
        Create a connection pool and ensure tables exist.
        """
        self.pool = await asyncpg.create_pool(self._dsn)
        await self._create_tables()

    async def _create_tables(self):
        """
        Create minimal tables if they do not exist.
        For now we only store users and their paused state.
        """
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id SERIAL PRIMARY KEY,
                    telegram_id BIGINT UNIQUE NOT NULL,
                    username TEXT,
                    is_paused BOOLEAN NOT NULL DEFAULT FALSE,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                """
            )

    async def get_or_create_user(self, telegram_id: int, username: str | None) -> User:
        """
        Return existing user or create a new one.
        Username is updated on every call to keep it fresh.
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, telegram_id, username, is_paused FROM users WHERE telegram_id = $1",
                telegram_id,
            )
            if row:
                # Update username if changed
                await conn.execute(
                    "UPDATE users SET username = $1 WHERE telegram_id = $2",
                    username,
                    telegram_id,
                )
            else:
                row = await conn.fetchrow(
                    """
                    INSERT INTO users (telegram_id, username)
                    VALUES ($1, $2)
                    RETURNING id, telegram_id, username, is_paused
                    """,
                    telegram_id,
                    username,
                )

            return User(
                id=row["id"],
                telegram_id=row["telegram_id"],
                username=row["username"],
                is_paused=row["is_paused"],
            )

    async def set_pause(self, telegram_id: int, paused: bool):
        """
        Set pause state for a user.
        If user does not exist yet, create it first with default values.
        """
        async with self.pool.acquire() as conn:
            # Ensure user exists
            row = await conn.fetchrow(
                "SELECT id FROM users WHERE telegram_id = $1",
                telegram_id,
            )
            if not row:
                # Create user with default values
                await conn.execute(
                    "INSERT INTO users (telegram_id, is_paused) VALUES ($1, $2)",
                    telegram_id,
                    paused,
                )
            else:
                await conn.execute(
                    "UPDATE users SET is_paused = $1 WHERE telegram_id = $2",
                    paused,
                    telegram_id,
                )

    async def is_paused(self, telegram_id: int) -> bool:
        """
        Check if user is paused.
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT is_paused FROM users WHERE telegram_id = $1",
                telegram_id,
            )
            if row:
                return row["is_paused"]
            return False


db = DB(DATABASE_URL)


# -------------------------------------------------------------------
# UI: MAIN KEYBOARD
# -------------------------------------------------------------------

def main_keyboard(is_paused: bool) -> ReplyKeyboardMarkup:
    """
    Creates the main menu keyboard for the Kind Friends bot.
    Pause/Resume label depends on user state.
    """
    pause_button = "⏸ Pause" if not is_paused else "▶️ Resume"

    kb = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📤 Send a link")],
            [KeyboardButton(text="➕ Invite friends"), KeyboardButton(text="👥 Friends")],
            [KeyboardButton(text=pause_button)],
            [KeyboardButton(text="ℹ️ Help"), KeyboardButton(text="💡 Feedback")],
        ],
        resize_keyboard=True,
    )
    return kb


# -------------------------------------------------------------------
# HANDLERS
# -------------------------------------------------------------------

@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    """
    Handles /start.
    Creates or loads user from the database,
    then shows the main menu with correct pause state.
    """
    user = await db.get_or_create_user(
        telegram_id=message.from_user.id,
        username=message.from_user.username,
    )

    text = (
        "Hi! I’m **Kind Friends** 👋\n\n"
        "I help friends share important links with each other.\n\n"
        "• Paste a link here – I will treat it as something you want to share.\n"
        "• Use the buttons below to invite friends, manage your list,\n"
        "  pause/resume, or send feedback.\n"
    )
    await message.answer(text, reply_markup=main_keyboard(is_paused=user.is_paused))


@dp.message(F.text == "ℹ️ Help")
async def cmd_help(message: types.Message):
    """
    Explains how the bot works.
    """
    text = (
        "I’m **Kind Friends** 👋\n\n"
        "What I do:\n"
        "• Let your friends send you important links (posts, events, articles, etc.)\n"
        "• Let you send your important links to your friends\n\n"
        "The idea is mutual support: when you get a link, you open it and, if possible,\n"
        "support your friend (like, comment, share, sign up, etc.).\n\n"
        "How to use me:\n"
        "• Just paste a link directly into this chat — that’s enough.\n"
        "• Use the buttons to invite friends, manage your list, pause/resume,\n"
        "  or send feedback and ideas.\n"
    )
    await message.answer(text)


@dp.message(F.text == "📤 Send a link")
async def btn_send_link(message: types.Message):
    """
    Button: Send a link.
    Currently just explains what to do.
    """
    await message.answer(
        "Send me a link you want to share with all your friends. Just paste a single URL."
    )


@dp.message(F.text == "⏸ Pause")
async def btn_pause(message: types.Message):
    """
    Enables pause mode for the user.
    In pause mode:
    - user cannot send links
    - user will not receive links from friends (logic will be added later)
    """
    user_id = message.from_user.id
    await db.set_pause(user_id, True)

    await message.answer(
        "You are now on pause.\n\n"
        "While you’re on pause:\n"
        "• You can’t send links\n"
        "• You won’t receive new links from friends\n\n"
        "Tap ▶️ Resume when you want to come back.",
        reply_markup=main_keyboard(is_paused=True),
    )


@dp.message(F.text == "▶️ Resume")
async def btn_resume(message: types.Message):
    """
    Disables pause mode for the user.
    Later we will add a digest of missed links here.
    """
    user_id = message.from_user.id
    await db.set_pause(user_id, False)

    await message.answer(
        "Welcome back! 👋\n"
        "You are active again.\n"
        "Later I will show you a digest of what you missed here.",
        reply_markup=main_keyboard(is_paused=False),
    )


@dp.message(F.text == "💡 Feedback")
async def btn_feedback(message: types.Message):
    """
    Starts feedback collection (demo mode).
    """
    await message.answer(
        "Please type your feedback or ideas in one message and send it here.\n\n"
        "(In this minimal version it will not be saved yet,\n"
        "but later it will be forwarded to the creator.)"
    )


@dp.message()
async def handle_message(message: types.Message):
    """
    Handles:
    - direct link messages
    - all unknown text
    """
    text = message.text or ""
    user_id = message.from_user.id

    # Check if user is paused
    if await db.is_paused(user_id):
        await message.answer(
            "You are currently on pause.\n"
            "Tap ▶️ Resume if you want to send and receive links again."
        )
        return

    # Button "Send a link"
    if text == "📤 Send a link":
        await message.answer(
            "Send me a link you want to share with all your friends. Just paste a single URL."
        )
        return

    # Check if message looks like a link
    if text.startswith("http://") or text.startswith("https://"):
        # Later:
        # - save link in DB
        # - send to all friends
        await message.answer(
            "Got your link ✅\n"
            "In the real MVP I will send it to all your active friends.\n"
            "For now this is just a test response."
        )
        print(f"[DEBUG] User {user_id} sent link: {text}")
        return

    # Unknown text
    await message.answer(
        "I only understand links and the menu buttons for now.\n"
        "If you want to test sending, just paste a link (starting with http:// or https://)."
    )


# -------------------------------------------------------------------
# ENTRYPOINT
# -------------------------------------------------------------------

async def main():
    """
    Entrypoint for running the bot with long polling.
    Connects to the database and starts polling.
    """
    print("Connecting to database...")
    await db.connect()
    print("Database connected. Starting Kind Friends bot polling...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
