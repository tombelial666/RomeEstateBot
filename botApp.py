# app.py
import os
import re
import json
import sqlite3
import logging
import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from aiohttp import web

from aiogram import Bot, Dispatcher, Router, F
from aiogram.filters import CommandStart
from aiogram.types import Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.types import URLInputFile, BufferedInputFile

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger

import gspread
from google.oauth2.service_account import Credentials
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

# -------------------- Загрузка окружения --------------------
def load_env_file(path: str = "environment.ini"):
    if not os.path.exists(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                # убираем инлайн-комментарии
                if "#" in line:
                    line = line.split("#", 1)[0].strip()
                if "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key and value and key not in os.environ:
                    os.environ[key] = value
    except Exception:
        # не критично для запуска, просто логируем позже если чего-то не хватает
        pass

# загрузим переменные до чтения через os.getenv
load_env_file()

# -------------------- Конфиг --------------------
TIMEZONE = os.getenv("TIMEZONE", "Europe/Amsterdam")
TZ = ZoneInfo(TIMEZONE)

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "0"))

# Канал: НУЖЕН NUMERIC chat_id (например -1001234567890), а не t.me/...
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "0"))
CHANNEL_LINK = os.getenv("CHANNEL_LINK", "https://t.me/rome_estate_channel")
MANAGER_CONTACT = os.getenv("MANAGER_CONTACT", "https://t.me/manager_telegram_or_site")

PDF_URL = os.getenv("PDF_URL", "https://drive.google.com/uc?id=DRIVE_FILE_ID&export=download")
# Разрешаем русское/латинское написание, а также 'proekt' с латинской/русской 'o'
PROJECT_RE = re.compile(r"(?i)^\s*(?:проек(?:t|т)\w*|pr[oо]ekt|project)\s*$")

REMINDER_INTERVAL_DAYS = int(os.getenv("REMINDER_INTERVAL_DAYS", "2"))
REMINDER_MAX_ATTEMPTS = int(os.getenv("REMINDER_MAX_ATTEMPTS", "3"))

GSHEET_ID = os.getenv("GSHEET_ID", "GOOGLE_SHEET_ID")
GSHEET_WORKSHEET = os.getenv("GSHEET_WORKSHEET", "Leads")
GOOGLE_SERVICE_JSON = os.getenv("GOOGLE_SERVICE_JSON", "")  # путь к файлу, либо JSON строка

# Webhook (опционально)
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")  # например, https://your.domain.com/telegram/webhook
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", "/telegram/webhook")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
WEBAPP_HOST = os.getenv("WEBAPP_HOST", "0.0.0.0")
WEBAPP_PORT = int(os.getenv("WEBAPP_PORT", "8080"))

# -------------------- Логирование --------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
logger = logging.getLogger("rome_estate_bot")

from templates import TEMPLATES

# -------------------- SQLite --------------------
DB_PATH = os.getenv("DB_PATH", "bot.db")

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            chat_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            subscribed INTEGER DEFAULT 0,
            last_message TEXT,
            last_interaction TEXT,
            file_sent_at TEXT,
            followup_attempts INTEGER DEFAULT 0,
            manager_contacted INTEGER DEFAULT 0,
            lang TEXT
        )
    """)
    # миграция: добавить колонку lang, если её нет
    try:
        cur = conn.execute("PRAGMA table_info(users)")
        cols = {row[1] for row in cur.fetchall()}
        if "lang" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN lang TEXT")
    except Exception:
        pass
    conn.commit()
    conn.close()

def upsert_user(chat_id: int, username: str | None, first_name: str | None):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        INSERT INTO users (chat_id, username, first_name, last_interaction)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(chat_id) DO UPDATE SET
          username=COALESCE(EXCLUDED.username, username),
          first_name=COALESCE(EXCLUDED.first_name, first_name),
          last_interaction=?
    """, (chat_id, username, first_name, datetime.now(TZ).isoformat(), datetime.now(TZ).isoformat()))
    conn.commit()
    conn.close()

def update_user_fields(chat_id: int, **fields):
    if not fields:
        return
    conn = sqlite3.connect(DB_PATH)
    cols = ", ".join([f"{k}=?" for k in fields.keys()])
    values = list(fields.values())
    values.append(chat_id)
    conn.execute(f"UPDATE users SET {cols} WHERE chat_id=?", values)
    conn.commit()
    conn.close()

def get_user(chat_id: int) -> dict | None:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute("SELECT * FROM users WHERE chat_id=?", (chat_id,))
    row = cur.fetchone()
    conn.close()
    if not row:
        return None
    keys = ["chat_id", "username", "first_name", "subscribed", "last_message",
            "last_interaction", "file_sent_at", "followup_attempts", "manager_contacted", "lang"]
    return dict(zip(keys, row))

