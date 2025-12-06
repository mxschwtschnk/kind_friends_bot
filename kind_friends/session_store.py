"""Session store for tracking per-user flow state across workers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .repositories import Database


@dataclass
class SessionState:
    """Represents the current flow state for a user."""

    flow: str | None
    metadata: dict[str, Any]


class SessionStore:
    """Persisted session storage backed by the database."""

    def __init__(self, database: Database):
        self.database = database

    async def set_flow(
        self,
        user_id: int,
        flow: str,
        metadata: dict[str, Any] | None = None,
        username: str | None = None,
    ):
        """Set the user's current flow with optional metadata.

        Ensures the user record exists to satisfy the foreign key constraint
        on ``session_states``. This lets the session store be used safely even
        if the user interacts before ``/start`` initializes their record.
        """

        await self.database.get_or_create_user(user_id, username)
        await self.database.set_session_state(user_id, flow, metadata or {})

    async def clear_flow(self, user_id: int):
        """Clear any active flow for the user."""

        await self.database.clear_session_state(user_id)

    async def get_state(self, user_id: int) -> SessionState:
        """Fetch the current state for the user."""

        flow, metadata = await self.database.get_session_state(user_id)
        return SessionState(flow=flow, metadata=metadata)

    async def is_in_flow(self, user_id: int, flow: str) -> bool:
        """Check whether the user is currently in a specific flow."""

        current_flow, _ = await self.database.get_session_state(user_id)
        return current_flow == flow

    async def update_metadata(self, user_id: int, metadata: dict[str, Any]):
        """Merge metadata for the current flow."""

        await self.database.update_session_metadata(user_id, metadata)
