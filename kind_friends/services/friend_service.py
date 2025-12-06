from dataclasses import dataclass
from datetime import date

from kind_friends.config import Settings
from kind_friends.repositories import Database


def normalize_username_input(raw: str) -> str:
    return raw.strip().lstrip("-").lstrip("@").strip()


def display_username(username: str | None, fallback: str = "friend") -> str:
    return f"@{username}" if username else fallback


def get_max_friends(settings: Settings, user_id: int) -> int:
    return settings.admin_max_friends if user_id == settings.admin_id else settings.max_friends


@dataclass
class FriendInviteDecision:
    allowed: bool
    reason: str | None = None


class FriendService:
    def __init__(self, repo: Database, settings: Settings):
        self.repo = repo
        self.settings = settings

    async def ensure_user(self, tg_id: int, username: str | None) -> bool:
        return await self.repo.get_or_create_user(tg_id, username)

    async def can_invite_more(self, tg_id: int) -> FriendInviteDecision:
        friend_count = await self.repo.get_friend_count(tg_id)
        max_friends = get_max_friends(self.settings, tg_id)
        if friend_count >= max_friends:
            return FriendInviteDecision(
                allowed=False,
                reason=(
                    "Your friend pool is full. Delete someone first if you want to connect with "
                    "more people."
                ),
            )
        return FriendInviteDecision(allowed=True)

    async def record_invite(self, requester_id: int):
        await self.repo.increment_invite_count(requester_id)

    async def reset_daily_links_if_needed(self, sender_id: int):
        sent_today, sent_date, total = await self.repo.get_sent_link_counts(sender_id)
        if sent_date and sent_date != date.today():
            await self.repo.clear_sent_links(sender_id)
            return 0, total
        return sent_today, total

    async def record_link_send(self, sender_id: int, urls_today: int, total_urls: int):
        await self.repo.increment_sent_links(sender_id, urls_today=urls_today, total_urls=total_urls)

    async def remove_friend(self, user_id: int, friend_id: int):
        await self.repo.remove_friendship(user_id, friend_id)

    async def delete_user(self, tg_id: int):
        await self.repo.delete_user_completely(tg_id)