# -------------------- Google Sheets --------------------
GSCOPE = ["https://www.googleapis.com/auth/spreadsheets"]

def _build_gspread_client():
    if not GOOGLE_SERVICE_JSON:
        raise RuntimeError("GOOGLE_SERVICE_JSON is empty")
    # Если указан путь к файлу и он существует — читаем файл
    if os.path.isfile(GOOGLE_SERVICE_JSON):
        creds = Credentials.from_service_account_file(GOOGLE_SERVICE_JSON, scopes=GSCOPE)
    else:
        # Если строка похожа на JSON — парсим; иначе бросаем понятную ошибку
        stripped = GOOGLE_SERVICE_JSON.strip()
        if stripped.startswith("{"):
            info = json.loads(stripped)
            creds = Credentials.from_service_account_info(info, scopes=GSCOPE)
        else:
            raise FileNotFoundError(f"Service account file not found: {GOOGLE_SERVICE_JSON}")
    gc = gspread.authorize(creds)
    return gc

async def gs_write_new_user(user: dict):
    def _task():
        try:
            gc = _build_gspread_client()
            sh = gc.open_by_key(GSHEET_ID).worksheet(GSHEET_WORKSHEET)
            sh.append_row([
                str(user["chat_id"]),
                user.get("username") or "",
                user.get("first_name") or "",
                datetime.now(TZ).isoformat(),
                str(bool(user.get("subscribed", 0))),
                user.get("last_message") or "",
                "",  # file_sent
                str(user.get("followup_attempts", 0)),
                str(bool(user.get("manager_contacted", 0))),
            ], value_input_option="USER_ENTERED")
        except Exception as e:
            logger.warning(f"Sheets write new user skipped: {e}")
    await asyncio.to_thread(_task)

async def gs_update_by_chat_id(chat_id: int, updates: dict):
    def _task():
        try:
            gc = _build_gspread_client()
            ws = gc.open_by_key(GSHEET_ID).worksheet(GSHEET_WORKSHEET)
            try:
                cell = ws.find(str(chat_id))
            except Exception:
                cell = None
            if not cell:
                ws.append_row([
                    str(chat_id),
                    "", "", datetime.now(TZ).isoformat(),
                    str(bool(updates.get("subscribed", 0))),
                    updates.get("last_message", ""),
                    "",
                    str(updates.get("followup_attempts", "")),
                    str(bool(updates.get("manager_contacted", 0))),
                ], value_input_option="USER_ENTERED")
                return
            row = cell.row
            header = [h.strip() for h in ws.row_values(1)]
            name_to_idx = {name: idx+1 for idx, name in enumerate(header)}
            cells_to_update = []
            for k, v in updates.items():
                if k in name_to_idx:
                    cells_to_update.append({
                        "range": gspread.utils.rowcol_to_a1(row, name_to_idx[k]),
                        "values": [[str(v)]],
                    })
            if cells_to_update:
                body = {"valueInputOption": "USER_ENTERED", "data": [{"range": c["range"], "values": c["values"]} for c in cells_to_update]}
                ws.spreadsheet.values_batch_update(body)
        except Exception as e:
            logger.warning(f"Sheets update skipped for {chat_id}: {e}")
    await asyncio.to_thread(_task)

# -------------------- Бот и маршруты --------------------
router = Router()
scheduler = AsyncIOScheduler(timezone=str(TZ))

def greeting_keyboard(lang: str = "ru"):
    kb = InlineKeyboardBuilder()
    btns = TEMPLATES.get(lang, TEMPLATES["ru"]) ["buttons"]
    kb.button(text=btns["subscribe"], url=CHANNEL_LINK)
    kb.button(text=btns["check_sub"], callback_data="check_sub")
    return kb.as_markup()

def followup_keyboard(lang: str = "ru"):
    kb = InlineKeyboardBuilder()
    btns = TEMPLATES.get(lang, TEMPLATES["ru"]) ["buttons"]
    kb.button(text=btns["contact_manager"], url=MANAGER_CONTACT)
    return kb.as_markup()

@router.message(CommandStart())
async def on_start(message: Message, bot: Bot):
    upsert_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
    await gs_write_new_user(get_user(message.from_user.id))
    kb = InlineKeyboardBuilder()
    kb.button(text=TEMPLATES["ru"]["lang_buttons"]["ru"], callback_data="lang:ru")
    kb.button(text=TEMPLATES["ru"]["lang_buttons"]["en"], callback_data="lang:en")
    kb.button(text=TEMPLATES["ru"]["lang_buttons"]["th"], callback_data="lang:th")
    await message.answer(TEMPLATES["ru"]["choose_lang"], reply_markup=kb.as_markup())

