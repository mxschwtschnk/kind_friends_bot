import asyncpg
from datetime import date, datetime
from typing import Iterable


class Database:
    def __init__(self, dsn: str):
        self.dsn = dsn
        self.pool: asyncpg.Pool | None = None

    async def connect(self):
        self.pool = await asyncpg.create_pool(self.dsn)
        await self._ensure_schema()

    async def close(self):
        if self.pool:
            await self.pool.close()

    async def _ensure_schema(self):
        if not self.pool:
            raise RuntimeError("Pool not initialized")
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id SERIAL PRIMARY KEY,
                    telegram_id BIGINT UNIQUE NOT NULL,
                    username TEXT,
                    is_paused BOOLEAN NOT NULL DEFAULT FALSE,
                    sent_links_count BIGINT NOT NULL DEFAULT 0,
                    invites_sent_count BIGINT NOT NULL DEFAULT 0,
                    sent_links_today_count INT NOT NULL DEFAULT 0,
                    sent_links_date DATE,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                """
            )
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS friendships (
                    id SERIAL PRIMARY KEY,
                    user_telegram_id BIGINT NOT NULL,
                    friend_telegram_id BIGINT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    UNIQUE (user_telegram_id, friend_telegram_id)
                );
                """
            )
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS pending_friend_requests (
                    id SERIAL PRIMARY KEY,
                    requester_telegram_id BIGINT NOT NULL,
                    recipient_telegram_id BIGINT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    UNIQUE (requester_telegram_id, recipient_telegram_id)
                );
                """
            )
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS pending_links (
                    id SERIAL PRIMARY KEY,
                    recipient_telegram_id BIGINT NOT NULL,
                    sender_telegram_id BIGINT NOT NULL,
                    url TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                """
            )
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sent_links_history (
                    sender_telegram_id BIGINT NOT NULL,
                    url TEXT NOT NULL,
                    sent_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    PRIMARY KEY (sender_telegram_id, url)
                );
                """
            )
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS session_states (
                    user_telegram_id BIGINT PRIMARY KEY,
                    flow TEXT,
                    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    CONSTRAINT fk_user FOREIGN KEY (user_telegram_id)
                        REFERENCES users(telegram_id) ON DELETE CASCADE
                );
                """
            )

    async def get_or_create_user(self, tg_id: int, username: str | None) -> bool:
        if not self.pool:
            raise RuntimeError("Pool not initialized")
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, is_paused FROM users WHERE telegram_id=$1",
                tg_id,
            )
            if row:
                await conn.execute(
                    "UPDATE users SET username=$1 WHERE telegram_id=$2",
                    username,
                    tg_id,
                )
                return row["is_paused"]

            await conn.execute(
                "INSERT INTO users (telegram_id, username) VALUES ($1, $2)",
                tg_id,
                username,
            )
            return False

    async def set_pause(self, tg_id: int, paused: bool):
        if not self.pool:
            raise RuntimeError("Pool not initialized")
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id FROM users WHERE telegram_id=$1",
                tg_id,
            )
            if row:
                await conn.execute(
                    "UPDATE users SET is_paused=$1 WHERE telegram_id=$2",
                    paused,
                    tg_id,
                )
            else:
                await conn.execute(
                    "INSERT INTO users (telegram_id, is_paused) VALUES ($1, $2)",
                    tg_id,
                    paused,
                )

    async def is_paused(self, tg_id: int) -> bool:
        if not self.pool:
            raise RuntimeError("Pool not initialized")
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT is_paused FROM users WHERE telegram_id=$1",
                tg_id,
            )
        return row["is_paused"] if row else False

    async def get_telegram_id_by_username(self, username: str | None):
        if not self.pool:
            raise RuntimeError("Pool not initialized")
        if not username:
            return None
        username = username.lstrip("@").strip().lower()
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT telegram_id FROM users WHERE LOWER(username)=$1",
                username,
            )
        return row["telegram_id"] if row else None

    async def add_mutual_friendship(self, a_tg_id: int, b_tg_id: int):
        if not self.pool:
            raise RuntimeError("Pool not initialized")
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO friendships (user_telegram_id, friend_telegram_id)
                VALUES ($1, $2)
                ON CONFLICT (user_telegram_id, friend_telegram_id) DO NOTHING;
                """,
                a_tg_id,
                b_tg_id,
            )
            await conn.execute(
                """
                INSERT INTO friendships (user_telegram_id, friend_telegram_id)
                VALUES ($1, $2)
                ON CONFLICT (user_telegram_id, friend_telegram_id) DO NOTHING;
                """,
                b_tg_id,
                a_tg_id,
            )

    async def get_friend_usernames(self, tg_id: int):
        if not self.pool:
            raise RuntimeError("Pool not initialized")
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT u.username
                FROM friendships f
                JOIN users u ON u.telegram_id = f.friend_telegram_id
                WHERE f.user_telegram_id = $1
                ORDER BY u.username;
                """,
                tg_id,
            )
        return [r["username"] for r in rows if r["username"]]

    async def get_friend_count(self, tg_id: int) -> int:
        if not self.pool:
            raise RuntimeError("Pool not initialized")
        async with self.pool.acquire() as conn:
            return await conn.fetchval(
                """
                SELECT COUNT(*)
                FROM friendships
                WHERE user_telegram_id = $1;
                """,
                tg_id,
            )

    async def get_all_friend_ids(self, tg_id: int):
        if not self.pool:
            raise RuntimeError("Pool not initialized")
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT friend_telegram_id
                FROM friendships
                WHERE user_telegram_id = $1;
                """,
                tg_id,
            )
        return [r["friend_telegram_id"] for r in rows]

    async def remove_friendship(self, a_tg_id: int, b_tg_id: int):
        if not self.pool:
            raise RuntimeError("Pool not initialized")
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                DELETE FROM friendships
                WHERE (user_telegram_id=$1 AND friend_telegram_id=$2)
                   OR (user_telegram_id=$2 AND friend_telegram_id=$1);
                """,
                a_tg_id,
                b_tg_id,
            )

    async def count_pending_requests_for_requester(self, tg_id: int) -> int:
        if not self.pool:
            raise RuntimeError("Pool not initialized")
        async with self.pool.acquire() as conn:
            return await conn.fetchval(
                """
                SELECT COUNT(*)
                FROM pending_friend_requests
                WHERE requester_telegram_id = $1;
                """,
                tg_id,
            )

    async def get_pending_requests_for_requester(self, tg_id: int):
        if not self.pool:
            raise RuntimeError("Pool not initialized")
        async with self.pool.acquire() as conn:
            return await conn.fetch(
                """
                SELECT id, recipient_telegram_id
                FROM pending_friend_requests
                WHERE requester_telegram_id = $1;
                """,
                tg_id,
            )

    async def get_pending_requests_with_usernames_for_requester(self, tg_id: int):
        if not self.pool:
            raise RuntimeError("Pool not initialized")
        async with self.pool.acquire() as conn:
            return await conn.fetch(
                """
                SELECT p.id, p.recipient_telegram_id, u.username
                FROM pending_friend_requests p
                LEFT JOIN users u ON u.telegram_id = p.recipient_telegram_id
                WHERE p.requester_telegram_id = $1
                ORDER BY p.created_at DESC;
                """,
                tg_id,
            )

    async def get_username_by_telegram_id(self, tg_id: int) -> str | None:
        if not self.pool:
            raise RuntimeError("Pool not initialized")
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT username FROM users WHERE telegram_id=$1;",
                tg_id,
            )
        return row["username"] if row else None

    async def get_all_user_ids(self) -> list[int]:
        if not self.pool:
            raise RuntimeError("Pool not initialized")
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("SELECT telegram_id FROM users;")
        return [row["telegram_id"] for row in rows]

    async def get_pending_friend_request(self, requester_id: int, recipient_id: int):
        if not self.pool:
            raise RuntimeError("Pool not initialized")
        async with self.pool.acquire() as conn:
            return await conn.fetchrow(
                """
                SELECT id, requester_telegram_id, recipient_telegram_id
                FROM pending_friend_requests
                WHERE requester_telegram_id=$1 AND recipient_telegram_id=$2;
                """,
                requester_id,
                recipient_id,
            )

    async def create_friend_request(self, requester_id: int, recipient_id: int) -> int | None:
        if not self.pool:
            raise RuntimeError("Pool not initialized")
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO pending_friend_requests (requester_telegram_id, recipient_telegram_id)
                VALUES ($1, $2)
                ON CONFLICT (requester_telegram_id, recipient_telegram_id) DO NOTHING
                RETURNING id;
                """,
                requester_id,
                recipient_id,
            )
        return row["id"] if row else None

    async def get_friend_request_by_id(self, request_id: int):
        if not self.pool:
            raise RuntimeError("Pool not initialized")
        async with self.pool.acquire() as conn:
            return await conn.fetchrow(
                """
                SELECT id, requester_telegram_id, recipient_telegram_id
                FROM pending_friend_requests
                WHERE id=$1;
                """,
                request_id,
            )

    async def delete_friend_request(self, request_id: int):
        if not self.pool:
            raise RuntimeError("Pool not initialized")
        async with self.pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM pending_friend_requests WHERE id=$1;",
                request_id,
            )

    async def delete_friend_requests_for_user(self, tg_id: int):
        if not self.pool:
            raise RuntimeError("Pool not initialized")
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                DELETE FROM pending_friend_requests
                WHERE requester_telegram_id = $1
                   OR recipient_telegram_id = $1;
                """,
                tg_id,
            )

    async def save_pending_link(self, recipient_id: int, sender_id: int, url: str):
        if not self.pool:
            raise RuntimeError("Pool not initialized")
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO pending_links (recipient_telegram_id, sender_telegram_id, url)
                VALUES ($1, $2, $3);
                """,
                recipient_id,
                sender_id,
                url,
            )

    async def get_pending_links(self, recipient_id: int):
        if not self.pool:
            raise RuntimeError("Pool not initialized")
        async with self.pool.acquire() as conn:
            return await conn.fetch(
                """
                SELECT id, recipient_telegram_id, sender_telegram_id, url, created_at
                FROM pending_links
                WHERE recipient_telegram_id = $1
                ORDER BY created_at ASC;
                """,
                recipient_id,
            )

    async def delete_pending_link(self, link_id: int):
        if not self.pool:
            raise RuntimeError("Pool not initialized")
        async with self.pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM pending_links WHERE id = $1;",
                link_id,
            )

    async def delete_pending_links_for_user(self, tg_id: int):
        if not self.pool:
            raise RuntimeError("Pool not initialized")
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                DELETE FROM pending_links
                WHERE recipient_telegram_id = $1
                   OR sender_telegram_id = $1;
                """,
                tg_id,
            )

    async def increment_sent_links(self, sender_id: int, *, urls_today: int, total_urls: int):
        if not self.pool:
            raise RuntimeError("Pool not initialized")
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE users
                SET sent_links_today_count = $1,
                    sent_links_count = $2,
                    sent_links_date = $3
                WHERE telegram_id = $4;
                """,
                urls_today,
                total_urls,
                date.today(),
                sender_id,
            )

    async def get_sent_link_counts(self, sender_id: int):
        if not self.pool:
            raise RuntimeError("Pool not initialized")
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT sent_links_today_count, sent_links_date, sent_links_count
                FROM users
                WHERE telegram_id = $1;
                """,
                sender_id,
            )
            if row:
                return (
                    int(row["sent_links_today_count"]),
                    row["sent_links_date"],
                    int(row["sent_links_count"]),
                )
            return 0, None, 0

    async def save_sent_link_history(self, sender_id: int, url: str):
        if not self.pool:
            raise RuntimeError("Pool not initialized")
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO sent_links_history (sender_telegram_id, url, sent_at)
                VALUES ($1, $2, $3)
                ON CONFLICT (sender_telegram_id, url) DO NOTHING;
                """,
                sender_id,
                url,
                datetime.now(),
            )

    async def has_recently_sent_link(self, sender_id: int, url: str) -> bool:
        if not self.pool:
            raise RuntimeError("Pool not initialized")
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT sent_at
                FROM sent_links_history
                WHERE sender_telegram_id = $1 AND url = $2;
                """,
                sender_id,
                url,
            )
        return bool(row)

    async def delete_sent_link_history_for_user(self, tg_id: int):
        if not self.pool:
            raise RuntimeError("Pool not initialized")
        async with self.pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM sent_links_history WHERE sender_telegram_id = $1;",
                tg_id,
            )

    async def delete_user_completely(self, tg_id: int):
        if not self.pool:
            raise RuntimeError("Pool not initialized")
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                DELETE FROM friendships
                WHERE user_telegram_id = $1
                   OR friend_telegram_id = $1;
                """,
                tg_id,
            )
            await conn.execute(
                """
                DELETE FROM pending_friend_requests
                WHERE requester_telegram_id = $1
                   OR recipient_telegram_id = $1;
                """,
                tg_id,
            )
            await conn.execute(
                """
                DELETE FROM pending_links
                WHERE recipient_telegram_id = $1
                   OR sender_telegram_id = $1;
                """,
                tg_id,
            )
            await conn.execute(
                "DELETE FROM users WHERE telegram_id = $1;",
                tg_id,
            )

    async def increment_invite_count(self, requester_id: int):
        if not self.pool:
            raise RuntimeError("Pool not initialized")
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE users
                SET invites_sent_count = invites_sent_count + 1
                WHERE telegram_id = $1;
                """,
                requester_id,
            )

    async def get_invite_count(self, requester_id: int) -> int:
        if not self.pool:
            raise RuntimeError("Pool not initialized")
        async with self.pool.acquire() as conn:
            count = await conn.fetchval(
                "SELECT invites_sent_count FROM users WHERE telegram_id = $1;",
                requester_id,
            )
        return int(count) if count is not None else 0

    async def clear_sent_links(self, sender_id: int):
        if not self.pool:
            raise RuntimeError("Pool not initialized")
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE users
                SET sent_links_today_count = 0, sent_links_date = NULL
                WHERE telegram_id = $1;
                """,
                sender_id,
            )

    async def bulk_delete_users(self, telegram_ids: Iterable[int]):
        ids = list(telegram_ids)
        if not ids:
            return
        if not self.pool:
            raise RuntimeError("Pool not initialized")
        async with self.pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM users WHERE telegram_id = ANY($1::bigint[]);",
                ids,
            )

    async def set_session_state(self, tg_id: int, flow: str, metadata: dict):
        if not self.pool:
            raise RuntimeError("Pool not initialized")
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO session_states (user_telegram_id, flow, metadata, updated_at)
                VALUES ($1, $2, $3, NOW())
                ON CONFLICT (user_telegram_id) DO UPDATE
                SET flow = EXCLUDED.flow,
                    metadata = EXCLUDED.metadata,
                    updated_at = NOW();
                """,
                tg_id,
                flow,
                metadata,
            )

    async def clear_session_state(self, tg_id: int):
        if not self.pool:
            raise RuntimeError("Pool not initialized")
        async with self.pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM session_states WHERE user_telegram_id = $1;",
                tg_id,
            )

    async def get_session_state(self, tg_id: int) -> tuple[str | None, dict]:
        if not self.pool:
            raise RuntimeError("Pool not initialized")
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT flow, metadata FROM session_states WHERE user_telegram_id = $1;",
                tg_id,
            )
        if not row:
            return None, {}
        return row["flow"], row["metadata"] or {}

    async def update_session_metadata(self, tg_id: int, metadata: dict):
        if not self.pool:
            raise RuntimeError("Pool not initialized")
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO session_states (user_telegram_id, metadata)
                VALUES ($1, $2)
                ON CONFLICT (user_telegram_id) DO UPDATE
                SET metadata = session_states.metadata || EXCLUDED.metadata,
                    updated_at = NOW();
                """,
                tg_id,
                metadata,
            )
