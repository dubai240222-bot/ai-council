import base64
import hashlib
import hmac
import html
import json
import os
import secrets
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse

load_dotenv()

DATABASE_PATH = os.getenv("DATABASE_PATH", "bot.sqlite3")
TIMEZONE = ZoneInfo(os.getenv("TIMEZONE", "Europe/Istanbul"))
WEB_SECRET = os.getenv("WEB_SECRET") or os.getenv("BOT_TOKEN") or "change-me"
WEB_OWNER_LOGIN = os.getenv("WEB_OWNER_LOGIN", "owner")
WEB_OWNER_PASSWORD = os.getenv("WEB_OWNER_PASSWORD", "")
SESSION_COOKIE = "reklama_admin_session"

app = FastAPI(title="Reklama Bot Admin")


def db():
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def now_dt():
    return datetime.now(TIMEZONE).replace(second=0, microsecond=0)


def fmt(value):
    if not value:
        return "-"
    return datetime.fromisoformat(value).astimezone(TIMEZONE).strftime("%d.%m.%Y %H:%M")


def tenant_has_access(tenant):
    return bool(tenant and tenant["is_active"] and datetime.fromisoformat(tenant["access_until"]) >= now_dt())


def init_web_db():
    parent = Path(DATABASE_PATH).parent
    if str(parent) != ".":
        parent.mkdir(parents=True, exist_ok=True)
    with db() as conn:
        tenant_columns = {row["name"] for row in conn.execute("PRAGMA table_info(tenants)").fetchall()}
        if "web_login" not in tenant_columns:
            conn.execute("ALTER TABLE tenants ADD COLUMN web_login TEXT")
        if "web_password_hash" not in tenant_columns:
            conn.execute("ALTER TABLE tenants ADD COLUMN web_password_hash TEXT")
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_tenants_web_login
            ON tenants(web_login)
            WHERE web_login IS NOT NULL
            """
        )


def hash_password(password):
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 120_000)
    return f"pbkdf2_sha256${salt}${digest.hex()}"


def verify_password(password, password_hash):
    if not password_hash:
        return False
    try:
        method, salt, digest = password_hash.split("$", 2)
        if method != "pbkdf2_sha256":
            return False
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 120_000).hex()
        return hmac.compare_digest(actual, digest)
    except Exception:
        return False


def sign_payload(payload):
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    body = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    signature = hmac.new(WEB_SECRET.encode("utf-8"), body.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{body}.{signature}"


def read_session(request):
    token = request.cookies.get(SESSION_COOKIE)
    if not token or "." not in token:
        return None
    body, signature = token.rsplit(".", 1)
    expected = hmac.new(WEB_SECRET.encode("utf-8"), body.encode("ascii"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        return None
    try:
        padded = body + "=" * (-len(body) % 4)
        return json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
    except Exception:
        return None


async def form_data(request):
    raw = (await request.body()).decode("utf-8")
    return {key: values[-1] for key, values in parse_qs(raw, keep_blank_values=True).items()}


def esc(value):
    return html.escape(str(value or ""))


def page(title, body, session=None):
    logout = '<a class="btn ghost" href="/logout">Выйти</a>' if session else ""
    return HTMLResponse(
        f"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{esc(title)}</title>
  <style>
    body {{ margin:0; font-family: Arial, sans-serif; background:#f4f6f8; color:#18202a; }}
    header {{ height:56px; background:#18202a; color:white; display:flex; align-items:center; justify-content:space-between; padding:0 24px; }}
    main {{ max-width:1120px; margin:24px auto; padding:0 16px; }}
    h1 {{ font-size:24px; margin:0 0 16px; }}
    h2 {{ font-size:18px; margin:0 0 12px; }}
    .grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(220px,1fr)); gap:12px; }}
    .card {{ background:white; border:1px solid #dde3ea; border-radius:8px; padding:16px; margin-bottom:16px; }}
    .muted {{ color:#697586; }}
    .ok {{ color:#047857; font-weight:700; }}
    .bad {{ color:#b42318; font-weight:700; }}
    table {{ width:100%; border-collapse:collapse; background:white; }}
    th, td {{ padding:10px; border-bottom:1px solid #e5e9ef; text-align:left; vertical-align:top; }}
    th {{ background:#f8fafc; font-size:13px; color:#475467; }}
    input {{ padding:9px; border:1px solid #cbd5e1; border-radius:6px; min-width:120px; }}
    .btn, button {{ display:inline-block; padding:9px 12px; border:0; border-radius:6px; background:#2563eb; color:white; text-decoration:none; cursor:pointer; }}
    .ghost {{ background:#334155; }}
    .danger {{ background:#b42318; }}
    .success {{ background:#047857; }}
    form.inline {{ display:inline-flex; gap:6px; align-items:center; flex-wrap:wrap; margin:2px 0; }}
  </style>
</head>
<body>
  <header><strong>Reklama Bot Admin</strong>{logout}</header>
  <main>{body}</main>
</body>
</html>"""
    )