@router.callback_query(F.data.startswith("lang:"))
async def on_set_lang(callback: CallbackQuery):
    lang = callback.data.split(":",1)[1]
    if lang not in ("ru","en","th"):
        lang = "ru"
    # сохраним в last_message специальный маркер
    update_user_fields(callback.from_user.id, last_message=f"_lang:{lang}")
    tmpl = TEMPLATES[lang]
    await callback.message.edit_text(tmpl["greeting"], reply_markup=greeting_keyboard(lang))

@router.callback_query(F.data == "check_sub")
async def on_check_sub(callback: CallbackQuery, bot: Bot):
    try:
        lang = "ru"
        u = get_user(callback.from_user.id)
        if u and (u.get("last_message") or "").startswith("_lang:"):
            lang = u["last_message"].split(":",1)[1]
        await callback.message.answer(TEMPLATES[lang]["checking_subscription"])
        member = await bot.get_chat_member(chat_id=CHANNEL_ID, user_id=callback.from_user.id)
        status = getattr(member, "status", None)
        if status in {"creator", "administrator", "member"}:
            update_user_fields(callback.from_user.id, subscribed=1)
            await gs_update_by_chat_id(callback.from_user.id, {"subscribed": True})
            await callback.message.answer(TEMPLATES[lang]["subscribed_ok"])
        else:
            await callback.answer("Похоже, вы ещё не подписаны 😔", show_alert=True)
    except Exception as e:
        logger.exception("getChatMember error")
        await callback.answer("Не удалось проверить подписку, попробуйте ещё раз.", show_alert=True)

@router.message(F.text.regexp(PROJECT_RE))
async def on_project(message: Message, bot: Bot):
    # проверим подписку на всякий
    try:
        member = await bot.get_chat_member(chat_id=CHANNEL_ID, user_id=message.from_user.id)
        status = getattr(member, "status", None)
        if status not in {"creator", "administrator", "member"}:
            await message.answer(
                "Похоже, вы ещё не подписаны!😔\nНажмите кнопку ниже, чтобы подписаться и продолжить.",
                reply_markup=greeting_keyboard(),
            )
            return
    except Exception:
        # мягкий отказ
        await message.answer(
            "Не удалось проверить подписку. Попробуйте позже или нажмите «Проверить подписку».",
            reply_markup=greeting_keyboard(),
        )
        return

    lang = "ru"
    u = get_user(message.from_user.id)
    if u:
        if u.get("lang") in ("ru","en","th"):
            lang = u["lang"]
        elif (u.get("last_message") or "").startswith("_lang:"):
            lang = u["last_message"].split(":",1)[1]
    await message.answer(TEMPLATES[lang]["pdf_sent"])
    try:
        await message.answer_document(URLInputFile(PDF_URL, filename="RomeEstate_30_Projects.pdf"))
    except Exception as e:
        logger.exception("Send document via URL failed: %s", e)
        # Fallback: скачиваем и отправляем как байты
        try:
            import aiohttp
            async with aiohttp.ClientSession() as session:
                async with session.get(PDF_URL) as resp:
                    content = await resp.read()
                    if resp.status == 200 and content:
                        await message.answer_document(BufferedInputFile(content, filename="RomeEstate_30_Projects.pdf"))
                    else:
                        await message.answer(
                            "Не удалось загрузить PDF по ссылке. Свяжитесь с менеджером 👇",
                            reply_markup=followup_keyboard()
                        )
        except Exception:
            logger.exception("Fallback download+send failed")
            await message.answer(
                "Не удалось отправить PDF. Свяжитесь с менеджером 👇",
                reply_markup=followup_keyboard()
            )

    now_iso = datetime.now(TZ).isoformat()
    update_user_fields(
        message.from_user.id,
        last_message="project_requested",
        file_sent_at=now_iso,
        followup_attempts=0
    )
    await gs_update_by_chat_id(message.from_user.id, {
        "last_message": "project_requested",
        "file_sent": now_iso,
        "followup_attempts": 0
    })

    schedule_followup(message.from_user.id, initial=True)

