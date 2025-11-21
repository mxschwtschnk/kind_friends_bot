import os
from datetime import datetime, timedelta, timezone
import math

import asyncio
from aiohttp import web

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart
from aiogram.types import (
    ReplyKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardRemove,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
import asyncpg

# -------------------------------------------------------------------
# CONFIG
# -------------------------------------------------------------------

BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
BOT_USERNAME = os.getenv("BOT_USERNAME", "KindFriendsBot")  # without @
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))  # your Telegram ID

MAX_FRIENDS = 15
ADMIN_MAX_FRIENDS = 50
MAX_DAILY_LINKS = 5

feedback_recipient_raw = os.getenv("FEEDBACK_RECIPIENT_CHAT_ID")
FEEDBACK_RECIPIENT_CHAT_ID = (
    int(feedback_recipient_raw) if feedback_recipient_raw else ADMIN_ID
)

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is not set")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

pool: asyncpg.Pool | None = None
add_friend_mode = set()  # user_ids who are adding a friend
remove_friend_mode = set()  # user_ids who are removing a friend
feedback_mode = set()  # user_ids who are sending feedback
delete_account_confirmation = set()  # user_ids awaiting delete confirmation


class LoadingIndicator:
    """
    Shows a loading hint (typing + temporary keyboard removal) if a handler
    takes longer than a short delay. Helps users understand the bot is waking
    up after Render sleeps.
    """

    def __init__(self, message: types.Message, delay: float = 2.0):
        self.message = message
        self.delay = delay
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._indicator_message: types.Message | None = None

    async def __aenter__(self):
        self._task = asyncio.create_task(self._runner())
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._indicator_message:
            try:
                await self._indicator_message.delete()
            except Exception as e:  # noqa: BLE001
                print(f"[WARN] Failed to delete loading message: {e}")

    async def _runner(self):
        try:
            await asyncio.sleep(self.delay)
            self._indicator_message = await self.message.answer(
                "⏳ Waking up the server… please wait a couple seconds",
                reply_markup=ReplyKeyboardRemove(),
            )
            while not self._stop.is_set():
                await bot.send_chat_action(self.message.chat.id, "typing")
                await asyncio.sleep(4)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            print(f"[WARN] Loading indicator error: {e}")


# -------------------------------------------------------------------
# DATABASE SETUP
# -------------------------------------------------------------------

async def init_db():
    """
    Create connection pool and required tables.
    """
    global pool
    pool = await asyncpg.create_pool(DATABASE_URL)

    async with pool.acquire() as conn:
        # Users table
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                telegram_id BIGINT UNIQUE NOT NULL,
                username TEXT,
                is_paused BOOLEAN NOT NULL DEFAULT FALSE,
                sent_links_count BIGINT NOT NULL DEFAULT 0,
                invites_sent_count BIGINT NOT NULL DEFAULT 0,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            """
        )
        # Ensure sent_links_count exists for older schema
        await conn.execute(
            """
            ALTER TABLE users
            ADD COLUMN IF NOT EXISTS sent_links_count BIGINT NOT NULL DEFAULT 0;
            """
        )
        await conn.execute(
            """
            ALTER TABLE users
            ADD COLUMN IF NOT EXISTS invites_sent_count BIGINT NOT NULL DEFAULT 0;
            """
        )
        await conn.execute(
            """
            ALTER TABLE users
            ADD COLUMN IF NOT EXISTS sent_links_today_count INT NOT NULL DEFAULT 0;
            """
        )
        await conn.execute(
            """
            ALTER TABLE users
            ADD COLUMN IF NOT EXISTS sent_links_date DATE;
            """
        )

        # Friendships table
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS friendships (
                id SERIAL PRIMARY KEY,
                user_telegram_id BIGINT NOT NULL,
                friend_telegram_id BIGINT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                UNIQUE (user_telegram_id, friend_telegram_id)
            );
            """
        )

        # Friend requests awaiting confirmation
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS pending_friend_requests (
                id SERIAL PRIMARY KEY,
                requester_telegram_id BIGINT NOT NULL,
                recipient_telegram_id BIGINT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                UNIQUE (requester_telegram_id, recipient_telegram_id)
            );
            """
        )

        # Pending links for paused users
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS pending_links (
                id SERIAL PRIMARY KEY,
                recipient_telegram_id BIGINT NOT NULL,
                sender_telegram_id BIGINT NOT NULL,
                url TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            """
        )

        # Track when users last sent a specific link (spam control)
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sent_links_history (
                sender_telegram_id BIGINT NOT NULL,
                url TEXT NOT NULL,
                sent_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (sender_telegram_id, url)
            );
            """
        )


# -------------------------------------------------------------------
# DATABASE HELPERS
# -------------------------------------------------------------------

async def get_or_create_user(tg_id: int, username: str | None) -> bool:
    """
    Make sure user exists; always keep username fresh.
    Return current is_paused flag.
    """
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, is_paused FROM users WHERE telegram_id=$1",
            tg_id,
        )
        if row:
            await conn.execute(
                "UPDATE users SET username=$1 WHERE telegram_id=$2",
                username,
                tg_id,
            )
            return row["is_paused"]

        await conn.execute(
            "INSERT INTO users (telegram_id, username) VALUES ($1, $2)",
            tg_id,
            username,
        )
        return False


async def set_pause(tg_id: int, paused: bool):
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id FROM users WHERE telegram_id=$1",
            tg_id,
        )
        if row:
            await conn.execute(
                "UPDATE users SET is_paused=$1 WHERE telegram_id=$2",
                paused,
                tg_id,
            )
        else:
            await conn.execute(
                "INSERT INTO users (telegram_id, is_paused) VALUES ($1, $2)",
                tg_id,
                paused,
            )


async def is_paused(tg_id: int) -> bool:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT is_paused FROM users WHERE telegram_id=$1",
            tg_id,
        )
    return row["is_paused"] if row else False


async def get_telegram_id_by_username(username: str | None):
    """
    Find telegram_id by @username (case-insensitive).
    """
    if not username:
        return None
    username = username.lstrip("@").strip().lower()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT telegram_id FROM users WHERE LOWER(username)=$1",
            username,
        )
    return row["telegram_id"] if row else None


def normalize_username_input(raw: str) -> str:
    """Strip formatting characters users might copy from lists."""
    return raw.strip().lstrip("-").lstrip("@").strip()


async def add_mutual_friendship(a_tg_id: int, b_tg_id: int):
    """
    Make friendship A<->B in both directions.
    """
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO friendships (user_telegram_id, friend_telegram_id)
            VALUES ($1, $2)
            ON CONFLICT (user_telegram_id, friend_telegram_id) DO NOTHING;
            """,
            a_tg_id,
            b_tg_id,
        )
        await conn.execute(
            """
            INSERT INTO friendships (user_telegram_id, friend_telegram_id)
            VALUES ($1, $2)
            ON CONFLICT (user_telegram_id, friend_telegram_id) DO NOTHING;
            """,
            b_tg_id,
            a_tg_id,
        )


async def get_friend_usernames(tg_id: int):
    """
    Return list of friends' usernames for display.
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT u.username
            FROM friendships f
            JOIN users u ON u.telegram_id = f.friend_telegram_id
            WHERE f.user_telegram_id = $1
            ORDER BY u.username;
            """,
            tg_id,
        )
    return [r["username"] for r in rows if r["username"]]


async def get_friend_count(tg_id: int) -> int:
    async with pool.acquire() as conn:
        return await conn.fetchval(
            """
            SELECT COUNT(*)
            FROM friendships
            WHERE user_telegram_id = $1;
            """,
            tg_id,
        )


async def get_all_friend_ids(tg_id: int):
    """
    Get telegram_ids of all friends (paused or not).
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT friend_telegram_id
            FROM friendships
            WHERE user_telegram_id = $1;
            """,
            tg_id,
        )
    return [r["friend_telegram_id"] for r in rows]


async def remove_friendship(a_tg_id: int, b_tg_id: int):
    """
    Remove friendship A-B and B-A.
    """
    async with pool.acquire() as conn:
        await conn.execute(
            """
            DELETE FROM friendships
            WHERE (user_telegram_id=$1 AND friend_telegram_id=$2)
               OR (user_telegram_id=$2 AND friend_telegram_id=$1);
            """,
            a_tg_id,
            b_tg_id,
        )


async def notify_friend_removed(remover_id: int, friend_id: int):
    """Notify a friend that they were removed from someone's list."""

    remover_username = await get_username_by_telegram_id(remover_id)
    remover_display = display_username(remover_username, "A friend")

    try:
        await bot.send_message(
            friend_id,
            f"{remover_display} removed you from Kind Friends. You are no longer connected.",
        )
    except Exception as e:  # noqa: BLE001
        print(f"[WARN] Failed to notify user {friend_id} about removal: {e}")


async def count_pending_requests_for_requester(tg_id: int) -> int:
    async with pool.acquire() as conn:
        return await conn.fetchval(
            """
            SELECT COUNT(*)
            FROM pending_friend_requests
            WHERE requester_telegram_id = $1;
            """,
            tg_id,
        )


async def get_pending_requests_for_requester(tg_id: int):
    async with pool.acquire() as conn:
        return await conn.fetch(
            """
            SELECT id, recipient_telegram_id
            FROM pending_friend_requests
            WHERE requester_telegram_id = $1;
            """,
            tg_id,
        )


async def get_pending_requests_with_usernames_for_requester(tg_id: int):
    async with pool.acquire() as conn:
        return await conn.fetch(
            """
            SELECT p.id, p.recipient_telegram_id, u.username
            FROM pending_friend_requests p
            LEFT JOIN users u ON u.telegram_id = p.recipient_telegram_id
            WHERE p.requester_telegram_id = $1
            ORDER BY p.created_at DESC;
            """,
            tg_id,
        )


async def notify_user_friend_pool_full_with_pending(tg_id: int):
    friend_count = await get_friend_count(tg_id)
    pending_count = await count_pending_requests_for_requester(tg_id)
    max_friends = get_max_friends(tg_id)
    if friend_count >= max_friends and pending_count:
        try:
            await bot.send_message(
                tg_id,
                "Your friend pool is full. These outstanding invitations can no longer be accepted. "
                "Please delete a friend and send a new invitation if you still want to connect.",
            )
        except Exception as e:  # noqa: BLE001
            print(f"[WARN] Failed to notify user {tg_id} about full friend pool: {e}")


async def get_username_by_telegram_id(tg_id: int) -> str | None:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT username FROM users WHERE telegram_id=$1;",
            tg_id,
        )
    return row["username"] if row else None


async def get_all_user_ids() -> list[int]:
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT telegram_id FROM users;")
    return [row["telegram_id"] for row in rows]


def get_max_friends(user_id: int) -> int:
    return ADMIN_MAX_FRIENDS if user_id == ADMIN_ID else MAX_FRIENDS


async def get_pending_friend_request(
    requester_id: int, recipient_id: int
) -> asyncpg.Record | None:
    """
    Fetch a pending friend request from requester to recipient, if any.
    """
    async with pool.acquire() as conn:
        return await conn.fetchrow(
            """
            SELECT id, requester_telegram_id, recipient_telegram_id
            FROM pending_friend_requests
            WHERE requester_telegram_id=$1 AND recipient_telegram_id=$2;
            """,
            requester_id,
            recipient_id,
        )


async def create_friend_request(requester_id: int, recipient_id: int) -> int | None:
    """
    Create a pending friend request. Returns its ID, or None on failure.
    """
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO pending_friend_requests (requester_telegram_id, recipient_telegram_id)
            VALUES ($1, $2)
            ON CONFLICT (requester_telegram_id, recipient_telegram_id) DO NOTHING
            RETURNING id;
            """,
            requester_id,
            recipient_id,
        )
    return row["id"] if row else None


async def get_friend_request_by_id(request_id: int) -> asyncpg.Record | None:
    async with pool.acquire() as conn:
        return await conn.fetchrow(
            """
            SELECT id, requester_telegram_id, recipient_telegram_id
            FROM pending_friend_requests
            WHERE id=$1;
            """,
            request_id,
        )


async def delete_friend_request(request_id: int):
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM pending_friend_requests WHERE id=$1;",
            request_id,
        )


async def delete_friend_requests_for_user(tg_id: int):
    async with pool.acquire() as conn:
        await conn.execute(
            """
            DELETE FROM pending_friend_requests
            WHERE requester_telegram_id = $1
               OR recipient_telegram_id = $1;
            """,
            tg_id,
        )


async def save_pending_link(recipient_id: int, sender_id: int, url: str):
    """
    Store a link for a recipient who is on pause.
    """
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO pending_links (recipient_telegram_id, sender_telegram_id, url)
            VALUES ($1, $2, $3);
            """,
            recipient_id,
            sender_id,
            url,
        )


