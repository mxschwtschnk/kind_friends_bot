from dataclasses import dataclass
from datetime import datetime


@dataclass
class FriendRequest:
    id: int
    requester_id: int
    recipient_id: int
    created_at: datetime | None = None


@dataclass
class PendingLink:
    id: int
    recipient_id: int
    sender_id: int
    url: str
    created_at: datetime | None = None
