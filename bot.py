import os
import re
import json
from datetime import datetime, timedelta, timezone
import math

import asyncio
import aiohttp
from aiohttp import web
import asyncpg

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

from kind_friends.config import load_settings
from kind_friends.repositories import Database
from kind_friends.services.friend_service import (
    FriendInviteDecision,
    FriendService,
    display_username,
    get_max_friends,
    normalize_username_input,
)
from admin_commands import register_admin_handlers

settings = load_settings()

BOT_USERNAME = settings.bot_username  # without @
MAX_DAILY_LINKS = settings.max_daily_links
ADMIN_ID = settings.admin_id
FEEDBACK_RECIPIENT_CHAT_ID = settings.feedback_recipient_chat_id

bot = Bot(token=settings.bot_token)
dp = Dispatcher()

database = Database(settings.database_url)
friend_service = FriendService(database, settings)

pool = database.pool

get_or_create_user = database.get_or_create_user
set_pause = database.set_pause
is_paused = database.is_paused
get_telegram_id_by_username = database.get_telegram_id_by_username
add_mutual_friendship = database.add_mutual_friendship
get_friend_usernames = database.get_friend_usernames
get_friend_count = database.get_friend_count
get_all_friend_ids = database.get_all_friend_ids
remove_friendship = database.remove_friendship
count_pending_requests_for_requester = database.count_pending_requests_for_requester
get_pending_requests_for_requester = database.get_pending_requests_for_requester
get_pending_requests_with_usernames_for_requester = (
    database.get_pending_requests_with_usernames_for_requester
)
get_username_by_telegram_id = database.get_username_by_telegram_id
get_all_user_ids = database.get_all_user_ids
get_pending_friend_request = database.get_pending_friend_request
create_friend_request = database.create_friend_request
get_friend_request_by_id = database.get_friend_request_by_id
delete_friend_request = database.delete_friend_request
delete_friend_requests_for_user = database.delete_friend_requests_for_user
save_pending_link = database.save_pending_link
get_pending_links = database.get_pending_links
delete_pending_link = database.delete_pending_link
delete_pending_links_for_user = database.delete_pending_links_for_user
increment_sent_links = database.increment_sent_links
get_sent_link_counts = database.get_sent_link_counts
save_sent_link_history = database.save_sent_link_history
has_recently_sent_link = database.has_recently_sent_link
delete_sent_link_history_for_user = database.delete_sent_link_history_for_user
delete_user_completely = database.delete_user_completely
increment_invite_count = database.increment_invite_count
get_invite_count = database.get_invite_count
clear_sent_links = database.clear_sent_links
bulk_delete_users = database.bulk_delete_users
add_wishlist_item = database.add_wishlist_item
get_wishlist_items = database.get_wishlist_items
get_wishlist_item = database.get_wishlist_item
delete_wishlist_item = database.delete_wishlist_item
add_friend_mode = set()  # user_ids who are adding a friend
remove_friend_mode = set()  # user_ids who are removing a friend
feedback_mode = set()  # user_ids who are sending feedback
delete_account_confirmation = set()  # user_ids awaiting delete confirmation
wishlist_add_mode = set()  # user_ids adding wishlist entries
pending_link_actions: dict[int, str] = {}  # last link sent by user waiting for action


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
    await database.connect()
    pool = database.pool


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
    max_friends = get_max_friends(settings, tg_id)
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
            [KeyboardButton(text="👥 Friends"), KeyboardButton(text="📜 Wishlist")],
            [KeyboardButton(text="▶️ Resume"), KeyboardButton(text="ℹ️ Help")],
        ]
    else:
        keyboard = [
            [KeyboardButton(text="👥 Friends"), KeyboardButton(text="📜 Wishlist")],
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


def wishlist_keyboard() -> ReplyKeyboardMarkup:
    return back_only_keyboard()


def link_action_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Add to 🎁", callback_data="link_action:wishlist"
                ),
                InlineKeyboardButton(text="Send to 👥", callback_data="link_action:send"),
            ]
        ]
    )


