from collections import defaultdict
from datetime import datetime, timezone

from aiogram import F, types
from aiogram.filters import CommandStart
from aiogram.types import KeyboardButton, ReplyKeyboardMarkup

from handlers.state import BotState
from kind_friends.config import Settings
from kind_friends.repositories import Database
from utils.ui import (
    back_only_keyboard,
    feedback_keyboard,
    friends_keyboard,
    help_keyboard,
    main_keyboard,
    pause_overlay_keyboard,
    wishlist_keyboard,
)


async def fetch_pending_links_with_usernames(database: Database, recipient_id: int):
    if not database.pool:
        raise RuntimeError("Database not initialized")
    async with database.pool.acquire() as conn:
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


async def clear_pending_links_for_user(database: Database, recipient_id: int):
    if not database.pool:
        raise RuntimeError("Database not initialized")
    async with database.pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM pending_links WHERE recipient_telegram_id=$1",
            recipient_id,
        )


async def deliver_feedback_to_admin(bot, settings: Settings, sender: types.User, text: str) -> bool:
    target_chat_id = settings.feedback_recipient_chat_id or settings.admin_id
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


def register_general_handlers(dp, bot, settings: Settings, database: Database, state: BotState):
    @dp.message(CommandStart())
    async def cmd_start(message: types.Message):
        paused = await database.get_or_create_user(
            message.from_user.id,
            message.from_user.username,
        )
        state.set_submenu(message.from_user.id, "root")

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
            inviter_username = await database.get_username_by_telegram_id(inviter_id)

            if inviter_username is not None:
                inviter_display_name = f"@{inviter_username}" if inviter_username else "your friend"
                await database.add_mutual_friendship(message.from_user.id, inviter_id)

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
            "Hey 👋 \n"
            "I help to connect friends to support each other.\n\n"
            "What you can do here:\n"
            "• Create your circle of friends (up to 15).\n"
            "• Share any link (your post, podcast, article, video or stream) and your friends instantly get it.\n"
            "• Add any product's link to your personal Wishlist.\n"
            "• Look up for your friends' Wishes.\n"
            "• Anti-spam limits (5 links/day + 7-day cooldown per link).\n\n"
            "Just send me a link — I handle the rest.\n\n"
            "*This is an MVP version. Your feedback is very welcome!*\n"
            + invite_note,
            reply_markup=pause_overlay_keyboard() if paused else main_keyboard(False),
        )

    @dp.message(F.text == "Start")
    async def start_text_handler(message: types.Message):
        await cmd_start(message)

    @dp.message(F.text == "ℹ️ Help")
    async def help_handler(message: types.Message):
        state.set_submenu(message.from_user.id, "help")
        await message.answer(
            "Pick a help option:",
            reply_markup=help_keyboard(),
        )

    @dp.message(F.text == "How to")
    async def how_to_handler(message: types.Message):
        await message.answer(
            "1️⃣ Share links\n"
            "• Paste any link — choose to send, wishlist.\n"
            "• If “Send” active friends get it instantly; paused friends get it when they return.\n\n"
            "2️⃣ Wishlist\n"
            "• If “Wishlist” you save links for later in 🎁 Wishlist.\n"
            "• View, edit, or delete items anytime. Browse your friends’ Wishlists.\n\n"
            "3️⃣ Anti-spam Limits\n"
            "• Up to 5 links/day.\n"
            "• Same link: 7-day cooldown.\n"
            "• If your friend list is full, I’ll warn you about new incoming invites.\n\n"
            "4️⃣ Friends\n"
            "• Add friends via \"👥 Friends → Invite\".\n"
            "• Unfollow via \"👥 Friends → Remove\" or from a friend’s card.\n"
            "• Existing users get a request; new users give you a forwardable invite link.\n"
            "• Max 15 friends.\n\n"
            "5️⃣ Pause / Resume\n"
            "• Pause stops sending & receiving; links are saved for later.\n"
            "• Resume gives you a quick digest of what you missed.\n\n"
            "7️⃣ Wipe account\n"
            "• Deletes your data, friendships, requests, wishlist & stored links.\n"
            "• Sent messages in chats can’t be removed.",
            reply_markup=help_keyboard(),
        )

    @dp.message(F.text == "Wipe Account")
    async def delete_account_prompt(message: types.Message):
        user_id = message.from_user.id
        state.delete_account_confirmation.add(user_id)
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

    @dp.message(F.text == "Feedback")
    async def start_feedback_from_menu(message: types.Message):
        state.set_submenu(message.from_user.id, "help_feedback")
        state.feedback_mode.add(message.from_user.id)
        await message.answer(
            "Thanks for willing to share feedback!\nSend your thoughts in one message and I'll pass it along.",
            reply_markup=back_only_keyboard(),
        )

    @dp.message(F.text == "⬅️ Back")
    async def back_to_main(message: types.Message):
        user_id = message.from_user.id
        user_paused = await database.is_paused(user_id)
        current_menu = state.get_submenu(user_id)

        if current_menu == "root":
            state.add_friend_mode.discard(user_id)
            state.remove_friend_mode.discard(user_id)
            state.remove_friend_confirmations.pop(user_id, None)
            state.feedback_mode.discard(user_id)
            state.delete_account_confirmation.discard(user_id)
            state.wishlist_add_mode.discard(user_id)
            state.wishlist_delete_confirmations.pop(user_id, None)
            await message.answer(
                "Back to the main menu.",
                reply_markup=pause_overlay_keyboard()
                if user_paused
                else main_keyboard(False),
            )
            return

        target_menu = state.pop_submenu(user_id)

        if target_menu == "friends":
            state.add_friend_mode.discard(user_id)
            state.remove_friend_mode.discard(user_id)
            state.remove_friend_confirmations.pop(user_id, None)
            await message.answer("Back to Friends.", reply_markup=friends_keyboard())
        elif target_menu == "wishlist":
            state.wishlist_add_mode.discard(user_id)
            state.wishlist_delete_confirmations.pop(user_id, None)
            await message.answer("Back to Wishlist.", reply_markup=wishlist_keyboard())
        elif target_menu == "help":
            state.feedback_mode.discard(user_id)
            state.delete_account_confirmation.discard(user_id)
            await message.answer("Back to Help.", reply_markup=help_keyboard())
        else:
            state.add_friend_mode.discard(user_id)
            state.remove_friend_mode.discard(user_id)
            state.remove_friend_confirmations.pop(user_id, None)
            state.feedback_mode.discard(user_id)
            state.delete_account_confirmation.discard(user_id)
            state.wishlist_add_mode.discard(user_id)
            state.wishlist_delete_confirmations.pop(user_id, None)
            state.set_submenu(user_id, "root")
            await message.answer(
                "Back to the main menu.",
                reply_markup=pause_overlay_keyboard()
                if user_paused
                else main_keyboard(False),
            )

    @dp.message(F.text == "⏸ Pause")
    async def pause_handler(message: types.Message):
        user_id = message.from_user.id
        await database.set_pause(user_id, True)
        state.set_submenu(user_id, "root")
        await message.answer(
            "You are now on pause.\n\n"
            "While you’re on pause:\n"
            "• You can’t send links\n"
            "• You won’t receive new links from friends\n\n"
            "Tap ▶️ Resume when you want to come back.",
            reply_markup=pause_overlay_keyboard(),
        )

    @dp.message(F.text == "▶️ Resume")
    async def resume_handler(message: types.Message):
        user_id = message.from_user.id
        await database.set_pause(user_id, False)
        state.set_submenu(user_id, "root")

        rows = await fetch_pending_links_with_usernames(database, user_id)

        if not rows:
            await message.answer(
                "Welcome back! 👋\n"
                "You are active again.\n"
                "You did not miss any links while you were on pause.",
                reply_markup=main_keyboard(False),
            )
            return

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
            parts.append("")

        digest_text = "Here is what you missed while you were on pause:\n\n" + "\n".join(parts)

        await clear_pending_links_for_user(database, user_id)

        await message.answer(
            "Welcome back! 👋\nYou are active again.",
            reply_markup=main_keyboard(False),
        )
        await message.answer(digest_text)

    @dp.callback_query(F.data == "start_feedback")
    async def start_feedback(callback: types.CallbackQuery):
        state.feedback_mode.add(callback.from_user.id)
        await callback.message.answer(
            "Thanks for willing to share feedback!\n"
            "Send your thoughts in one message and I'll pass it along.",
            reply_markup=help_keyboard(),
        )
        await callback.answer()

    @dp.message(F.text == "/wipe_me")
    async def wipe_me_handler(message: types.Message):
        user_id = message.from_user.id
        await database.delete_user_completely(user_id)
        await message.answer(
            "All your Kind Friends data has been deleted ✅\n\n"
            "You’re starting from scratch—just like you’ve never used Kind Friends before."
        )

    return {"deliver_feedback_to_admin": deliver_feedback_to_admin}
