import os
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
WEBHOOK_URL = os.getenv("WEBHOOK_URL")  

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is not set")

if not WEBHOOK_URL:
    raise RuntimeError("WEBHOOK_URL is not set")


bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# Global DB connection pool and simple in-memory state
pool = None
add_friend_mode = set()  # set of user_ids who are currently adding a friend


# -------------------------------------------------------------------
# DATABASE HELPERS
# -------------------------------------------------------------------

async def init_db():
    """
    Create a connection pool and ensure tables exist.
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
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            """
        )
        # Friendships table (store relations by telegram_id)
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


async def get_or_create_user(tg_id, username):
    """
    Return pause state for user, creating them if needed.
    Username is updated each time.
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


async def set_pause(tg_id, paused):
    """
    Set pause state for user.
    """
    async with pool.acquire() as conn:
        # Ensure user exists
        row = await conn.fetchrow(
            "SELECT id FROM users WHERE telegram_id=$1",
            tg_id,
        )
        if not row:
            await conn.execute(
                "INSERT INTO users (telegram_id, is_paused) VALUES ($1, $2)",
                tg_id,
                paused,
            )
        else:
            await conn.execute(
                "UPDATE users SET is_paused=$1 WHERE telegram_id=$2",
                paused,
                tg_id,
            )


async def is_paused(tg_id):
    """
    Check if user is paused.
    """
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT is_paused FROM users WHERE telegram_id=$1",
            tg_id,
        )
    return row["is_paused"] if row else False


async def get_telegram_id_by_username(username):
    """
    Find a user by username (case-insensitive).
    Username is stored without @ in Telegram, so we strip it.
    """
    username = username.lstrip("@").strip().lower()
    if not username:
        return None

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT telegram_id FROM users WHERE LOWER(username)=$1",
            username,
        )
    return row["telegram_id"] if row else None


async def add_mutual_friendship(a_tg_id, b_tg_id):
    """
    Create mutual friendship between two users (A<->B).
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


async def get_friend_usernames(tg_id):
    """
    Return a list of usernames for all friends of a user.
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


async def get_active_friend_ids(tg_id):
    """
    Return telegram_ids of all friends who are not paused.
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT u.telegram_id
            FROM friendships f
            JOIN users u ON u.telegram_id = f.friend_telegram_id
            WHERE f.user_telegram_id = $1
              AND u.is_paused = FALSE;
            """,
            tg_id,
        )
    return [r["telegram_id"] for r in rows]


async def remove_friendship(a_tg_id, b_tg_id):
    """
    Remove friendship in both directions (A-B and B-A).
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


# -------------------------------------------------------------------
# KEYBOARD
# -------------------------------------------------------------------

def main_keyboard(paused):
    """
    Main menu keyboard for Kind Friends.
    """
    pause_button = "▶️ Resume" if paused else "⏸ Pause"

    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📤 Send a link")],
            [KeyboardButton(text="➕ Invite friends"), KeyboardButton(text="👥 Friends")],
            [KeyboardButton(text=pause_button)],
            [KeyboardButton(text="ℹ️ Help"), KeyboardButton(text="💡 Feedback")],
        ],
        resize_keyboard=True,
    )


# -------------------------------------------------------------------
# HANDLERS
# -------------------------------------------------------------------

@dp.message(CommandStart())
async def start(message: types.Message):
    """
    /start handler: create or load user and show main menu.
    """
    paused = await get_or_create_user(
        message.from_user.id,
        message.from_user.username,
    )
    await message.answer(
        "Hi! I’m **Kind Friends** 👋\n\n"
        "I help friends share important links with each other.\n\n"
        "• Paste a link here – I will treat it as something you want to share.\n"
        "• Use the buttons below to invite friends, see your list,\n"
        "  pause/resume, or send feedback.\n",
        reply_markup=main_keyboard(paused),
    )


