import os

from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

from handlers.admin import register_admin_handlers
from handlers.friends import register_friends_handlers
from handlers.general import register_general_handlers
from handlers.links import register_links_handlers
from handlers.state import BotState
from handlers.wishlist import register_wishlist_handlers
from kind_friends.config import load_settings
from kind_friends.repositories import Database
from kind_friends.services.friend_service import FriendService

settings = load_settings()

bot = Bot(token=settings.bot_token)
dp = Dispatcher()
database = Database(settings.database_url)
friend_service = FriendService(database, settings)  # kept for future business-logic reuse
state = BotState()


async def init_db():
    await database.connect()


def register_handlers():
    register_general_handlers(dp, bot, settings, database, state)
    register_wishlist_handlers(dp, bot, database, state)
    register_friends_handlers(dp, bot, settings, database, state)
    generic_handler = register_links_handlers(dp, bot, settings, database, state)
    register_admin_handlers(dp, bot, settings, database, generic_handler)


register_handlers()


async def on_startup(app):
    print("Connecting to database...")
    await init_db()

    webhook_url = os.getenv("WEBHOOK_URL")
    if not webhook_url:
        raise RuntimeError("WEBHOOK_URL is not set")

    await bot.set_webhook(webhook_url, drop_pending_updates=True)
    print(f"Webhook set to {webhook_url}")


async def on_shutdown(app):
    await bot.session.close()
    await database.close()
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
