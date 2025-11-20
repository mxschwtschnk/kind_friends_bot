import os

from aiohttp import web
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.webhook.aiohttp_server import SimpleRequestHandler
import asyncpg

# -------------------------------------------------------------------
# CONFIG
# -------------------------------------------------------------------

BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
BOT_USERNAME = os.getenv("BOT_USERNAME", "KindFriendsBot")  # without @
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))  # your Telegram ID
FEEDBACK_BOT_TOKEN = os.getenv("FEEDBACK_BOT_TOKEN")
FEEDBACK_RECIPIENT_CHAT_ID = int(os.getenv("FEEDBACK_RECIPIENT_CHAT_ID", "0"))
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL")

if not WEBHOOK_URL and RENDER_EXTERNAL_URL:
    WEBHOOK_URL = RENDER_EXTERNAL_URL.rstrip("/") + "/webhook"

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is not set")
if not WEBHOOK_URL:
    raise RuntimeError("WEBHOOK_URL is not set and RENDER_EXTERNAL_URL is missing")

bot = Bot(token=BOT_TOKEN)
feedback_bot = Bot(token=FEEDBACK_BOT_TOKEN) if FEEDBACK_BOT_TOKEN else None
dp = Dispatcher()

pool: asyncpg.Pool | None = None
add_friend_mode = set()  # user_ids who are adding a friend
remove_friend_mode = set()  # user_ids who are removing a friend
feedback_mode = set()  # user_ids who are sending feedback


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