def friend_list_keyboard(friends: list[str]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=f"@{username}", callback_data=f"friend_card:{username}")]
        for username in friends
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def friend_options_keyboard(username: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🗑 Remove", callback_data=f"friend_remove:{username}")],
            [InlineKeyboardButton(text="📜 Wishlist", callback_data=f"friend_wishlist:{username}")],
        ]
    )


def _shorten_url(url: str, max_length: int = 32) -> str:
    trimmed = url.removeprefix("https://").removeprefix("http://")
    if len(trimmed) <= max_length:
        return trimmed
    return trimmed[: max_length - 1] + "…"


async def _fetch_title_for_url(session, url: str) -> str | None:
    try:
        async with session.get(
            url,
            timeout=10,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
                ),
                "Accept": (
                    "text/html,application/xhtml+xml,application/xml;"
                    "q=0.9,image/avif,image/webp,*/*;q=0.8"
                ),
                "Accept-Language": "en-US,en;q=0.9",
            },
        ) as response:
            if response.status >= 400:
                return None
            content_type = response.headers.get("content-type", "")
            if "text/html" not in content_type:
                return None
            raw_html = await response.text(errors="ignore")
    except Exception as e:  # noqa: BLE001
        print(f"[WARN] Failed to fetch metadata for {url}: {e}")
        return None

    html_slice = raw_html[:200000]

    def _clean_title(title: str | None) -> str | None:
        if not title:
            return None

        # Prefer the most descriptive portion (usually the product) when stores include
        # their brand in the page title. Choose the longest non-empty chunk to avoid
        # returning only the shop name (e.g., "Store | Product" -> "Product").
        parts = re.split(r"\s+[\-|–|—|:]\s+|\s*\|\s*|:\s*", title)
        parts = [p.strip() for p in parts if p and p.strip()]

        # Filter out chunks that look like domain names or store names so that we keep
        # the actual product title when possible (e.g., drop "Amazon.de" and keep the
        # product description).
        store_pattern = re.compile(
            r"\b(amazon|etsy|ebay|walmart|aliexpress|target|zalando|shein|bestbuy|rakuten|mercado)\b",
            re.IGNORECASE,
        )
        domain_pattern = re.compile(r"\b[a-z0-9-]+\.[a-z]{2,6}\b", re.IGNORECASE)
        descriptive_parts = [
            p for p in parts if not store_pattern.search(p) and not domain_pattern.search(p)
        ]

        if descriptive_parts:
            parts = descriptive_parts

        if not parts:
            return title.strip()

        best = max(parts, key=len).strip()

        # If the best part still looks like just a store or domain (e.g., "Amazon.de"),
        # skip it so that later fallbacks (headings or URL slug) can provide a more
        # product-like label.
        if store_pattern.search(best) or domain_pattern.search(best):
            return None

        # Skip very short leftovers such as "Home" or "Shop" that are unlikely to be
        # the product name.
        if len(best) < 4:
            return None

        return best

    def _extract_product_from_ld_json():
        scripts = re.findall(
            r"<script[^>]+type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>",
            html_slice,
            flags=re.IGNORECASE | re.DOTALL,
        )

        def _normalize_image(img_val):
            if isinstance(img_val, list):
                if not img_val:
                    return None
                first = img_val[0]
                if isinstance(first, dict):
                    return first.get("url") or first.get("@id")
                return first
            if isinstance(img_val, dict):
                return img_val.get("url") or img_val.get("@id")
            return img_val

        def _walk(data):
            best: dict[str, str | None] | None = None

            def _maybe_update(candidate: dict[str, str | None]):
                nonlocal best
                if not candidate or not candidate.get("title"):
                    return
                if not best or len(candidate["title"] or "") > len(best.get("title") or ""):
                    best = candidate

            if isinstance(data, list):
                for item in data:
                    found = _walk(item)
                    if found:
                        _maybe_update(found)
            elif isinstance(data, dict):
                type_field = data.get("@type")
                types = [type_field] if isinstance(type_field, str) else type_field or []
                if any(isinstance(t, str) and t.lower() == "product" for t in types):
                    name = data.get("name") or data.get("headline")
                    _maybe_update({"title": name})

                if any(isinstance(t, str) and t.lower() == "offer" for t in types):
                    item_offered = data.get("itemOffered")
                    if isinstance(item_offered, dict):
                        name = item_offered.get("name") or item_offered.get("headline")
                        _maybe_update({"title": name})
                for value in data.values():
                    found = _walk(value)
                    if found:
                        _maybe_update(found)
            return best

        for script in scripts:
            try:
                parsed = json.loads(script.strip())
            except Exception:
                continue
            found = _walk(parsed)
            if found:
                return found
        return None

    def _extract_attr(tag: str, attr: str) -> str | None:
        match = re.search(rf'{attr}\s*=\s*["\']([^"\']+)["\']', tag, re.IGNORECASE)
        return match.group(1).strip() if match else None

    meta_tags = re.findall(r"<meta[^>]*>", html_slice, flags=re.IGNORECASE)
    meta_map: dict[str, str] = {}
    for tag in meta_tags:
        name = (
            _extract_attr(tag, "property")
            or _extract_attr(tag, "name")
            or _extract_attr(tag, "itemprop")
        )
        content = _extract_attr(tag, "content")
        if name and content and name.lower() not in meta_map:
            meta_map[name.lower()] = content.strip()

    def _search_img_tags():
        images = []
        for tag in re.findall(r"<img[^>]+>", html_slice, flags=re.IGNORECASE):
            src = (
                _extract_attr(tag, "src")
                or _extract_attr(tag, "data-src")
                or _extract_attr(tag, "data-original")
            )
            if not src:
                continue
            width = _extract_attr(tag, "width")
            height = _extract_attr(tag, "height")
            score = 0
            try:
                if width:
                    score += int(width)
                if height:
                    score += int(height)
            except ValueError:
                pass
            if re.search(r"1200|1600|2048|_UX|_SL", src):
                score += 500
            images.append((score, src))
        if not images:
            return None
        images.sort(key=lambda x: x[0], reverse=True)
        return images[0][1]

    def _search_meta(names: tuple[str, ...]):
        for name in names:
            if name.lower() in meta_map:
                return meta_map[name.lower()]
        return None

    def _search_title_tag():
        match = re.search(r"<title[^>]*>(.*?)</title>", html_slice, re.IGNORECASE | re.DOTALL)
        return match.group(1).strip() if match else None

    def _search_heading():
        match = re.search(r"<h1[^>]*>(.*?)</h1>", html_slice, re.IGNORECASE | re.DOTALL)
        return match.group(1).strip() if match else None

    def _slug_from_url():
        try:
            path = re.sub(r"[?#].*$", "", url)
            segments = [s for s in path.split("/") if s and not re.match(r"https?", s, re.I)]
            candidates = []
            for seg in segments[::-1]:
                cleaned = re.sub(r"[-_]+", " ", seg)
                cleaned = re.sub(r"\s+", " ", cleaned).strip()
                if len(cleaned) >= 8 and re.search(r"[A-Za-z]", cleaned):
                    candidates.append(cleaned)
            return candidates[0] if candidates else None
        except Exception:
            return None

    structured = _extract_product_from_ld_json() or {}

    title_candidates = [structured.get("title")]
    title_candidates.append(
        _search_meta(
            (
                "og:title",
                "twitter:title",
                "twitter:text:title",
                "product:title",
                "title",
                "name",
                "itemprop:title",
                "itemprop:name",
            )
        )
    )
    title_candidates.append(_search_title_tag())
    title_candidates.append(_search_heading())
    title_candidates.append(_slug_from_url())

    title = None
    for candidate in title_candidates:
        cleaned = _clean_title(candidate)
        if cleaned:
            title = cleaned
            break

    return title


