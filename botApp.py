# app.py
import os
import re
import json
import sqlite3
import logging
import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, Router, F
from aiogram.filters import CommandStart
from aiogram.types import Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.types import URLInputFile

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger

import gspread
from google.oauth2.service_account import Credentials

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
PROJECT_RE = re.compile(r"(?i)^\s*(?:проек(?:t|т)\w*|prоekt|project)\s*$")

REMINDER_INTERVAL_DAYS = int(os.getenv("REMINDER_INTERVAL_DAYS", "2"))
REMINDER_MAX_ATTEMPTS = int(os.getenv("REMINDER_MAX_ATTEMPTS", "3"))

GSHEET_ID = os.getenv("GSHEET_ID", "GOOGLE_SHEET_ID")
GSHEET_WORKSHEET = os.getenv("GSHEET_WORKSHEET", "Leads")
GOOGLE_SERVICE_JSON = os.getenv("GOOGLE_SERVICE_JSON", "")  # путь к файлу, либо JSON строка

# -------------------- Логирование --------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
logger = logging.getLogger("rome_estate_bot")

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
            manager_contacted INTEGER DEFAULT 0
        )
    """)
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
            "last_interaction", "file_sent_at", "followup_attempts", "manager_contacted"]
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

def greeting_keyboard():
    kb = InlineKeyboardBuilder()
    kb.button(text="Подписаться на канал Rome Estate", url=CHANNEL_LINK)
    kb.button(text="Проверить подписку", callback_data="check_sub")
    return kb.as_markup()

def followup_keyboard():
    kb = InlineKeyboardBuilder()
    kb.button(text="Связаться с менеджером", url=MANAGER_CONTACT)
    return kb.as_markup()

@router.message(CommandStart())
async def on_start(message: Message, bot: Bot):
    upsert_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
    await gs_write_new_user(get_user(message.from_user.id))
    await message.answer(
        "Приветствуем в Rome Estate!\n\nМы приготовили для вас лучшие инвестиционные проекты на Пхукете.\n"
        "Чтобы продолжить, подпишитесь на наш канал 👇",
        reply_markup=greeting_keyboard()
    )

@router.callback_query(F.data == "check_sub")
async def on_check_sub(callback: CallbackQuery, bot: Bot):
    try:
        member = await bot.get_chat_member(chat_id=CHANNEL_ID, user_id=callback.from_user.id)
        status = getattr(member, "status", None)
        if status in {"creator", "administrator", "member"}:
            update_user_fields(callback.from_user.id, subscribed=1)
            await gs_update_by_chat_id(callback.from_user.id, {"subscribed": True})
            await callback.message.edit_text(
                "Отлично ✅ Вы в шаге от волшебной презентации!✨\n"
                "Теперь напишите слово «Проект», и получите подборку из 30 лучших инвестиционных проектов на Пхукете!💼"
            )
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

    await message.answer(
        "📂 Ваша подборка готова!\nЭто 30 лучших инвестиционных проектов на Пхукете.\n"
        "Уверены, что вы найдете то, что ищете! ✨"
    )
    await message.answer_document(URLInputFile(PDF_URL, filename="RomeEstate_30_Projects.pdf"))

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
    await message.answer(
        "Спасибо за ваш вопрос!\nЧтобы получить быстрый ответ — свяжитесь с менеджером 👇",
        reply_markup=followup_keyboard()
    )

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
            "Напоминаем о себе 👋\nУ нас для вас всегда открыты лучшие возможности на Пхукете.\n"
            "Хотите, свяжем вас напрямую с нашим менеджером?👩🏼‍💻",
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

    await dp.start_polling(Bot(BOT_TOKEN))

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass