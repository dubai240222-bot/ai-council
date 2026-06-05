# Product Tasks

## Current Goal

Turn the working Telegram autopost bot into a convenient product for group admins.

## Near-Term Tasks

- Make the Telegram admin cabinet easier to navigate.
- Improve preview and editing before scheduling.
- Make copying ads to another group clear and safe.
- Add package logic for paid placements: 50, 100, 200 posts.
- Add reports for admins: published, remaining, end date.
- Prepare a desktop web admin panel for PC usage.

## Safety Rules

- Do not commit `.env`.
- Do not commit `bot.sqlite3` or backups.
- Do not hard-code bot tokens in Python files.
- Keep the VPS deploy script local, or use `deploy.example.ps1` as a template.

## VPS Notes

- Project directory on VPS: `/root/reklama_bot`
- Systemd service: `reklama-bot`
- Local deploy command:

```powershell
powershell -ExecutionPolicy Bypass -File .\deploy.ps1
```
