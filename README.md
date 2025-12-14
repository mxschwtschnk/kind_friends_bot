# Kind Friends

Kind Friends is a Telegram bot for sharing links with up to 15 friends, featuring wishlists and pause/resume.

## Setup
- Install deps with `pip install -r requirements.txt`.
- Set env vars: `BOT_TOKEN`, `DATABASE_URL`, `ADMIN_ID`, `MAX_DAILY_LINKS`.

## Architecture
- Modular structure: `kind_friends/domain/` (models), `services/` (logic), `repositories/` (DB), `handlers/` (UI).

## AI Guidelines
- For friend features, edit `handlers/friends.py`.
- Avoid `bot.py` for new code. Use services for business logic.

## Codex Tips
- Focus on one module per change to save tokens.
- Reference this README for context.
