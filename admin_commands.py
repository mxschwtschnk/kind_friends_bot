from aiogram import F, types


def register_admin_handlers(dp, bot, settings, database, generic_handler):
    """Register admin-only commands."""

    ADMIN_ID = settings.admin_id

    @dp.message(F.text == "/admin")
    async def admin_handler(message: types.Message):
        """
        Simple admin stats, available only for ADMIN_ID.
        """
        if message.from_user.id != ADMIN_ID:
            return

        async with database.pool.acquire() as conn:
            users_count = await conn.fetchval("SELECT COUNT(*) FROM users;")
            paused_count = await conn.fetchval(
                "SELECT COUNT(*) FROM users WHERE is_paused = TRUE;"
            )
            friendships_count = await conn.fetchval("SELECT COUNT(*) FROM friendships;")
            pending_count = await conn.fetchval("SELECT COUNT(*) FROM pending_links;")
            sent_links_total = await conn.fetchval(
                "SELECT COALESCE(SUM(sent_links_count), 0) FROM users;"
            )
            invites_sent_total = await conn.fetchval(
                "SELECT COALESCE(SUM(invites_sent_count), 0) FROM users;"
            )

        text = (
            "📊 **Kind Friends — Admin Panel**\n\n"
            f"👥 Total users: **{users_count}**\n"
            f"⏸ Paused users: **{paused_count}**\n"
            f"🔗 Friend connections: **{friendships_count}**\n"
            f"📥 Pending links (stored for paused): **{pending_count}**\n"
            f"📤 Total links sent attempts: **{sent_links_total}**\n"
            f"✉️ Total invites sent: **{invites_sent_total}**\n"
        )

        await message.answer(text, parse_mode="Markdown")

    @dp.message(F.text == "/testitnow")
    async def test_it_now_handler(message: types.Message):
        """Admin self-check command. Verifies link flow without broadcasting to users."""

        if message.from_user.id != ADMIN_ID:
            return

        await message.answer(
            "Running /testitnow — admin-only self-check (no broadcasts or user pings)…"
        )

        test_friend_id = ADMIN_ID + 999_999
        test_friend_username = "kind_friends_test"
        seeded_url = "https://example.com/kind-friends-seeded"
        live_url = "https://example.com/kind-friends-live"

        steps: list[str] = []

        def add_step(label: str, ok: bool, detail: str = ""):
            icon = "✅" if ok else "❌"
            suffix = f": {detail}" if detail else ""
            steps.append(f"{icon} {label}{suffix}")

        try:
            await database.get_or_create_user(
                message.from_user.id, message.from_user.username
            )
            await database.get_or_create_user(test_friend_id, test_friend_username)
            await database.set_pause(test_friend_id, True)
            await database.add_mutual_friendship(message.from_user.id, test_friend_id)
            await database.delete_pending_links_for_user(test_friend_id)
            await database.delete_pending_links_for_user(message.from_user.id)
            await database.clear_sent_links(message.from_user.id)
            add_step("Setup test users and friendship", True)
        except Exception as e:  # noqa: BLE001
            add_step("Setup test users and friendship", False, str(e))

        try:
            await database.save_pending_link(
                test_friend_id, message.from_user.id, seeded_url
            )
            add_step("Seeded pending link", True)
        except Exception as e:  # noqa: BLE001
            add_step("Seeded pending link", False, str(e))

        try:
            original_send_message = bot.send_message

            async def guarded_send_message(chat_id, *args, **kwargs):
                if chat_id == test_friend_id:
                    return None
                return await original_send_message(chat_id, *args, **kwargs)

            bot.send_message = guarded_send_message  # type: ignore[assignment]

            synthetic_message = message.model_copy(update={"text": live_url})
            await generic_handler(synthetic_message)
            add_step("Simulated link delivery", True)
        except Exception as e:  # noqa: BLE001
            add_step("Simulated link delivery", False, str(e))
        finally:
            bot.send_message = original_send_message  # type: ignore[assignment]

        try:
            await database.remove_friendship(message.from_user.id, test_friend_id)
            await database.delete_pending_links_for_user(test_friend_id)
            await database.delete_pending_links_for_user(message.from_user.id)
            add_step("Cleanup", True)
        except Exception as e:  # noqa: BLE001
            add_step("Cleanup", False, str(e))

        await message.answer(
            "🔧 /testitnow report (admin self-check only):\n" + "\n".join(steps)
        )

    @dp.message(
        F.text.startswith("/broadcast") | F.caption.startswith("/broadcast")
    )
    async def broadcast_handler(message: types.Message):
        """
        Broadcast a text message to all registered users, optionally with a photo.
        Only the configured ADMIN_ID can use this command.
        """

        if message.from_user.id != ADMIN_ID:
            return

        command_payload = (message.text or message.caption or "").split(maxsplit=1)
        if len(command_payload) < 2 or not command_payload[1].strip():
            await message.answer(
                "Usage: /broadcast <message to send to all users>.\n"
                "You can attach an image to send it with the message.",
            )
            return

        broadcast_text = command_payload[1].strip()
        photo_id = message.photo[-1].file_id if message.photo else None
        user_ids = await database.get_all_user_ids()

        delivered = 0
        failed = 0

        for user_id in user_ids:
            try:
                if photo_id:
                    await bot.send_photo(user_id, photo_id, caption=broadcast_text)
                else:
                    await bot.send_message(user_id, broadcast_text)
                delivered += 1
            except Exception as e:  # noqa: BLE001
                print(f"[WARN] Failed to broadcast to {user_id}: {e}")
                failed += 1

        await message.answer(
            f"Broadcast complete. Delivered: {delivered}. Failed: {failed}.",
        )
