from dataclasses import dataclass
import os


@dataclass
class Settings:
    bot_token: str
    database_url: str
    bot_username: str
    admin_id: int
    feedback_recipient_chat_id: int
    max_friends: int
    admin_max_friends: int
    max_daily_links: int
    alpha_users: list[int]
    alpha_users_max_friends: int


class SettingsLoaderError(RuntimeError):
    pass


def _get_required_int_env(var_name: str) -> int:
    value = os.getenv(var_name)
    if value is None or value.strip() == "":
        raise SettingsLoaderError(f"{var_name} is not set")

    try:
        return int(value)
    except ValueError as exc:
        raise SettingsLoaderError(f"{var_name} must be an integer") from exc


def _parse_id_list(raw: str | None, var_name: str) -> list[int]:
    if raw is None:
        return []

    normalized = raw.strip()
    if normalized == "":
        return []

    # Support JSON-like lists such as "[1,2,3]" while still accepting plain
    # comma-separated values. This also helps when the env var contains
    # '[""]' or similar placeholders.
    if normalized.startswith("[") and normalized.endswith("]"):
        normalized = normalized[1:-1]

    ids: list[int] = []
    for tg_id in normalized.split(","):
        cleaned = tg_id.strip().strip("\"").strip("'").strip()
        if not cleaned:
            continue

        try:
            ids.append(int(cleaned))
        except ValueError as exc:
            raise SettingsLoaderError(f"{var_name} must be a comma-separated list of integers") from exc

    return ids


def load_settings() -> Settings:
    bot_token = os.getenv("BOT_TOKEN")
    database_url = os.getenv("DATABASE_URL")
    bot_username = os.getenv("BOT_USERNAME", "KindFriendsBot")
    admin_id = _get_required_int_env("ADMIN_ID")
    max_friends = _get_required_int_env("MAX_FRIENDS")
    admin_max_friends = _get_required_int_env("ADMIN_MAX_FRIENDS")
    max_daily_links = _get_required_int_env("MAX_DAILY_LINKS")
    alpha_users_raw = os.getenv("ALPHA_USERS", "")
    alpha_users_max_friends = _get_required_int_env("ALPHA_USERS_MAX_FRIENDS")

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
        max_friends=max_friends,
        admin_max_friends=admin_max_friends,
        max_daily_links=max_daily_links,
        alpha_users=_parse_id_list(alpha_users_raw, "ALPHA_USERS"),
        alpha_users_max_friends=alpha_users_max_friends,
    )
