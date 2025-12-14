from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)


def main_keyboard(paused: bool) -> ReplyKeyboardMarkup:
    pause_button = KeyboardButton(text="▶️ Resume") if paused else KeyboardButton(text="⏸ Pause")
    if paused:
        keyboard = [
            [pause_button, KeyboardButton(text="🎁 Wishlist")],
            [KeyboardButton(text="ℹ️ Help")],
        ]
    else:
        keyboard = [
            [pause_button, KeyboardButton(text="👥 Friends"), KeyboardButton(text="🎁 Wishlist")],
            [KeyboardButton(text="ℹ️ Help")],
        ]

    return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)


def pause_overlay_keyboard() -> ReplyKeyboardMarkup:
    return main_keyboard(True)


def friends_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="My Friends"),
                KeyboardButton(text="Invite"),
                KeyboardButton(text="Remove"),
            ],
            [KeyboardButton(text="⬅️ Back")],
        ],
        resize_keyboard=True,
    )


def help_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="How to"),
                KeyboardButton(text="Feedback"),
                KeyboardButton(text="Wipe Account"),
            ],
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


def confirmation_keyboard(
    confirm_text: str, cancel_text: str = "Cancel"
) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=confirm_text)],
            [KeyboardButton(text=cancel_text)],
        ],
        resize_keyboard=True,
    )


def remove_friends_keyboard(friends: list[str]) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text=f"@{username}", callback_data=f"remove_friend:{username}")]
        for username in friends
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def wishlist_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="My Wishes"),
                KeyboardButton(text="Add"),
                KeyboardButton(text="Delete"),
            ],
            [KeyboardButton(text="⬅️ Back")],
        ],
        resize_keyboard=True,
    )


def link_action_keyboard(multiple: bool = False) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text="Add to 🎁", callback_data="link_action:wishlist")]
    ]
    if not multiple:
        buttons[0].append(
            InlineKeyboardButton(text="Send to 👥", callback_data="link_action:send")
        )
    buttons.append([InlineKeyboardButton(text="Cancel", callback_data="link_action:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def friend_list_keyboard(friends: list[str]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=f"@{username}", callback_data=f"friend_card:{username}")]
        for username in friends
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def friend_options_keyboard(username: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Remove", callback_data=f"friend_remove:{username}")],
            [InlineKeyboardButton(text="🎁 Wishlist", callback_data=f"friend_wishlist:{username}")],
        ]
    )
