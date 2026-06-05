import json
import logging
import os
import sqlite3
from calendar import monthrange
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, KeyboardButton, ReplyKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
SUPER_ADMIN_ID = int(os.getenv("SUPER_ADMIN_ID", "0"))
DATABASE_PATH = os.getenv("DATABASE_PATH", "bot.sqlite3")
TIMEZONE = ZoneInfo(os.getenv("TIMEZONE", "Europe/Istanbul"))
BOT_USERNAME = os.getenv("BOT_USERNAME", "carservise_bot")
OWNER_CONTACT_URL = os.getenv("OWNER_CONTACT_URL", f"https://t.me/{BOT_USERNAME}")
RENT_BOT_URL = os.getenv("RENT_BOT_URL", OWNER_CONTACT_URL)

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN не задан. Создай .env по примеру .env.example")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
logger = logging.getLogger(__name__)
scheduler = AsyncIOScheduler(timezone=TIMEZONE)


def now_dt():
    return datetime.now(TIMEZONE).replace(second=0, microsecond=0)


def parse_dt(value):
    return datetime.strptime(value.strip(), "%d.%m.%Y %H:%M").replace(tzinfo=TIMEZONE)


def fmt(value):
    return datetime.fromisoformat(value).astimezone(TIMEZONE).strftime("%d.%m.%Y %H:%M") if value else "-"


MONTH_NAMES = [
    "",
    "Январь",
    "Февраль",
    "Март",
    "Апрель",
    "Май",
    "Июнь",
    "Июль",
    "Август",
    "Сентябрь",
    "Октябрь",
    "Ноябрь",
    "Декабрь",
]


