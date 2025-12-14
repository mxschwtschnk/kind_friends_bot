from aiogram import F, types
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from handlers.state import BotState, FRIEND_REMOVE_CONFIRM_TEXT
from kind_friends.config import Settings
from kind_friends.repositories import Database
from kind_friends.services.friend_service import display_username, get_max_friends
from utils.ui import (
    back_only_keyboard,
    confirmation_keyboard,
    friend_list_keyboard,
    friend_options_keyboard,
    friends_keyboard,
    pause_overlay_keyboard,
    remove_friends_keyboard,
)


async def notify_friend_removed(bot, database: Database, remover_id: int, friend_id: int):
    remover_username = await database.get_username_by_telegram_id(remover_id)
    remover_display = display_username(remover_username, "A friend")

    try:
        await bot.send_message(
            friend_id,
            f"{remover_display} removed you from Kind Friends. You are no longer connected.",
        )
    except Exception as e:  # noqa: BLE001
        print(f"[WARN] Failed to notify user {friend_id} about removal: {e}")


async def notify_user_friend_pool_full_with_pending(bot, database: Database, settings: Settings, tg_id: int):
    friend_count = await database.get_friend_count(tg_id)
    pending_count = await database.count_pending_requests_for_requester(tg_id)
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


async def handle_friend_username_input(
    bot,
    database: Database,
    settings: Settings,
    state: BotState,
    message: types.Message,
):
    user_id = message.from_user.id
    state.add_friend_mode.discard(user_id)
    friend_username_raw = message.text.strip()

    friend_count = await database.get_friend_count(user_id)
    max_friends = get_max_friends(settings, user_id)
    if friend_count >= max_friends:
        await message.answer(
            f"You already have {max_friends} friends. Unfortunately there is a limit because it’s MVP, "
            "and we’ll notify you when it will be possible to add more. For now you can "
            "delete some friends from your list before sending new invitations.",
            reply_markup=friends_keyboard(),
        )
        return

    friend_tg_id = await database.get_telegram_id_by_username(friend_username_raw)
    clean_username = friend_username_raw.lstrip("@")

    if not friend_tg_id:
        await database.increment_invite_count(user_id)
        invite_link = f"https://t.me/{settings.bot_username}?start={user_id}"
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

    existing_friends = await database.get_all_friend_ids(user_id)
    if friend_tg_id in existing_friends:
        await message.answer(
            f"You are already connected with @{clean_username} in Kind Friends.",
            reply_markup=friends_keyboard(),
        )
        return

    friend_friend_count = await database.get_friend_count(friend_tg_id)
    friend_limit_for_friend = get_max_friends(settings, friend_tg_id)
    if friend_friend_count >= friend_limit_for_friend:
        await message.answer(
            "Unfortunately it’s not possible to add this friend because their friend pool is full.",
            reply_markup=friends_keyboard(),
        )
        return

    outgoing_request = await database.get_pending_friend_request(user_id, friend_tg_id)
    if outgoing_request:
        await message.answer(
            "You already sent an invitation. Please wait for your friend to respond.",
            reply_markup=friends_keyboard(),
        )
        return

    incoming_request = await database.get_pending_friend_request(friend_tg_id, user_id)
    if incoming_request:
        friend_max_friends = get_max_friends(settings, user_id)
        if friend_count >= friend_max_friends:
            await database.delete_friend_request(incoming_request["id"])
            await message.answer(
                f"You already have {friend_max_friends} friends. Unfortunately there is a limit because it’s MVP, "
                "and we’ll notify you when it will be possible to add more. For now you can "
                "delete some friends from your list before sending new invitations.",
                reply_markup=friends_keyboard(),
            )
            return

        if friend_friend_count >= friend_limit_for_friend:
            await database.delete_friend_request(incoming_request["id"])
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

        await database.delete_friend_request(incoming_request["id"])
        await database.add_mutual_friendship(user_id, friend_tg_id)

        friend_username = await database.get_username_by_telegram_id(friend_tg_id)
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
        await notify_user_friend_pool_full_with_pending(bot, database, settings, user_id)
        await notify_user_friend_pool_full_with_pending(bot, database, settings, friend_tg_id)
        return

    request_id = await database.create_friend_request(user_id, friend_tg_id)
    if not request_id:
        await message.answer(
            "I couldn't send the request right now. Please try again in a bit.",
            reply_markup=friends_keyboard(),
        )
        return

    friend_username = await database.get_username_by_telegram_id(friend_tg_id)
    friend_display = display_username(friend_username or clean_username, "friend")
    pending_after_request = await database.count_pending_requests_for_requester(user_id)
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
    confirmation_keyboard_markup = InlineKeyboardMarkup(
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
            reply_markup=confirmation_keyboard_markup,
        )
    except Exception as e:
        print(f"[WARN] Failed to deliver friend confirmation to {friend_tg_id}: {e}")