async def get_pending_links_for_user(recipient_id: int):
    """
    Get all pending links for a recipient with sender usernames.
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT p.url, p.created_at, u.username AS sender_username
            FROM pending_links p
            JOIN users u ON u.telegram_id = p.sender_telegram_id
            WHERE p.recipient_telegram_id = $1
            ORDER BY p.created_at;
            """,
            recipient_id,
        )
    return rows


async def clear_pending_links_for_user(recipient_id: int):
    """
    Delete all pending links for a recipient after digest is sent.
    """
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM pending_links WHERE recipient_telegram_id=$1",
            recipient_id,
        )


async def get_daily_sent_links_count(tg_id: int) -> int:
    """
    Return how many links the user has sent today, resetting the counter if the day changed.
    """
    today = datetime.now(timezone.utc).date()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT sent_links_today_count, sent_links_date
            FROM users
            WHERE telegram_id = $1;
            """,
            tg_id,
        )

        if not row:
            return 0

        count = row["sent_links_today_count"] or 0
        last_date = row["sent_links_date"]

        if last_date != today:
            await conn.execute(
                """
                UPDATE users
                SET sent_links_today_count = 0,
                    sent_links_date = $1
                WHERE telegram_id = $2;
                """,
                today,
                tg_id,
            )
            return 0

    return count


async def increment_sent_links(tg_id: int) -> int:
    """
    Increase per-user counter of sent links.
    """
    today = datetime.now(timezone.utc).date()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            UPDATE users
            SET sent_links_count = sent_links_count + 1,
                sent_links_today_count = CASE
                    WHEN sent_links_date = $1 THEN sent_links_today_count + 1
                    ELSE 1
                END,
                sent_links_date = $1
            WHERE telegram_id = $2
            RETURNING sent_links_today_count;
            """,
            today,
            tg_id,
        )
    return row["sent_links_today_count"] if row else 0


async def increment_invites_sent(tg_id: int):
    """
    Increase per-user counter of sent invites.
    """
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE users
            SET invites_sent_count = invites_sent_count + 1
            WHERE telegram_id = $1;
            """,
            tg_id,
        )


async def get_recent_sent_link_timestamp(sender_id: int, url: str):
    """
    Return the last time the sender shared the exact URL, if any.
    """
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT sent_at
            FROM sent_links_history
            WHERE sender_telegram_id = $1 AND url = $2;
            """,
            sender_id,
            url,
        )
    return row["sent_at"] if row else None