def wishlist_items_keyboard(items, editable: bool) -> InlineKeyboardMarkup:
    buttons = []
    for item in items:
        label = item.get("title") or _shorten_url(item["url"])
        if editable:
            buttons.append(
                [InlineKeyboardButton(text=label, callback_data=f"wishlist_item:{item['id']}")]
            )
        else:
            buttons.append([InlineKeyboardButton(text=label, url=item["url"])])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def wishlist_item_action_keyboard(item) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🌐 Open item", url=item["url"])],
            [InlineKeyboardButton(text="🗑 Delete", callback_data=f"wishlist_delete:{item['id']}")],
        ]
    )
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
        "Hey 👋 I help friends share their links and content to support each other with likes, shares, and comments.\n\n"
        "• Create your circle of friends (up to 15)\n"
        "• Paste a link here and I will share it with your circle.\n"
        "• Use the buttons to open Friends, Pause/Resume, or get more information in Help.\n\n"
        "*This is an MVP version. Your feedback is very welcome!*\n"
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


async def send_editable_wishlist(message: types.Message, user_id: int):
    wishlist_add_mode.add(user_id)
    items = [dict(item) for item in await get_wishlist_items(user_id)]
    if not items:
        await message.answer(
            "Your wishlist is empty right now. Send me a link to add your first item.",
            reply_markup=wishlist_keyboard(),
        )
        return

    await message.answer(
        "Tap an item to open or delete it. Send another link here to add it to your wishlist.\n\n"
        "Wishlist:",
        reply_markup=wishlist_items_keyboard(items, editable=True),
    )


