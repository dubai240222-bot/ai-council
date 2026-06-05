import base64
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import sqlite3
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

load_dotenv()

DATABASE_PATH = os.getenv("DATABASE_PATH", "bot.sqlite3")
TIMEZONE = ZoneInfo(os.getenv("TIMEZONE", "Europe/Istanbul"))
WEB_SECRET = os.getenv("WEB_SECRET") or os.getenv("BOT_TOKEN") or "change-me"
WEB_OWNER_LOGIN = os.getenv("WEB_OWNER_LOGIN", "owner")
WEB_OWNER_PASSWORD = os.getenv("WEB_OWNER_PASSWORD", "")
SUPER_ADMIN_ID = int(os.getenv("SUPER_ADMIN_ID", "0"))
SESSION_COOKIE = "reklama_admin_session"
BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR_RAW = Path(os.getenv("UPLOAD_DIR", "uploads"))
UPLOAD_DIR = UPLOAD_DIR_RAW if UPLOAD_DIR_RAW.is_absolute() else BASE_DIR / UPLOAD_DIR_RAW
MAX_UPLOAD_BYTES = 10 * 1024 * 1024

app = FastAPI(title="Reklama Bot Admin")
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")


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


def dt_input(value=None, minutes=0):
    moment = datetime.fromisoformat(value).astimezone(TIMEZONE) if value else now_dt() + timedelta(minutes=minutes)
    return moment.strftime("%Y-%m-%dT%H:%M")


def parse_dt_input(value):
    return datetime.strptime(value.strip(), "%Y-%m-%dT%H:%M").replace(tzinfo=TIMEZONE)


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
        ad_columns = {row["name"] for row in conn.execute("PRAGMA table_info(ads)").fetchall()}
        if "updated_at" not in ad_columns:
            conn.execute("ALTER TABLE ads ADD COLUMN updated_at TEXT")
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


def plain_text(value):
    value = re.sub(r"<br\s*/?>", "\n", str(value or ""), flags=re.IGNORECASE)
    value = re.sub(r"</p\s*>", "\n", value, flags=re.IGNORECASE)
    value = re.sub(r"<[^>]+>", "", value)
    return html.unescape(value).strip()


async def save_uploaded_image(tenant_id, upload):
    if not upload or not getattr(upload, "filename", ""):
        return None
    content_type = (getattr(upload, "content_type", "") or "").lower()
    if not content_type.startswith("image/"):
        return None
    ext = Path(upload.filename).suffix.lower()
    if ext not in {".jpg", ".jpeg", ".png", ".webp"}:
        ext = ".jpg"
    data = await upload.read()
    if not data or len(data) > MAX_UPLOAD_BYTES:
        return None
    tenant_dir = UPLOAD_DIR / f"tenant_{tenant_id}"
    tenant_dir.mkdir(parents=True, exist_ok=True)
    path = tenant_dir / f"{uuid.uuid4().hex}{ext}"
    path.write_bytes(data)
    return path.relative_to(BASE_DIR).as_posix()


def local_upload_url(file_id):
    if not file_id:
        return ""
    normalized = str(file_id).replace("\\", "/")
    if normalized.startswith("uploads/"):
        return "/" + normalized
    return ""


def image_upload_block(current_file_id=""):
    current_url = local_upload_url(current_file_id)
    current = (
        f"""
        <div class="current-image">
          <div class="preview-title">Текущее фото</div>
          <img src="{esc(current_url)}" alt="Текущее фото">
          <label><input type="checkbox" name="remove_image" value="1" style="width:auto"> Убрать фото и оставить только текст</label>
        </div>
        """
        if current_url
        else ""
    )
    return f"""
      <label>Фото к рекламе</label>
      <div class="upload-box" data-current-url="{esc(current_url)}">
        <input type="file" name="image" accept="image/png,image/jpeg,image/webp" class="image-input">
        <p class="muted">Можно добавить JPG, PNG или WebP до 10 MB. Альбомы 2-10 фото добавим следующим шагом.</p>
        {current}
        <img class="image-preview" alt="Предпросмотр фото">
      </div>
    """


def ad_thumbnail(ad):
    url = local_upload_url(ad["file_id"] if "file_id" in ad.keys() else "")
    return f'<img class="ad-thumb" src="{esc(url)}" alt="Фото объявления">' if url else ""


def rich_editor(name, value="", label="Текст объявления", placeholder="Введите рекламный текст..."):
    field_id = f"editor_{name}"
    return f"""
      <label>{esc(label)}</label>
      <div class="editor">
        <div class="toolbar" data-target="{field_id}">
          <button type="button" data-before="<b>" data-after="</b>"><b>B</b></button>
          <button type="button" data-before="<i>" data-after="</i>"><i>I</i></button>
          <button type="button" data-before="<u>" data-after="</u>"><u>U</u></button>
          <button type="button" data-before="<s>" data-after="</s>"><s>S</s></button>
          <button type="button" data-before="<code>" data-after="</code>">code</button>
          <button type="button" data-before="<tg-spoiler>" data-after="</tg-spoiler>">Спойлер</button>
          <button type="button" data-link="1">Ссылка</button>
          <button type="button" data-insert="✅">✅</button>
          <button type="button" data-insert="🔥">🔥</button>
          <button type="button" data-insert="📞">📞</button>
          <button type="button" data-insert="🌐">🌐</button>
          <button type="button" data-insert="👇">👇</button>
        </div>
        <textarea id="{field_id}" class="rich-textarea" name="{name}" placeholder="{esc(placeholder)}" required>{esc(value)}</textarea>
        <div class="preview-title">Предпросмотр текста</div>
        <div class="telegram-preview" data-preview-for="{field_id}"></div>
      </div>
    """


def interval_presets_block(selected="240"):
    presets = [
        ("30", "30 мин"),
        ("60", "1 час"),
        ("120", "2 часа"),
        ("240", "4 часа"),
        ("1440", "1 день"),
    ]
    current = str(selected or "240")
    buttons = []
    for value, label in presets:
        active = " active" if value == current else ""
        buttons.append(f'<button type="button" class="chip{active}" data-interval-value="{value}">{label}</button>')
    return f'<div class="chip-row">{"".join(buttons)}</div>'


def ad_form_preview_block(group_name="", start_value="", end_value="", interval_value="240", image_url=""):
    image_html = (
        f'<img class="tg-preview-image" src="{esc(image_url)}" alt="Превью фото" style="display:block">'
        if image_url
        else '<img class="tg-preview-image" alt="Превью фото">'
    )
    return f"""
      <div class="preview-title">Предпросмотр объявления</div>
      <div class="ad-form-preview"
           data-group-name="{esc(group_name)}"
           data-start="{esc(start_value)}"
           data-end="{esc(end_value)}"
           data-interval="{esc(str(interval_value))}">
        <div class="tg-meta">
          <div class="tg-avatar">R</div>
          <div>
            <div class="tg-group-name">Выберите группу</div>
            <div class="tg-time-line">Старт и расписание пока не заполнены</div>
          </div>
        </div>
        <div class="tg-preview-card">
          {image_html}
          <div class="tg-preview-text">Здесь появится текст объявления</div>
        </div>
      </div>
    """


def redirect(path):
    return RedirectResponse(path, status_code=303)


def nav(session):
    if not session:
        return ""
    if session.get("role") == "owner":
        links = [
            ("/owner", "Владелец"),
            ("/logout", "Выйти"),
        ]
    else:
        links = [
            ("/cabinet", "Главная"),
            ("/cabinet/ads", "Объявления"),
            ("/cabinet/ads/new", "Создать"),
            ("/cabinet/groups", "Группы"),
            ("/cabinet/reports", "Отчёты"),
            ("/logout", "Выйти"),
        ]
        if session.get("owner"):
            links.insert(0, ("/owner/return", "Владелец"))
    return "".join(f'<a href="{href}">{label}</a>' for href, label in links)


def page(title, body, session=None):
    return HTMLResponse(
        f"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{esc(title)}</title>
  <style>
    :root {{ --bg:#f4f6f8; --text:#18202a; --muted:#667085; --line:#d9e1ea; --blue:#2563eb; --green:#047857; --red:#b42318; }}
    * {{ box-sizing:border-box; }}
    body {{ margin:0; font-family:Arial, sans-serif; background:var(--bg); color:var(--text); }}
    header {{ min-height:58px; background:#17202b; color:white; display:flex; align-items:center; justify-content:space-between; gap:16px; padding:0 24px; flex-wrap:wrap; }}
    header strong {{ font-size:17px; }}
    nav {{ display:flex; gap:8px; flex-wrap:wrap; }}
    nav a {{ color:white; text-decoration:none; padding:8px 10px; border-radius:6px; background:rgba(255,255,255,.10); }}
    main {{ max-width:1180px; margin:24px auto; padding:0 16px 48px; }}
    h1 {{ font-size:26px; margin:0 0 16px; }}
    h2 {{ font-size:18px; margin:0 0 12px; }}
    .grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(210px,1fr)); gap:12px; }}
    .card {{ background:white; border:1px solid var(--line); border-radius:8px; padding:16px; margin-bottom:16px; }}
    .muted {{ color:var(--muted); }}
    .ok {{ color:var(--green); font-weight:700; }}
    .bad {{ color:var(--red); font-weight:700; }}
    .pill {{ display:inline-block; padding:4px 8px; border-radius:999px; background:#eef2ff; color:#3730a3; font-size:13px; }}
    table {{ width:100%; border-collapse:collapse; background:white; }}
    th, td {{ padding:10px; border-bottom:1px solid #e5e9ef; text-align:left; vertical-align:top; }}
    th {{ background:#f8fafc; font-size:13px; color:#475467; }}
    input, select, textarea {{ width:100%; padding:10px; border:1px solid #cbd5e1; border-radius:6px; font:inherit; }}
    textarea {{ min-height:180px; resize:vertical; }}
    label {{ display:block; font-weight:700; margin:10px 0 6px; }}
    .form-grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(220px,1fr)); gap:12px; }}
    .actions {{ display:flex; gap:8px; flex-wrap:wrap; align-items:center; }}
    .btn, button {{ display:inline-block; padding:10px 13px; border:0; border-radius:6px; background:var(--blue); color:white; text-decoration:none; cursor:pointer; font:inherit; }}
    .ghost {{ background:#334155; }}
    .danger {{ background:var(--red); }}
    .success {{ background:var(--green); }}
    form.inline {{ display:inline-flex; gap:6px; align-items:center; flex-wrap:wrap; margin:2px 0; }}
    form.inline input {{ width:auto; min-width:90px; }}
    .preview {{ white-space:pre-wrap; background:#f8fafc; border:1px dashed #cbd5e1; border-radius:8px; padding:12px; }}
    .editor {{ border:1px solid var(--line); border-radius:8px; background:#fbfdff; padding:10px; }}
    .toolbar {{ display:flex; gap:6px; flex-wrap:wrap; margin-bottom:8px; }}
    .toolbar button {{ padding:7px 9px; background:#eef2f7; color:#1f2937; border:1px solid #cbd5e1; }}
    .toolbar button:hover {{ background:#dbeafe; border-color:#93c5fd; }}
    .rich-textarea {{ min-height:220px; font-family:Arial, sans-serif; background:white; }}
    .preview-title {{ margin:10px 0 6px; color:var(--muted); font-size:13px; font-weight:700; }}
    .telegram-preview {{ min-height:80px; white-space:pre-wrap; background:#e9f7df; border:1px solid #bfdab2; border-radius:8px; padding:12px; line-height:1.35; }}
    .telegram-preview a {{ color:#2563eb; }}
    .telegram-spoiler {{ background:#111827; color:#111827; border-radius:3px; padding:0 3px; }}
    .upload-box {{ border:1px solid var(--line); border-radius:8px; background:#fbfdff; padding:10px; }}
    .image-preview, .current-image img {{ display:none; max-width:360px; width:100%; max-height:260px; object-fit:contain; margin-top:10px; border:1px solid var(--line); border-radius:8px; background:white; }}
    .current-image img {{ display:block; }}
    .ad-thumb {{ display:block; width:92px; height:70px; object-fit:cover; border-radius:6px; border:1px solid var(--line); margin-bottom:6px; }}
    .chip-row {{ display:flex; gap:8px; flex-wrap:wrap; margin-top:8px; }}
    .chip {{ padding:8px 10px; background:#eef2f7; color:#1f2937; border:1px solid #cbd5e1; border-radius:999px; cursor:pointer; }}
    .chip.active {{ background:#dbeafe; border-color:#93c5fd; color:#1d4ed8; }}
    .ad-form-preview {{ background:#f8fafc; border:1px solid var(--line); border-radius:10px; padding:12px; }}
    .tg-meta {{ display:flex; gap:10px; align-items:center; margin-bottom:10px; }}
    .tg-avatar {{ width:38px; height:38px; border-radius:999px; background:#dcfce7; color:#166534; display:flex; align-items:center; justify-content:center; font-weight:700; }}
    .tg-group-name {{ font-weight:700; }}
    .tg-time-line {{ color:var(--muted); font-size:13px; }}
    .tg-preview-card {{ background:#e9f7df; border:1px solid #bfdab2; border-radius:10px; overflow:hidden; }}
    .tg-preview-image {{ display:none; width:100%; max-height:360px; object-fit:cover; background:#fff; }}
    .tg-preview-text {{ padding:12px; white-space:pre-wrap; line-height:1.4; }}
    @media (max-width:720px) {{ header {{ padding:12px 14px; }} table {{ font-size:14px; }} th, td {{ padding:8px; }} }}
  </style>
  <script>
    function escapeHtml(value) {{
      return value.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
    }}
    function telegramPreview(raw) {{
      let value = escapeHtml(raw || "");
      const pairs = [
        ["b", "strong"],
        ["strong", "strong"],
        ["i", "em"],
        ["em", "em"],
        ["u", "u"],
        ["s", "s"],
        ["strike", "s"],
        ["del", "s"],
        ["code", "code"]
      ];
      for (const pair of pairs) {{
        const source = pair[0];
        const target = pair[1];
        value = value.replace(new RegExp("&lt;" + source + "&gt;", "gi"), "<" + target + ">");
        value = value.replace(new RegExp("&lt;/" + source + "&gt;", "gi"), "</" + target + ">");
      }}
      value = value.replace(/&lt;tg-spoiler&gt;/gi, '<span class="telegram-spoiler">');
      value = value.replace(/&lt;\\/tg-spoiler&gt;/gi, "</span>");
      value = value.replace(/&lt;a href=&quot;([^&]+)&quot;&gt;/gi, '<a href="$1" target="_blank">');
      value = value.replace(/&lt;\\/a&gt;/gi, "</a>");
      return value;
    }}
    function updatePreview(textarea) {{
      const preview = document.querySelector('[data-preview-for="' + textarea.id + '"]');
      if (preview) preview.innerHTML = telegramPreview(textarea.value);
    }}
    function formatDtInput(value) {{
      if (!value) return "";
      const parts = value.split("T");
      if (parts.length !== 2) return value;
      const date = parts[0].split("-");
      const time = parts[1].slice(0, 5);
      if (date.length !== 3) return value;
      return date[2] + "." + date[1] + "." + date[0] + " " + time;
    }}
    function intervalLabel(value) {{
      const minutes = parseInt(value || "0", 10);
      if (!minutes || minutes < 1) return "Интервал не задан";
      if (minutes % 1440 === 0) return "Каждые " + (minutes / 1440) + " дн.";
      if (minutes % 60 === 0) return "Каждые " + (minutes / 60) + " ч.";
      return "Каждые " + minutes + " мин.";
    }}
    function getImagePreviewSource(uploadBox) {{
      if (!uploadBox) return "";
      const input = uploadBox.querySelector(".image-input");
      if (input && input.files && input.files[0]) {{
        return URL.createObjectURL(input.files[0]);
      }}
      const remove = uploadBox.querySelector('input[name="remove_image"]');
      if (remove && remove.checked) return "";
      return uploadBox.dataset.currentUrl || "";
    }}
    function updateAdFormPreview(form) {{
      const preview = form.querySelector(".ad-form-preview");
      if (!preview) return;
      const groupSelect = form.querySelector('select[name="group_id"]');
      const textarea = form.querySelector(".rich-textarea");
      const start = form.querySelector('input[name="start_at"]');
      const end = form.querySelector('input[name="end_at"]');
      const interval = form.querySelector('input[name="interval_minutes"]');
      const uploadBox = form.querySelector(".upload-box");
      const groupName = groupSelect && groupSelect.selectedOptions.length ? groupSelect.selectedOptions[0].textContent.trim() : "Выберите группу";
      const startText = start && start.value ? formatDtInput(start.value) : "не задан";
      const endText = end && end.value ? formatDtInput(end.value) : "не задано";
      const intervalText = intervalLabel(interval ? interval.value : "");
      const groupNode = preview.querySelector(".tg-group-name");
      const timeNode = preview.querySelector(".tg-time-line");
      const textNode = preview.querySelector(".tg-preview-text");
      const imageNode = preview.querySelector(".tg-preview-image");
      if (groupNode) groupNode.textContent = groupName;
      if (timeNode) timeNode.textContent = "Старт: " + startText + " • До: " + endText + " • " + intervalText;
      if (textNode) textNode.innerHTML = telegramPreview(textarea ? textarea.value : "");
      const imageSrc = getImagePreviewSource(uploadBox);
      if (imageNode) {{
        if (imageSrc) {{
          imageNode.src = imageSrc;
          imageNode.style.display = "block";
        }} else {{
          imageNode.removeAttribute("src");
          imageNode.style.display = "none";
        }}
      }}
    }}
    function wrapSelection(textarea, before, after) {{
      const start = textarea.selectionStart || 0;
      const end = textarea.selectionEnd || 0;
      const selected = textarea.value.slice(start, end) || "текст";
      textarea.value = textarea.value.slice(0, start) + before + selected + after + textarea.value.slice(end);
      textarea.focus();
      textarea.selectionStart = start + before.length;
      textarea.selectionEnd = start + before.length + selected.length;
      updatePreview(textarea);
    }}
    document.addEventListener("DOMContentLoaded", () => {{
      document.querySelectorAll(".rich-textarea").forEach((textarea) => {{
        textarea.addEventListener("input", () => updatePreview(textarea));
        updatePreview(textarea);
        const form = textarea.closest("form");
        if (form) textarea.addEventListener("input", () => updateAdFormPreview(form));
      }});
      document.querySelectorAll(".toolbar button").forEach((button) => {{
        button.addEventListener("click", () => {{
          const toolbar = button.closest(".toolbar");
          const textarea = document.getElementById(toolbar.dataset.target);
          if (!textarea) return;
          if (button.dataset.insert) {{
            const start = textarea.selectionStart || 0;
            textarea.value = textarea.value.slice(0, start) + button.dataset.insert + textarea.value.slice(start);
            textarea.focus();
            textarea.selectionStart = textarea.selectionEnd = start + button.dataset.insert.length;
            updatePreview(textarea);
            return;
          }}
          if (button.dataset.link) {{
            const url = prompt("Вставьте ссылку, например https://t.me/username");
            if (!url) return;
            wrapSelection(textarea, '<a href="' + url + '">', "</a>");
            return;
          }}
          wrapSelection(textarea, button.dataset.before || "", button.dataset.after || "");
        }});
      }});
      document.querySelectorAll(".image-input").forEach((input) => {{
        input.addEventListener("change", () => {{
          const preview = input.closest(".upload-box").querySelector(".image-preview");
          const file = input.files && input.files[0];
          if (!preview || !file) {{
            if (preview) preview.style.display = "none";
            const form = input.closest("form");
            if (form) updateAdFormPreview(form);
            return;
          }}
          preview.src = URL.createObjectURL(file);
          preview.style.display = "block";
          const form = input.closest("form");
          if (form) updateAdFormPreview(form);
        }});
      }});
      document.querySelectorAll('input[name="remove_image"]').forEach((checkbox) => {{
        checkbox.addEventListener("change", () => {{
          const form = checkbox.closest("form");
          if (form) updateAdFormPreview(form);
        }});
      }});
      document.querySelectorAll("[data-interval-value]").forEach((button) => {{
        button.addEventListener("click", () => {{
          const form = button.closest("form");
          if (!form) return;
          const input = form.querySelector('input[name="interval_minutes"]');
          if (!input) return;
          input.value = button.dataset.intervalValue;
          form.querySelectorAll("[data-interval-value]").forEach((other) => other.classList.remove("active"));
          button.classList.add("active");
          updateAdFormPreview(form);
        }});
      }});
      document.querySelectorAll('form[action="/cabinet/ads/new"], form[action$="/edit"]').forEach((form) => {{
        form.querySelectorAll('select[name="group_id"], input[name="start_at"], input[name="end_at"], input[name="interval_minutes"]').forEach((field) => {{
          field.addEventListener("input", () => updateAdFormPreview(form));
          field.addEventListener("change", () => updateAdFormPreview(form));
        }});
        updateAdFormPreview(form);
      }});
    }});
  </script>
</head>
<body>
  <header><strong>Reklama Bot Admin</strong><nav>{nav(session)}</nav></header>
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
            <label>Логин</label>
            <input name="login" autocomplete="username" required>
            <label>Пароль</label>
            <input name="password" type="password" autocomplete="current-password" required>
            <p><button type="submit">Войти</button></p>
          </form>
        </div>
        <p class="muted">Доступ работает только до даты, оплаченной владельцу бота.</p>
        """,
    )


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
        return None, session
    with db() as conn:
        tenant = conn.execute("SELECT * FROM tenants WHERE id = ?", (session["tenant_id"],)).fetchone()
    return tenant, session


@app.get("/owner", response_class=HTMLResponse)
async def owner_dashboard(request: Request):
    session = require_owner(request)
    if not session:
        return redirect("/login")
    with db() as conn:
        tenants = conn.execute("SELECT * FROM tenants ORDER BY id DESC").fetchall()
        total_ads = conn.execute("SELECT COUNT(*) FROM ads").fetchone()[0]
        total_posts = conn.execute("SELECT COALESCE(SUM(published_count), 0) FROM ads").fetchone()[0]
        owner_tenant = conn.execute("SELECT * FROM tenants WHERE telegram_user_id = ?", (SUPER_ADMIN_ID,)).fetchone()
    rows = []
    cabinet_cards = []
    for tenant in tenants:
        state = '<span class="ok">активен</span>' if tenant_has_access(tenant) else '<span class="bad">нет доступа</span>'
        toggle_text = "Отключить" if tenant["is_active"] else "Активировать"
        if owner_tenant and tenant["id"] == owner_tenant["id"]:
            owner_label = "Мой рекламный кабинет"
        else:
            owner_label = f"Кабинет: {esc(tenant['name'])}"
        cabinet_cards.append(
            f"""
            <div class="card">
              <h2>{owner_label}</h2>
              <p class="muted">Группы, объявления, создание, редактирование и отчёты.</p>
              <a class="btn" href="/owner/tenant/{tenant['id']}">Открыть управление рекламой</a>
            </div>
            """
        )
        rows.append(
            f"""
            <tr>
              <td>#{tenant['id']}<br><span class="muted">{esc(tenant['name'])}</span></td>
              <td><code>{esc(tenant['telegram_user_id'])}</code></td>
              <td>{state}<br>до {fmt(tenant['access_until'])}</td>
              <td>{esc(tenant['web_login']) or '<span class="muted">не задан</span>'}</td>
              <td>
                <a class="btn ghost" href="/owner/tenant/{tenant['id']}">Открыть кабинет</a>
                <form class="inline" method="post" action="/owner/credentials">
                  <input type="hidden" name="tenant_id" value="{tenant['id']}">
                  <input name="login" placeholder="логин" value="{esc(tenant['web_login'])}">
                  <input name="password" placeholder="новый пароль">
                  <button>Сохранить</button>
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
          <h2>Управление рекламой</h2>
          <p class="muted">Владелец может открыть свой рекламный кабинет или зайти в кабинет любого арендатора как суперадмин.</p>
        </div>
        <div class="grid">
          {''.join(cabinet_cards) or '<div class="card">Кабинетов пока нет.</div>'}
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


@app.get("/owner/tenant/{tenant_id}")
async def owner_open_tenant(request: Request, tenant_id: int):
    if not require_owner(request):
        return redirect("/login")
    with db() as conn:
        tenant = conn.execute("SELECT * FROM tenants WHERE id = ?", (tenant_id,)).fetchone()
    if not tenant:
        return redirect("/owner")
    response = redirect("/cabinet")
    response.set_cookie(
        SESSION_COOKIE,
        sign_payload({"role": "tenant", "tenant_id": tenant_id, "owner": True}),
        httponly=True,
        samesite="lax",
    )
    return response


@app.get("/owner/return")
async def owner_return(request: Request):
    session = read_session(request)
    if not session or not session.get("owner"):
        return redirect("/login")
    response = redirect("/owner")
    response.set_cookie(SESSION_COOKIE, sign_payload({"role": "owner"}), httponly=True, samesite="lax")
    return response


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
            conn.execute("UPDATE tenants SET web_login = ?, web_password_hash = ? WHERE id = ?", (login, hash_password(password), tenant_id))
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


def tenant_guard(request):
    tenant, session = require_tenant(request)
    if not tenant:
        return None, session, redirect("/login")
    if not tenant_has_access(tenant):
        return None, session, page(
            "Доступ закончился",
            "<h1>Доступ закончился</h1><div class='card'>Настройки сохранены. Для продления обратитесь к владельцу бота.</div>",
            session=session,
        )
    return tenant, session, None


def tenant_stats(tenant_id):
    with db() as conn:
        groups = conn.execute("SELECT COUNT(*) FROM groups WHERE tenant_id = ?", (tenant_id,)).fetchone()[0]
        row = conn.execute(
            "SELECT COUNT(*) total, COALESCE(SUM(active), 0) active, COALESCE(SUM(published_count), 0) posts FROM ads WHERE tenant_id = ?",
            (tenant_id,),
        ).fetchone()
        next_ad = conn.execute(
            """
            SELECT ads.*, groups.title group_title
            FROM ads JOIN groups ON groups.id = ads.group_id
            WHERE ads.tenant_id = ? AND ads.active = 1 AND ads.end_at >= ?
            ORDER BY ads.start_at ASC
            LIMIT 1
            """,
            (tenant_id, now_dt().isoformat()),
        ).fetchone()
    return groups, row, next_ad


@app.get("/cabinet", response_class=HTMLResponse)
async def tenant_dashboard(request: Request):
    tenant, session, response = tenant_guard(request)
    if response:
        return response
    groups, ads, next_ad = tenant_stats(tenant["id"])
    return page(
        "Кабинет арендатора",
        f"""
        <h1>Кабинет арендатора: {esc(tenant['name'])}</h1>
        <div class="grid">
          <div class="card"><h2>Доступ до</h2><strong>{fmt(tenant['access_until'])}</strong></div>
          <div class="card"><h2>Группы</h2><strong>{groups}</strong></div>
          <div class="card"><h2>Активные объявления</h2><strong>{ads['active']}</strong></div>
          <div class="card"><h2>Опубликовано</h2><strong>{ads['posts']}</strong></div>
        </div>
        <div class="card">
          <h2>Быстрые действия</h2>
          <div class="actions">
            <a class="btn" href="/cabinet/ads/new">Создать объявление</a>
            <a class="btn ghost" href="/cabinet/ads">Мои объявления</a>
            <a class="btn ghost" href="/cabinet/groups">Мои группы</a>
            <a class="btn ghost" href="/cabinet/reports">Отчёт клиенту</a>
          </div>
        </div>
        <div class="card">
          <h2>Следующая активная реклама</h2>
          {f"<p><span class='pill'>#{next_ad['id']}</span> {esc(next_ad['group_title'])}</p>{ad_thumbnail(next_ad)}<div class='preview'>{esc(next_ad['text'] or next_ad['caption'] or '[' + next_ad['media_type'] + ']')[:500]}</div>" if next_ad else "<p class='muted'>Активных объявлений пока нет.</p>"}
        </div>
        """,
        session=session,
    )


@app.get("/cabinet/groups", response_class=HTMLResponse)
async def cabinet_groups(request: Request):
    tenant, session, response = tenant_guard(request)
    if response:
        return response
    with db() as conn:
        groups = conn.execute("SELECT * FROM groups WHERE tenant_id = ? ORDER BY title", (tenant["id"],)).fetchall()
    rows = "".join(f"<tr><td>{esc(g['title'])}</td><td><code>{g['chat_id']}</code></td></tr>" for g in groups)
    return page(
        "Мои группы",
        f"""
        <h1>Мои группы</h1>
        <div class="card">
          <p class="muted">Группы подключаются через Telegram: добавьте бота админом в группу или канал и отправьте там <code>/register_group</code>.</p>
          <table><tr><th>Название</th><th>Chat ID</th></tr>{rows or '<tr><td colspan="2">Группы пока не добавлены.</td></tr>'}</table>
        </div>
        """,
        session=session,
    )


@app.get("/cabinet/ads", response_class=HTMLResponse)
async def cabinet_ads(request: Request):
    tenant, session, response = tenant_guard(request)
    if response:
        return response
    with db() as conn:
        ads = conn.execute(
            """
            SELECT ads.*, groups.title group_title
            FROM ads JOIN groups ON groups.id = ads.group_id
            WHERE ads.tenant_id = ?
            ORDER BY ads.id DESC
            """,
            (tenant["id"],),
        ).fetchall()
    rows = []
    for ad in ads:
        status = '<span class="ok">активно</span>' if ad["active"] else '<span class="muted">пауза</span>'
        preview = esc(ad["text"] or ad["caption"] or f"[{ad['media_type']}]")
        toggle_label = "Пауза" if ad["active"] else "Запуск"
        rows.append(
            f"""
            <tr>
              <td>#{ad['id']}<br>{status}</td>
              <td>{esc(ad['group_title'])}</td>
              <td>{fmt(ad['start_at'])}<br>{fmt(ad['end_at'])}</td>
              <td>{ad['interval_minutes']} мин<br>{ad['published_count']} постов</td>
              <td>{ad_thumbnail(ad)}{preview[:260]}</td>
              <td>
                <a class="btn ghost" href="/cabinet/ads/{ad['id']}/edit">Редактировать</a>
                <form class="inline" method="post" action="/cabinet/ads/toggle">
                  <input type="hidden" name="ad_id" value="{ad['id']}">
                  <button>{toggle_label}</button>
                </form>
                <a class="btn ghost" href="/cabinet/reports?ad_id={ad['id']}">Отчёт</a>
              </td>
            </tr>
            """
        )
    return page(
        "Мои объявления",
        f"""
        <h1>Мои объявления</h1>
        <div class="actions card">
          <a class="btn" href="/cabinet/ads/new">Создать текстовое объявление</a>
        </div>
        <div class="card">
          <table><tr><th>ID</th><th>Группа</th><th>Срок</th><th>Публикации</th><th>Текст</th><th>Действия</th></tr>{''.join(rows) or '<tr><td colspan="6">Объявлений пока нет.</td></tr>'}</table>
        </div>
        """,
        session=session,
    )


@app.get("/cabinet/ads/new", response_class=HTMLResponse)
async def new_ad_get(request: Request):
    tenant, session, response = tenant_guard(request)
    if response:
        return response
    with db() as conn:
        groups = conn.execute("SELECT * FROM groups WHERE tenant_id = ? ORDER BY title", (tenant["id"],)).fetchall()
    options = "".join(f'<option value="{g["id"]}">{esc(g["title"])}</option>' for g in groups)
    first_group = groups[0]["title"] if groups else ""
    start_value = dt_input(minutes=10)
    end_value = dt_input(minutes=60 * 24)
    empty = "<p class='bad'>Сначала подключите группу через Telegram командой /register_group.</p>" if not groups else ""
    return page(
        "Создать объявление",
        f"""
        <h1>Создать объявление</h1>
        <div class="card">
          {empty}
          <form method="post" action="/cabinet/ads/new" enctype="multipart/form-data">
            <label>Группа или канал</label>
            <select name="group_id" required>{options}</select>
            {image_upload_block()}
            {rich_editor("text")}
            <div class="form-grid">
              <div><label>Старт</label><input type="datetime-local" name="start_at" value="{start_value}" required></div>
              <div><label>Окончание</label><input type="datetime-local" name="end_at" value="{end_value}" required></div>
              <div><label>Интервал, минут</label><input type="number" name="interval_minutes" value="240" min="1" required></div>
            </div>
            {interval_presets_block("240")}
            {ad_form_preview_block(first_group, start_value, end_value, "240")}
            <p class="muted">Пока сайт создаёт текстовые объявления. Фото, видео и альбомы временно удобнее добавлять через Telegram.</p>
            <p><button type="submit">Запланировать</button> <a class="btn ghost" href="/cabinet/ads">Отмена</a></p>
          </form>
        </div>
        """,
        session=session,
    )


@app.post("/cabinet/ads/new")
async def new_ad_post(request: Request):
    tenant, session, response = tenant_guard(request)
    if response:
        return response
    data = await request.form()
    group_id = int(data["group_id"])
    text_html = data.get("text", "").strip()
    text = plain_text(text_html)
    image_path = await save_uploaded_image(tenant["id"], data.get("image"))
    start_at = parse_dt_input(data["start_at"])
    end_at = parse_dt_input(data["end_at"])
    interval = int(data["interval_minutes"])
    if not text or end_at <= start_at or interval < 1:
        return redirect("/cabinet/ads/new")
    with db() as conn:
        group = conn.execute("SELECT * FROM groups WHERE id = ? AND tenant_id = ?", (group_id, tenant["id"])).fetchone()
        if not group:
            return redirect("/cabinet/ads/new")
        conn.execute(
            """
            INSERT INTO ads (
                tenant_id, group_id, media_type, file_id, text, caption, text_html, caption_html,
                start_at, end_at, interval_minutes, active, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
            """,
            (
                tenant["id"],
                group_id,
                "photo" if image_path else "text",
                image_path,
                None if image_path else text,
                text if image_path else None,
                None if image_path else text_html,
                text_html if image_path else None,
                start_at.isoformat(),
                end_at.isoformat(),
                interval,
                now_dt().isoformat(),
                now_dt().isoformat(),
            ),
        )
    return redirect("/cabinet/ads")


@app.get("/cabinet/ads/{ad_id}/edit", response_class=HTMLResponse)
async def edit_ad_get(request: Request, ad_id: int):
    tenant, session, response = tenant_guard(request)
    if response:
        return response
    with db() as conn:
        ad = conn.execute("SELECT * FROM ads WHERE id = ? AND tenant_id = ?", (ad_id, tenant["id"])).fetchone()
        groups = conn.execute("SELECT * FROM groups WHERE tenant_id = ? ORDER BY title", (tenant["id"],)).fetchall()
    if not ad:
        return redirect("/cabinet/ads")
    options = "".join(
        f'<option value="{g["id"]}" {"selected" if g["id"] == ad["group_id"] else ""}>{esc(g["title"])}</option>'
        for g in groups
    )
    selected_group = next((g["title"] for g in groups if g["id"] == ad["group_id"]), "")
    content = (ad["text_html"] or ad["text"]) if ad["media_type"] == "text" else (ad["caption_html"] or ad["caption"])
    active_checked = "checked" if ad["active"] else ""
    media_note = (
        "<p class='muted'>Это медиа-объявление. На сайте сейчас можно редактировать подпись и расписание; замену фото/альбома пока оставляем через Telegram.</p>"
        if ad["media_type"] != "text"
        else ""
    )
    return page(
        f"Редактировать объявление #{ad_id}",
        f"""
        <h1>Редактировать объявление #{ad_id}</h1>
        <div class="card">
          <form method="post" action="/cabinet/ads/{ad_id}/edit" enctype="multipart/form-data">
            <label>Группа или канал</label>
            <select name="group_id" required>{options}</select>
            {image_upload_block(ad["file_id"])}
            {rich_editor("content", content, "Текст объявления" if ad["media_type"] == "text" else "Подпись к медиа")}
            <div class="form-grid">
              <div><label>Старт</label><input type="datetime-local" name="start_at" value="{dt_input(ad['start_at'])}" required></div>
              <div><label>Окончание</label><input type="datetime-local" name="end_at" value="{dt_input(ad['end_at'])}" required></div>
              <div><label>Интервал, минут</label><input type="number" name="interval_minutes" value="{ad['interval_minutes']}" min="1" required></div>
            </div>
            {interval_presets_block(ad["interval_minutes"])}
            {ad_form_preview_block(selected_group, dt_input(ad['start_at']), dt_input(ad['end_at']), ad["interval_minutes"], local_upload_url(ad["file_id"]))}
            <label><input type="checkbox" name="active" value="1" {active_checked} style="width:auto"> Объявление активно</label>
            {media_note}
            <p>
              <button type="submit">Сохранить изменения</button>
              <a class="btn ghost" href="/cabinet/ads">Отмена</a>
              <a class="btn ghost" href="/cabinet/reports?ad_id={ad_id}">Отчёт</a>
            </p>
          </form>
        </div>
        """,
        session=session,
    )


@app.post("/cabinet/ads/{ad_id}/edit")
async def edit_ad_post(request: Request, ad_id: int):
    tenant, session, response = tenant_guard(request)
    if response:
        return response
    data = await request.form()
    group_id = int(data["group_id"])
    content_html = data.get("content", "").strip()
    content = plain_text(content_html)
    image_path = await save_uploaded_image(tenant["id"], data.get("image"))
    remove_image = data.get("remove_image") == "1"
    start_at = parse_dt_input(data["start_at"])
    end_at = parse_dt_input(data["end_at"])
    interval = int(data["interval_minutes"])
    active = 1 if data.get("active") == "1" else 0
    if not content or end_at <= start_at or interval < 1:
        return redirect(f"/cabinet/ads/{ad_id}/edit")
    with db() as conn:
        ad = conn.execute("SELECT * FROM ads WHERE id = ? AND tenant_id = ?", (ad_id, tenant["id"])).fetchone()
        group = conn.execute("SELECT * FROM groups WHERE id = ? AND tenant_id = ?", (group_id, tenant["id"])).fetchone()
        if not ad or not group:
            return redirect("/cabinet/ads")
        if image_path:
            conn.execute(
                """
                UPDATE ads
                SET group_id = ?, media_type = 'photo', file_id = ?, text = NULL, text_html = NULL,
                    caption = ?, caption_html = ?, start_at = ?, end_at = ?,
                    interval_minutes = ?, active = ?, updated_at = ?
                WHERE id = ? AND tenant_id = ?
                """,
                (
                    group_id,
                    image_path,
                    content,
                    content_html,
                    start_at.isoformat(),
                    end_at.isoformat(),
                    interval,
                    active,
                    now_dt().isoformat(),
                    ad_id,
                    tenant["id"],
                ),
            )
        elif remove_image and ad["media_type"] == "photo" and local_upload_url(ad["file_id"]):
            conn.execute(
                """
                UPDATE ads
                SET group_id = ?, media_type = 'text', file_id = NULL, caption = NULL, caption_html = NULL,
                    text = ?, text_html = ?, start_at = ?, end_at = ?,
                    interval_minutes = ?, active = ?, updated_at = ?
                WHERE id = ? AND tenant_id = ?
                """,
                (
                    group_id,
                    content,
                    content_html,
                    start_at.isoformat(),
                    end_at.isoformat(),
                    interval,
                    active,
                    now_dt().isoformat(),
                    ad_id,
                    tenant["id"],
                ),
            )
        elif ad["media_type"] == "text":
            conn.execute(
                """
                UPDATE ads
                SET group_id = ?, text = ?, text_html = ?, start_at = ?, end_at = ?,
                    interval_minutes = ?, active = ?, updated_at = ?
                WHERE id = ? AND tenant_id = ?
                """,
                (
                    group_id,
                    content,
                    content_html,
                    start_at.isoformat(),
                    end_at.isoformat(),
                    interval,
                    active,
                    now_dt().isoformat(),
                    ad_id,
                    tenant["id"],
                ),
            )
        else:
            conn.execute(
                """
                UPDATE ads
                SET group_id = ?, caption = ?, caption_html = ?, start_at = ?, end_at = ?,
                    interval_minutes = ?, active = ?, updated_at = ?
                WHERE id = ? AND tenant_id = ?
                """,
                (
                    group_id,
                    content,
                    content_html,
                    start_at.isoformat(),
                    end_at.isoformat(),
                    interval,
                    active,
                    now_dt().isoformat(),
                    ad_id,
                    tenant["id"],
                ),
            )
    return redirect("/cabinet/ads")


@app.post("/cabinet/ads/toggle")
async def ad_toggle(request: Request):
    tenant, session, response = tenant_guard(request)
    if response:
        return response
    data = await form_data(request)
    ad_id = int(data["ad_id"])
    with db() as conn:
        ad = conn.execute("SELECT * FROM ads WHERE id = ? AND tenant_id = ?", (ad_id, tenant["id"])).fetchone()
        if ad:
            conn.execute(
                "UPDATE ads SET active = ?, updated_at = ? WHERE id = ?",
                (0 if ad["active"] else 1, now_dt().isoformat(), ad_id),
            )
    return redirect("/cabinet/ads")


@app.get("/cabinet/reports", response_class=HTMLResponse)
async def reports(request: Request):
    tenant, session, response = tenant_guard(request)
    if response:
        return response
    ad_id = request.query_params.get("ad_id")
    params = [tenant["id"]]
    where = "ads.tenant_id = ?"
    if ad_id:
        where += " AND ads.id = ?"
        params.append(int(ad_id))
    with db() as conn:
        ads = conn.execute(
            f"""
            SELECT ads.*, groups.title group_title
            FROM ads JOIN groups ON groups.id = ads.group_id
            WHERE {where}
            ORDER BY ads.id DESC
            """,
            params,
        ).fetchall()
    lines = []
    for ad in ads:
        status = "активно" if ad["active"] else "пауза"
        lines.append(
            f"Объявление #{ad['id']} | {ad['group_title']}\n"
            f"Статус: {status}\n"
            f"Период: {fmt(ad['start_at'])} - {fmt(ad['end_at'])}\n"
            f"Интервал: {ad['interval_minutes']} мин\n"
            f"Опубликовано: {ad['published_count']}\n"
        )
    report = "\n".join(lines) if lines else "Объявлений для отчёта пока нет."
    return page(
        "Отчёты",
        f"""
        <h1>Отчёты</h1>
        <div class="card">
          <p class="muted">Этот текст можно выделить, скопировать и отправить рекламодателю.</p>
          <div class="preview">{esc(report)}</div>
        </div>
        """,
        session=session,
    )