def db():
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    parent = Path(DATABASE_PATH).parent
    if str(parent) != ".":
        parent.mkdir(parents=True, exist_ok=True)
    with db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS tenants (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_user_id INTEGER NOT NULL UNIQUE,
                name TEXT NOT NULL,
                access_until TEXT NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS groups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant_id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(tenant_id, chat_id)
            );
            CREATE TABLE IF NOT EXISTS ads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant_id INTEGER NOT NULL,
                group_id INTEGER NOT NULL,
                media_type TEXT NOT NULL,
                file_id TEXT,
                text TEXT,
                caption TEXT,
                text_html TEXT,
                caption_html TEXT,
                start_at TEXT NOT NULL,
                end_at TEXT NOT NULL,
                interval_minutes INTEGER NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                published_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS publish_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ad_id INTEGER NOT NULL,
                published_at TEXT NOT NULL,
                status TEXT NOT NULL,
                error TEXT
            );
            """
        )
        existing = {row["name"] for row in conn.execute("PRAGMA table_info(ads)").fetchall()}
        if "text_html" not in existing:
            conn.execute("ALTER TABLE ads ADD COLUMN text_html TEXT")
        if "caption_html" not in existing:
            conn.execute("ALTER TABLE ads ADD COLUMN caption_html TEXT")
        if "post_limit" not in existing:
            conn.execute("ALTER TABLE ads ADD COLUMN post_limit INTEGER NOT NULL DEFAULT 0")
        if "last_error" not in existing:
            conn.execute("ALTER TABLE ads ADD COLUMN last_error TEXT")
        if "last_error_at" not in existing:
            conn.execute("ALTER TABLE ads ADD COLUMN last_error_at TEXT")
        if "deleted_at" not in existing:
            conn.execute("ALTER TABLE ads ADD COLUMN deleted_at TEXT")

        group_columns = {row["name"] for row in conn.execute("PRAGMA table_info(groups)").fetchall()}
        if "deleted_at" not in group_columns:
            conn.execute("ALTER TABLE groups ADD COLUMN deleted_at TEXT")

        tenant_columns = {row["name"] for row in conn.execute("PRAGMA table_info(tenants)").fetchall()}
        if "deleted_at" not in tenant_columns:
            conn.execute("ALTER TABLE tenants ADD COLUMN deleted_at TEXT")

        log_columns = {row["name"] for row in conn.execute("PRAGMA table_info(publish_logs)").fetchall()}
        if "scheduled_time" not in log_columns:
            conn.execute("ALTER TABLE publish_logs ADD COLUMN scheduled_time TEXT")

        conn.executescript(
            """
            CREATE INDEX IF NOT EXISTS idx_ads_active ON ads(active);
            CREATE INDEX IF NOT EXISTS idx_ads_tenant_group ON ads(tenant_id, group_id);
            CREATE INDEX IF NOT EXISTS idx_groups_tenant ON groups(tenant_id);
            CREATE INDEX IF NOT EXISTS idx_publish_logs_ad_id ON publish_logs(ad_id);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_publish_logs_ad_scheduled
                ON publish_logs(ad_id, scheduled_time)
                WHERE scheduled_time IS NOT NULL;
            """
        )


def tenant_by_user(user_id):
    with db() as conn:
        return conn.execute("SELECT * FROM tenants WHERE telegram_user_id = ?", (user_id,)).fetchone()


def ensure_super_tenant():
    tenant = tenant_by_user(SUPER_ADMIN_ID)
    if tenant:
        return tenant
    access_until = now_dt() + timedelta(days=3650)
    with db() as conn:
        conn.execute(
            """
            INSERT INTO tenants (telegram_user_id, name, access_until, is_active, created_at)
            VALUES (?, ?, ?, 1, ?)
            """,
            (SUPER_ADMIN_ID, "Владелец", access_until.isoformat(), now_dt().isoformat()),
        )
    return tenant_by_user(SUPER_ADMIN_ID)


def tenant_has_access(tenant):
    return bool(tenant and tenant["is_active"] and datetime.fromisoformat(tenant["access_until"]) >= now_dt())


def super_menu():
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("👤 Арендаторы"), KeyboardButton("➕ Арендатор")],
            [KeyboardButton("📣 Рекламный кабинет"), KeyboardButton("📊 Общая статистика")],
        ],
        resize_keyboard=True,
    )


def tenant_menu():
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("📢 Мои группы"), KeyboardButton("➕ Группа")],
            [KeyboardButton("📝 Мои объявления"), KeyboardButton("➕ Объявление")],
            [KeyboardButton("📊 Статистика")],
        ],
        resize_keyboard=True,
    )


def super_tenant_menu():
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("📢 Мои группы"), KeyboardButton("➕ Группа")],
            [KeyboardButton("📝 Мои объявления"), KeyboardButton("➕ Объявление")],
            [KeyboardButton("📊 Статистика")],
            [KeyboardButton("⬅️ Меню владельца")],
        ],
        resize_keyboard=True,
    )


def no_access_keyboard():
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("💳 Арендовать такого бота", url=RENT_BOT_URL)],
            [InlineKeyboardButton("📞 Связаться с владельцем", url=OWNER_CONTACT_URL)],
        ]
    )


def add_bot_to_group_keyboard():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "➕ Добавить бота в группу/канал",
                    url=f"https://t.me/{BOT_USERNAME}?startgroup=connect",
                )
            ],
            [InlineKeyboardButton("Открыть личный кабинет", url=f"https://t.me/{BOT_USERNAME}")],
        ]
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id == SUPER_ADMIN_ID:
        await update.message.reply_text("👑 Режим владельца: доступы, арендаторы и общая статистика", reply_markup=super_menu())
        return
    tenant = tenant_by_user(user_id)
    if tenant_has_access(tenant):
        await update.message.reply_text(f"✅ Доступ активен до {fmt(tenant['access_until'])}", reply_markup=tenant_menu())
    elif tenant:
        await update.message.reply_text(
            "⛔ Доступ закончился. Настройки сохранены, публикации остановлены.\n\n"
            "Продлите аренду, чтобы снова включить автопостинг.",
            reply_markup=no_access_keyboard(),
        )
    else:
        await update.message.reply_text(
            "У вас пока нет доступа к кабинету.\n\n"
            "Отправьте владельцу бота этот ID:\n"
            f"<code>{user_id}</code>\n\n"
            "Или нажмите кнопку ниже, чтобы арендовать такого же бота для своей группы.",
            parse_mode=ParseMode.HTML,
            reply_markup=no_access_keyboard(),
        )


async def register_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    if not chat or chat.type == "private":
        await update.message.reply_text("Эту команду нужно отправить в группе, которую надо добавить.")
        return
    if not user:
        await update.message.reply_text("Не могу определить админа. Отключите анонимный режим администратора и повторите.")
        return

    if user.id == SUPER_ADMIN_ID:
        tenant = ensure_super_tenant()
    else:
        tenant = tenant_by_user(user.id)
        if not tenant_has_access(tenant):
            await update.message.reply_text(
                "Группу пока нельзя зарегистрировать: у вас нет доступа к кабинету.\n\n"
                "Отправьте владельцу бота этот ID:\n"
                f"<code>{user.id}</code>\n\n"
                "После включения доступа снова напишите здесь /register_group.",
                parse_mode=ParseMode.HTML,
            )
            return

    try:
        member = await context.bot.get_chat_member(chat.id, user.id)
        if member.status not in {"administrator", "creator"} and user.id != SUPER_ADMIN_ID:
            await update.message.reply_text("Группу может зарегистрировать только администратор этой группы.")
            return
    except Exception as exc:
        await update.message.reply_text(f"Не удалось проверить права администратора: {exc}")
        return

    title = chat.title or str(chat.id)
    with db() as conn:
        conn.execute(
            """
            INSERT INTO groups (tenant_id, chat_id, title, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(tenant_id, chat_id) DO UPDATE SET title = excluded.title
            """,
            (tenant["id"], chat.id, title, now_dt().isoformat()),
        )
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("Открыть кабинет", url=f"https://t.me/{BOT_USERNAME}")]]
    )
    await update.message.reply_text(
        "Группа зарегистрирована.\n\n"
        "Дальше нажмите кнопку ниже, откройте личный чат с ботом и выберите:\n"
        "Рекламный кабинет -> Мои группы.",
        reply_markup=keyboard,
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    if update.effective_chat and update.effective_chat.type != "private":
        return
    user_id = update.effective_user.id
    text = update.message.text or update.message.caption or ""
    if user_id == SUPER_ADMIN_ID:
        await handle_super(update, context, text)
        return
    tenant = tenant_by_user(user_id)
    if not tenant_has_access(tenant):
        await update.message.reply_text(
            "У вас пока нет активного доступа к кабинету.\n\n"
            "Отправьте владельцу бота этот ID:\n"
            f"<code>{user_id}</code>\n\n"
            "Или нажмите кнопку ниже, чтобы арендовать такого же бота для своей группы.",
            parse_mode=ParseMode.HTML,
            reply_markup=no_access_keyboard(),
        )
        return
    await handle_tenant(update, context, tenant, text)


async def handle_super(update, context, text):
    step = context.user_data.get("step")
    tenant_buttons = {"📢 Мои группы", "➕ Группа", "📝 Мои объявления", "➕ Объявление", "📊 Статистика"}
    tenant_steps = {"add_group", "ad_media", "ad_album_collect", "ad_edit_text", "ad_start", "ad_end", "ad_interval"}

    if text == "⬅️ Меню владельца":
        context.user_data.clear()
        await update.message.reply_text("👑 Меню владельца бота", reply_markup=super_menu())
        return

    if text in {"🧪 Мой кабинет", "📣 Рекламный кабинет"}:
        ensure_super_tenant()
        context.user_data.clear()
        await update.message.reply_text("📣 Рекламный кабинет: группы, объявления и расписание публикаций", reply_markup=super_tenant_menu())
        return

    if text in tenant_buttons or step in tenant_steps:
        tenant = ensure_super_tenant()
        await handle_tenant(update, context, tenant, text)
        return

    if text == "➕ Арендатор":
        context.user_data["step"] = "add_tenant"
        await update.message.reply_text(
            "Введите: <code>TelegramID дней имя</code>\nНапример: <code>123456789 30 Иван</code>",
            parse_mode=ParseMode.HTML,
        )
        return
    if step == "add_tenant":
        try:
            user_id_raw, days_raw, name = text.strip().split(maxsplit=2)
            access_until = now_dt() + timedelta(days=int(days_raw))
            with db() as conn:
                conn.execute(
                    """
                    INSERT INTO tenants (telegram_user_id, name, access_until, is_active, created_at)
                    VALUES (?, ?, ?, 1, ?)
                    ON CONFLICT(telegram_user_id)
                    DO UPDATE SET name = excluded.name, access_until = excluded.access_until, is_active = 1
                    """,
                    (int(user_id_raw), name, access_until.isoformat(), now_dt().isoformat()),
                )
            context.user_data.clear()
            await update.message.reply_text(f"✅ Арендатор добавлен до {fmt(access_until.isoformat())}", reply_markup=super_menu())
        except Exception:
            await update.message.reply_text("❌ Неверный формат. Пример: 123456789 30 Иван")
        return
    if text == "👤 Арендаторы":
        with db() as conn:
            tenants = conn.execute("SELECT * FROM tenants ORDER BY id DESC").fetchall()
        if not tenants:
            await update.message.reply_text("Пока нет арендаторов.")
            return
        for tenant in tenants:
            mark = "✅" if tenant_has_access(tenant) else "⛔"
            toggle_label = "🔴 Отключить" if tenant["is_active"] else "🟢 Активировать"
            toggle_cb = f"tenant_disable_{tenant['id']}" if tenant["is_active"] else f"tenant_enable_{tenant['id']}"
            keyboard = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("+10 дней", callback_data=f"tenant_extend_{tenant['id']}_10"),
                        InlineKeyboardButton("+30 дней", callback_data=f"tenant_extend_{tenant['id']}_30"),
                        InlineKeyboardButton("+90 дней", callback_data=f"tenant_extend_{tenant['id']}_90"),
                    ],
                    [InlineKeyboardButton(toggle_label, callback_data=toggle_cb)],
                ]
            )
            await update.message.reply_text(
                f"{mark} #{tenant['id']} {tenant['name']}\nID: <code>{tenant['telegram_user_id']}</code>\nДо: {fmt(tenant['access_until'])}",
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
            )
        return
    if text == "📊 Общая статистика":
        with db() as conn:
            tenants = conn.execute("SELECT COUNT(*) FROM tenants").fetchone()[0]
            ads = conn.execute("SELECT COUNT(*) FROM ads").fetchone()[0]
            posts = conn.execute("SELECT COALESCE(SUM(published_count), 0) FROM ads").fetchone()[0]
        await update.message.reply_text(f"📊 Всего арендаторов: {tenants}\n📝 Объявлений: {ads}\n📨 Публикаций: {posts}")
        return
    await update.message.reply_text("Выберите действие в меню.", reply_markup=super_menu())


async def handle_tenant(update, context, tenant, text):
    step = context.user_data.get("step")
    if text == "➕ Группа":
        context.user_data["step"] = "add_group"
        await update.message.reply_text(
            "Подключение группы без ручного ID:\n\n"
            "1. Нажмите кнопку «Добавить бота в группу/канал».\n"
            "2. Выберите нужную группу или канал.\n"
            "3. Назначьте бота администратором с правом публиковать сообщения.\n"
            "4. В этой группе/канале отправьте команду <code>/register_group</code>.\n"
            "5. Вернитесь сюда и нажмите «Мои группы».\n\n"
            "Запасной ручной способ: введите ID и название сообщением сюда:\n"
            "<code>-1001234567890 Моя группа</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=add_bot_to_group_keyboard(),
        )
        return
    if step == "add_group":
        try:
            chat_id_raw, title = text.strip().split(maxsplit=1)
            with db() as conn:
                conn.execute(
                    """
                    INSERT INTO groups (tenant_id, chat_id, title, created_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(tenant_id, chat_id) DO UPDATE SET title = excluded.title
                    """,
                    (tenant["id"], int(chat_id_raw), title, now_dt().isoformat()),
                )
            context.user_data.clear()
            await update.message.reply_text("✅ Группа сохранена.", reply_markup=tenant_menu())
        except Exception:
            await update.message.reply_text("❌ Пример: -1001234567890 Моя группа")
        return
    if text == "📢 Мои группы":
        with db() as conn:
            groups = conn.execute("SELECT * FROM groups WHERE tenant_id = ? ORDER BY id DESC", (tenant["id"],)).fetchall()
        if not groups:
            await update.message.reply_text(
                "Группы пока не добавлены.\n\n"
                "Нажмите «Добавить бота в группу/канал», выберите группу и затем отправьте там команду /register_group.",
                reply_markup=add_bot_to_group_keyboard(),
            )
            return
        await update.message.reply_text(
            "\n".join(f"#{g['id']} | <code>{g['chat_id']}</code> | {g['title']}" for g in groups),
            parse_mode=ParseMode.HTML,
            reply_markup=add_bot_to_group_keyboard(),
        )
        return
    if text == "➕ Объявление":
        with db() as conn:
            groups = conn.execute("SELECT * FROM groups WHERE tenant_id = ? ORDER BY id DESC", (tenant["id"],)).fetchall()
        if not groups:
            await update.message.reply_text(
                "Сначала подключите группу или канал.\n\n"
                "После добавления бота администратором отправьте в группе команду /register_group.",
                reply_markup=add_bot_to_group_keyboard(),
            )
            return
        if len(groups) == 1:
            context.user_data["step"] = "ad_media"
            context.user_data["new_ad"] = {"group_id": groups[0]["id"]}
            await update.message.reply_text(
                f"Группа выбрана: {groups[0]['title']}\n\n"
                "Отправьте объявление: текст, фото, видео, GIF, документ или альбом 2-10 фото."
            )
            return
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton(g["title"], callback_data=f"ad_group_{g['id']}")] for g in groups])
        await update.message.reply_text("Выберите группу:", reply_markup=keyboard)
        return
    if step == "ad_media":
        if update.message.media_group_id and update.message.photo:
            context.user_data["new_ad"].update(
                {
                    "media_type": "album",
                    "file_id": json.dumps([update.message.photo[-1].file_id]),
                    "caption": update.message.caption,
                    "caption_html": update.message.caption_html,
                }
            )
            context.user_data["album_group_id"] = update.message.media_group_id
            context.user_data["step"] = "ad_album_collect"
            keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("Готово", callback_data="ad_album_done")]])
            await update.message.reply_text(
                "Получаю альбом. Когда все фото загрузятся, нажмите «Готово».",
                reply_markup=keyboard,
            )
            return
        media = extract_media(update)
        if not media:
            await update.message.reply_text("Отправьте текст, фото, видео, GIF, документ или альбом 2-10 фото.")
            return
        context.user_data["new_ad"].update(media)
        context.user_data["step"] = "ad_preview"
        await send_ad_preview(update, context)
        return
    if step == "ad_album_collect":
        if update.message.media_group_id == context.user_data.get("album_group_id") and update.message.photo:
            new_ad = context.user_data["new_ad"]
            files = json.loads(new_ad["file_id"])
            file_id = update.message.photo[-1].file_id
            if file_id not in files:
                files.append(file_id)
            new_ad["file_id"] = json.dumps(files)
            if update.message.caption:
                new_ad["caption"] = update.message.caption
                new_ad["caption_html"] = update.message.caption_html
            return
        if text.strip().lower() in {"готово", "done"}:
            context.user_data["step"] = "ad_preview"
            await send_ad_preview(update, context)
            return
        await update.message.reply_text("Дождитесь загрузки всех фото и нажмите «Готово».")
        return
    if step == "ad_edit_text":
        new_ad = context.user_data["new_ad"]
        if new_ad["media_type"] == "text":
            if not update.message.text:
                await update.message.reply_text("Отправьте новый текст объявления.")
                return
            new_ad["text"] = update.message.text
            new_ad["text_html"] = update.message.text_html
        else:
            if not (update.message.text or update.message.caption):
                await update.message.reply_text("Отправьте новую подпись текстом.")
                return
            new_ad["caption"] = update.message.text or update.message.caption
            new_ad["caption_html"] = update.message.text_html or update.message.caption_html
        context.user_data["step"] = "ad_preview"
        await send_ad_preview(update, context)
        return
    if step == "ad_start":
        try:
            context.user_data["new_ad"]["start_at"] = parse_dt(text).isoformat()
            context.user_data["step"] = "ad_end"
            await update.message.reply_text("Введите окончание: ДД.ММ.ГГГГ ЧЧ:ММ")
        except Exception:
            await update.message.reply_text("❌ Пример: 01.06.2026 14:30")
        return
    if step == "ad_end":
        try:
            end_at = parse_dt(text)
            start_at = datetime.fromisoformat(context.user_data["new_ad"]["start_at"])
            if end_at <= start_at:
                await update.message.reply_text("❌ Окончание должно быть позже старта.")
                return
            context.user_data["new_ad"]["end_at"] = end_at.isoformat()
            context.user_data["step"] = "ad_interval"
            await update.message.reply_text("Введите интервал в минутах, например 240.")
        except Exception:
            await update.message.reply_text("❌ Пример: 30.06.2026 23:59")
        return
    if step == "ad_interval":
        try:
            interval = int(text.strip())
            if interval < 1:
                raise ValueError
            new_ad = context.user_data["new_ad"]
            ad_id = create_ad(tenant, new_ad, interval)
            context.user_data.clear()
            schedule_ad(context.application, ad_id)
            await update.message.reply_text(f"✅ Объявление #{ad_id} добавлено.", reply_markup=tenant_menu())
        except Exception:
            await update.message.reply_text("❌ Введите число минут.")
        return
    if text == "📝 Мои объявления":
        await show_ads(update, tenant)
        return
    if text == "📊 Статистика":
        with db() as conn:
            row = conn.execute(
                "SELECT COUNT(*) total, COALESCE(SUM(active), 0) active, COALESCE(SUM(published_count), 0) posts FROM ads WHERE tenant_id = ?",
                (tenant["id"],),
            ).fetchone()
        await update.message.reply_text(f"📝 Объявлений: {row['total']}\n✅ Активных: {row['active']}\n📨 Публикаций: {row['posts']}")
        return
    await update.message.reply_text("Выберите действие в меню.", reply_markup=tenant_menu())


def extract_media(update):
    m = update.message
    if m.text:
        return {"media_type": "text", "text": m.text, "text_html": m.text_html}
    if m.photo:
        return {"media_type": "photo", "file_id": m.photo[-1].file_id, "caption": m.caption, "caption_html": m.caption_html}
    if m.video:
        return {"media_type": "video", "file_id": m.video.file_id, "caption": m.caption, "caption_html": m.caption_html}
    if m.animation:
        return {"media_type": "animation", "file_id": m.animation.file_id, "caption": m.caption, "caption_html": m.caption_html}
    if m.document:
        return {"media_type": "document", "file_id": m.document.file_id, "caption": m.caption, "caption_html": m.caption_html}
    return None


async def send_ad_preview(update, context):
    ad = context.user_data["new_ad"]
    message = update.message if hasattr(update, "message") else update
    chat_id = message.chat_id
    await message.reply_text("Предпросмотр. Проверьте текст, фото и подпись перед постановкой в сетку:")
    if ad["media_type"] == "text":
        await context.bot.send_message(chat_id, ad.get("text_html") or ad["text"], parse_mode=ParseMode.HTML)
    elif ad["media_type"] == "photo":
        await context.bot.send_photo(chat_id, ad["file_id"], caption=ad.get("caption_html") or ad.get("caption"), parse_mode=ParseMode.HTML)
    elif ad["media_type"] == "video":
        await context.bot.send_video(chat_id, ad["file_id"], caption=ad.get("caption_html") or ad.get("caption"), parse_mode=ParseMode.HTML)
    elif ad["media_type"] == "animation":
        await context.bot.send_animation(chat_id, ad["file_id"], caption=ad.get("caption_html") or ad.get("caption"), parse_mode=ParseMode.HTML)
    elif ad["media_type"] == "document":
        await context.bot.send_document(chat_id, ad["file_id"], caption=ad.get("caption_html") or ad.get("caption"), parse_mode=ParseMode.HTML)
    elif ad["media_type"] == "album":
        files = json.loads(ad["file_id"])
        if len(files) == 1:
            await context.bot.send_photo(chat_id, files[0], caption=ad.get("caption_html") or ad.get("caption"), parse_mode=ParseMode.HTML)
        else:
            media = []
            for index, file_id in enumerate(files):
                if index == 0:
                    media.append(InputMediaPhoto(file_id, caption=ad.get("caption_html") or ad.get("caption"), parse_mode=ParseMode.HTML))
                else:
                    media.append(InputMediaPhoto(file_id))
            await context.bot.send_media_group(chat_id, media)
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Все верно, дальше", callback_data="ad_preview_ok")],
            [InlineKeyboardButton("Исправить текст/подпись", callback_data="ad_preview_edit_text")],
            [InlineKeyboardButton("Заменить фото/материал", callback_data="ad_preview_edit")],
            [InlineKeyboardButton("Выбрать другую группу", callback_data="ad_preview_change_group")],
            [InlineKeyboardButton("Отмена", callback_data="ad_cancel")],
        ]
    )
    await message.reply_text("Поставить это объявление в расписание?", reply_markup=keyboard)


async def ask_schedule_date(q):
    today = now_dt()
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Через 5 мин", callback_data="ad_quickstart_5"),
                InlineKeyboardButton("Через 15 мин", callback_data="ad_quickstart_15"),
            ],
            [
                InlineKeyboardButton("Через 30 мин", callback_data="ad_quickstart_30"),
                InlineKeyboardButton("Через 1 час", callback_data="ad_quickstart_60"),
            ],
            [
                InlineKeyboardButton("Сегодня", callback_data="ad_date_today"),
                InlineKeyboardButton("Завтра", callback_data="ad_date_tomorrow"),
            ],
            [InlineKeyboardButton("Календарь", callback_data=f"ad_calendar_{today.year}_{today.month}")],
            [InlineKeyboardButton("Ввести дату вручную", callback_data="ad_date_manual")],
            [InlineKeyboardButton("Отмена", callback_data="ad_cancel")],
        ]
    )
    await reply_to_callback(q, "Когда начать публикацию?", reply_markup=keyboard)


def calendar_markup(year, month):
    today = now_dt().date()
    first_weekday, days_count = monthrange(year, month)
    buttons = [[InlineKeyboardButton(f"{MONTH_NAMES[month]} {year}", callback_data="noop")]]
    buttons.append([InlineKeyboardButton(day, callback_data="noop") for day in ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]])

    row = [InlineKeyboardButton(" ", callback_data="noop") for _ in range(first_weekday)]
    for day in range(1, days_count + 1):
        current = datetime(year, month, day).date()
        if current < today:
            row.append(InlineKeyboardButton("·", callback_data="noop"))
        else:
            row.append(InlineKeyboardButton(str(day), callback_data=f"ad_day_{year}_{month}_{day}"))
        if len(row) == 7:
            buttons.append(row)
            row = []
    if row:
        row.extend([InlineKeyboardButton(" ", callback_data="noop") for _ in range(7 - len(row))])
        buttons.append(row)

    prev_month = month - 1 or 12
    prev_year = year - 1 if month == 1 else year
    next_month = month + 1 if month < 12 else 1
    next_year = year + 1 if month == 12 else year
    buttons.append(
        [
            InlineKeyboardButton("<", callback_data=f"ad_calendar_{prev_year}_{prev_month}"),
            InlineKeyboardButton("Сегодня", callback_data="ad_date_today"),
            InlineKeyboardButton(">", callback_data=f"ad_calendar_{next_year}_{next_month}"),
        ]
    )
    buttons.append([InlineKeyboardButton("Отмена", callback_data="ad_cancel")])
    return InlineKeyboardMarkup(buttons)


async def ask_schedule_calendar(q, year=None, month=None):
    current = now_dt()
    year = year or current.year
    month = month or current.month
    await reply_to_callback(q, "Выберите дату старта:", reply_markup=calendar_markup(year, month))


async def ask_schedule_time(q):
    buttons = []
    for hour in range(10, 24):
        label = f"{hour:02d}"
        buttons.append(InlineKeyboardButton(label, callback_data=f"ad_hour_{hour}"))
    rows = [buttons[i : i + 4] for i in range(0, len(buttons), 4)]
    rows.append([InlineKeyboardButton("Ввести время вручную", callback_data="ad_time_manual")])
    rows.append([InlineKeyboardButton("Отмена", callback_data="ad_cancel")])
    await reply_to_callback(q, "Выберите час старта:", reply_markup=InlineKeyboardMarkup(rows))


async def ask_schedule_minute(q):
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("00", callback_data="ad_minute_0"),
                InlineKeyboardButton("05", callback_data="ad_minute_5"),
                InlineKeyboardButton("10", callback_data="ad_minute_10"),
                InlineKeyboardButton("15", callback_data="ad_minute_15"),
            ],
            [
                InlineKeyboardButton("20", callback_data="ad_minute_20"),
                InlineKeyboardButton("25", callback_data="ad_minute_25"),
                InlineKeyboardButton("30", callback_data="ad_minute_30"),
                InlineKeyboardButton("35", callback_data="ad_minute_35"),
            ],
            [
                InlineKeyboardButton("40", callback_data="ad_minute_40"),
                InlineKeyboardButton("45", callback_data="ad_minute_45"),
                InlineKeyboardButton("50", callback_data="ad_minute_50"),
                InlineKeyboardButton("55", callback_data="ad_minute_55"),
            ],
            [InlineKeyboardButton("Отмена", callback_data="ad_cancel")],
        ]
    )
    await reply_to_callback(q, "Выберите минуты старта:", reply_markup=keyboard)


async def ask_schedule_duration(q):
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("30 мин", callback_data="ad_duration_minutes_30"),
                InlineKeyboardButton("1 час", callback_data="ad_duration_hours_1"),
            ],
            [
                InlineKeyboardButton("3 часа", callback_data="ad_duration_hours_3"),
                InlineKeyboardButton("6 часов", callback_data="ad_duration_hours_6"),
            ],
            [
                InlineKeyboardButton("До 23:00", callback_data="ad_duration_today"),
                InlineKeyboardButton("1 день", callback_data="ad_duration_1"),
            ],
            [
                InlineKeyboardButton("7 дней", callback_data="ad_duration_7"),
                InlineKeyboardButton("30 дней", callback_data="ad_duration_30"),
            ],
            [InlineKeyboardButton("Ввести окончание вручную", callback_data="ad_duration_manual")],
            [InlineKeyboardButton("Отмена", callback_data="ad_cancel")],
        ]
    )
    await reply_to_callback(q, "Выберите срок размещения:", reply_markup=keyboard)


async def ask_schedule_interval(q):
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("3 мин", callback_data="ad_interval_3"),
                InlineKeyboardButton("5 мин", callback_data="ad_interval_5"),
                InlineKeyboardButton("10 мин", callback_data="ad_interval_10"),
            ],
            [
                InlineKeyboardButton("30 мин", callback_data="ad_interval_30"),
                InlineKeyboardButton("1 час", callback_data="ad_interval_60"),
                InlineKeyboardButton("2 часа", callback_data="ad_interval_120"),
            ],
            [
                InlineKeyboardButton("3 часа", callback_data="ad_interval_180"),
                InlineKeyboardButton("4 часа", callback_data="ad_interval_240"),
            ],
            [InlineKeyboardButton("Ввести интервал вручную", callback_data="ad_interval_manual")],
            [InlineKeyboardButton("Отмена", callback_data="ad_cancel")],
        ]
    )
    await reply_to_callback(q, "Выберите интервал повторения:", reply_markup=keyboard)


async def ask_final_confirmation(q, context, interval):
    new_ad = context.user_data["new_ad"]
    context.user_data["pending_interval"] = interval
    approx = max(1, int((datetime.fromisoformat(new_ad["end_at"]) - datetime.fromisoformat(new_ad["start_at"])).total_seconds() // (interval * 60)) + 1)
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Запустить", callback_data="ad_confirm_start")],
            [InlineKeyboardButton("Изменить расписание", callback_data="ad_schedule_restart")],
            [InlineKeyboardButton("Вернуться к предпросмотру", callback_data="ad_back_to_preview")],
            [InlineKeyboardButton("Отмена", callback_data="ad_cancel")],
        ]
    )
    await reply_to_callback(q, 
        "Проверьте расписание:\n\n"
        f"Старт: {fmt(new_ad['start_at'])}\n"
        f"Окончание: {fmt(new_ad['end_at'])}\n"
        f"Интервал: {interval} мин.\n"
        f"Примерно публикаций: {approx}\n\n"
        "Запустить это объявление?",
        reply_markup=keyboard,
    )


def create_ad(tenant, new_ad, interval):
    with db() as conn:
        cur = conn.execute(
            """
            INSERT INTO ads (
                tenant_id, group_id, media_type, file_id, text, caption,
                text_html, caption_html, start_at, end_at, interval_minutes, active, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
            """,
            (
                tenant["id"],
                new_ad["group_id"],
                new_ad["media_type"],
                new_ad.get("file_id"),
                new_ad.get("text"),
                new_ad.get("caption"),
                new_ad.get("text_html"),
                new_ad.get("caption_html"),
                new_ad["start_at"],
                new_ad["end_at"],
                interval,
                now_dt().isoformat(),
            ),
        )
        return cur.lastrowid


def ad_preview_text(ad):
    status = "✅ Активно" if ad["active"] else "⏸ На паузе"
    preview = ad["text"] or ad["caption"] or f"[{ad['media_type']}]"
    return (
        f"{status} #{ad['id']} | {ad['group_title']}\n"
        f"{fmt(ad['start_at'])} - {fmt(ad['end_at'])}\n"
        f"⏱ {ad['interval_minutes']} мин\n"
        f"📨 {ad['published_count']}\n\n"
        f"{preview[:300]}"
    )


async def send_ad_card(message, ad):
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("⏸ Пауза" if ad["active"] else "▶️ Запуск", callback_data=f"ad_toggle_{ad['id']}"),
                InlineKeyboardButton("📤 В другую группу", callback_data=f"ad_clone_{ad['id']}"),
            ],
            [InlineKeyboardButton("🗑 Удалить", callback_data=f"ad_delete_{ad['id']}")],
        ]
    )
    text = ad_preview_text(ad)
    try:
        if ad["media_type"] == "photo":
            await message.reply_photo(ad["file_id"], caption=text, reply_markup=keyboard)
        elif ad["media_type"] == "album":
            files = json.loads(ad["file_id"])
            await message.reply_photo(files[0], caption=text, reply_markup=keyboard)
        else:
            await message.reply_text(text, reply_markup=keyboard)
    except Exception:
        await message.reply_text(text, reply_markup=keyboard)


def clone_ad_to_group(tenant_id, ad_id, group_id):
    with db() as conn:
        original = conn.execute(
            "SELECT * FROM ads WHERE id = ? AND tenant_id = ?",
            (ad_id, tenant_id),
        ).fetchone()
        group = conn.execute(
            "SELECT * FROM groups WHERE id = ? AND tenant_id = ?",
            (group_id, tenant_id),
        ).fetchone()
        if not original or not group:
            return None
        cur = conn.execute(
            """
            INSERT INTO ads (
                tenant_id, group_id, media_type, file_id, text, caption,
                text_html, caption_html, start_at, end_at, interval_minutes,
                active, post_limit, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
            """,
            (
                tenant_id,
                group_id,
                original["media_type"],
                original["file_id"],
                original["text"],
                original["caption"],
                original["text_html"],
                original["caption_html"],
                original["start_at"],
                original["end_at"],
                original["interval_minutes"],
                original["post_limit"] if "post_limit" in original.keys() else 0,
                now_dt().isoformat(),
            ),
        )
        return cur.lastrowid


async def show_ads(update, tenant):
    with db() as conn:
        ads = conn.execute(
            """
            SELECT ads.*, groups.title group_title
            FROM ads JOIN groups ON groups.id = ads.group_id
            WHERE ads.tenant_id = ? ORDER BY ads.id DESC
            """,
            (tenant["id"],),
        ).fetchall()
    if not ads:
        await update.message.reply_text("Объявлений пока нет.")
        return
    for ad in ads:
        await send_ad_card(update.message, ad)


async def handle_tenant_callback(q, context, tenant, data):
    if data == "noop":
        return

    if data == "ad_cancel":
        context.user_data.clear()
        await reply_to_callback(q, "Отменено. Объявление не поставлено в расписание.")
        return

    if data == "ad_preview_edit_text":
        context.user_data["step"] = "ad_edit_text"
        await reply_to_callback(q, "Отправьте новый текст/подпись. Фото останутся прежними.")
        return

    if data == "ad_preview_edit":
        group_id = context.user_data.get("new_ad", {}).get("group_id")
        context.user_data.clear()
        context.user_data["step"] = "ad_media"
        context.user_data["new_ad"] = {"group_id": group_id}
        await reply_to_callback(q, "Отправьте исправленный материал: текст, фото, видео или альбом 2-10 фото.")
        return

    if data == "ad_preview_change_group":
        new_ad = context.user_data.get("new_ad")
        if not new_ad:
            await reply_to_callback(q, "Черновик не найден. Начните добавление объявления заново.")
            return
        with db() as conn:
            groups = conn.execute(
                "SELECT * FROM groups WHERE tenant_id = ? ORDER BY title",
                (tenant["id"],),
            ).fetchall()
        if not groups:
            await reply_to_callback(q, "Сначала добавьте группу.")
            return
        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton(g["title"], callback_data=f"ad_change_group_to_{g['id']}")] for g in groups]
            + [[InlineKeyboardButton("Назад к предпросмотру", callback_data="ad_back_to_preview")]]
        )
        await reply_to_callback(q, "Выберите группу для этого объявления. Текст и фото сохранятся.", reply_markup=keyboard)
        return

    if data.startswith("ad_change_group_to_"):
        group_id = int(data.split("_")[4])
        if "new_ad" not in context.user_data:
            await reply_to_callback(q, "Черновик не найден. Начните добавление объявления заново.")
            return
        with db() as conn:
            group = conn.execute(
                "SELECT * FROM groups WHERE id = ? AND tenant_id = ?",
                (group_id, tenant["id"]),
            ).fetchone()
        if not group:
            await reply_to_callback(q, "Группа не найдена.")
            return
        context.user_data["new_ad"]["group_id"] = group_id
        context.user_data["step"] = "ad_preview"
        await reply_to_callback(q, f"Группа изменена: {group['title']}. Сейчас снова покажу предпросмотр.")
        await send_ad_preview(q, context)
        return

    if data == "ad_back_to_preview":
        if "new_ad" not in context.user_data:
            await reply_to_callback(q, "Черновик не найден. Начните добавление объявления заново.")
            return
        context.user_data["step"] = "ad_preview"
        await send_ad_preview(q, context)
        return

    if data == "ad_schedule_restart":
        if "new_ad" not in context.user_data:
            await reply_to_callback(q, "Черновик не найден. Начните добавление объявления заново.")
            return
        for key in ("schedule_date", "schedule_hour", "pending_interval"):
            context.user_data.pop(key, None)
        context.user_data["new_ad"].pop("start_at", None)
        context.user_data["new_ad"].pop("end_at", None)
        await ask_schedule_date(q)
        return

    if data == "ad_album_done":
        context.user_data["step"] = "ad_preview"
        await reply_to_callback(q, "Альбом принят. Сейчас покажу предпросмотр.")
        await send_ad_preview(q, context)
        return

    if data == "ad_confirm_start":
        new_ad = context.user_data["new_ad"]
        interval = context.user_data["pending_interval"]
        ad_id = create_ad(tenant, new_ad, interval)
        context.user_data.clear()
        schedule_ad(context.application, ad_id)
        await reply_to_callback(q, 
            f"Объявление #{ad_id} запущено.\n"
            f"Старт: {fmt(new_ad['start_at'])}\n"
            f"Окончание: {fmt(new_ad['end_at'])}\n"
            f"Интервал: {interval} мин."
        )
        return

    if data == "ad_preview_ok":
        await ask_schedule_date(q)
        return

    if data.startswith("ad_quickstart_"):
        minutes = int(data.split("_")[2])
        context.user_data["new_ad"]["start_at"] = (now_dt() + timedelta(minutes=minutes)).isoformat()
        await ask_schedule_duration(q)
        return

    if data.startswith("ad_date_"):
        choice = data.split("_")[2]
        if choice == "manual":
            context.user_data["step"] = "ad_start"
            await reply_to_callback(q, "Введите старт вручную: ДД.ММ.ГГГГ ЧЧ:ММ")
            return
        base = now_dt().date()
        if choice == "tomorrow":
            base = base + timedelta(days=1)
        context.user_data["schedule_date"] = base.isoformat()
        await ask_schedule_time(q)
        return

    if data.startswith("ad_calendar_"):
        _, _, year, month = data.split("_")
        await ask_schedule_calendar(q, int(year), int(month))
        return

    if data.startswith("ad_day_"):
        _, _, year, month, day = data.split("_")
        context.user_data["schedule_date"] = datetime(int(year), int(month), int(day)).date().isoformat()
        await ask_schedule_time(q)
        return

    if data.startswith("ad_hour_"):
        context.user_data["schedule_hour"] = int(data.split("_")[2])
        await ask_schedule_minute(q)
        return

    if data.startswith("ad_minute_"):
        minute = int(data.split("_")[2])
        hour = context.user_data["schedule_hour"]
        day = datetime.fromisoformat(context.user_data["schedule_date"] + "T00:00:00")
        start_at = day.replace(hour=hour, minute=minute, tzinfo=TIMEZONE)
        if start_at <= now_dt():
            await reply_to_callback(q, "Это время уже прошло. Выберите время позже.")
            await ask_schedule_time(q)
            return
        context.user_data["new_ad"]["start_at"] = start_at.isoformat()
        await ask_schedule_duration(q)
        return

    if data.startswith("ad_time_"):
        choice = data.split("_")[2]
        if choice == "manual":
            context.user_data["step"] = "ad_start"
            await reply_to_callback(q, "Введите старт вручную: ДД.ММ.ГГГГ ЧЧ:ММ")
            return
        hour = int(choice[:2])
        minute = int(choice[2:])
        day = datetime.fromisoformat(context.user_data["schedule_date"] + "T00:00:00")
        start_at = day.replace(hour=hour, minute=minute, tzinfo=TIMEZONE)
        if start_at <= now_dt():
            await reply_to_callback(q, "Это время уже прошло. Выберите время позже.")
            await ask_schedule_time(q)
            return
        context.user_data["new_ad"]["start_at"] = start_at.isoformat()
        await ask_schedule_duration(q)
        return

    if data.startswith("ad_duration_"):
        parts = data.split("_")
        choice = parts[2]
        if choice == "manual":
            context.user_data["step"] = "ad_end"
            await reply_to_callback(q, "Введите окончание вручную: ДД.ММ.ГГГГ ЧЧ:ММ")
            return
        start_at = datetime.fromisoformat(context.user_data["new_ad"]["start_at"])
        if choice == "minutes":
            end_at = start_at + timedelta(minutes=int(parts[3]))
        elif choice == "hours":
            end_at = start_at + timedelta(hours=int(parts[3]))
        elif choice == "today":
            end_at = start_at.replace(hour=23, minute=0)
            if end_at <= start_at:
                end_at = start_at + timedelta(hours=1)
        else:
            end_at = start_at + timedelta(days=int(choice))
        context.user_data["new_ad"]["end_at"] = end_at.isoformat()
        await ask_schedule_interval(q)
        return

    if data.startswith("ad_interval_"):
        choice = data.split("_")[2]
        if choice == "manual":
            context.user_data["step"] = "ad_interval"
            await reply_to_callback(q, "Введите интервал вручную в минутах, например 240.")
            return
        interval = int(choice)
        new_ad = context.user_data["new_ad"]
        await ask_final_confirmation(q, context, interval)
        return

    if data.startswith("ad_group_"):
        group_id = int(data.split("_")[2])
        context.user_data["step"] = "ad_media"
        context.user_data["new_ad"] = {"group_id": group_id}
        await reply_to_callback(q, "Отправьте объявление: текст, фото, видео, GIF, документ или альбом 2-10 фото.")
        return

    if data.startswith("ad_clone_to_"):
        _, _, _, ad_id_raw, group_id_raw = data.split("_")
        new_ad_id = clone_ad_to_group(tenant["id"], int(ad_id_raw), int(group_id_raw))
        if not new_ad_id:
            await reply_to_callback(q, "Не удалось скопировать объявление.")
            return
        schedule_ad(context.application, new_ad_id)
        await reply_to_callback(
            q,
            f"✅ Копия создана.\n"
            f"Новый номер: #{new_ad_id}\n\n"
            "Она будет публиковаться по тому же расписанию, что и исходное объявление.",
        )
        return

    if data.startswith("ad_clone_"):
        ad_id = int(data.split("_")[2])
        with db() as conn:
            ad = conn.execute(
                "SELECT * FROM ads WHERE id = ? AND tenant_id = ?",
                (ad_id, tenant["id"]),
            ).fetchone()
            groups = conn.execute(
                "SELECT * FROM groups WHERE tenant_id = ? AND id != ? ORDER BY title",
                (tenant["id"], ad["group_id"] if ad else 0),
            ).fetchall()
        if not ad:
            await reply_to_callback(q, "Объявление не найдено.")
            return
        if not groups:
            await reply_to_callback(q, "Других групп пока нет. Сначала добавьте или зарегистрируйте ещё одну группу.")
            return
        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton(g["title"], callback_data=f"ad_clone_to_{ad_id}_{g['id']}")] for g in groups]
            + [[InlineKeyboardButton("Отмена", callback_data="noop")]]
        )
        await reply_to_callback(
            q,
            "Будет создана копия объявления: тот же текст/фото, тот же старт, окончание и интервал.\n\n"
            "Это бонусное размещение в другой вашей группе. Выберите группу для копии:",
            reply_markup=keyboard,
        )
        return

    if data.startswith("ad_toggle_"):
        ad_id = int(data.split("_")[2])
        with db() as conn:
            ad = conn.execute(
                "SELECT * FROM ads WHERE id = ? AND tenant_id = ?",
                (ad_id, tenant["id"]),
            ).fetchone()
            if not ad:
                await reply_to_callback(q, "Объявление не найдено.")
                return
            active = 0 if ad["active"] else 1
            conn.execute("UPDATE ads SET active = ? WHERE id = ?", (active, ad_id))
        if active:
            schedule_ad(context.application, ad_id)
            await reply_to_callback(q, f"▶️ Объявление #{ad_id} запущено. Оно осталось в списке «Мои объявления».")
        else:
            remove_job(ad_id)
            await reply_to_callback(q, f"⏸ Объявление #{ad_id} поставлено на паузу. Оно не удалено.")
        return

    if data.startswith("ad_delete_"):
        ad_id = int(data.split("_")[2])
        with db() as conn:
            conn.execute("DELETE FROM ads WHERE id = ? AND tenant_id = ?", (ad_id, tenant["id"]))
        remove_job(ad_id)
        await reply_to_callback(q, "Удалено.")

async def reply_to_callback(q, text, reply_markup=None):
    try:
        if q.message and q.message.text:
            await q.edit_message_text(text, reply_markup=reply_markup)
            return
        if q.message:
            try:
                await q.edit_message_reply_markup(reply_markup=None)
            except Exception:
                pass
            await q.message.reply_text(text, reply_markup=reply_markup)
            return
    except Exception as exc:
        logger.warning("Callback response failed: %s", exc)
    if q.message:
        await q.message.reply_text(text, reply_markup=reply_markup)

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data
    user_id = q.from_user.id
    if user_id == SUPER_ADMIN_ID:
        if data.startswith("ad_"):
            await handle_tenant_callback(q, context, ensure_super_tenant(), data)
            return
        if data.startswith("tenant_extend_"):
            _, _, tenant_id, days = data.split("_")
            with db() as conn:
                tenant = conn.execute("SELECT * FROM tenants WHERE id = ?", (int(tenant_id),)).fetchone()
                base = max(datetime.fromisoformat(tenant["access_until"]), now_dt())
                access_until = base + timedelta(days=int(days))
                conn.execute("UPDATE tenants SET access_until = ?, is_active = 1 WHERE id = ?", (access_until.isoformat(), int(tenant_id)))
            reschedule_all(context.application)
            await reply_to_callback(q, f"✅ Продлено до {fmt(access_until.isoformat())}")
        elif data.startswith("tenant_disable_"):
            tenant_id = int(data.split("_")[2])
            with db() as conn:
                conn.execute("UPDATE tenants SET is_active = 0 WHERE id = ?", (tenant_id,))
            reschedule_all(context.application)
            await reply_to_callback(q, "⛔ Арендатор отключен.")
        elif data.startswith("tenant_enable_"):
            tenant_id = int(data.split("_")[2])
            with db() as conn:
                conn.execute("UPDATE tenants SET is_active = 1 WHERE id = ?", (tenant_id,))
            reschedule_all(context.application)
            await reply_to_callback(q, "✅ Арендатор активирован.")
        return

    tenant = tenant_by_user(user_id)
    if not tenant_has_access(tenant):
        await reply_to_callback(q, "⛔ Доступ не активен.")
        return
    if data.startswith("ad_"):
        await handle_tenant_callback(q, context, tenant, data)
        return


def remove_job(ad_id):
    try:
        scheduler.remove_job(str(ad_id))
    except Exception:
        pass


def schedule_ad(app, ad_id):
    remove_job(ad_id)
    with db() as conn:
        ad = conn.execute(
            """
            SELECT ads.*, tenants.access_until, tenants.is_active tenant_active
            FROM ads JOIN tenants ON tenants.id = ads.tenant_id
            WHERE ads.id = ?
            """,
            (ad_id,),
        ).fetchone()
    if not ad or not ad["active"] or not ad["tenant_active"]:
        return
    start_at = datetime.fromisoformat(ad["start_at"])
    end_at = min(datetime.fromisoformat(ad["end_at"]), datetime.fromisoformat(ad["access_until"]))
    if end_at <= now_dt():
        return
    scheduler.add_job(
        post_ad,
        IntervalTrigger(minutes=ad["interval_minutes"], start_date=max(start_at, now_dt()), end_date=end_at, timezone=TIMEZONE),
        args=[app, ad_id],
        id=str(ad_id),
        replace_existing=True,
        misfire_grace_time=300,
    )


def reschedule_all(app):
    for job in scheduler.get_jobs():
        job.remove()
    with db() as conn:
        ad_ids = [r[0] for r in conn.execute("SELECT id FROM ads WHERE active = 1").fetchall()]
    for ad_id in ad_ids:
        schedule_ad(app, ad_id)


def should_notify_error(ad, error_text):
    if ad["last_error"] != error_text:
        return True
    if not ad["last_error_at"]:
        return True
    try:
        return now_dt() - datetime.fromisoformat(ad["last_error_at"]) > timedelta(minutes=30)
    except Exception:
        return True


async def send_ad_content(bot, ad):
    timeout_kwargs = {"connect_timeout": 20, "read_timeout": 90, "write_timeout": 90, "pool_timeout": 20}
    if ad["media_type"] == "text":
        await bot.send_message(ad["chat_id"], ad["text_html"] or ad["text"], parse_mode=ParseMode.HTML, **timeout_kwargs)
    elif ad["media_type"] == "photo":
        await bot.send_photo(ad["chat_id"], ad["file_id"], caption=ad["caption_html"] or ad["caption"], parse_mode=ParseMode.HTML, **timeout_kwargs)
    elif ad["media_type"] == "video":
        await bot.send_video(ad["chat_id"], ad["file_id"], caption=ad["caption_html"] or ad["caption"], parse_mode=ParseMode.HTML, **timeout_kwargs)
    elif ad["media_type"] == "animation":
        await bot.send_animation(ad["chat_id"], ad["file_id"], caption=ad["caption_html"] or ad["caption"], parse_mode=ParseMode.HTML, **timeout_kwargs)
    elif ad["media_type"] == "document":
        await bot.send_document(ad["chat_id"], ad["file_id"], caption=ad["caption_html"] or ad["caption"], parse_mode=ParseMode.HTML, **timeout_kwargs)
    elif ad["media_type"] == "album":
        files = json.loads(ad["file_id"])
        if len(files) == 1:
            await bot.send_photo(ad["chat_id"], files[0], caption=ad["caption_html"] or ad["caption"], parse_mode=ParseMode.HTML, **timeout_kwargs)
        else:
            media = []
            for index, file_id in enumerate(files):
                if index == 0:
                    media.append(InputMediaPhoto(file_id, caption=ad["caption_html"] or ad["caption"], parse_mode=ParseMode.HTML))
                else:
                    media.append(InputMediaPhoto(file_id))
            await bot.send_media_group(ad["chat_id"], media, **timeout_kwargs)


async def post_ad(app, ad_id):
    with db() as conn:
        ad = conn.execute(
            """
            SELECT ads.*, groups.chat_id, tenants.telegram_user_id, tenants.access_until, tenants.is_active tenant_active
            FROM ads
            JOIN groups ON groups.id = ads.group_id
            JOIN tenants ON tenants.id = ads.tenant_id
            WHERE ads.id = ?
            """,
            (ad_id,),
        ).fetchone()
    if not ad or not ad["active"] or not ad["tenant_active"] or datetime.fromisoformat(ad["access_until"]) < now_dt():
        with db() as conn:
            conn.execute("UPDATE ads SET active = 0 WHERE id = ?", (ad_id,))
        remove_job(ad_id)
        return
    try:
        await send_ad_content(app.bot, ad)
        with db() as conn:
            conn.execute("UPDATE ads SET published_count = published_count + 1 WHERE id = ?", (ad_id,))
            conn.execute("INSERT INTO publish_logs (ad_id, published_at, status) VALUES (?, ?, 'ok')", (ad_id, now_dt().isoformat()))
            conn.execute("UPDATE ads SET last_error = NULL, last_error_at = NULL WHERE id = ?", (ad_id,))
    except Exception as exc:
        logger.exception("Publish error")
        error_text = str(exc)
        notify = should_notify_error(ad, error_text)
        with db() as conn:
            conn.execute("INSERT INTO publish_logs (ad_id, published_at, status, error) VALUES (?, ?, 'error', ?)", (ad_id, now_dt().isoformat(), error_text))
            conn.execute("UPDATE ads SET last_error = ?, last_error_at = ? WHERE id = ?", (error_text, now_dt().isoformat(), ad_id))
        if notify:
            await app.bot.send_message(ad["telegram_user_id"], f"❌ Ошибка публикации объявления #{ad_id}:\n{error_text}")


async def post_init(app):
    init_db()
    scheduler.start()
    reschedule_all(app)
    logger.info("Bot started")


def main():
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .connect_timeout(20)
        .read_timeout(90)
        .write_timeout(90)
        .pool_timeout(20)
        .build()
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("register_group", register_group))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_message))
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