def login_page(error=""):
    message = f'<div class="card bad">{esc(error)}</div>' if error else ""
    return page(
        "Вход",
        f"""
        <h1>Вход в кабинет</h1>
        {message}
        <div class="card">
          <form method="post" action="/login">
            <p><input name="login" placeholder="Логин" autocomplete="username" required></p>
            <p><input name="password" type="password" placeholder="Пароль" autocomplete="current-password" required></p>
            <button type="submit">Войти</button>
          </form>
        </div>
        <p class="muted">Доступ работает только до даты, оплаченной владельцу бота.</p>
        """,
    )


def redirect(path):
    return RedirectResponse(path, status_code=303)


@app.on_event("startup")
async def startup():
    init_web_db()


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    session = read_session(request)
    if not session:
        return login_page()
    if session.get("role") == "owner":
        return redirect("/owner")
    return redirect("/cabinet")


@app.get("/login", response_class=HTMLResponse)
async def login_get():
    return login_page()


@app.post("/login")
async def login_post(request: Request):
    data = await form_data(request)
    login = data.get("login", "").strip()
    password = data.get("password", "")
    if login == WEB_OWNER_LOGIN and WEB_OWNER_PASSWORD and hmac.compare_digest(password, WEB_OWNER_PASSWORD):
        response = redirect("/owner")
        response.set_cookie(SESSION_COOKIE, sign_payload({"role": "owner"}), httponly=True, samesite="lax")
        return response

    with db() as conn:
        tenant = conn.execute("SELECT * FROM tenants WHERE web_login = ?", (login,)).fetchone()
    if not tenant or not verify_password(password, tenant["web_password_hash"]):
        return login_page("Неверный логин или пароль.")
    if not tenant_has_access(tenant):
        return login_page("Доступ не активен или срок аренды закончился.")
    response = redirect("/cabinet")
    response.set_cookie(SESSION_COOKIE, sign_payload({"role": "tenant", "tenant_id": tenant["id"]}), httponly=True, samesite="lax")
    return response


@app.get("/logout")
async def logout():
    response = redirect("/login")
    response.delete_cookie(SESSION_COOKIE)
    return response


def require_owner(request):
    session = read_session(request)
    return session if session and session.get("role") == "owner" else None


def require_tenant(request):
    session = read_session(request)
    if not session or session.get("role") != "tenant":
        return None
    with db() as conn:
        return conn.execute("SELECT * FROM tenants WHERE id = ?", (session["tenant_id"],)).fetchone()


@app.get("/owner", response_class=HTMLResponse)
async def owner_dashboard(request: Request):
    session = require_owner(request)
    if not session:
        return redirect("/login")
    with db() as conn:
        tenants = conn.execute("SELECT * FROM tenants ORDER BY id DESC").fetchall()
        total_ads = conn.execute("SELECT COUNT(*) FROM ads").fetchone()[0]
        total_posts = conn.execute("SELECT COALESCE(SUM(published_count), 0) FROM ads").fetchone()[0]
    rows = []
    for tenant in tenants:
        state = '<span class="ok">активен</span>' if tenant_has_access(tenant) else '<span class="bad">нет доступа</span>'
        toggle_text = "Отключить" if tenant["is_active"] else "Активировать"
        rows.append(
            f"""
            <tr>
              <td>#{tenant['id']}<br><span class="muted">{esc(tenant['name'])}</span></td>
              <td><code>{esc(tenant['telegram_user_id'])}</code></td>
              <td>{state}<br>до {fmt(tenant['access_until'])}</td>
              <td>{esc(tenant['web_login']) or '<span class="muted">не задан</span>'}</td>
              <td>
                <form class="inline" method="post" action="/owner/credentials">
                  <input type="hidden" name="tenant_id" value="{tenant['id']}">
                  <input name="login" placeholder="логин" value="{esc(tenant['web_login'])}">
                  <input name="password" placeholder="новый пароль">
                  <button>Сохранить доступ</button>
                </form>
                <form class="inline" method="post" action="/owner/extend">
                  <input type="hidden" name="tenant_id" value="{tenant['id']}">
                  <input name="days" value="30">
                  <button class="success">+ дни</button>
                </form>
                <form class="inline" method="post" action="/owner/toggle">
                  <input type="hidden" name="tenant_id" value="{tenant['id']}">
                  <button class="danger">{toggle_text}</button>
                </form>
              </td>
            </tr>
            """
        )
    return page(
        "Кабинет владельца",
        f"""
        <h1>Кабинет владельца</h1>
        <div class="grid">
          <div class="card"><h2>Арендаторы</h2><strong>{len(tenants)}</strong></div>
          <div class="card"><h2>Объявления</h2><strong>{total_ads}</strong></div>
          <div class="card"><h2>Публикации</h2><strong>{total_posts}</strong></div>
        </div>
        <div class="card">
          <h2>Арендаторы и веб-доступ</h2>
          <table>
            <tr><th>Арендатор</th><th>Telegram ID</th><th>Статус</th><th>Логин</th><th>Действия</th></tr>
            {''.join(rows) or '<tr><td colspan="5">Арендаторов пока нет.</td></tr>'}
          </table>
        </div>
        """,
        session=session,
    )


