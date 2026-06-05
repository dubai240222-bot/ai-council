# Telegram Autopost Rental Bot

Working Telegram bot for scheduled advertising posts in groups.

The bot is already running on a VPS. This repository is for improving the product safely with Cursor, Codex, and GitHub.

## What It Does

- Super admin gives paid access to renters for a selected period.
- Each renter sees only their own groups and ads.
- Ads can contain text, photo, video, GIF, document, or photo album.
- Ads can be scheduled by start time, end time, and interval.
- Access expiration stops future publishing but keeps the cabinet and settings.
- Ads can be copied to another group with the same content and schedule.

## Important Security Notes

Never commit real secrets:

- `.env`
- bot token
- SQLite database
- backups
- private deploy script with real VPS address

If the bot token was shown in chats or screenshots, regenerate it in `@BotFather` and update `.env` on the VPS.

## Local Files

- `bot.py` - main bot code
- `web_admin.py` - simple desktop web cabinet for owner and renters
- `requirements.txt` - Python dependencies
- `.env.example` - environment template
- `deploy.example.ps1` - safe deploy template
- `TASKS.md` - product roadmap and working notes

The real local deploy script can be named `deploy.ps1` and is ignored by Git.

## VPS

Typical VPS project directory:

```bash
/root/reklama_bot
```

Typical service name:

```bash
reklama-bot
```

Check status:

```bash
systemctl status reklama-bot --no-pager
```

Check recent logs:

```bash
journalctl -u reklama-bot -n 80 --no-pager
```

## Local Deploy

From the project folder on Windows:

```powershell
powershell -ExecutionPolicy Bypass -File .\deploy.ps1
```

The deploy script should:

1. Back up SQLite database on VPS.
2. Copy `bot.py` to VPS.
3. Restart `reklama-bot`.
4. Show service status.

## Web Admin Preview

The first web admin version is a mini-site for PC usage.

Run locally or on VPS:

```bash
uvicorn web_admin:app --host 0.0.0.0 --port 8000
```

Owner login is configured in `.env`:

```env
WEB_OWNER_LOGIN=owner
WEB_OWNER_PASSWORD=change-this-password
WEB_SECRET=change-this-long-random-secret
WEB_CABINET_URL=https://your-domain.example/login
```

Owner can set renter web logins/passwords. Renters can log in while their paid access is active and see their groups, ads, publication counts, and access end date.