async def record_sent_link(sender_id: int, url: str):
    """
    Upsert the timestamp for a sent link to enforce cooldowns.
    """
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO sent_links_history (sender_telegram_id, url, sent_at)
            VALUES ($1, $2, NOW())
            ON CONFLICT (sender_telegram_id, url)
            DO UPDATE SET sent_at = EXCLUDED.sent_at;
            """,
            sender_id,
            url,
        )


async def delete_user_completely(tg_id: int):
    """
    Fully remove user from Kind Friends:
    - all friendships
    - all pending links (sent and received)
    - user record itself
    """
    async with pool.acquire() as conn:
        # Remove friendships where user is either side
        await conn.execute(
            """
            DELETE FROM friendships
            WHERE user_telegram_id = $1
               OR friend_telegram_id = $1;
            """,
            tg_id,
        )

        # Remove pending friend requests
        await conn.execute(
            """
            DELETE FROM pending_friend_requests
            WHERE requester_telegram_id = $1
               OR recipient_telegram_id = $1;
            """,
            tg_id,
        )

        # Remove pending links sent or received by this user
        await conn.execute(
            """
            DELETE FROM pending_links
            WHERE recipient_telegram_id = $1
               OR sender_telegram_id = $1;
            """,
            tg_id,
        )

        # Finally remove user record
        await conn.execute(
            "DELETE FROM users WHERE telegram_id = $1;",
            tg_id,
        )


# -------------------------------------------------------------------
# UI
# -------------------------------------------------------------------


def display_username(username: str | None, fallback: str = "friend") -> str:
    return f"@{username}" if username else fallback

def main_keyboard(paused: bool) -> ReplyKeyboardMarkup:
    if paused:
        keyboard = [
            [KeyboardButton(text="▶️ Resume"), KeyboardButton(text="ℹ️ Help")],
        ]
    else:
        keyboard = [
            [KeyboardButton(text="👥 Friends")],
            [KeyboardButton(text="⏸ Pause"), KeyboardButton(text="ℹ️ Help")],
        ]

    return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)


def friends_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="➕ Invite"), KeyboardButton(text="➖ Remove")],
            [KeyboardButton(text="⬅️ Back")],
        ],
        resize_keyboard=True,
    )


def help_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📖 How to"), KeyboardButton(text="💬 Feedback")],
            [KeyboardButton(text="🧹 Wipe Account")],
            [KeyboardButton(text="⬅️ Back")],
        ],
        resize_keyboard=True,
    )


def feedback_keyboard() -> ReplyKeyboardMarkup:
    return back_only_keyboard()


def back_only_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="⬅️ Back")]],
        resize_keyboard=True,
    )


def remove_friends_keyboard(friends: list[str]) -> InlineKeyboardMarkup:
    buttons = [
        [
            InlineKeyboardButton(
                text=f"@{username}",
                callback_data=f"remove_friend:{username}",
            )
        ]
        for username in friends
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


# -------------------------------------------------------------------
# FEEDBACK
# -------------------------------------------------------------------


async def deliver_feedback_to_admin(sender: types.User, text: str) -> bool:
    """
    Send feedback to the admin (or explicitly configured recipient).
    Returns True on success, False otherwise.
    """

    target_chat_id = FEEDBACK_RECIPIENT_CHAT_ID or ADMIN_ID
    if not target_chat_id:
        return False

    sender_display = (
        f"@{sender.username}" if sender.username else sender.full_name or "A user"
    )
    try:
        await bot.send_message(
            target_chat_id,
            f"Feedback from {sender_display} (ID: {sender.id}):\n\n{text}",
        )
        return True
    except Exception as e:  # noqa: BLE001
        print(f"[WARN] Failed to deliver feedback to {target_chat_id}: {e}")
        return False


# -------------------------------------------------------------------
# HANDLERS
# -------------------------------------------------------------------

@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    """
    /start: ensure user exists, handle invite deep-links, and show main menu.
    """
    paused = await get_or_create_user(
        message.from_user.id,
        message.from_user.username,
    )

    # Handle deep-link invitation (/start <inviter_id>)
    inviter_id: int | None = None
    inviter_display_name: str | None = None

    if message.text:
        parts = message.text.split(maxsplit=1)
        if len(parts) == 2:
            try:
                inviter_id = int(parts[1])
            except ValueError:
                inviter_id = None

    if inviter_id and inviter_id != message.from_user.id:
        async with pool.acquire() as conn:
            inviter_row = await conn.fetchrow(
                "SELECT username FROM users WHERE telegram_id=$1",
                inviter_id,
            )

        if inviter_row:
            inviter_username = inviter_row["username"] or "your friend"
            inviter_display_name = (
                f"@{inviter_username}" if inviter_row["username"] else inviter_username
            )
            await add_mutual_friendship(message.from_user.id, inviter_id)

            try:
                new_user_display = (
                    f"@{message.from_user.username}" if message.from_user.username else "A friend"
                )
                await bot.send_message(
                    inviter_id,
                    f"{new_user_display} joined Kind Friends via your link. You are now connected!",
                )
            except Exception as e:
                print(f"[WARN] Failed to notify inviter {inviter_id}: {e}")

    invite_note = (
        f"\n\n✅ I connected you with {inviter_display_name}. "
        "You can now share important links with each other."
        if inviter_display_name
        else ""
    )

    await message.answer(
        "Hi! I’m **Kind Friends** 👋\n\n"
        "I help friends share important links with each other.\n\n"
        "Paste a link here – I will treat it as something you want to share.\n"
        "• Use the buttons to open Friends, pause/resume, or read help.\n"
        + invite_note,
        reply_markup=main_keyboard(paused),
    )


@dp.message(F.text == "Start")
async def start_text_handler(message: types.Message):
    await cmd_start(message)


@dp.message(F.text == "ℹ️ Help")
async def help_handler(message: types.Message):
    await message.answer(
        "Pick a help option:",
        reply_markup=help_keyboard(),
    )


@dp.message(F.text == "📖 How to")
async def how_to_handler(message: types.Message):
    await message.answer(
        "This should be a message under How to\n\n"
        "1️⃣ Sharing links\n"
        "• Paste any http(s) link into the chat.\n"
        "• I send it to all your connected friends who are not on pause.\n"
        "• Friends who are on pause get your links later in a digest when they resume.\n\n"
        "2️⃣ Daily limits & anti-spam\n"
        "• You can share up to 5 links per day.\n"
        "• The same exact link can only be sent again after 7 days (to avoid spam).\n\n"
        "3️⃣ Adding friends\n"
        "• Tap “👥 Friends →➕ Invite” and send your friend’s @username.\n"
        "• If they already use Kind Friends, they get a request to connect.\n"
        "• If they don’t, you’ll receive an invite message + link that you can forward.\n"
        "• You can have up to 15 friends connected.\n\n"
        "4️⃣ Removing friends\n"
        "• Use “👥 Friends → ➖ Remove”,\n"
        "or send -@username directly in the chat.\n"
        "• Removing a friend stops future link sharing, but past messages stay in their chat.\n\n"
        "5️⃣ Pause / Resume\n"
        "• Tap “⏸ Pause” to stop sending and receiving links.\n"
        "• While paused, your friends’ links are stored for you.\n"
        "• Tap “▶️ Resume” to become active again and get a summary of links you missed.\n\n"
        "6️⃣ Wipe account\n"
        "• Tap “🧹 Wipe Account” to delete your Kind Friends account, friendships,\n"
        "and stored pending links.\n"
        "• Already delivered messages in other chats can’t be removed.",
        reply_markup=help_keyboard(),
    )


@dp.message(F.text == "🧹 Wipe Account")
async def delete_account_prompt(message: types.Message):
    user_id = message.from_user.id
    delete_account_confirmation.add(user_id)
    await message.answer(
        "Are you sure you want to wipe your Kind Friends account?\n\n"
        "This will remove all friends, delete any stored pending links, and new links will no longer arrive.\n"
        "Continue?",
        reply_markup=ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text="Yes, wipe"), KeyboardButton(text="No, keep my account")],
            ],
            resize_keyboard=True,
        ),
    )


@dp.message(F.text == "💬 Feedback")
async def start_feedback_from_menu(message: types.Message):
    feedback_mode.add(message.from_user.id)
    await message.answer(
        "Thanks for willing to share feedback!\nSend your thoughts in one message and I'll pass it along.",
        reply_markup=feedback_keyboard(),
    )


@dp.message(F.text == "⬅️ Back")
async def back_to_main(message: types.Message):
    user_paused = await is_paused(message.from_user.id)
    add_friend_mode.discard(message.from_user.id)
    remove_friend_mode.discard(message.from_user.id)
    feedback_mode.discard(message.from_user.id)
    delete_account_confirmation.discard(message.from_user.id)
    await message.answer(
        "Back to the main menu.",
        reply_markup=main_keyboard(user_paused),
    )


@dp.message(F.text == "⏸ Pause")
async def pause_handler(message: types.Message):
    """
    Enable pause mode.
    """
    user_id = message.from_user.id
    await set_pause(user_id, True)
    await message.answer(
        "You are now on pause.\n\n"
        "While you’re on pause:\n"
        "• You can’t send links\n"
        "• You won’t receive new links from friends\n\n"
        "Tap ▶️ Resume when you want to come back.",
        reply_markup=main_keyboard(True),
    )


@dp.message(F.text == "▶️ Resume")
async def resume_handler(message: types.Message):
    """
    Disable pause mode and send digest of missed links.
    """
    user_id = message.from_user.id
    await set_pause(user_id, False)

    rows = await get_pending_links_for_user(user_id)

    if not rows:
        await message.answer(
            "Welcome back! 👋\n"
            "You are active again.\n"
            "You did not miss any links while you were on pause.",
            reply_markup=main_keyboard(False),
        )
        return

    from collections import defaultdict

    grouped = defaultdict(list)
    for r in rows:
        dt = r["created_at"]
        date_key = dt.date().isoformat()
        sender_username = r["sender_username"] or "your friend"
        url = r["url"]
        grouped[date_key].append(f"@{sender_username} — {url}")

    parts = []
    for date_key in sorted(grouped.keys()):
        parts.append(f"📅 {date_key}")
        parts.extend(grouped[date_key])
        parts.append("")  # empty line between days

    digest_text = "Here is what you missed while you were on pause:\n\n" + "\n".join(parts)

    await clear_pending_links_for_user(user_id)

    await message.answer(
        "Welcome back! 👋\nYou are active again.",
        reply_markup=main_keyboard(False),
    )
    await message.answer(digest_text)


@dp.message(F.text == "➕ Invite")
async def invite_friends_handler(message: types.Message):
    user_id = message.from_user.id
    add_friend_mode.add(user_id)
    remove_friend_mode.discard(user_id)
    invite_link = f"https://t.me/{BOT_USERNAME}?start={user_id}"
    await message.answer(
        "Send me your friend's Telegram @username.\n"
        "If they have not started Kind Friends yet, here is an invite message you can forward right away:",
        reply_markup=back_only_keyboard(),
    )
    await message.answer(
        "👋 I'm using Kind Friends to share interesting links with friends.\n\n"
        f"I'd love to add you—open this link and press *Start* to join: {invite_link}",
        parse_mode="Markdown",
    )


@dp.message(F.text == "👥 Friends")
async def friends_handler(message: types.Message):
    user_id = message.from_user.id
    if await is_paused(user_id):
        await message.answer(
            "You are currently on pause. Tap ▶️ Resume to manage friends again.",
            reply_markup=main_keyboard(True),
        )
        return

    friend_count = await get_friend_count(user_id)
    friends = await get_friend_usernames(user_id)
    pending_requests = await get_pending_requests_with_usernames_for_requester(user_id)
    max_friends = get_max_friends(user_id)
    header = f"You have {friend_count}/{max_friends} friends.\n\n"

    if friends:
        lines = [f"- @{u}" for u in friends]
        text = header + "Your friends:\n" + "\n".join(lines)
    else:
        text = header + "You don't have any friends connected yet."

    if pending_requests:
        pending_usernames = [display_username(req["username"], "friend") for req in pending_requests]
        pending_lines = [f"- {name}" for name in pending_usernames]
        text += (
            f"\n\n✉️ Pending invitations: {len(pending_requests)}\n"
            + "\n".join(pending_lines)
        )

    text += "\n\nChoose an option below."
    add_friend_mode.discard(user_id)
    remove_friend_mode.discard(user_id)
    await message.answer(text, parse_mode="Markdown", reply_markup=friends_keyboard())


@dp.message(F.text == "➖ Remove")
async def remove_friend_handler(message: types.Message):
    user_id = message.from_user.id
    friends = await get_friend_usernames(user_id)

    if not friends:
        await message.answer(
            "You don't have any friends connected yet.\n"
            "Tap “➕ Invite” to add someone.",
        )
        return

    remove_friend_mode.add(user_id)
    add_friend_mode.discard(user_id)
    await message.answer(
        "You can now delete your friends from the list.\n"
        "Choose one or a few by clicking on the tags below.",
        reply_markup=back_only_keyboard(),
    )
    await message.answer(
        "Tap a friend to remove them:",
        reply_markup=remove_friends_keyboard(friends),
    )


@dp.callback_query(F.data.startswith("remove_friend:"))
async def remove_friend_callback(callback: types.CallbackQuery):
    user_id = callback.from_user.id

    if user_id not in remove_friend_mode:
        await callback.answer("Remove mode is closed. Tap ➖ Remove to start again.", show_alert=True)
        return

    try:
        friend_username_raw = callback.data.split(":", maxsplit=1)[1]
    except (IndexError, ValueError):
        await callback.answer("Invalid remove action.", show_alert=True)
        return

    friend_tg_id = await get_telegram_id_by_username(friend_username_raw)
    if not friend_tg_id:
        await callback.answer("I can't find this friend in Kind Friends.", show_alert=True)
        return

    await remove_friendship(user_id, friend_tg_id)
    await notify_friend_removed(user_id, friend_tg_id)

    await callback.answer("Friend removed.")
    await callback.message.answer(
        f"You are no longer connected with @{friend_username_raw} on Kind Friends.",
        reply_markup=back_only_keyboard(),
    )

    updated_friends = await get_friend_usernames(user_id)
    if updated_friends:
        try:
            await callback.message.edit_reply_markup(
                reply_markup=remove_friends_keyboard(updated_friends)
            )
        except Exception as e:  # noqa: BLE001
            print(f"[WARN] Failed to refresh removal keyboard for {user_id}: {e}")
    else:
        remove_friend_mode.discard(user_id)
        try:
            await callback.message.edit_text(
                "You don't have any friends connected yet.\nTap “➕ Invite” to add someone.",
                reply_markup=None,
            )
        except Exception as e:  # noqa: BLE001
            print(f"[WARN] Failed to update empty removal message for {user_id}: {e}")
        await callback.message.answer(
            "Friend list is empty now. Returning to Friends menu.",
            reply_markup=friends_keyboard(),
        )


@dp.callback_query(F.data.startswith("friend_accept:"))
async def accept_friend_request(callback: types.CallbackQuery):
    try:
        request_id = int(callback.data.split(":", maxsplit=1)[1])
    except (IndexError, ValueError):
        await callback.answer("Invalid request.", show_alert=True)
        return

    request = await get_friend_request_by_id(request_id)
    if not request:
        await callback.answer("This request has already been handled.", show_alert=True)
        return

    if request["recipient_telegram_id"] != callback.from_user.id:
        await callback.answer("This request is for a different user.", show_alert=True)
        return

    requester_id = request["requester_telegram_id"]
    recipient_id = request["recipient_telegram_id"]

    requester_friend_count = await get_friend_count(requester_id)
    recipient_friend_count = await get_friend_count(recipient_id)
    requester_max_friends = get_max_friends(requester_id)
    recipient_max_friends = get_max_friends(recipient_id)

    if requester_friend_count >= requester_max_friends:
        await delete_friend_request(request_id)
        await callback.message.answer(
            "Unfortunately it’s impossible to add this friend because their friend pool is full.",
            reply_markup=friends_keyboard(),
        )
        await notify_user_friend_pool_full_with_pending(requester_id)
        return

    if recipient_friend_count >= recipient_max_friends:
        await delete_friend_request(request_id)
        await callback.message.answer(
            f"You already have {recipient_max_friends} friends. Unfortunately there is a limit because it’s MVP, "
            "and we’ll notify you when it will be possible to add more. For now you can "
            "delete some friends from your list before sending new invitations.",
            reply_markup=friends_keyboard(),
        )
        try:
            await bot.send_message(
                requester_id,
                f"Your invitation could not be accepted because your friend reached the {recipient_max_friends} friend limit.",
            )
        except Exception as e:  # noqa: BLE001
            print(f"[WARN] Failed to notify requester {requester_id} about full pool: {e}")
        return

    await delete_friend_request(request_id)
    await add_mutual_friendship(requester_id, recipient_id)

    requester_username = await get_username_by_telegram_id(requester_id)
    recipient_username = await get_username_by_telegram_id(callback.from_user.id)
    requester_display = display_username(requester_username, "friend")
    recipient_display = display_username(recipient_username, "friend")

    await callback.message.answer(
        f"You confirmed the friend request from {requester_display}. You're now friends."
    )

    try:
        await bot.send_message(
            requester_id,
            f"{recipient_display} accepted your invitation. You're now friends!",
        )
    except Exception as e:
        print(f"[WARN] Failed to notify requester {requester_id}: {e}")

    await notify_user_friend_pool_full_with_pending(requester_id)
    await notify_user_friend_pool_full_with_pending(recipient_id)

    await callback.answer("Friendship confirmed!")


@dp.callback_query(F.data.startswith("friend_decline:"))
async def decline_friend_request(callback: types.CallbackQuery):
    try:
        request_id = int(callback.data.split(":", maxsplit=1)[1])
    except (IndexError, ValueError):
        await callback.answer("Invalid request.", show_alert=True)
        return

    request = await get_friend_request_by_id(request_id)
    if not request:
        await callback.answer("This request has already been handled.", show_alert=True)
        return

    if request["recipient_telegram_id"] != callback.from_user.id:
        await callback.answer("This request is for a different user.", show_alert=True)
        return

    await delete_friend_request(request_id)

    decliner_display = display_username(callback.from_user.username, "user")

    await callback.message.answer("You declined the friend invitation.")

    try:
        await bot.send_message(
            request["requester_telegram_id"],
            f"{decliner_display} declined your invitation. You can send another request later.",
        )
    except Exception as e:
        print(f"[WARN] Failed to notify requester {request['requester_telegram_id']}: {e}")

    await callback.answer("Request declined.")


@dp.callback_query(F.data == "start_feedback")
async def start_feedback(callback: types.CallbackQuery):
    feedback_mode.add(callback.from_user.id)
    await callback.message.answer(
        "Thanks for willing to share feedback!\n"
        "Send your thoughts in one message and I'll pass it along.",
        reply_markup=feedback_keyboard(),
    )
    await callback.answer()


@dp.message(F.text == "/admin")
async def admin_handler(message: types.Message):
    """
    Simple admin stats, available only for ADMIN_ID.
    """
    if message.from_user.id != ADMIN_ID:
        return

    async with pool.acquire() as conn:
        users_count = await conn.fetchval("SELECT COUNT(*) FROM users;")
        paused_count = await conn.fetchval(
            "SELECT COUNT(*) FROM users WHERE is_paused = TRUE;"
        )
        friendships_count = await conn.fetchval("SELECT COUNT(*) FROM friendships;")
        pending_count = await conn.fetchval("SELECT COUNT(*) FROM pending_links;")
        sent_links_total = await conn.fetchval(
            "SELECT COALESCE(SUM(sent_links_count), 0) FROM users;"
        )
        invites_sent_total = await conn.fetchval(
            "SELECT COALESCE(SUM(invites_sent_count), 0) FROM users;"
        )

    text = (
        "📊 **Kind Friends — Admin Panel**\n\n"
        f"👥 Total users: **{users_count}**\n"
        f"⏸ Paused users: **{paused_count}**\n"
        f"🔗 Friend connections: **{friendships_count}**\n"
        f"📥 Pending links (stored for paused): **{pending_count}**\n"
        f"📤 Total links sent attempts: **{sent_links_total}**\n"
        f"✉️ Total invites sent: **{invites_sent_total}**\n"
    )

    await message.answer(text, parse_mode="Markdown")


@dp.message(
    F.text.startswith("/broadcast") | F.caption.startswith("/broadcast")
)
async def broadcast_handler(message: types.Message):
    """
    Broadcast a text message to all registered users, optionally with a photo.
    Only the configured ADMIN_ID can use this command.
    """

    if message.from_user.id != ADMIN_ID:
        return

    command_payload = (message.text or message.caption or "").split(maxsplit=1)
    if len(command_payload) < 2 or not command_payload[1].strip():
        await message.answer(
            "Usage: /broadcast <message to send to all users>.\n"
            "You can attach an image to send it with the message.",
        )
        return

    broadcast_text = command_payload[1].strip()
    photo_id = message.photo[-1].file_id if message.photo else None
    user_ids = await get_all_user_ids()

    delivered = 0
    failed = 0

    for user_id in user_ids:
        try:
            if photo_id:
                await bot.send_photo(user_id, photo_id, caption=broadcast_text)
            else:
                await bot.send_message(user_id, broadcast_text)
            delivered += 1
        except Exception as e:  # noqa: BLE001
            print(f"[WARN] Failed to broadcast to {user_id}: {e}")
            failed += 1

    await message.answer(
        f"Broadcast complete. Delivered: {delivered}. Failed: {failed}.",
    )


@dp.message(F.text == "/wipe_me")
async def wipe_me_handler(message: types.Message):
    """
    Completely delete current user from DB.
    Available for any user (self-delete).
    """
    user_id = message.from_user.id
    await delete_user_completely(user_id)
    await message.answer(
        "All your Kind Friends data has been deleted ✅\n\n"
        "You’re starting from scratch—just like you’ve never used Kind Friends before."
    )


@dp.message()
async def generic_handler(message: types.Message):
    """
    Generic handler:
    - add friend by @username (with invite fallback)
    - remove friend via -@username
    - send links to friends or store for paused friends
    - fallback text
    """
    user_id = message.from_user.id
    text = message.text or ""
    user_paused = await is_paused(user_id)

    async with LoadingIndicator(message):
        # 0) Feedback mode
        if user_id in feedback_mode:
            if message.content_type != types.ContentType.TEXT:
                await message.answer(
                    "I couldn't deliver your feedback because it included media. "
                    "Please send your feedback as plain text without any photos, videos, or other attachments so I can pass it along.",
                    reply_markup=feedback_keyboard(),
                )
                return

            feedback_mode.discard(user_id)
            delete_account_confirmation.discard(user_id)
            delivered = await deliver_feedback_to_admin(message.from_user, text)
            if not delivered:
                await message.answer(
                    "I couldn't deliver your feedback because the admin destination isn't configured yet.",
                    reply_markup=help_keyboard() if not user_paused else main_keyboard(True),
                )
                return

            await message.answer(
                "Thanks! I delivered your feedback to the admin.",
                reply_markup=help_keyboard() if not user_paused else main_keyboard(True),
            )
            return

        # 0.1) Delete confirmation
        if user_id in delete_account_confirmation:
            delete_account_confirmation.discard(user_id)
            if text == "Yes, wipe":
                await delete_user_completely(user_id)
                await message.answer(
                    "All your Kind Friends data has been deleted ✅\n\n"
                    "You’re starting from scratch—just like you’ve never used Kind Friends before.",
                    reply_markup=ReplyKeyboardMarkup(
                        keyboard=[[KeyboardButton(text="Let’s start again")]], resize_keyboard=True
                    ),
                )
                return
            else:
                await message.answer(
                    "I kept your account. You can continue using Kind Friends.",
                    reply_markup=help_keyboard() if not user_paused else main_keyboard(True),
                )
                return

        # 1) Add friend mode
        if user_id in add_friend_mode and text.startswith("@"):
            add_friend_mode.discard(user_id)
            friend_username_raw = text.strip()

            friend_count = await get_friend_count(user_id)
            max_friends = get_max_friends(user_id)
            if friend_count >= max_friends:
                await message.answer(
                    f"You already have {max_friends} friends. Unfortunately there is a limit because it’s MVP, "
                    "and we’ll notify you when it will be possible to add more. For now you can "
                    "delete some friends from your list before sending new invitations.",
                    reply_markup=friends_keyboard(),
                )
                return

            friend_tg_id = await get_telegram_id_by_username(friend_username_raw)
            clean_username = friend_username_raw.lstrip("@")

            if not friend_tg_id:
                await increment_invites_sent(user_id)
                invite_link = f"https://t.me/{BOT_USERNAME}?start={user_id}"
                await message.answer(
                    "I can't find this user in **Kind Friends** yet.\n\n"
                    "I'll send you an invite message next that you can forward to them.",
                )
                await message.answer(
                    "👋 I'm using Kind Friends to share interesting links with friends.\n\n"
                    f"I'd love to add you—open this link and press *Start* to join: {invite_link}",
                    parse_mode="Markdown",
                    reply_markup=friends_keyboard(),
                )
                return

            if friend_tg_id == user_id:
                await message.answer(
                    "You cannot add yourself 🙂",
                    reply_markup=friends_keyboard(),
                )
                return

            existing_friends = await get_all_friend_ids(user_id)
            if friend_tg_id in existing_friends:
                await message.answer(
                    f"You are already connected with @{clean_username} in Kind Friends.",
                    reply_markup=friends_keyboard(),
                )
                return

            friend_friend_count = await get_friend_count(friend_tg_id)
            friend_limit_for_friend = get_max_friends(friend_tg_id)
            if friend_friend_count >= friend_limit_for_friend:
                await message.answer(
                    "Unfortunately it’s not possible to add this friend because their friend pool is full.",
                    reply_markup=friends_keyboard(),
                )
                return

            outgoing_request = await get_pending_friend_request(user_id, friend_tg_id)
            if outgoing_request:
                await message.answer(
                    "You already sent an invitation. Please wait for your friend to respond.",
                    reply_markup=friends_keyboard(),
                )
                return

            incoming_request = await get_pending_friend_request(friend_tg_id, user_id)
            if incoming_request:
                friend_max_friends = get_max_friends(user_id)
                if friend_count >= friend_max_friends:
                    await delete_friend_request(incoming_request["id"])
                    await message.answer(
                        f"You already have {friend_max_friends} friends. Unfortunately there is a limit because it’s MVP, "
                        "and we’ll notify you when it will be possible to add more. For now you can "
                        "delete some friends from your list before sending new invitations.",
                        reply_markup=friends_keyboard(),
                    )
                    return

                if friend_friend_count >= friend_limit_for_friend:
                    await delete_friend_request(incoming_request["id"])
                    await message.answer(
                        "Unfortunately it’s impossible to add this friend because their friend pool is full.",
                        reply_markup=friends_keyboard(),
                    )
                    try:
                        await bot.send_message(
                            friend_tg_id,
                            "Someone tried to accept your invitation, but your friend pool is full. "
                            "Please remove a friend and send a new invitation.",
                        )
                    except Exception as e:
                        print(f"[WARN] Failed to notify friend {friend_tg_id}: {e}")
                    return

                await delete_friend_request(incoming_request["id"])
                await add_mutual_friendship(user_id, friend_tg_id)

                friend_username = await get_username_by_telegram_id(friend_tg_id)
                friend_display = display_username(friend_username or clean_username, "friend")
                user_display = display_username(message.from_user.username, "friend")

                await message.answer(
                    f"You confirmed the request from {friend_display}. You're now friends.",
                    reply_markup=friends_keyboard(),
                )
                try:
                    await bot.send_message(
                        friend_tg_id,
                        f"{user_display} accepted your request. You're now friends!",
                    )
                except Exception as e:
                    print(f"[WARN] Failed to notify friend {friend_tg_id}: {e}")
                await notify_user_friend_pool_full_with_pending(user_id)
                await notify_user_friend_pool_full_with_pending(friend_tg_id)
                return

            request_id = await create_friend_request(user_id, friend_tg_id)
            if not request_id:
                await message.answer(
                    "I couldn't send the request right now. Please try again in a bit.",
                    reply_markup=friends_keyboard(),
                )
                return

            friend_username = await get_username_by_telegram_id(friend_tg_id)
            friend_display = display_username(friend_username or clean_username, "friend")
            pending_after_request = await count_pending_requests_for_requester(user_id)
            total_connections = friend_count + pending_after_request
            extra_hint = (
                f"\n\nYou have already sent more than {max_friends} invitations in total. Not all of them can be accepted "
                f"because of the current {max_friends} friend limit."
                if total_connections > max_friends
                else ""
            )
            await message.answer(
                f"Request sent to {friend_display}. Waiting for confirmation.{extra_hint}",
                reply_markup=friends_keyboard(),
            )

            requester_display = display_username(message.from_user.username, "User")
            confirmation_keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="✅ Accept",
                            callback_data=f"friend_accept:{request_id}",
                        ),
                        InlineKeyboardButton(
                            text="❌ Decline",
                            callback_data=f"friend_decline:{request_id}",
                        ),
                    ]
                ]
            )

            try:
                await bot.send_message(
                    friend_tg_id,
                    f"{requester_display} wants to add you as a friend on Kind Friends.\n\n"
                    "Ready to confirm?",
                    reply_markup=confirmation_keyboard,
                )
            except Exception as e:
                print(f"[WARN] Failed to deliver friend confirmation to {friend_tg_id}: {e}")
            return

        # 1.5) Remove friend mode
        if user_id in remove_friend_mode:
            remove_friend_mode.discard(user_id)
            friend_username_raw = normalize_username_input(text)
            if not friend_username_raw:
                await message.answer(
                    "Please send a valid @username to remove a friend.",
                    reply_markup=friends_keyboard(),
                )
                return

            friend_tg_id = await get_telegram_id_by_username(friend_username_raw)
            if not friend_tg_id:
                await message.answer(
                    "I can't find this friend in Kind Friends.",
                    reply_markup=friends_keyboard(),
                )
                return

            await remove_friendship(user_id, friend_tg_id)
            await notify_friend_removed(user_id, friend_tg_id)
            await message.answer(
                f"You are no longer connected with @{friend_username_raw} on Kind Friends.",
                reply_markup=friends_keyboard(),
            )
            return

        # 2) Remove friend: -@username
        if text.startswith("-@"):
            friend_username_raw = normalize_username_input(text[2:])
            if not friend_username_raw:
                await message.answer("Please send a valid @username to remove a friend.")
                return
            friend_tg_id = await get_telegram_id_by_username(friend_username_raw)
            if not friend_tg_id:
                await message.answer("I can't find this friend in Kind Friends.")
                return

            await remove_friendship(user_id, friend_tg_id)
            await notify_friend_removed(user_id, friend_tg_id)
            await message.answer(
                f"You are no longer connected with {friend_username_raw} on Kind Friends."
            )
            return

        # 3) Link sending
        if text.startswith("http://") or text.startswith("https://"):
            recent_sent_at = await get_recent_sent_link_timestamp(user_id, text)
            if recent_sent_at:
                retry_after = recent_sent_at + timedelta(days=7)
                now = datetime.now(timezone.utc)
                if now < retry_after:
                    remaining_seconds = (retry_after - now).total_seconds()
                    remaining_days = max(1, math.ceil(remaining_seconds / 86400))
                    await message.answer(
                        "You already shared this link recently.\n"
                        f"Please wait {remaining_days} day(s) before sending it again to avoid spam.",
                    )
                    return

            paused = user_paused
            if paused:
                await message.answer(
                    "You are currently on pause.\n"
                    "Tap ▶️ Resume if you want to send and receive links again."
                )
                return

            daily_sent = await get_daily_sent_links_count(user_id)
            if daily_sent >= MAX_DAILY_LINKS:
                await message.answer(
                    "You have reached the daily limit of 5 links.\n"
                    "0/5 links are left for today.",
                )
                return

            friends_ids = await get_all_friend_ids(user_id)
            sender_username = message.from_user.username or "your friend"
            sent_count = 0
            stored_count = 0

            for fid in friends_ids:
                try:
                    if await is_paused(fid):
                        await save_pending_link(fid, user_id, text)
                        stored_count += 1
                    else:
                        await bot.send_message(
                            fid,
                            f"@{sender_username} shared a link with you:\n{text}",
                        )
                        sent_count += 1
                except Exception as e:
                    print(f"[WARN] Failed to deliver link to {fid}: {e}")

            # increment personal counter
            new_daily_total = await increment_sent_links(user_id)
            await record_sent_link(user_id, text)

            remaining_links = max(0, MAX_DAILY_LINKS - new_daily_total)
            remaining_text = f"{remaining_links}/{MAX_DAILY_LINKS} links are left for today."

            if sent_count == 0 and stored_count == 0:
                await message.answer(
                    "Got your link ✅\n"
                    "Right now you don't have any friends to send it to.\n"
                    f"{remaining_text}"
                )
            else:
                parts = ["Got your link ✅"]
                if sent_count:
                    parts.append(f"Sent to {sent_count} active friend(s).")
                if stored_count:
                    parts.append(f"Saved for {stored_count} friend(s) who are on pause.")
                parts.append(remaining_text)
                await message.answer("\n".join(parts))
            return

        # 4) Fallback
        await message.answer(
            "Use the buttons to open Friends or Help, or paste a link to share it with friends.",
            reply_markup=main_keyboard(user_paused),
        )


async def on_startup(app):
    """Initialize database and set webhook."""

    print("Connecting to database...")
    await init_db()

    webhook_url = os.getenv("WEBHOOK_URL")
    if not webhook_url:
        raise RuntimeError("WEBHOOK_URL is not set")

    await bot.set_webhook(webhook_url, drop_pending_updates=True)
    print(f"Webhook set to {webhook_url}")


async def on_shutdown(app):
    await bot.session.close()
    print("Kind Friends bot stopped.")


def main():
    port = int(os.getenv("PORT", "10000"))

    app = web.Application()

    async def health(request: web.Request):
        return web.Response(text="OK")

    app.router.add_get("/", health)

    webhook_handler = SimpleRequestHandler(dispatcher=dp, bot=bot)
    webhook_handler.register(app, path="/webhook")

    setup_application(app, dp, bot=bot)

    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)

    print(f"Aiohttp server running on 0.0.0.0:{port}")
    web.run_app(app, host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
