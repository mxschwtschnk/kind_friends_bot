from typing import Optional

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from kind_friends.todo.store import ListStore, Task, ToDoList

store = ListStore()
wait_mode: dict[int, Optional[str]] = {}


def escape_md(text: str) -> str:
    escape_chars = r"\\_*[]()~`>#+-=|{}.!"
    return "".join(f"\\{c}" if c in escape_chars else c for c in text)


def render_list(todo_list: ToDoList) -> str:
    lines = []
    if todo_list.title:
        lines.append(f"*{escape_md(todo_list.title)}*")
    else:
        lines.append("_Untitled_")

    for task in todo_list.tasks:
        escaped_text = escape_md(task.text)
        if task.done:
            lines.append(f"❌ ☑ ~{escaped_text}~")
        else:
            lines.append(f"❌ ☐ {escaped_text}")

    return "\n".join(lines)


def task_buttons(task: Task) -> list[InlineKeyboardButton]:
    toggle_text = "☑" if task.done else "☐"
    return [
        InlineKeyboardButton(text="❌", callback_data=f"task:del:{task.id}"),
        InlineKeyboardButton(text=toggle_text, callback_data=f"task:toggle:{task.id}"),
    ]


def main_buttons() -> list[list[InlineKeyboardButton]]:
    return [
        [InlineKeyboardButton(text="📝 Rename list", callback_data="title:set")],
        [InlineKeyboardButton(text="➕ Add task", callback_data="task:add")],
        [InlineKeyboardButton(text="🗑️ Delete list", callback_data="list:del")],
    ]


def build_keyboard(todo_list: ToDoList) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for task in todo_list.tasks:
        rows.append(task_buttons(task))
    rows.extend(main_buttons())
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def send_anchor(chat_id: int, todo_list: ToDoList, bot: Bot) -> None:
    text = render_list(todo_list)
    markup = build_keyboard(todo_list)
    msg = await bot.send_message(
        chat_id, text, reply_markup=markup, parse_mode="MarkdownV2"
    )
    store.set_anchor(todo_list.owner_id, msg.chat.id, msg.message_id)


async def update_anchor(todo_list: ToDoList, bot: Bot) -> None:
    anchor = store.get_anchor(todo_list.owner_id)
    text = render_list(todo_list)
    markup = build_keyboard(todo_list)
    if anchor:
        chat_id, message_id = anchor
        try:
            await bot.edit_message_text(
                text,
                chat_id,
                message_id,
                reply_markup=markup,
                parse_mode="MarkdownV2",
            )
            return
        except Exception:
            pass
    target_chat = anchor[0] if anchor else todo_list.owner_id
    await send_anchor(target_chat, todo_list, bot)


async def handle_newlist(message: Message) -> None:
    user_id = message.from_user.id
    if store.has_list(user_id):
        await message.answer("You already have a list. Delete it first.")
        return
    todo_list = store.create_list(user_id)
    await send_anchor(message.chat.id, todo_list, message.bot)


async def handle_mylist(message: Message) -> None:
    user_id = message.from_user.id
    if store.has_list(user_id):
        text = "You have 1 list."
        markup = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="Open list", callback_data="list:open")]]
        )
    else:
        text = "You have 0 lists."
        markup = None
    await message.answer(text, reply_markup=markup)


async def handle_text(message: Message) -> None:
    user_id = message.from_user.id
    mode = wait_mode.get(user_id)
    if not mode:
        return

    todo_list = store.get_list(user_id)
    if not todo_list:
        wait_mode[user_id] = None
        await message.answer("Create a list first with /newlist.")
        return

    anchor = store.get_anchor(user_id)
    status_text = "Saving title..." if mode == "title" else "Adding task..."
    if anchor:
        chat_id, message_id = anchor
        try:
            await message.bot.edit_message_text(status_text, chat_id, message_id)
        except Exception:
            pass

    if mode == "title":
        store.set_title(user_id, message.text)
    elif mode == "task":
        store.add_task(user_id, message.text)

    wait_mode[user_id] = None
    await send_anchor(message.chat.id, todo_list, message.bot)


