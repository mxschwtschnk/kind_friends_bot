import asyncio
import sys
from pathlib import Path
import types

import pytest

sys.path.append(str(Path(__file__).resolve().parents[1]))

from kind_friends.config import Settings
from kind_friends.services.friend_service import (
    FriendInviteDecision,
    FriendService,
    display_username,
    get_max_friends,
    normalize_username_input,
)


class DummyRepo:
    def __init__(self, friend_count=0):
        self.friend_count = friend_count
        self.removed = []
        self.deleted = []
        self.invites_recorded = 0

    async def get_or_create_user(self, tg_id: int, username: str | None):
        return False

    async def get_friend_count(self, tg_id: int) -> int:
        return self.friend_count

    async def increment_invite_count(self, requester_id: int):
        self.invites_recorded += 1

    async def get_sent_link_counts(self, sender_id: int):
        return 0, None, 0

    async def clear_sent_links(self, sender_id: int):
        return None

    async def increment_sent_links(self, sender_id: int, urls_today: int, total_urls: int):
        self.invites_recorded += 0

    async def remove_friendship(self, user_id: int, friend_id: int):
        self.removed.append((user_id, friend_id))

    async def delete_user_completely(self, tg_id: int):
        self.deleted.append(tg_id)


@pytest.fixture
def service():
    settings = Settings(
        bot_token="t",
        database_url="postgres://example",
        bot_username="bot",
        admin_id=1,
        feedback_recipient_chat_id=1,
        max_friends=2,
        admin_max_friends=3,
        max_daily_links=5,
    )
    repo = DummyRepo()
    return FriendService(repo, settings)


def test_normalize_username_input():
    assert normalize_username_input("-@Alice  ") == "Alice"


def test_display_username():
    assert display_username("alice") == "@alice"
    assert display_username(None, "fallback") == "fallback"


def test_get_max_friends_for_admin():
    settings = Settings(
        bot_token="t",
        database_url="postgres://example",
        bot_username="bot",
        admin_id=42,
        feedback_recipient_chat_id=42,
    )
    assert get_max_friends(settings, 42) == settings.admin_max_friends
    assert get_max_friends(settings, 1) == settings.max_friends


def test_can_invite_more_allows_under_limit(service: FriendService):
    decision = asyncio.run(service.can_invite_more(5))
    assert decision == FriendInviteDecision(allowed=True, reason=None)


def test_can_invite_more_blocks_when_full():
    settings = Settings(
        bot_token="t",
        database_url="postgres://example",
        bot_username="bot",
        admin_id=1,
        feedback_recipient_chat_id=1,
        max_friends=1,
        admin_max_friends=2,
        max_daily_links=5,
    )
    repo = DummyRepo(friend_count=1)
    service = FriendService(repo, settings)
    decision = asyncio.run(service.can_invite_more(5))
    assert decision.allowed is False
    assert "friend pool" in decision.reason


def test_record_invite_calls_repo(service: FriendService):
    asyncio.run(service.record_invite(99))
    assert service.repo.invites_recorded == 1


def test_reset_daily_links_clears_old_counts():
    class RepoWithHistory(DummyRepo):
        async def get_sent_link_counts(self, sender_id: int):
            return 2, "2020-01-01", 5

        async def clear_sent_links(self, sender_id: int):
            self.cleared = sender_id

    settings = Settings(
        bot_token="t",
        database_url="postgres://example",
        bot_username="bot",
        admin_id=1,
        feedback_recipient_chat_id=1,
    )
    repo = RepoWithHistory()
    service = FriendService(repo, settings)
    today_count, total = asyncio.run(service.reset_daily_links_if_needed(7))
    assert today_count == 0
    assert total == 5
    assert repo.cleared == 7


def test_remove_friend_and_delete_user(service: FriendService):
    asyncio.run(service.remove_friend(1, 2))
    asyncio.run(service.delete_user(3))
    assert service.repo.removed == [(1, 2)]
    assert service.repo.deleted == [3]