@router.message()
async def on_any_message(message: Message):
    upsert_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
    update_user_fields(
        message.from_user.id,
        last_message=message.text or "",
        last_interaction=datetime.now(TZ).isoformat()
    )
    await gs_update_by_chat_id(message.from_user.id, {
        "last_message": message.text or "",
        "last_interaction": datetime.now(TZ).isoformat()
    })

    # Fallback/вопросы — отправим контакт менеджера
    lang = "ru"
    u = get_user(message.from_user.id)
    if u and (u.get("last_message") or "").startswith("_lang:"):
        lang = u["last_message"].split(":",1)[1]
    await message.answer(TEMPLATES[lang]["fallback_question"], reply_markup=followup_keyboard(lang))

# -------------------- Follow-up --------------------
def schedule_followup(chat_id: int, initial: bool = False):
    user = get_user(chat_id)
    if not user:
        return
    attempts = int(user.get("followup_attempts") or 0)
    if not initial and attempts >= REMINDER_MAX_ATTEMPTS:
        return

    if initial:
        start_from = datetime.now(TZ) + timedelta(days=REMINDER_INTERVAL_DAYS)
    else:
        start_from = datetime.now(TZ) + timedelta(days=REMINDER_INTERVAL_DAYS)

    scheduler.add_job(
        func=async_followup_job,
        trigger=DateTrigger(run_date=start_from),
        args=[chat_id],
        id=f"followup_{chat_id}_{attempts+1}",
        replace_existing=True,
        misfire_grace_time=3600
    )

async def async_followup_job(chat_id: int):
    user = get_user(chat_id)
    if not user:
        return
    attempts = int(user.get("followup_attempts") or 0)
    if attempts >= REMINDER_MAX_ATTEMPTS:
        return

    # условие: не было ответа с момента file_sent
    file_sent_at = user.get("file_sent_at")
    last_interaction = user.get("last_interaction")
    if not file_sent_at:
        return
    try:
        file_sent_dt = datetime.fromisoformat(file_sent_at)
        last_interaction_dt = datetime.fromisoformat(last_interaction) if last_interaction else None
    except Exception:
        return

    if last_interaction_dt and last_interaction_dt > file_sent_dt:
        return  # пользователь что-то писал после отправки файла

    # отправим follow-up
    try:
        bot = Bot(BOT_TOKEN)
        await bot.send_message(
            chat_id,
            TEMPLATES["followup"],
            reply_markup=followup_keyboard()
        )
        await bot.session.close()
    except Exception:
        logger.exception("Follow-up send failed")
        return

    attempts += 1
    update_user_fields(chat_id, followup_attempts=attempts)
    await gs_update_by_chat_id(chat_id, {"followup_attempts": attempts})
    if attempts < REMINDER_MAX_ATTEMPTS:
        schedule_followup(chat_id, initial=False)

# -------------------- Admin (MVP) --------------------
@router.message(F.text.startswith("/update_pdf"))
async def admin_update_pdf(message: Message):
    if message.from_user.id != ADMIN_CHAT_ID:
        return
    parts = message.text.strip().split(maxsplit=1)
    if len(parts) < 2:
        await message.reply("Использование: /update_pdf <url>")
        return
    global PDF_URL
    PDF_URL = parts[1].strip()
    await message.reply("PDF ссылка обновлена.")

@router.message(F.text.startswith("/force_followup"))
async def admin_force_followup(message: Message):
    if message.from_user.id != ADMIN_CHAT_ID:
        return
    parts = message.text.strip().split(maxsplit=1)
    if len(parts) < 2 or not parts[1].isdigit():
        await message.reply("Использование: /force_followup <chat_id>")
        return
    chat_id = int(parts[1])
    schedule_followup(chat_id, initial=False)
    await message.reply(f"Follow-up поставлен для {chat_id}")

# -------------------- Доп. админ-команды --------------------
@router.message(F.text.startswith("/export_leads"))
async def admin_export_leads(message: Message):
    if message.from_user.id != ADMIN_CHAT_ID:
        return
    # простой CSV-экспорт текущей таблицы users из SQLite
    import csv
    from io import StringIO
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute("SELECT chat_id, username, first_name, last_interaction, subscribed, last_message, file_sent_at, followup_attempts, manager_contacted FROM users")
    rows = cur.fetchall()
    conn.close()
    buf = StringIO()
    writer = csv.writer(buf)
    writer.writerow(["chat_id","username","first_name","last_interaction","subscribed","last_message","file_sent","followup_attempts","manager_contacted"])
    writer.writerows(rows)
    buf.seek(0)
    await message.answer_document(document=("leads.csv", buf.getvalue()))

