from aiogram import F, types
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup

from handlers.state import BotState, WISHLIST_DELETE_CONFIRM_TEXT
from kind_friends.repositories import Database
from utils.ui import (
    back_only_keyboard,
    confirmation_keyboard,
    pause_overlay_keyboard,
    wishlist_keyboard,
)


def _wishlist_item_text(item, viewer_id: int) -> str:
    prefix = None
    reserver = item.get("reserved_by_telegram_id")
    if reserver:
        prefix = "(Reserved by me)" if reserver == viewer_id else "(Reserved by friend)"

    base = item.get("product_name") or item.get("title") or item.get("url") or ""
    if prefix:
        return f"{prefix} {base}"
    return base


def wishlist_item_keyboard(item, viewer_id: int, is_owner: bool) -> InlineKeyboardMarkup:
    buttons: list[list[InlineKeyboardButton]] = []
    reserved_by = item.get("reserved_by_telegram_id")

    if is_owner:
        if reserved_by is None:
            buttons.append(
                [
                    InlineKeyboardButton(
                        text="Delete", callback_data=f"wishlist_delete:{item['id']}"
                    ),
                    InlineKeyboardButton(
                        text="Reserve", callback_data=f"wishlist_reserve:{item['id']}"
                    ),
                ]
            )
        elif reserved_by == viewer_id:
            buttons.append(
                [
                    InlineKeyboardButton(
                        text="Got it!", callback_data=f"wishlist_got:{item['id']}"
                    ),
                    InlineKeyboardButton(
                        text="Unreserve", callback_data=f"wishlist_unreserve:{item['id']}"
                    ),
                ]
            )
        else:
            buttons.append(
                [InlineKeyboardButton(text="Got it!", callback_data=f"wishlist_got:{item['id']}")]
            )
    else:
        if reserved_by == viewer_id:
            buttons.append(
                [
                    InlineKeyboardButton(
                        text="Unreserve", callback_data=f"wishlist_unreserve:{item['id']}"
                    )
                ]
            )
        else:
            buttons.append(
                [
                    InlineKeyboardButton(
                        text="Reserve", callback_data=f"wishlist_reserve:{item['id']}"
                    )
                ]
            )

    return InlineKeyboardMarkup(inline_keyboard=buttons)


async def _get_visible_wishlist_item(database: Database, item_id: int, viewer_id: int):
    item = await database.get_wishlist_item(item_id)
    if not item:
        return None

    if item.get("got_at"):
        return None

    reserved_by = item.get("reserved_by_telegram_id")
    if reserved_by and reserved_by != viewer_id and item.get("owner_telegram_id") != viewer_id:
        return None

    return item


async def _remove_wishlist_item_button(message: types.Message, item_id: int) -> None:
    markup = message.reply_markup
    target = f"wishlist_item:{item_id}"

    if not markup or not markup.inline_keyboard:
        return

    new_keyboard: list[list[InlineKeyboardButton]] = []

    for row in markup.inline_keyboard:
        new_row = [button for button in row if button.callback_data != target]
        if new_row:
            new_keyboard.append(new_row)

    try:
        await message.edit_reply_markup(
            reply_markup=InlineKeyboardMarkup(inline_keyboard=new_keyboard)
            if new_keyboard
            else None
        )
    except Exception as e:  # noqa: BLE001
        print(f"[WARN] Failed to remove wishlist item button {item_id}: {e}")


async def add_links_to_wishlist(
    database: Database,
    user_id: int,
    urls: list[str],
    message: types.Message,
    *,
    fetch_metadata: bool = False,
    metadata_fetcher=None,
) -> None:
    saved: list[tuple[int, str]] = []
    for url in urls:
        meta: dict[str, str | None] | None = None
        if fetch_metadata and metadata_fetcher:
            meta = await metadata_fetcher(url)
        meta_kwargs = {
            key: value for key, value in (meta or {}).items() if key in {"product_name", "shop", "price", "image_url"}
        }
        item_id = await database.add_wishlist_item(user_id, url, **meta_kwargs)
        saved.append((item_id, url))

    for idx, (item_id, _) in enumerate(saved):
        prefix = "Saved to your wishlist ✅\n" if idx == 0 else ""
        item = await database.get_wishlist_item(item_id)
        await message.answer(
            f"{prefix}{_wishlist_item_text(item, user_id)}",
            reply_markup=wishlist_item_keyboard(item, user_id, is_owner=True),
        )


async def send_editable_wishlist(
    database: Database,
    state: BotState,
    message: types.Message,
    user_id: int,
    preface_text: str | None = None,
    reply_markup: ReplyKeyboardMarkup | None = None,
    show_summary: bool = True,
):
    state.wishlist_add_mode.discard(user_id)

    if not show_summary:
        return

    items = [dict(item) for item in await database.get_wishlist_items(user_id)]
    count = len(items)
    summary_line = preface_text or f"You have {count} wish(es) saved."

    if count == 0:
        summary_line += "\n\nSend me a link to add your first item."
    else:
        summary_line += "\n\nUse Add to save more wishes or Delete to remove one."

    await message.answer(
        summary_line,
        reply_markup=reply_markup or wishlist_keyboard(),
    )


async def send_wishlist_items(
    database: Database,
    message: types.Message,
    user_id: int,
    delete_mode: bool = False,
    viewer_id: int | None = None,
) -> None:
    viewer_id = viewer_id or user_id
    is_owner = user_id == viewer_id
    items = [dict(item) for item in await database.get_wishlist_items(user_id, viewer_id)]
    if not items:
        if is_owner:
            await message.answer(
                "Your wishlist is empty right now. Send me a link to add your first item.",
                reply_markup=wishlist_keyboard(),
            )
        else:
            await message.answer("This wishlist is empty right now.")
        return

    if delete_mode:
        await message.answer(
            "Delete mode: tap Delete below any item, then confirm using the keyboard.",
            reply_markup=back_only_keyboard(),
        )

    for item in items:
        await message.answer(
            _wishlist_item_text(item, viewer_id),
            reply_markup=wishlist_item_keyboard(item, viewer_id, is_owner=is_owner),
        )


async def _confirm_or_delete_wishlist_item(
    database: Database, state: BotState, callback: types.CallbackQuery, item_id: int
) -> None:
    item = await database.get_wishlist_item(item_id)
    if not item or item["owner_telegram_id"] != callback.from_user.id:
        state.wishlist_delete_confirmations.pop(callback.from_user.id, None)
        await callback.answer("This wishlist item is no longer available.", show_alert=True)
        return

    if item.get("reserved_by_telegram_id"):
        await callback.answer(
            "This wishlist item is reserved and cannot be deleted right now.",
            show_alert=True,
        )
        return

    state.wishlist_delete_confirmations.pop(callback.from_user.id, None)
    state.wishlist_delete_confirmations[callback.from_user.id] = (item_id, callback.message.message_id)
    await callback.message.answer(
        f"Are you sure you want to delete this wishlist item?\n{item['url']}",
        reply_markup=confirmation_keyboard(WISHLIST_DELETE_CONFIRM_TEXT),
        disable_web_page_preview=True,
    )
    await callback.answer()


async def _delete_wishlist_item_from_confirmation(
    database: Database, state: BotState, message: types.Message
) -> None:
    user_id = message.from_user.id
    saved_confirmation = state.wishlist_delete_confirmations.pop(user_id, None)

    if not saved_confirmation:
        await message.answer(
            "No wishlist item is pending deletion.", reply_markup=wishlist_keyboard()
        )
        return

    if isinstance(saved_confirmation, tuple):
        item_id, target_message_id = saved_confirmation
    else:
        item_id = int(saved_confirmation)
        target_message_id = None

    item = await database.get_wishlist_item(item_id)
    if not item or item.get("owner_telegram_id") != user_id:
        await message.answer(
            "This wishlist item is no longer available.",
            reply_markup=wishlist_keyboard(),
        )
        return

    if item.get("reserved_by_telegram_id"):
        await message.answer(
            "You can't delete this wish because it is reserved.",
            reply_markup=wishlist_keyboard(),
        )
        return

    await database.delete_wishlist_item(user_id, item_id)

    if not target_message_id:
        target_message_id = message.message_id

    try:
        await message.bot.delete_message(message.chat.id, target_message_id)
    except Exception as e:  # noqa: BLE001
        print(f"[WARN] Failed to delete wishlist item message {target_message_id}: {e}")
        await _remove_wishlist_item_button(message, item_id)

    reply_markup = (
        back_only_keyboard()
        if state.get_submenu(user_id) == "wishlist_delete"
        else wishlist_keyboard()
    )

    await message.answer("Wishlist item deleted.", reply_markup=reply_markup)

    if state.get_submenu(user_id) == "wishlist_delete":
        await send_wishlist_items(database, message, user_id, delete_mode=True)


def register_wishlist_handlers(dp, bot, database: Database, state: BotState):
    @dp.message(F.text == "🎁 Wishlist")
    async def wishlist_menu(message: types.Message):
        state.wishlist_add_mode.discard(message.from_user.id)
        state.set_submenu(message.from_user.id, "wishlist")
        await send_editable_wishlist(database, state, message, message.from_user.id)

    @dp.message(F.text == "Add")
    async def wishlist_add_prompt(message: types.Message):
        user_id = message.from_user.id
        state.set_submenu(user_id, "wishlist_add")
        state.wishlist_add_mode.add(user_id)
        await message.answer(
            "Send me a link to add to your wishlist.",
            reply_markup=back_only_keyboard(),
        )

    @dp.message(F.text == "Delete")
    async def wishlist_delete_prompt(message: types.Message):
        state.set_submenu(message.from_user.id, "wishlist_delete")
        await send_wishlist_items(database, message, message.from_user.id, delete_mode=True)

    @dp.message(F.text == "My Wishes")
    async def wishlist_show_message(message: types.Message):
        state.wishlist_add_mode.discard(message.from_user.id)
        state.set_submenu(message.from_user.id, "wishlist")
        await send_wishlist_items(database, message, message.from_user.id)

    @dp.message(F.text == "✏️ Edit")
    async def wishlist_edit(message: types.Message):
        if await database.is_paused(message.from_user.id):
            await message.answer(
                "You are currently on pause. Tap ▶️ Resume to edit your wishlist.",
                reply_markup=pause_overlay_keyboard(),
            )
            return

        await send_editable_wishlist(database, state, message, message.from_user.id)

    @dp.callback_query(F.data.startswith("wishlist_item:"))
    async def wishlist_item_callback(callback: types.CallbackQuery):
        try:
            item_id = int(callback.data.split(":", maxsplit=1)[1])
        except (IndexError, ValueError):
            await callback.answer("Invalid wishlist item.", show_alert=True)
            return

        await _confirm_or_delete_wishlist_item(database, state, callback, item_id)

    @dp.callback_query(F.data.startswith("wishlist_delete:"))
    async def wishlist_delete_callback(callback: types.CallbackQuery):
        try:
            item_id = int(callback.data.split(":", maxsplit=1)[1])
        except (IndexError, ValueError):
            await callback.answer("Invalid wishlist item.", show_alert=True)
            return

        await _confirm_or_delete_wishlist_item(database, state, callback, item_id)

    async def _refresh_wishlist_message(
        callback: types.CallbackQuery, item, is_owner: bool, viewer_id: int
    ):
        await callback.message.edit_text(
            _wishlist_item_text(item, viewer_id),
            reply_markup=wishlist_item_keyboard(item, viewer_id, is_owner=is_owner),
            disable_web_page_preview=False,
        )

    @dp.callback_query(F.data.startswith("wishlist_reserve:"))
    async def wishlist_reserve_callback(callback: types.CallbackQuery):
        try:
            item_id = int(callback.data.split(":", maxsplit=1)[1])
        except (IndexError, ValueError):
            await callback.answer("Invalid wishlist item.", show_alert=True)
            return

        user_id = callback.from_user.id
        item = await _get_visible_wishlist_item(database, item_id, user_id)
        if not item:
            await callback.answer("This wish is no longer available.", show_alert=True)
            return

        success = await database.reserve_wishlist_item(item_id, user_id)
        if not success:
            await callback.answer("Someone else already reserved this wish.", show_alert=True)
            return

        item = await database.get_wishlist_item(item_id)
        await _refresh_wishlist_message(callback, item, item["owner_telegram_id"] == user_id, user_id)
        await callback.answer("Reserved!")

    @dp.callback_query(F.data.startswith("wishlist_unreserve:"))
    async def wishlist_unreserve_callback(callback: types.CallbackQuery):
        try:
            item_id = int(callback.data.split(":", maxsplit=1)[1])
        except (IndexError, ValueError):
            await callback.answer("Invalid wishlist item.", show_alert=True)
            return

        user_id = callback.from_user.id
        item = await _get_visible_wishlist_item(database, item_id, user_id)
        if not item:
            await callback.answer("This wish is no longer available.", show_alert=True)
            return

        success = await database.unreserve_wishlist_item(item_id, user_id)
        if not success:
            await callback.answer("You can't unreserve this wish.", show_alert=True)
            return

        item = await database.get_wishlist_item(item_id)
        await _refresh_wishlist_message(callback, item, item["owner_telegram_id"] == user_id, user_id)
        await callback.answer("Unreserved.")

    @dp.callback_query(F.data.startswith("wishlist_got:"))
    async def wishlist_got_callback(callback: types.CallbackQuery):
        try:
            item_id = int(callback.data.split(":", maxsplit=1)[1])
        except (IndexError, ValueError):
            await callback.answer("Invalid wishlist item.", show_alert=True)
            return

        user_id = callback.from_user.id
        item = await database.get_wishlist_item(item_id)
        if not item or item.get("owner_telegram_id") != user_id:
            await callback.answer("This wish is not available.", show_alert=True)
            return

        success = await database.mark_wishlist_item_gotten(user_id, item_id)
        if not success:
            await callback.answer("This wish was already handled.", show_alert=True)
            return

        try:
            await callback.message.delete()
        except Exception as e:  # noqa: BLE001
            print(f"[WARN] Failed to delete wish message {item_id}: {e}")
        await callback.answer("Marked as received!", show_alert=False)

    @dp.callback_query(F.data.startswith("wishlist_delete_confirm:"))
    async def wishlist_delete_confirm_callback(callback: types.CallbackQuery):
        try:
            item_id = int(callback.data.split(":", maxsplit=1)[1])
        except (IndexError, ValueError):
            await callback.answer("Invalid wishlist item.", show_alert=True)
            return

        await _confirm_or_delete_wishlist_item(database, state, callback, item_id)

    @dp.callback_query(F.data == "wishlist_delete_cancel")
    async def wishlist_delete_cancel_callback(callback: types.CallbackQuery):
        state.wishlist_delete_confirmations.pop(callback.from_user.id, None)
        await callback.answer("Deletion cancelled.")
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception as e:  # noqa: BLE001
            print(f"[WARN] Failed to clear wishlist deletion prompt: {e}")

    @dp.message(F.text == WISHLIST_DELETE_CONFIRM_TEXT)
    async def wishlist_delete_via_keyboard(message: types.Message):
        await _delete_wishlist_item_from_confirmation(database, state, message)

    @dp.callback_query(F.data == "wishlist_show")
    async def wishlist_show_callback(callback: types.CallbackQuery):
        state.set_submenu(callback.from_user.id, "wishlist")
        await send_wishlist_items(database, callback.message, callback.from_user.id)
        await callback.answer()

    return {
        "add_links_to_wishlist": add_links_to_wishlist,
        "send_editable_wishlist": send_editable_wishlist,
        "send_wishlist_items": send_wishlist_items,
        "_wishlist_item_text": _wishlist_item_text,
        "wishlist_item_keyboard": wishlist_item_keyboard,
    }
