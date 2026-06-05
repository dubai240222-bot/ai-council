$ErrorActionPreference = "Stop"

$Server = "root@YOUR_VPS_IP"
$RemoteDir = "/root/reklama_bot"

Write-Host "1/4 Backing up SQLite database..." -ForegroundColor Cyan
ssh $Server "cd $RemoteDir && if [ -f bot.sqlite3 ]; then mkdir -p backups && cp bot.sqlite3 backups/bot.sqlite3.backup.`$(date +%Y%m%d_%H%M%S); fi"

Write-Host "2/4 Sending bot.py to VPS..." -ForegroundColor Cyan
scp ".\bot.py" "$Server`:$RemoteDir/"

Write-Host "3/4 Restarting bot service..." -ForegroundColor Cyan
ssh $Server "systemctl restart reklama-bot"

Write-Host "4/4 Checking status..." -ForegroundColor Cyan
ssh $Server "systemctl status reklama-bot --no-pager"

Write-Host "Done." -ForegroundColor Green
