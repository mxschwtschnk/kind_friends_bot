import os
import asyncio

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton

# BOT_TOKEN must be provided as an environment variable on the server (Render).
BOT_TOKEN = os.getenv("BOT_TOKEN")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set. Please set it in the environment variables.")


# Initialize bot and dispatcher
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


def main_keyboard(is_paused: bool) -> ReplyKeyboardMarkup:
    """
    Creates the main menu keyboard for the Kind Friends bot.
    For now pause/resume does not have real logic — only UI.
    """
    pause_button = "⏸ Pause" if not is_paused else "▶️ Resume"

    kb = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📤 Send a link")],
            [KeyboardButton(text="➕ Invite friends"), KeyboardButton(text="👥 Friends")],
            [KeyboardButton(text=pause_button)],
            [KeyboardButton(text="ℹ️ Help"), KeyboardButton(text="💡 Feedback")],
        ],
        resize_keyboard=True,
    )
    return kb


@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    """
    Handles /start.
    Sends a welcome message and shows the main keyboard.
    """
    text = (
        "Hi! I’m **Kind Friends** 👋\n\n"
        "I help friends share important links with each other.\n\n"
        "• Paste a link here – I will treat it as something you want to share.\n"
        "• Use the buttons below to invite friends, manage your list,\n"
        "  pause/resume, or send feedback.\n"
    )
    await message.answer(text, reply_markup=main_keyboard(is_paused=False))


@dp.message(F.text == "ℹ️ Help")
async def cmd_help(message: types.Message):
    """
    Explains how the bot works.
    """
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


@dp.message(F.text == "📤 Send a link")
async def btn_send_link(message: types.Message):
    """
    Button: Send a link.
    Currently just explains what to do.
    """
    await message.answer(
        "Send me a link you want to share with all your friends. Just paste a single URL."
    )


@dp.message(F.text == "⏸ Pause")
async def btn_pause(message: types.Message):
    """
    Placeholder pause mode.
    Real logic will be added later.
    """
    await message.answer(
        "You are now on pause (demo).\n"
        "In the MVP this will become a real pause that stops sending and receiving links.",
        reply_markup=main_keyboard(is_paused=True),
    )


@dp.message(F.text == "▶️ Resume")
async def btn_resume(message: types.Message):
    """
    Placeholder resume mode.
    """
    await message.answer(
        "Welcome back! (demo)\n"
        "In the MVP I will also show you a digest of what you missed.",
        reply_markup=main_keyboard(is_paused=False),
    )


@dp.message(F.text == "💡 Feedback")
async def btn_feedback(message: types.Message):
    """
    Starts feedback collection (demo mode).
    """
    await message.answer(
        "Please type your feedback or ideas in one message and send it here.\n\n"
        "(In this minimal version it will not be saved yet,\n"
        "but later it will be forwarded to the creator.)"
    )


@dp.message()
async def handle_message(message: types.Message):
    """
    Handles:
    • direct link messages
    • all unknown text
    """
    text = message.text or ""

    # Check if message looks like a link
    if text.startswith("http://") or text.startswith("https://"):
        await message.answer(
            "Got your link ✅\n"
            "In the real MVP I will send it to all your active friends.\n"
            "For now this is just a test response."
        )
        print(f"[DEBUG] User {message.from_user.id} sent link: {text}")
        return

    # Unknown text
    await message.answer(
        "I only understand links and the menu buttons for now.\n"
        "If you want to test sending, just paste a link (starting with http:// or https://)."
    )


async def main():
    """
    Entrypoint for running the bot with long polling.
    Render will execute this file directly.
    """
    print("Kind Friends bot is starting polling...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
