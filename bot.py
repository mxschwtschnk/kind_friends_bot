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

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is not set")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# Global DB pool and simple in-memory state
pool: asyncpg.Pool | None = None
add_friend_mode = set()  # user_ids who are adding a friend


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
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
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


# -------------------------------------------------------------------
# UI
# -------------------------------------------------------------------

def main_keyboard(paused: bool) -> ReplyKeyboardMarkup:
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
async def cmd_start(message: types.Message):
    """
    /start: ensure user exists and show main menu.
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

    # Group links by date
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

    # Clear inbox after sending digest
    await clear_pending_links_for_user(user_id)

    await message.answer(
        "Welcome back! 👋\nYou are active again.",
        reply_markup=main_keyboard(False),
    )
    await message.answer(digest_text)


@dp.message(F.text == "➕ Invite friends")
async def invite_friends_handler(message: types.Message):
    user_id = message.from_user.id
    add_friend_mode.add(user_id)
    await message.answer(
        "Send me your friend's Telegram @username (for example @username).\n"
        "They need to have started Kind Friends at least once."
    )


@dp.message(F.text == "👥 Friends")
async def friends_handler(message: types.Message):
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
    Generic handler:
    - add friend by @username
    - remove friend via -@username
    - send links to friends or store for paused friends
    - fallback text
    """
    user_id = message.from_user.id
    text = message.text or ""

    # 1) Add friend mode
    if user_id in add_friend_mode and text.startswith("@"):
    add_friend_mode.discard(user_id)
    friend_username_raw = text.strip().lstrip("@").lower()

    # Try to find friend in DB
    friend_tg_id = await get_telegram_id_by_username(friend_username_raw)

    if not friend_tg_id:
        # friend not in Kind Friends yet → give invite link
        invite_link = f"https://t.me/{BOT_USERNAME}?start={user_id}"
        await message.answer(
            "Your friend hasn't started **Kind Friends** yet.\n\n"
            "Send them this link and ask them to press Start:\n"
            f"{invite_link}"
        )
        return

    if friend_tg_id == user_id:
        await message.answer("You cannot add yourself 🙂")
        return

    await add_mutual_friendship(user_id, friend_tg_id)

    # Notify friend (if possible)
    try:
        await bot.send_message(
            friend_tg_id,
            f"@{message.from_user.username} added you as a friend on **Kind Friends** 🎉"
        )
    except:
        pass

    await message.answer(
        f"You are now connected with @{friend_username_raw}. "
        "You can share links with each other."
    )
    return


    # 2) Remove friend: -@username
    if text.startswith("-@"):
        friend_username_raw = text[2:].strip()
        friend_tg_id = await get_telegram_id_by_username(friend_username_raw)
        if not friend_tg_id:
            await message.answer("I can't find this friend in Kind Friends.")
            return

        await remove_friendship(user_id, friend_tg_id)
        await message.answer(
            f"You are no longer connected with {friend_username_raw} on Kind Friends."
        )
        return

    # 3) Link sending
    if text.startswith("http://") or text.startswith("https://"):
        # If sender is paused — do not send
        paused = await is_paused(user_id)
        if paused:
            await message.answer(
                "You are currently on pause.\n"
                "Tap ▶️ Resume if you want to send and receive links again."
            )
            return

        friends_ids = await get_all_friend_ids(user_id)
        sender_username = message.from_user.username or "your friend"
        sent_count = 0
        stored_count = 0

        for fid in friends_ids:
            try:
                if await is_paused(fid):
                    # friend is paused → store link
                    await save_pending_link(fid, user_id, text)
                    stored_count += 1
                else:
                    # friend is active → send now
                    await bot.send_message(
                        fid,
                        f"@{sender_username} shared a link with you:\n{text}",
                    )
                    sent_count += 1
            except Exception as e:
                print(f"[WARN] Failed to deliver link to {fid}: {e}")

        if sent_count == 0 and stored_count == 0:
            await message.answer(
                "Got your link ✅\n"
                "Right now you don't have any friends to send it to."
            )
        else:
            parts = ["Got your link ✅"]
            if sent_count:
                parts.append(f"Sent to {sent_count} active friend(s).")
            if stored_count:
                parts.append(f"Saved for {stored_count} friend(s) who are on pause.")
            await message.answer("\n".join(parts))
        return

    # 4) Fallback
    await message.answer(
        "I mostly understand links and the menu buttons for now.\n"
        "To add a friend, use “➕ Invite friends”.\n"
        "To send a link, just paste it here."
    )


# -------------------------------------------------------------------
# AIOHTTP APP + LONG POLLING
# -------------------------------------------------------------------

async def healthcheck(request):
    """
    Simple endpoint so Render sees an open port.
    """
    return web.Response(text="OK")


async def on_startup(app):
    """
    On startup:
    - init DB
    - delete webhook (if any)
    - start long polling in background
    """
    await init_db()
    await bot.delete_webhook(drop_pending_updates=True)
    app["bot_task"] = asyncio.create_task(dp.start_polling(bot))
    print("Kind Friends bot polling started.")


async def on_shutdown(app):
    """
    On shutdown:
    - cancel polling
    - close bot session
    """
    bot_task = app.get("bot_task")
    if bot_task:
        bot_task.cancel()
    await bot.session.close()
    print("Kind Friends bot stopped.")


def main():
    app = web.Application()
    app.router.add_get("/", healthcheck)

    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)

    port = int(os.getenv("PORT", 10000))
    web.run_app(app, host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