async def on_title(callback: CallbackQuery) -> None:
    wait_mode[callback.from_user.id] = "title"
    await callback.answer("Send new title.")


async def on_add_task(callback: CallbackQuery) -> None:
    wait_mode[callback.from_user.id] = "task"
    await callback.answer("Send task text.")


async def on_toggle_task(callback: CallbackQuery) -> None:
    user_id = callback.from_user.id
    task_id = int(callback.data.split(":")[-1])
    todo_list = store.get_list(user_id)
    if not todo_list:
        await callback.answer("No list found.", show_alert=True)
        return

    store.toggle_task(user_id, task_id)
    await update_anchor(todo_list, callback.bot)
    await callback.answer()


async def on_delete_task(callback: CallbackQuery) -> None:
    user_id = callback.from_user.id
    task_id = int(callback.data.split(":")[-1])
    todo_list = store.get_list(user_id)
    if not todo_list:
        await callback.answer("No list found.", show_alert=True)
        return

    store.delete_task(user_id, task_id)
    await update_anchor(todo_list, callback.bot)
    await callback.answer()


async def on_delete_list(callback: CallbackQuery) -> None:
    user_id = callback.from_user.id
    anchor = store.get_anchor(user_id)
    store.delete_list(user_id)
    wait_mode[user_id] = None
    if anchor:
        chat_id, message_id = anchor
        try:
            await callback.bot.edit_message_text("List deleted.", chat_id, message_id)
        except Exception:
            pass
    await callback.answer("List deleted.")


async def on_open_list(callback: CallbackQuery) -> None:
    user_id = callback.from_user.id
    todo_list = store.get_list(user_id)
    if not todo_list:
        await callback.answer("No list found.", show_alert=True)
        return

    anchor = store.get_anchor(user_id)
    if anchor:
        chat_id, message_id = anchor
        try:
            await callback.bot.edit_message_text(
                render_list(todo_list),
                chat_id,
                message_id,
                reply_markup=build_keyboard(todo_list),
                parse_mode="MarkdownV2",
            )
            await callback.answer()
            return
        except Exception:
            pass

    await send_anchor(callback.message.chat.id, todo_list, callback.bot)
    await callback.answer()


def _is_waiting_for_input(message: Message) -> bool:
    return wait_mode.get(message.from_user.id) is not None


async def show_todo_home(message: Message) -> None:
    user_id = message.from_user.id
    has_list = store.has_list(user_id)
    if has_list:
        text = "You have 1 list."
        buttons = [[InlineKeyboardButton(text="Open list", callback_data="list:open")]]
    else:
        text = "You have 0 lists. Tap Create list to start."
        buttons = [[InlineKeyboardButton(text="Create list", callback_data="list:new")]]

    await message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


async def on_create_list(callback: CallbackQuery) -> None:
    user_id = callback.from_user.id
    if store.has_list(user_id):
        await callback.answer("You already have a list. Delete it first.", show_alert=True)
        return

    todo_list = store.create_list(user_id)
    wait_mode[user_id] = None
    await send_anchor(callback.message.chat.id, todo_list, callback.bot)
    await callback.answer()


def register_todo_handlers(dp: Dispatcher) -> None:
    dp.message.register(handle_newlist, Command("newlist"))
    dp.message.register(handle_mylist, Command("mylist"))
    dp.message.register(handle_text, F.text, _is_waiting_for_input)

    dp.callback_query.register(on_title, F.data == "title:set")
    dp.callback_query.register(on_add_task, F.data == "task:add")
    dp.callback_query.register(on_toggle_task, F.data.startswith("task:toggle:"))
    dp.callback_query.register(on_delete_task, F.data.startswith("task:del:"))
    dp.callback_query.register(on_delete_list, F.data == "list:del")
    dp.callback_query.register(on_open_list, F.data == "list:open")
    dp.callback_query.register(on_create_list, F.data == "list:new")