async def increment_sent_links(tg_id: int):
    """
    Increase per-user counter of sent links.
    """
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE users
            SET sent_links_count = sent_links_count + 1
            WHERE telegram_id = $1;
            """,
            tg_id,
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

def main_keyboard(paused: bool) -> ReplyKeyboardMarkup:
    pause_button = "▶️ Resume" if paused else "⏸ Pause"
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="➕ Invite friends"), KeyboardButton(text="👥 Friends")],
            [KeyboardButton(text="➖ Remove friend")],
            [KeyboardButton(text=pause_button)],
            [KeyboardButton(text="ℹ️ Help")],
        ],
        resize_keyboard=True,
    )


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
        "• Paste a link here – I will treat it as something you want to share.\n"
        "• Use the buttons below to invite friends, see your list,\n"
        "  manage friends, or pause/resume.\n"
        + invite_note,
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
        "• Use the buttons to invite friends, manage your list, or pause/resume.\n\n"
        "Want to share feedback or ideas? Tap the button below.",
    )
    await message.answer(
        text,
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="💡 Send feedback", callback_data="start_feedback")]]
        ),
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


@dp.message(F.text == "➕ Invite friends")
async def invite_friends_handler(message: types.Message):
    user_id = message.from_user.id
    add_friend_mode.add(user_id)
    remove_friend_mode.discard(user_id)
    await message.answer(
        "Send me your friend's Telegram @username (for example @username).\n"
        "If they have not started Kind Friends yet, I will give you an invite link."
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
        "\n\nTo remove someone, tap “➖ Remove friend” and choose who to disconnect."
    )
    await message.answer(text, parse_mode="Markdown")


@dp.message(F.text == "➖ Remove friend")
async def remove_friend_handler(message: types.Message):
    user_id = message.from_user.id
    friends = await get_friend_usernames(user_id)

    if not friends:
        await message.answer(
            "You don't have any friends connected yet.\n"
            "Tap “➕ Invite friends” to add someone."
        )
        return

    remove_friend_mode.add(user_id)
    add_friend_mode.discard(user_id)
    await message.answer(
        "Send me the @username of the friend you want to remove.\n"
        "You can copy it from the list in “👥 Friends”."
    )


@dp.callback_query(F.data == "start_feedback")
async def start_feedback(callback: types.CallbackQuery):
    feedback_mode.add(callback.from_user.id)
    await callback.message.answer(
        "Thanks for willing to share feedback!\n"
        "Send your thoughts in one message and I'll pass it along."
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

    text = (
        "📊 **Kind Friends — Admin Panel**\n\n"
        f"👥 Total users: **{users_count}**\n"
        f"⏸ Paused users: **{paused_count}**\n"
        f"🔗 Friend connections: **{friendships_count}**\n"
        f"📥 Pending links (stored for paused): **{pending_count}**\n"
        f"📤 Total links sent attempts: **{sent_links_total}**\n"
    )

    await message.answer(text, parse_mode="Markdown")


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
        "If you send /start again, I will treat you as a completely new user."
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

    # 0) Feedback mode
    if user_id in feedback_mode:
        feedback_mode.discard(user_id)
        if not feedback_bot or not FEEDBACK_RECIPIENT_CHAT_ID:
            await message.answer(
                "I couldn't deliver your feedback because the feedback bot isn't configured yet."
            )
            return

        try:
            await feedback_bot.send_message(
                FEEDBACK_RECIPIENT_CHAT_ID,
                (
                    "💡 New feedback\n"
                    f"From: @{message.from_user.username or 'unknown'} (ID: {user_id})\n\n"
                    f"{text}"
                ),
            )
            await message.answer("Thanks! I sent your feedback to the creator 💌")
        except Exception as e:
            print(f"[WARN] Failed to forward feedback: {e}")
            await message.answer("Sorry, I couldn't deliver your feedback right now.")
        return

    # 1) Add friend mode
    if user_id in add_friend_mode and text.startswith("@"):
        add_friend_mode.discard(user_id)
        friend_username_raw = text.strip()

        friend_tg_id = await get_telegram_id_by_username(friend_username_raw)
        clean_username = friend_username_raw.lstrip("@")

        if not friend_tg_id:
            invite_link = f"https://t.me/{BOT_USERNAME}?start={user_id}"
            await message.answer(
                "I can't find this user in **Kind Friends** yet.\n\n"
                "Send them this invite link and ask them to press Start:\n"
                f"{invite_link}"
            )
            return

        if friend_tg_id == user_id:
            await message.answer("You cannot add yourself 🙂")
            return

        await add_mutual_friendship(user_id, friend_tg_id)

        # Notify friend (best-effort)
        try:
            await bot.send_message(
                friend_tg_id,
                f"@{message.from_user.username} added you as a friend on **Kind Friends** 🎉",
                parse_mode="Markdown",
            )
        except Exception as e:
            print(f"[WARN] Failed to notify friend {friend_tg_id}: {e}")

        await message.answer(
            f"You are now connected with @{clean_username}. "
            "You can share links with each other."
        )
        return

    # 1.5) Remove friend mode
    if user_id in remove_friend_mode:
        remove_friend_mode.discard(user_id)
        friend_username_raw = text.lstrip("@").strip()
        if not friend_username_raw:
            await message.answer("Please send a valid @username to remove a friend.")
            return

        friend_tg_id = await get_telegram_id_by_username(friend_username_raw)
        if not friend_tg_id:
            await message.answer("I can't find this friend in Kind Friends.")
            return

        await remove_friendship(user_id, friend_tg_id)
        await message.answer(
            f"You are no longer connected with @{friend_username_raw} on Kind Friends."
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
        await increment_sent_links(user_id)

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
# AIOHTTP APP + WEBHOOK
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
    - set webhook
    """
    await init_db()
    await bot.delete_webhook(drop_pending_updates=True)
    await bot.set_webhook(WEBHOOK_URL)
    print(f"Kind Friends webhook set to {WEBHOOK_URL}")


async def on_shutdown(app):
    """
    On shutdown:
    - remove webhook
    - close bot session
    """
    await bot.delete_webhook()
    await bot.session.close()
    if feedback_bot:
        await feedback_bot.session.close()
    print("Kind Friends bot stopped.")


def main():
    app = web.Application()
    app.router.add_get("/", healthcheck)

    webhook_handler = SimpleRequestHandler(dp, bot)
    webhook_handler.register(app, path="/webhook")

    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)

    port = int(os.getenv("PORT", 10000))
    web.run_app(app, host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