@app.post("/owner/credentials")
async def owner_credentials(request: Request):
    if not require_owner(request):
        return redirect("/login")
    data = await form_data(request)
    tenant_id = int(data["tenant_id"])
    login = data.get("login", "").strip() or None
    password = data.get("password", "")
    with db() as conn:
        if password:
            conn.execute(
                "UPDATE tenants SET web_login = ?, web_password_hash = ? WHERE id = ?",
                (login, hash_password(password), tenant_id),
            )
        else:
            conn.execute("UPDATE tenants SET web_login = COALESCE(?, web_login) WHERE id = ?", (login, tenant_id))
    return redirect("/owner")


@app.post("/owner/extend")
async def owner_extend(request: Request):
    if not require_owner(request):
        return redirect("/login")
    data = await form_data(request)
    tenant_id = int(data["tenant_id"])
    days = int(data.get("days") or 0)
    with db() as conn:
        tenant = conn.execute("SELECT * FROM tenants WHERE id = ?", (tenant_id,)).fetchone()
        base = max(datetime.fromisoformat(tenant["access_until"]), now_dt())
        access_until = base + timedelta(days=days)
        conn.execute("UPDATE tenants SET access_until = ?, is_active = 1 WHERE id = ?", (access_until.isoformat(), tenant_id))
    return redirect("/owner")


@app.post("/owner/toggle")
async def owner_toggle(request: Request):
    if not require_owner(request):
        return redirect("/login")
    data = await form_data(request)
    tenant_id = int(data["tenant_id"])
    with db() as conn:
        tenant = conn.execute("SELECT * FROM tenants WHERE id = ?", (tenant_id,)).fetchone()
        conn.execute("UPDATE tenants SET is_active = ? WHERE id = ?", (0 if tenant["is_active"] else 1, tenant_id))
    return redirect("/owner")


@app.get("/cabinet", response_class=HTMLResponse)
async def tenant_dashboard(request: Request):
    tenant = require_tenant(request)
    if not tenant:
        return redirect("/login")
    if not tenant_has_access(tenant):
        return page("Доступ закончился", "<h1>Доступ закончился</h1><div class='card'>Настройки сохранены. Для продления обратитесь к владельцу бота.</div>")
    session = read_session(request)
    with db() as conn:
        groups = conn.execute("SELECT * FROM groups WHERE tenant_id = ? ORDER BY id DESC", (tenant["id"],)).fetchall()
        ads = conn.execute(
            """
            SELECT ads.*, groups.title group_title
            FROM ads JOIN groups ON groups.id = ads.group_id
            WHERE ads.tenant_id = ?
            ORDER BY ads.id DESC
            """,
            (tenant["id"],),
        ).fetchall()
    active_ads = sum(1 for ad in ads if ad["active"])
    published = sum(ad["published_count"] for ad in ads)
    group_rows = "".join(f"<tr><td>{esc(g['title'])}</td><td><code>{g['chat_id']}</code></td></tr>" for g in groups)
    ad_rows = []
    for ad in ads:
        status = '<span class="ok">активно</span>' if ad["active"] else '<span class="muted">пауза</span>'
        preview = esc(ad["text"] or ad["caption"] or f"[{ad['media_type']}]")
        ad_rows.append(
            f"""
            <tr>
              <td>#{ad['id']}<br>{status}</td>
              <td>{esc(ad['group_title'])}</td>
              <td>{fmt(ad['start_at'])}<br>{fmt(ad['end_at'])}</td>
              <td>{ad['interval_minutes']} мин<br>{ad['published_count']} постов</td>
              <td>{preview[:240]}</td>
            </tr>
            """
        )
    return page(
        "Кабинет арендатора",
        f"""
        <h1>Кабинет арендатора: {esc(tenant['name'])}</h1>
        <div class="grid">
          <div class="card"><h2>Доступ до</h2><strong>{fmt(tenant['access_until'])}</strong></div>
          <div class="card"><h2>Группы</h2><strong>{len(groups)}</strong></div>
          <div class="card"><h2>Активные объявления</h2><strong>{active_ads}</strong></div>
          <div class="card"><h2>Опубликовано</h2><strong>{published}</strong></div>
        </div>
        <div class="card">
          <h2>Мои группы</h2>
          <table><tr><th>Название</th><th>Chat ID</th></tr>{group_rows or '<tr><td colspan="2">Группы пока не добавлены.</td></tr>'}</table>
        </div>
        <div class="card">
          <h2>Мои объявления</h2>
          <table><tr><th>ID</th><th>Группа</th><th>Срок</th><th>Интервал</th><th>Текст</th></tr>{''.join(ad_rows) or '<tr><td colspan="5">Объявлений пока нет.</td></tr>'}</table>
        </div>
        """,
        session=session,
    )