@dp.message(F.text == "ℹ️ Help")
async def help_handler(message: types.Message):
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
    Disable pause mode.
    """
    user_id = message.from_user.id
    await set_pause(user_id, False)
    await message.answer(
        "Welcome back! 👋\n"
        "You are active again.",
        reply_markup=main_keyboard(False),
    )


@dp.message(F.text == "➕ Invite friends")
async def invite_friends_handler(message: types.Message):
    """
    Start 'add friend' mode.
    """
    user_id = message.from_user.id
    add_friend_mode.add(user_id)
    await message.answer(
        "Send me your friend's Telegram @username (for example @username).\n"
        "They need to have started Kind Friends at least once."
    )


@dp.message(F.text == "👥 Friends")
async def friends_handler(message: types.Message):
    """
    Show list of friends and how to remove them.
    """
    user_id = message.from_user.id
    friends = await get_friend_usernames(user_id)

    if not friends:
        await message.answer(
            "You don't have any friends connected yet.\n"
            "Tap “➕ Invite friends” to add someone."
        )
        return

    lines = [f"- @{u}" for u in friends]
    text = (
        "Your friends:\n" + "\n".join(lines) +
        "\n\nTo remove a friend, send their @username starting with a minus, for example:\n"
        "`-@username`"
    )
    await message.answer(text, parse_mode="Markdown")


@dp.message(F.text == "💡 Feedback")
async def feedback_handler(message: types.Message):
    await message.answer(
        "Please type your feedback or ideas in one message and send it here.\n\n"
        "(In this minimal version it will not be saved yet,\n"
        "but later it will be forwarded to the creator.)"
    )


@dp.message()
async def generic_handler(message: types.Message):
    """
    Handles:
    - add friend by @username (when in add_friend_mode)
    - remove friend by '-@username'
    - direct link messages
    - everything else
    """
    user_id = message.from_user.id
    text = message.text or ""

    # 1) If user is in "add friend" mode
    if user_id in add_friend_mode and text.startswith("@"):
        add_friend_mode.discard(user_id)
        friend_username_raw = text.strip()
        friend_username = friend_username_raw.lstrip("@")

        friend_tg_id = await get_telegram_id_by_username(friend_username_raw)
        if not friend_tg_id:
            await message.answer(
                "I can't find this user in Kind Friends yet.\n"
                "Ask them to start the bot at least once, then try again."
            )
            return

        if friend_tg_id == user_id:
            await message.answer("You cannot add yourself as a friend 🙂")
            return

        await add_mutual_friendship(user_id, friend_tg_id)
        await message.answer(
            f"You are now connected with @{friend_username}. "
            "You can share links with each other."
        )
        return

    # 2) Removing a friend: pattern '-@username'
    if text.startswith("-@"):
        friend_username_raw = text[2:].strip()
        friend_tg_id = await get_telegram_id_by_username(friend_username_raw)

        if not friend_tg_id:
            await message.answer("I can't find this friend in Kind Friends.")
            return

        await remove_friendship(user_id, friend_tg_id)
        await message.answer(
            f"You are no longer connected with @{friend_username_raw.lstrip('@')} on Kind Friends."
        )
        return

    # 3) Sending a link
    if text.startswith("http://") or text.startswith("https://"):
        paused = await is_paused(user_id)
        if paused:
            await message.answer(
                "You are currently on pause.\n"
                "Tap ▶️ Resume if you want to send and receive links again."
            )
            return

        # Find all active friends and send them the link
        friends_ids = await get_active_friend_ids(user_id)
        sender_username = message.from_user.username or "your friend"
        sent_count = 0

        for fid in friends_ids:
            try:
                await bot.send_message(
                    fid,
                    f"@{sender_username} shared a link with you:\n{text}",
                )
                sent_count += 1
            except Exception as e:
                print(f"[WARN] Failed to send link to {fid}: {e}")

        if sent_count == 0:
            await message.answer(
                "Got your link ✅\n"
                "Right now you don't have any active friends to send it to."
            )
        else:
            await message.answer(
                f"Got your link ✅\n"
                f"I sent it to {sent_count} friend(s)."
            )
        return

    # 4) Unknown text
    await message.answer(
        "I mostly understand links and the menu buttons for now.\n"
        "To add a friend, use “➕ Invite friends”.\n"
        "To send a link, just paste it here."
    )


# -------------------------------------------------------------------
# AIOHTTP WEB SERVER FOR WEBHOOK
# -------------------------------------------------------------------

async def handle(request):
    """
    AIOHTTP handler for incoming Telegram webhooks.
    """
    data = await request.json()
    update = types.Update(**data)
    await dp.feed_update(bot, update)
    return web.Response()


async def on_startup(app):
    """
    Called when web app starts.
    - Initialize DB
    - Set Telegram webhook
    """
    await init_db()
    await bot.set_webhook(WEBHOOK_URL)
    print("Webhook is set:", WEBHOOK_URL)


async def on_shutdown(app):
    """
    Called on shutdown: remove webhook.
    """
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