@dp.message(F.text == "📜 Wishlist")
async def wishlist_menu(message: types.Message):
    await send_editable_wishlist(message, message.from_user.id)


@dp.message(F.text == "➕ Add link")
async def wishlist_add_prompt(message: types.Message):
    user_id = message.from_user.id
    wishlist_add_mode.add(user_id)
    await message.answer(
        "Send me a link to add to your wishlist.",
        reply_markup=wishlist_keyboard(),
    )


@dp.message(F.text == "✏️ Edit")
async def wishlist_edit(message: types.Message):
    await send_editable_wishlist(message, message.from_user.id)


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
    wishlist_add_mode.discard(message.from_user.id)
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
        "👋 I use Kind Friends to share my links with friends so they can support me "
        "with likes and comments whenever I post something new, and so I can support them "
        "when they need it too.\n\n"
        f"I'd love to add you! Open this link and press Start to join: {invite_link}",
        parse_mode="HTML",
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
    max_friends = get_max_friends(settings, user_id)
    header = f"You have {friend_count}/{max_friends} friends.\n\n"

    if friends:
        text = (
            header
            + "Tap a friend below to open their actions or choose an option to invite or remove."
        )
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

    if friends:
        await message.answer(
            "Tap a friend to open their actions:",
            reply_markup=friend_list_keyboard(friends),
        )


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


@dp.callback_query(F.data.startswith("wishlist_item:"))
async def wishlist_item_callback(callback: types.CallbackQuery):
    try:
        item_id = int(callback.data.split(":", maxsplit=1)[1])
    except (IndexError, ValueError):
        await callback.answer("Invalid wishlist item.", show_alert=True)
        return

    item = await get_wishlist_item(item_id)
    if not item or item["owner_telegram_id"] != callback.from_user.id:
        await callback.answer("This wishlist item is no longer available.", show_alert=True)
        return

    await callback.answer()
    await callback.message.answer(
        "What would you like to do with this wishlist item?",
        reply_markup=wishlist_item_action_keyboard(item),
    )


@dp.callback_query(F.data.startswith("wishlist_delete:"))
async def wishlist_delete_callback(callback: types.CallbackQuery):
    try:
        item_id = int(callback.data.split(":", maxsplit=1)[1])
    except (IndexError, ValueError):
        await callback.answer("Invalid wishlist item.", show_alert=True)
        return

    await delete_wishlist_item(callback.from_user.id, item_id)
    await callback.answer("Wishlist item deleted.")
    await send_editable_wishlist(callback.message, callback.from_user.id)


@dp.callback_query(F.data.startswith("friend_card:"))
async def friend_card_callback(callback: types.CallbackQuery):
    try:
        friend_username_raw = callback.data.split(":", maxsplit=1)[1]
    except (IndexError, ValueError):
        await callback.answer("Invalid friend action.", show_alert=True)
        return

    friend_tg_id = await get_telegram_id_by_username(friend_username_raw)
    if not friend_tg_id:
        await callback.answer("I can't find this friend in Kind Friends.", show_alert=True)
        return

    friends_ids = await get_all_friend_ids(callback.from_user.id)
    if friend_tg_id not in friends_ids:
        await callback.answer("You are no longer connected with this friend.", show_alert=True)
        return

    await callback.answer()
    await callback.message.answer(
        f"What do you want to do with @{friend_username_raw}?",
        reply_markup=friend_options_keyboard(friend_username_raw),
    )


