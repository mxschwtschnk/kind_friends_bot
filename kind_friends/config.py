from dataclasses import dataclass
import os


@dataclass
class Settings:
    bot_token: str
    database_url: str
    bot_username: str
    admin_id: int
    feedback_recipient_chat_id: int
    max_friends: int = 15
    admin_max_friends: int = 50
    max_daily_links: int = 5


class SettingsLoaderError(RuntimeError):
    pass


def load_settings() -> Settings:
    bot_token = os.getenv("BOT_TOKEN")
    database_url = os.getenv("DATABASE_URL")
    bot_username = os.getenv("BOT_USERNAME", "KindFriendsBot")
    admin_id = int(os.getenv("ADMIN_ID", "0"))

    feedback_raw = os.getenv("FEEDBACK_RECIPIENT_CHAT_ID")
    feedback_recipient = int(feedback_raw) if feedback_raw else admin_id

    if not bot_token:
        raise SettingsLoaderError("BOT_TOKEN is not set")
    if not database_url:
        raise SettingsLoaderError("DATABASE_URL is not set")

    return Settings(
        bot_token=bot_token,
        database_url=database_url,
        bot_username=bot_username,
        admin_id=admin_id,
        feedback_recipient_chat_id=feedback_recipient,
    )