def register_friends_handlers(dp, bot, settings: Settings, database: Database, state: BotState):
    @dp.message(F.text == "Invite")
    async def invite_friends_handler(message: types.Message):
        user_id = message.from_user.id
        state.set_submenu(user_id, "friends_invite")
        state.add_friend_mode.add(user_id)
        state.remove_friend_mode.discard(user_id)
        state.remove_friend_confirmations.pop(user_id, None)
        invite_link = f"https://t.me/{settings.bot_username}?start={user_id}"
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
        if await database.is_paused(user_id):
            state.set_submenu(user_id, "root")
            await message.answer(
                "You are currently on pause. Tap ▶️ Resume to manage friends again.",
                reply_markup=pause_overlay_keyboard(),
            )
            return

        state.set_submenu(user_id, "friends")
        friend_count = await database.get_friend_count(user_id)
        friends = await database.get_friend_usernames(user_id)
        max_friends = get_max_friends(settings, user_id)
        text = (
            f"You have {friend_count}/{max_friends} friends.\n\n"
            "Use Invite to get invitation link or Remove to unfollow your friends."
        )
        state.add_friend_mode.discard(user_id)
        state.remove_friend_mode.discard(user_id)
        state.remove_friend_confirmations.pop(user_id, None)
        await message.answer(
            text,
            parse_mode="Markdown",
            reply_markup=friends_keyboard(),
        )

    @dp.message(F.text == "My Friends")
    async def friends_list_handler(message: types.Message):
        user_id = message.from_user.id
        friends = await database.get_friend_usernames(user_id)
        state.add_friend_mode.discard(user_id)
        state.remove_friend_mode.discard(user_id)
        state.remove_friend_confirmations.pop(user_id, None)

        if not friends:
            await message.answer("You don't have any friends connected yet.")
            return

        await message.answer(
            "Tap a friend to open their actions:",
            reply_markup=friend_list_keyboard(friends),
        )

    @dp.message(F.text == "Remove")
    async def remove_friend_handler(message: types.Message):
        user_id = message.from_user.id
        state.set_submenu(user_id, "friends_remove")
        friends = await database.get_friend_usernames(user_id)

        if not friends:
            await message.answer("You don't have any friends connected yet.")
            return

        state.remove_friend_mode.add(user_id)
        state.add_friend_mode.discard(user_id)
        state.remove_friend_confirmations.pop(user_id, None)
        await message.answer("Removing friends.", reply_markup=back_only_keyboard())
        await message.answer(
            "Select a friend to remove.", reply_markup=remove_friends_keyboard(friends)
        )

    @dp.callback_query(F.data.startswith("remove_friend:"))
    async def remove_friend_callback(callback: types.CallbackQuery):
        user_id = callback.from_user.id

        if user_id not in state.remove_friend_mode:
            await callback.answer("Remove mode is closed. Tap Remove to start again.", show_alert=True)
            return

        try:
            friend_username_raw = callback.data.split(":", maxsplit=1)[1]
        except (IndexError, ValueError):
            await callback.answer("Invalid remove action.", show_alert=True)
            return

        friend_tg_id = await database.get_telegram_id_by_username(friend_username_raw)
        if not friend_tg_id:
            await callback.answer("I can't find this friend in Kind Friends.", show_alert=True)
            return

        friends = await database.get_friend_usernames(user_id)
        if friend_username_raw not in friends:
            await callback.answer("This friend is no longer available.", show_alert=True)
            return

        state.remove_friend_confirmations[user_id] = friend_username_raw
        await callback.message.answer(
            f"Remove @{friend_username_raw}?",
            reply_markup=confirmation_keyboard(FRIEND_REMOVE_CONFIRM_TEXT),
        )
        await callback.answer()

    async def _finalize_friend_removal(
        user_id: int, friend_username_raw: str, reply_target: types.Message
    ) -> None:
        friend_tg_id = await database.get_telegram_id_by_username(friend_username_raw)
        if not friend_tg_id:
            await reply_target.answer(
                "I can't find this friend in Kind Friends.",
                reply_markup=friends_keyboard(),
            )
            return

        friends_ids = await database.get_all_friend_ids(user_id)
        if friend_tg_id not in friends_ids:
            await reply_target.answer(
                "You are no longer connected with this friend.",
                reply_markup=friends_keyboard(),
            )
            return

        state.remove_friend_confirmations.pop(user_id, None)
        state.friend_card_remove_confirmations.pop(user_id, None)

        await database.remove_friendship(user_id, friend_tg_id)
        await notify_friend_removed(bot, database, user_id, friend_tg_id)

        in_remove_mode = user_id in state.remove_friend_mode
        updated_friends = await database.get_friend_usernames(user_id)

        if in_remove_mode:
            await reply_target.answer(
                f"Removed @{friend_username_raw}.",
                reply_markup=back_only_keyboard(),
            )
            if updated_friends:
                await reply_target.answer(
                    "Select a friend to remove.",
                    reply_markup=remove_friends_keyboard(updated_friends),
                )
            else:
                state.remove_friend_mode.discard(user_id)
                await reply_target.answer(
                    "You have no other friends to remove.",
                    reply_markup=friends_keyboard(),
                )
        else:
            state.set_submenu(user_id, "friends")
            await reply_target.answer(
                f"You are no longer connected with @{friend_username_raw} on Kind Friends.",
                reply_markup=friends_keyboard(),
            )

    @dp.callback_query(F.data.startswith("remove_friend_confirm:"))
    async def remove_friend_confirm_callback(callback: types.CallbackQuery):
        user_id = callback.from_user.id

        if user_id not in state.remove_friend_mode:
            await callback.answer("Remove mode is closed. Tap Remove to start again.", show_alert=True)
            return

        try:
            friend_username_raw = callback.data.split(":", maxsplit=1)[1]
        except (IndexError, ValueError):
            await callback.answer("Invalid remove action.", show_alert=True)
            return

        pending_confirmation = state.remove_friend_confirmations.get(user_id)
        if pending_confirmation != friend_username_raw:
            await callback.answer("Please select a friend to remove first.", show_alert=True)
            return
        await _finalize_friend_removal(user_id, friend_username_raw, callback.message)
        await callback.answer()

    @dp.callback_query(F.data == "remove_friend_cancel")
    async def remove_friend_cancel_callback(callback: types.CallbackQuery):
        user_id = callback.from_user.id

        if user_id not in state.remove_friend_mode:
            await callback.answer("Remove mode is closed. Tap Remove to start again.", show_alert=True)
            return

        state.remove_friend_confirmations.pop(user_id, None)
        friends = await database.get_friend_usernames(user_id)

        await callback.message.answer(
            "Cancelled.", reply_markup=back_only_keyboard()
        )
        if friends:
            await callback.message.answer(
                "Select a friend to remove.", reply_markup=remove_friends_keyboard(friends)
            )
        await callback.answer()

    @dp.message(F.text == FRIEND_REMOVE_CONFIRM_TEXT)
    async def remove_friend_from_keyboard(message: types.Message):
        user_id = message.from_user.id
        pending_username = state.remove_friend_confirmations.get(user_id) or state.friend_card_remove_confirmations.get(user_id)

        if not pending_username:
            await message.answer(
                "No friend is awaiting removal.", reply_markup=friends_keyboard()
            )
            return

        await _finalize_friend_removal(user_id, pending_username, message)

    @dp.callback_query(F.data.startswith("friend_card:"))
    async def friend_card_callback(callback: types.CallbackQuery):
        try:
            friend_username_raw = callback.data.split(":", maxsplit=1)[1]
        except (IndexError, ValueError):
            await callback.answer("Invalid friend action.", show_alert=True)
            return

        friend_tg_id = await database.get_telegram_id_by_username(friend_username_raw)
        if not friend_tg_id:
            await callback.answer("I can't find this friend in Kind Friends.", show_alert=True)
            return

        friends_ids = await database.get_all_friend_ids(callback.from_user.id)
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

        friend_tg_id = await database.get_telegram_id_by_username(friend_username_raw)
        if not friend_tg_id:
            await callback.answer("I can't find this friend in Kind Friends.", show_alert=True)
            return

        friends_ids = await database.get_all_friend_ids(user_id)
        if friend_tg_id not in friends_ids:
            await callback.answer("You are no longer connected with this friend.", show_alert=True)
            return

        state.friend_card_remove_confirmations[user_id] = friend_username_raw
        await callback.message.answer(
            f"Are you sure you want to remove @{friend_username_raw}?",
            reply_markup=confirmation_keyboard(FRIEND_REMOVE_CONFIRM_TEXT),
        )
        await callback.answer()

    @dp.callback_query(F.data.startswith("friend_card_remove_confirm:"))
    async def friend_remove_from_card_confirm(callback: types.CallbackQuery):
        user_id = callback.from_user.id
        try:
            friend_username_raw = callback.data.split(":", maxsplit=1)[1]
        except (IndexError, ValueError):
            await callback.answer("Invalid friend action.", show_alert=True)
            return

        pending_username = state.friend_card_remove_confirmations.get(user_id)
        if pending_username != friend_username_raw:
            await callback.answer("Please start the removal again from the friends list.", show_alert=True)
            return
        await _finalize_friend_removal(user_id, friend_username_raw, callback.message)
        await callback.answer()

    @dp.callback_query(F.data == "friend_card_remove_cancel")
    async def friend_remove_from_card_cancel(callback: types.CallbackQuery):
        state.friend_card_remove_confirmations.pop(callback.from_user.id, None)
        await callback.answer("Kept your friend.")

    @dp.callback_query(F.data.startswith("friend_wishlist:"))
    async def friend_wishlist_callback(callback: types.CallbackQuery):
        from handlers.wishlist import wishlist_item_keyboard, _wishlist_item_text

        try:
            friend_username_raw = callback.data.split(":", maxsplit=1)[1]
        except (IndexError, ValueError):
            await callback.answer("Invalid friend action.", show_alert=True)
            return

        friend_tg_id = await database.get_telegram_id_by_username(friend_username_raw)
        if not friend_tg_id:
            await callback.answer("I can't find this friend in Kind Friends.", show_alert=True)
            return

        friends_ids = await database.get_all_friend_ids(callback.from_user.id)
        if friend_tg_id not in friends_ids:
            await callback.answer("You are no longer connected with this friend.", show_alert=True)
            return

        visible_items = [dict(item) for item in await database.get_wishlist_items(friend_tg_id, callback.from_user.id)]
        if not visible_items:
            await callback.answer("This wishlist is empty right now.", show_alert=True)
            return

        await callback.answer()
        await callback.message.answer(f"Wishlist for @{friend_username_raw}:")
        for item in visible_items:
            await callback.message.answer(
                _wishlist_item_text(item, callback.from_user.id),
                reply_markup=wishlist_item_keyboard(item, callback.from_user.id, is_owner=False),
            )

    @dp.callback_query(F.data.startswith("friend_accept:"))
    async def accept_friend_request(callback: types.CallbackQuery):
        try:
            request_id = int(callback.data.split(":", maxsplit=1)[1])
        except (IndexError, ValueError):
            await callback.answer("Invalid request.", show_alert=True)
            return

        request = await database.get_friend_request_by_id(request_id)
        if not request:
            await callback.answer("This request has already been handled.", show_alert=True)
            return

        if request["recipient_telegram_id"] != callback.from_user.id:
            await callback.answer("This request is for a different user.", show_alert=True)
            return

        requester_id = request["requester_telegram_id"]
        recipient_id = request["recipient_telegram_id"]

        requester_friend_count = await database.get_friend_count(requester_id)
        recipient_friend_count = await database.get_friend_count(recipient_id)
        requester_max_friends = get_max_friends(settings, requester_id)
        recipient_max_friends = get_max_friends(settings, recipient_id)

        if requester_friend_count >= requester_max_friends:
            await database.delete_friend_request(request_id)
            await callback.message.answer(
                "Unfortunately it’s impossible to add this friend because their friend pool is full.",
                reply_markup=friends_keyboard(),
            )
            await notify_user_friend_pool_full_with_pending(bot, database, settings, requester_id)
            return

        if recipient_friend_count >= recipient_max_friends:
            await database.delete_friend_request(request_id)
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

        await database.delete_friend_request(request_id)
        await database.add_mutual_friendship(requester_id, recipient_id)

        requester_username = await database.get_username_by_telegram_id(requester_id)
        recipient_username = await database.get_username_by_telegram_id(callback.from_user.id)
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

        await notify_user_friend_pool_full_with_pending(bot, database, settings, requester_id)
        await notify_user_friend_pool_full_with_pending(bot, database, settings, recipient_id)

        await callback.answer("Friendship confirmed!")

    @dp.callback_query(F.data.startswith("friend_decline:"))
    async def decline_friend_request(callback: types.CallbackQuery):
        try:
            request_id = int(callback.data.split(":", maxsplit=1)[1])
        except (IndexError, ValueError):
            await callback.answer("Invalid request.", show_alert=True)
            return

        request = await database.get_friend_request_by_id(request_id)
        if not request:
            await callback.answer("This request has already been handled.", show_alert=True)
            return

        if request["recipient_telegram_id"] != callback.from_user.id:
            await callback.answer("This request is for a different user.", show_alert=True)
            return

        await database.delete_friend_request(request_id)

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

    return {
        "handle_friend_username_input": handle_friend_username_input,
        "notify_user_friend_pool_full_with_pending": notify_user_friend_pool_full_with_pending,
    }