@dp.callback_query(F.data.startswith("friend_remove:"))
async def friend_remove_from_card(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    try:
        friend_username_raw = callback.data.split(":", maxsplit=1)[1]
    except (IndexError, ValueError):
        await callback.answer("Invalid friend action.", show_alert=True)
        return

    friend_tg_id = await get_telegram_id_by_username(friend_username_raw)
    if not friend_tg_id:
        await callback.answer("I can't find this friend in Kind Friends.", show_alert=True)
        return

    friends_ids = await get_all_friend_ids(user_id)
    if friend_tg_id not in friends_ids:
        await callback.answer("You are no longer connected with this friend.", show_alert=True)
        return

    await remove_friendship(user_id, friend_tg_id)
    await notify_friend_removed(user_id, friend_tg_id)

    await callback.answer("Friend removed.")
    await callback.message.answer(
        f"You are no longer connected with @{friend_username_raw} on Kind Friends.",
        reply_markup=friends_keyboard(),
    )


@dp.callback_query(F.data.startswith("friend_wishlist:"))
async def friend_wishlist_callback(callback: types.CallbackQuery):
    try:
        friend_username_raw = callback.data.split(":", maxsplit=1)[1]
    except (IndexError, ValueError):
        await callback.answer("Invalid friend action.", show_alert=True)
        return

    friend_tg_id = await get_telegram_id_by_username(friend_username_raw)
    if not friend_tg_id:
        await callback.answer("I can't find this friend in Kind Friends.", show_alert=True)
        return

    friends_ids = await get_all_friend_ids(callback.from_user.id)
    if friend_tg_id not in friends_ids:
        await callback.answer("You are no longer connected with this friend.", show_alert=True)
        return

    items = [dict(item) for item in await get_wishlist_items(friend_tg_id)]
    if not items:
        await callback.answer("This wishlist is empty right now.", show_alert=True)
        return

    await callback.answer()
    await callback.message.answer(
        f"Wishlist for @{friend_username_raw}:",
        reply_markup=wishlist_items_keyboard(items, editable=False),
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
    requester_max_friends = get_max_friends(settings, requester_id)
    recipient_max_friends = get_max_friends(settings, recipient_id)

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


async def process_send_link_action(
    user_id: int, url: str, sender_username: str | None, reply_target: types.Message
):
    recent_sent_at = await get_recent_sent_link_timestamp(user_id, url)
    if recent_sent_at:
        retry_after = recent_sent_at + timedelta(days=7)
        now = datetime.now(timezone.utc)
        if now < retry_after:
            remaining_seconds = (retry_after - now).total_seconds()
            remaining_days = max(1, math.ceil(remaining_seconds / 86400))
            await reply_target.answer(
                "You already shared this link recently.\n"
                f"Please wait {remaining_days} day(s) before sending it again to avoid spam.",
            )
            return

    paused = await is_paused(user_id)
    if paused:
        await reply_target.answer(
            "You are currently on pause.\n"
            "Tap ▶️ Resume if you want to send and receive links again.",
        )
        return

    daily_sent = await get_daily_sent_links_count(user_id)
    if daily_sent >= MAX_DAILY_LINKS:
        await reply_target.answer(
            "You have reached the daily limit of 5 links.\n"
            "0/5 links are left for today.",
        )
        return

    friends_ids = await get_all_friend_ids(user_id)
    sender_username = sender_username or "your friend"
    sent_count = 0
    stored_count = 0

    for fid in friends_ids:
        try:
            if await is_paused(fid):
                await save_pending_link(fid, user_id, url)
                stored_count += 1
            else:
                await bot.send_message(
                    fid,
                    f"@{sender_username} shared a link with you:\n{url}",
                )
                sent_count += 1
        except Exception as e:
            print(f"[WARN] Failed to deliver link to {fid}: {e}")

    new_daily_total = await increment_sent_links(user_id)
    await record_sent_link(user_id, url)

    remaining_links = max(0, MAX_DAILY_LINKS - new_daily_total)
    remaining_text = (
        f"You have {remaining_links}/{MAX_DAILY_LINKS} link(s) left for today."
        if remaining_links > 0
        else "You have 0/5 links left for today."
    )

    if sent_count == 0 and stored_count == 0:
        await reply_target.answer(
            "Got your link ✅\n"
            "Right now you don't have any friends to send it to.\n"
            f"{remaining_text}"
        )
        return

    parts = ["Got your link ✅"]
    if sent_count:
        parts.append(f"Sent to {sent_count} active friend(s).")
    if stored_count:
        parts.append(f"Saved for {stored_count} friend(s) who are on pause.")
    parts.append(remaining_text)
    await reply_target.answer("\n".join(parts))


@dp.callback_query(F.data == "link_action:wishlist")
async def link_action_wishlist(callback: types.CallbackQuery):
    url = pending_link_actions.pop(callback.from_user.id, None)
    if not url:
        await callback.answer("Please send a link first.", show_alert=True)
        return

    async with aiohttp.ClientSession() as session:
        title = await _fetch_title_for_url(session, url)
    await add_wishlist_item(callback.from_user.id, url, title)
    await callback.message.answer(
        "Saved to your wishlist ✅",
        reply_markup=wishlist_keyboard(),
    )
    await send_editable_wishlist(callback.message, callback.from_user.id)
    await callback.answer()


@dp.callback_query(F.data == "link_action:send")
async def link_action_send(callback: types.CallbackQuery):
    url = pending_link_actions.pop(callback.from_user.id, None)
    if not url:
        await callback.answer("Please send a link first.", show_alert=True)
        return

    await process_send_link_action(
        user_id=callback.from_user.id,
        url=url,
        sender_username=callback.from_user.username,
        reply_target=callback.message,
    )
    await callback.answer()


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

        # 0.2) Wishlist add mode
        if user_id in wishlist_add_mode:
            if not (text.startswith("http://") or text.startswith("https://")):
                await message.answer(
                    "Please send a full link starting with http:// or https:// to save it to your wishlist.",
                    reply_markup=wishlist_keyboard(),
                )
                return

            wishlist_add_mode.discard(user_id)
            url = text.strip()
            async with aiohttp.ClientSession() as session:
                title = await _fetch_title_for_url(session, url)
            await add_wishlist_item(user_id, url, title)
            await message.answer(
                "Saved to your wishlist ✅",
                reply_markup=wishlist_keyboard(),
            )
            await send_editable_wishlist(message, user_id)
            return

        # 1) Add friend mode
        if user_id in add_friend_mode and text.startswith("@"):
            add_friend_mode.discard(user_id)
            friend_username_raw = text.strip()

            friend_count = await get_friend_count(user_id)
            max_friends = get_max_friends(settings, user_id)
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
                    "Forward my previous message with invitation link to your friends.",
                )
                await message.answer(
                    "👋 I use Kind Friends to share my links with friends so they can support me "
                    "with likes and comments whenever I post something new, and so I can support them "
                    "when they need it too.\n\n"
                    f"I'd love to add you! Open this link and press Start to join: {invite_link}",
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
            friend_limit_for_friend = get_max_friends(settings, friend_tg_id)
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
                friend_max_friends = get_max_friends(settings, user_id)
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
            pending_link_actions[user_id] = text.strip()
            await message.answer(
                "What would you like to do with this link?",
                reply_markup=link_action_keyboard(),
            )
            return

        # 4) Fallback
        await message.answer(
            "Use the buttons to open Friends or Help, or paste a link to share it with friends.",
            reply_markup=main_keyboard(user_paused),
        )


register_admin_handlers(
    dp=dp,
    bot=bot,
    settings=settings,
    database=database,
    generic_handler=generic_handler,
)

dp.message.register(generic_handler)


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
