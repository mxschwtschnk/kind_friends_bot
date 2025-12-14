import asyncio
import json
import math
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import aiohttp
from aiogram import F, types
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

from handlers.friends import handle_friend_username_input
from handlers.general import deliver_feedback_to_admin
from handlers.state import BotState
from handlers.wishlist import add_links_to_wishlist, send_editable_wishlist
from kind_friends.config import Settings
from kind_friends.repositories import Database
from utils.ui import (
    back_only_keyboard,
    friends_keyboard,
    help_keyboard,
    link_action_keyboard,
    main_keyboard,
    pause_overlay_keyboard,
    wishlist_keyboard,
)

ENABLE_METADATA_FETCH = False


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
                await self.message.bot.send_chat_action(self.message.chat.id, "typing")
                await asyncio.sleep(4)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            print(f"[WARN] Loading indicator error: {e}")


def _is_forwarded_message(message: types.Message) -> bool:
    return bool(
        message.forward_date
        or message.forward_from
        or message.forward_from_chat
        or message.forward_sender_name
        or getattr(message, "is_automatic_forward", False)
    )


def _deduplicate_urls(urls: list[str]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for url in urls:
        if url in seen:
            continue
        seen.add(url)
        unique.append(url)
    return unique


def _extract_shop_from_url(url: str) -> str | None:
    parsed = urlparse(url)
    host = parsed.netloc or parsed.path
    if not host:
        return None
    host = host.lower()
    if host.startswith("www."):
        host = host[4:]
    return host or None


URL_PATTERN = re.compile(
    (
        r"https?://[^\s<>\"]+"  # full URLs with protocol
        r"|www\.[^\s<>\"]+"  # domains starting with www.
        r"|[a-zA-Z0-9._-]+\.[a-zA-Z]{2,}[^\s<>\"]*"  # bare domains like example.com
    )
)


def extract_urls_from_text(text: str) -> list[str]:
    urls: list[str] = []
    for raw_url in URL_PATTERN.findall(text):
        cleaned = raw_url.rstrip(").,!?;:\"'”’]>}]")
        if not cleaned:
            continue
        if not cleaned.startswith("http://") and not cleaned.startswith("https://"):
            cleaned = f"https://{cleaned}"
        urls.append(cleaned)
    return urls


async def _fetch_link_metadata(session, url: str) -> dict[str, str | None]:
    metadata: dict[str, str | None] = {
        "product_name": None,
        "shop": _extract_shop_from_url(url),
        "price": None,
        "image_url": None,
    }

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
                return metadata
            content_type = response.headers.get("content-type", "")
            if "text/html" not in content_type:
                return metadata
            raw_html = await response.text(errors="ignore")
    except Exception as e:  # noqa: BLE001
        print(f"[WARN] Failed to fetch metadata for {url}: {e}")
        return metadata

    html_slice = raw_html[:200000]

    def _clean_title(title: str | None) -> str | None:
        if not title:
            return None

        parts = re.split(r"\s+[\-|–|—|:]\s+|\s*\|\s*|:\s*", title)
        parts = [p.strip() for p in parts if p and p.strip()]

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

        if store_pattern.search(best) or domain_pattern.search(best):
            return None

        if len(best) < 4:
            return None

        return best

    def _extract_product_from_ld_json():
        scripts = re.findall(
            r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
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

            def _maybe_update(candidate):
                nonlocal best
                if not candidate:
                    return

                def _extract_price(offer):
                    if not isinstance(offer, dict):
                        return None
                    price = offer.get("price") or offer.get("priceSpecification", {}).get("price")
                    currency = offer.get("priceCurrency") or offer.get("priceSpecification", {}).get("priceCurrency")
                    if price and currency and currency not in str(price):
                        return f"{currency} {price}"
                    return price

                def _build_candidate(title):
                    if not title:
                        return None
                    return {
                        "title": title,
                        "price": _extract_price(offers or data.get("offers")),
                        "image_url": _normalize_image(data.get("image")),
                    }

                if best is None or (candidate.get("title") and len(candidate["title"]) > len(best.get("title") or "")):
                    best = candidate

            if isinstance(data, list):
                for item in data:
                    found = _walk(item)
                    if found:
                        _maybe_update(found)
                return best

            if isinstance(data, dict):
                types = data.get("@type") or data.get("type")
                offers = data.get("offers")
                if isinstance(types, str):
                    types = [types]

                def _extract_price(offer):
                    if not isinstance(offer, dict):
                        return None
                    price = offer.get("price") or offer.get("priceSpecification", {}).get("price")
                    currency = offer.get("priceCurrency") or offer.get("priceSpecification", {}).get("priceCurrency")
                    if price and currency and currency not in str(price):
                        return f"{currency} {price}"
                    return price

                def _build_candidate(title):
                    return {
                        "title": title,
                        "price": _extract_price(offers or data.get("offers")),
                        "image_url": _normalize_image(data.get("image")),
                    }

                if any(isinstance(t, str) and t.lower() == "product" for t in types):
                    name = data.get("name") or data.get("headline")
                    _maybe_update(_build_candidate(name))

                if any(isinstance(t, str) and t.lower() == "offer" for t in types):
                    item_offered = data.get("itemOffered")
                    if isinstance(item_offered, dict):
                        name = item_offered.get("name") or item_offered.get("headline")
                        candidate = {
                            "title": name,
                            "price": _extract_price(item_offered),
                            "image_url": _normalize_image(item_offered.get("image")),
                        }
                        _maybe_update(candidate)
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
        match = re.search(rf"{attr}\s*=\s*[\"']([^\"']+)[\"']", tag, re.IGNORECASE)
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
        if name and content:
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

    for candidate in title_candidates:
        cleaned = _clean_title(candidate)
        if cleaned:
            metadata["product_name"] = cleaned
            break

    price_candidates = [
        structured.get("price"),
        _search_meta(
            (
                "product:price:amount",
                "og:price:amount",
                "twitter:data1",
                "price",
                "price:amount",
                "og:offer:price",
            )
        ),
    ]

    currency = _search_meta(
        (
            "product:price:currency",
            "og:price:currency",
            "price:currency",
            "og:offer:price:currency",
        )
    )

    for candidate in price_candidates:
        if candidate:
            metadata["price"] = (
                f"{currency} {candidate}" if currency and currency not in str(candidate) else str(candidate)
            )
            break

    metadata["image_url"] = structured.get("image_url") or _search_meta(
        (
            "og:image:secure_url",
            "og:image",
            "twitter:image",
            "image",
        )
    )

    if not metadata["image_url"]:
        metadata["image_url"] = _search_img_tags()

    return metadata


async def _delete_link_prompt_message(message: types.Message | None) -> None:
    if not message:
        return

    try:
        await message.delete()
    except Exception as e:  # noqa: BLE001
        print(f"[WARN] Failed to delete link prompt message: {e}")


async def get_recent_sent_link_timestamp(database: Database, sender_id: int, url: str):
    if not database.pool:
        raise RuntimeError("Database not initialized")
    async with database.pool.acquire() as conn:
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


async def record_sent_link(database: Database, sender_id: int, url: str):
    if not database.pool:
        raise RuntimeError("Database not initialized")
    async with database.pool.acquire() as conn:
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


async def get_daily_sent_links_count(database: Database, tg_id: int) -> int:
    today = datetime.now(timezone.utc).date()
    if not database.pool:
        raise RuntimeError("Database not initialized")
    async with database.pool.acquire() as conn:
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


async def increment_sent_links(database: Database, tg_id: int) -> int:
    today = datetime.now(timezone.utc).date()
    if not database.pool:
        raise RuntimeError("Database not initialized")
    async with database.pool.acquire() as conn:
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


async def process_send_link_action(
    bot,
    database: Database,
    settings: Settings,
    user_id: int,
    url: str,
    sender_username: str | None,
    reply_target: types.Message,
):
    recent_sent_at = await get_recent_sent_link_timestamp(database, user_id, url)
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

    paused = await database.is_paused(user_id)
    if paused:
        await reply_target.answer(
            "You are currently on pause.\n"
            "Tap ▶️ Resume if you want to send and receive links again.",
        )
        return

    daily_sent = await get_daily_sent_links_count(database, user_id)
    if daily_sent >= settings.max_daily_links:
        await reply_target.answer(
            "You have reached the daily limit of 5 links.\n"
            "0/5 links are left for today.",
        )
        return

    friends_ids = await database.get_all_friend_ids(user_id)
    sender_username = sender_username or "your friend"
    sent_count = 0
    stored_count = 0

    for fid in friends_ids:
        try:
            if await database.is_paused(fid):
                await database.save_pending_link(fid, user_id, url)
                stored_count += 1
            else:
                await bot.send_message(
                    fid,
                    f"@{sender_username} shared a link with you:\n{url}",
                )
                sent_count += 1
        except Exception as e:
            print(f"[WARN] Failed to deliver link to {fid}: {e}")

    new_daily_total = await increment_sent_links(database, user_id)
    await record_sent_link(database, user_id, url)

    remaining_links = max(0, settings.max_daily_links - new_daily_total)
    remaining_text = (
        f"You have {remaining_links}/{settings.max_daily_links} link(s) left for today."
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


def register_links_handlers(dp, bot, settings: Settings, database: Database, state: BotState):
    @dp.callback_query(F.data == "link_action:wishlist")
    async def link_action_wishlist(callback: types.CallbackQuery):
        urls = state.pending_link_actions.pop(callback.from_user.id, None)
        state.forwarded_link_collections.pop(callback.from_user.id, None)
        if not urls:
            await callback.answer("Please send a link first.", show_alert=True)
            return

        state.set_submenu(callback.from_user.id, "wishlist")

        if ENABLE_METADATA_FETCH:
            async with aiohttp.ClientSession() as session:
                await add_links_to_wishlist(
                    database,
                    callback.from_user.id,
                    urls,
                    callback.message,
                    fetch_metadata=True,
                    metadata_fetcher=lambda url: _fetch_link_metadata(session, url),
                )
        else:
            await add_links_to_wishlist(
                database, callback.from_user.id, urls, callback.message
            )
        await send_editable_wishlist(
            database, state, callback.message, callback.from_user.id, show_summary=False
        )
        await _delete_link_prompt_message(callback.message)
        await callback.answer()

    @dp.callback_query(F.data == "link_action:send")
    async def link_action_send(callback: types.CallbackQuery):
        urls = state.pending_link_actions.pop(callback.from_user.id, None)
        state.forwarded_link_collections.pop(callback.from_user.id, None)
        if not urls:
            await callback.answer("Please send a link first.", show_alert=True)
            return

        if len(urls) > 1:
            await callback.answer("Send links one at a time to share with friends.", show_alert=True)
            return

        await process_send_link_action(
            bot=bot,
            database=database,
            settings=settings,
            user_id=callback.from_user.id,
            url=urls[0],
            sender_username=callback.from_user.username,
            reply_target=callback.message,
        )
        await _delete_link_prompt_message(callback.message)
        await callback.answer()

    @dp.callback_query(F.data == "link_action:cancel")
    async def link_action_cancel(callback: types.CallbackQuery):
        state.pending_link_actions.pop(callback.from_user.id, None)
        state.forwarded_link_collections.pop(callback.from_user.id, None)
        await _delete_link_prompt_message(callback.message)
        await callback.answer("Cancelled.")

    @dp.message()
    async def generic_handler(message: types.Message):
        user_id = message.from_user.id
        text = message.text or ""
        is_forwarded = _is_forwarded_message(message)
        user_paused = await database.is_paused(user_id)

        async with LoadingIndicator(message):
            if user_id in state.feedback_mode:
                if message.content_type != types.ContentType.TEXT:
                    await message.answer(
                        "I couldn't deliver your feedback because it included media. "
                        "Please send your feedback as plain text without any photos, videos, or other attachments so I can pass it along.",
                        reply_markup=help_keyboard() if not user_paused else pause_overlay_keyboard(),
                    )
                    return

                state.feedback_mode.discard(user_id)
                state.delete_account_confirmation.discard(user_id)
                delivered = await deliver_feedback_to_admin(bot, settings, message.from_user, text)
                if not delivered:
                    await message.answer(
                        "I couldn't deliver your feedback because the admin destination isn't configured yet.",
                        reply_markup=help_keyboard() if not user_paused else pause_overlay_keyboard(),
                    )
                    return

                await message.answer(
                    "Thanks! I delivered your feedback to the admin.",
                    reply_markup=help_keyboard() if not user_paused else pause_overlay_keyboard(),
                )
                return

            if user_id in state.delete_account_confirmation:
                state.delete_account_confirmation.discard(user_id)
                if text == "Yes, wipe":
                    await database.delete_user_completely(user_id)
                    await message.answer(
                        "All your Kind Friends data has been deleted ✅\n\n"
                        "You’re starting from scratch—just like you’ve never used Kind Friends before.",
                        reply_markup=ReplyKeyboardMarkup(
                            keyboard=[[KeyboardButton(text="Let’s start again")]], resize_keyboard=True
                        ),
                    )
                    return
                await message.answer(
                    "I kept your account. You can continue using Kind Friends.",
                    reply_markup=help_keyboard() if not user_paused else pause_overlay_keyboard(),
                )
                return

            if user_id in state.wishlist_add_mode:
                urls = extract_urls_from_text(text)
                if not urls:
                    await message.answer(
                        "I couldn't find any links in that message. Please send at least one link to save it to your wishlist.",
                        reply_markup=wishlist_keyboard(),
                    )
                    return

                state.wishlist_add_mode.discard(user_id)

                if ENABLE_METADATA_FETCH:
                    async with aiohttp.ClientSession() as session:
                        await add_links_to_wishlist(
                            database,
                            user_id,
                            urls,
                            message,
                            fetch_metadata=True,
                            metadata_fetcher=lambda url: _fetch_link_metadata(session, url),
                        )
                else:
                    await add_links_to_wishlist(database, user_id, urls, message)
                await send_editable_wishlist(database, state, message, user_id, show_summary=False)
                return

            if user_id in state.add_friend_mode and text.startswith("@"):
                await handle_friend_username_input(bot, database, settings, state, message)
                return

            if user_id in state.remove_friend_mode:
                await message.answer(
                    "Tap a friend in the Remove menu to delete them, or tap ↩️ Back to exit.",
                    reply_markup=back_only_keyboard(),
                )
                return

            urls = extract_urls_from_text(text)
            if urls:
                if is_forwarded:
                    combined_urls = state.forwarded_link_collections.get(user_id, []) + urls
                    combined_urls = _deduplicate_urls(combined_urls)
                    state.forwarded_link_collections[user_id] = combined_urls
                    state.pending_link_actions[user_id] = combined_urls

                    summary_lines = [
                        "I gathered these links from your forwarded messages:",
                        *(f"• {url}" for url in combined_urls),
                        "Add all of them to your wishlist?",
                    ]

                    await message.answer(
                        "\n".join(summary_lines),
                        reply_markup=link_action_keyboard(multiple=True),
                        disable_web_page_preview=True,
                    )
                    return

                state.forwarded_link_collections.pop(user_id, None)
                state.pending_link_actions[user_id] = urls
                if len(urls) > 1:
                    await message.answer(
                        "I found multiple links. I can add them to your wishlist.",
                        reply_markup=link_action_keyboard(multiple=True),
                    )
                else:
                    await message.answer(
                        f"I found this link:\n{urls[0]}\nWhat would you like to do?",
                        reply_markup=link_action_keyboard(),
                    )
                return

            if not is_forwarded:
                state.forwarded_link_collections.pop(user_id, None)

            state.set_submenu(user_id, "root")
            await message.answer(
                "Use the buttons to open Friends or Help, or paste a link to share it with friends.",
                reply_markup=pause_overlay_keyboard() if user_paused else main_keyboard(False),
            )

    return generic_handler