@router.message(F.text.startswith("/manager_contacted"))
async def admin_manager_contacted(message: Message):
    if message.from_user.id != ADMIN_CHAT_ID:
        return
    parts = message.text.strip().split(maxsplit=2)
    if len(parts) < 2 or not parts[1].isdigit():
        await message.reply("Использование: /manager_contacted <chat_id> [on|off]")
        return
    chat_id = int(parts[1])
    state = True
    if len(parts) >= 3:
        state = parts[2].lower() == "on"
    update_user_fields(chat_id, manager_contacted=1 if state else 0)
    await gs_update_by_chat_id(chat_id, {"manager_contacted": state})
    await message.reply(f"manager_contacted={'on' if state else 'off'} для {chat_id}")

@router.message(F.text.startswith("/health"))
async def admin_health(message: Message):
    if message.from_user.id != ADMIN_CHAT_ID:
        return
    try:
        bot = Bot(BOT_TOKEN)
        me = await bot.get_me()
        await bot.session.close()
        await message.reply(f"OK: @{me.username}")
    except Exception as e:
        await message.reply(f"Health error: {e}")

@router.message(F.text.startswith("/chat_id"))
async def admin_chat_id(message: Message):
    await message.reply(f"Ваш chat_id: {message.chat.id}")

# Health-check раз в 60 минут
def schedule_healthcheck():
    scheduler.add_job(async_healthcheck, "interval", minutes=60, id="healthcheck", replace_existing=True)

async def async_healthcheck():
    try:
        bot = Bot(BOT_TOKEN)
        me = await bot.get_me()
        await bot.session.close()
        # Sheets быстрый ping
        await gs_update_by_chat_id(ADMIN_CHAT_ID, {"last_interaction": datetime.now(TZ).isoformat()})
        logger.info(f"Health OK: @{me.username}")
    except Exception as e:
        logger.exception("Health-check failed")
        if ADMIN_CHAT_ID:
            try:
                bot = Bot(BOT_TOKEN)
                await bot.send_message(ADMIN_CHAT_ID, f"Ошибка в боте: {e}")
                await bot.session.close()
            except Exception:
                pass

# -------------------- Restore follow-ups on start --------------------
def restore_followups():
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.execute("SELECT chat_id, file_sent_at, followup_attempts FROM users WHERE file_sent_at IS NOT NULL")
        rows = cur.fetchall()
        conn.close()
    except Exception:
        return
    now = datetime.now(TZ)
    for chat_id, file_sent_at, attempts in rows:
        try:
            if attempts is None:
                attempts = 0
            attempts = int(attempts)
            if attempts >= REMINDER_MAX_ATTEMPTS:
                continue
            sent_dt = datetime.fromisoformat(file_sent_at)
            next_dt = sent_dt + timedelta(days=REMINDER_INTERVAL_DAYS * (attempts + 1))
            run_date = now + timedelta(seconds=10) if next_dt <= now else next_dt
            scheduler.add_job(
                func=async_followup_job,
                trigger=DateTrigger(run_date=run_date),
                args=[int(chat_id)],
                id=f"followup_{chat_id}_{attempts+1}",
                replace_existing=True,
                misfire_grace_time=3600
            )
        except Exception:
            continue

# -------------------- Entry --------------------
async def main():
    if not BOT_TOKEN or not CHANNEL_ID or not GSHEET_ID:
        raise RuntimeError("Заполните BOT_TOKEN, CHANNEL_ID, GSHEET_ID и GOOGLE_SERVICE_JSON")

    init_db()
    dp = Dispatcher()
    dp.include_router(router)

    schedule_healthcheck()
    scheduler.start()
    restore_followups()

    bot = Bot(BOT_TOKEN)

    # long-polling по умолчанию
    if not WEBHOOK_URL:
        await dp.start_polling(bot)
        return

    # webhook-режим
    async def on_startup(app: web.Application):
        try:
            await bot.set_webhook(url=WEBHOOK_URL, secret_token=WEBHOOK_SECRET, drop_pending_updates=True)
            logger.info("Webhook set: %s", WEBHOOK_URL)
        except Exception as e:
            logger.exception("Failed to set webhook: %s", e)

    async def on_shutdown(app: web.Application):
        try:
            await bot.delete_webhook(drop_pending_updates=False)
        except Exception:
            pass

    app = web.Application()
    SimpleRequestHandler(dispatcher=dp, bot=bot, secret_token=WEBHOOK_SECRET).register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, on_startup=on_startup, on_shutdown=on_shutdown)
    logger.info("Starting webhook app on %s:%s %s", WEBAPP_HOST, WEBAPP_PORT, WEBHOOK_PATH)
    web.run_app(app, host=WEBAPP_HOST, port=WEBAPP_PORT)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass