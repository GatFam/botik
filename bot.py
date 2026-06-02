import os
import re
import json
import logging
import signal
import sys
import requests
from bs4 import BeautifulSoup
from datetime import datetime
from urllib.parse import urljoin
from dotenv import load_dotenv

load_dotenv()  # читаем .env файл

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)


# ============================================================
# НАСТРОЙКИ
# ============================================================
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
DATA_FILE = "data.json"
SITE_URL = "https://chpk.rchuv.ru/action/deyateljnostj/raspisanie-1"
SITE_BASE = "https://chpk.rchuv.ru"

# Заголовки — имитируем обычный браузер, иначе сайт блокирует (418)
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
    "Referer": "https://chpk.rchuv.ru/",
}

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    handlers=[
        logging.FileHandler("bot.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ============================================================
# ПАРСИНГ САЙТА
# ============================================================

def fetch_page(url: str) -> BeautifulSoup | None:
    """Скачивает страницу и возвращает BeautifulSoup объект"""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        resp.encoding = resp.apparent_encoding or "utf-8"
        return BeautifulSoup(resp.text, "html.parser")
    except Exception as e:
        logger.error(f"Ошибка загрузки {url}: {e}")
        return None

def parse_date_from_text(text: str) -> str | None:
    """Извлекает дату из строки в разных форматах и нормализует в дд.мм.гггг"""
    # Формат дд.мм.гггг
    m = re.search(r"\b(\d{2})\.(\d{2})\.(\d{4})\b", text)
    if m:
        return m.group(0)

    # Формат дд.мм.гг
    m = re.search(r"\b(\d{2})\.(\d{2})\.(\d{2})\b", text)
    if m:
        d, mo, y = m.group(1), m.group(2), m.group(3)
        year = "20" + y
        return f"{d}.{mo}.{year}"

    # Словесный формат: "5 июня 2025" / "5 июня"
    MONTHS = {
        "января": "01", "февраля": "02", "марта": "03", "апреля": "04",
        "мая": "05", "июня": "06", "июля": "07", "августа": "08",
        "сентября": "09", "октября": "10", "ноября": "11", "декабря": "12"
    }
    m = re.search(r"(\d{1,2})\s+(" + "|".join(MONTHS.keys()) + r")(?:\s+(\d{4}))?", text, re.IGNORECASE)
    if m:
        day = m.group(1).zfill(2)
        month = MONTHS[m.group(2).lower()]
        year = m.group(3) if m.group(3) else str(datetime.now().year)
        return f"{day}.{month}.{year}"

    return None

def get_site_files() -> list[dict]:
    from urllib.parse import unquote
    soup = fetch_page(SITE_URL)
    if not soup:
        return []

    results = []
    seen_urls = set()

    for a in soup.find_all("a", href=True):
        href = a["href"]
        link_text = a.get_text(strip=True)

        # Ищем файловые ссылки или ссылки с текстом скачать/pdf
        is_file = bool(re.search(r"\.(pdf|doc|docx)(\?|$)", href, re.IGNORECASE))
        is_download = bool(re.search(r"скачать|download|\.pdf", link_text, re.IGNORECASE))

        if not (is_file or is_download):
            continue

        # Абсолютный URL
        if href.startswith("http"):
            full_url = href
        else:
            full_url = urljoin(SITE_BASE, href)

        if full_url in seen_urls:
            continue
        seen_urls.add(full_url)

        # Имя файла из URL
        filename = unquote(os.path.basename(href.split("?")[0]))
        if not filename or "." not in filename:
            filename = "замены.pdf"

        # Ищем дату: сначала в имени файла (02062026.pdf -> 02.06.2026)
        date_str = None

        # Формат ДДММГГГГ в имени файла
        m = re.search(r"(\d{2})(\d{2})(\d{4})", filename)
        if m:
            d, mo, y = m.group(1), m.group(2), m.group(3)
            if 1 <= int(d) <= 31 and 1 <= int(mo) <= 12:
                date_str = f"{d}.{mo}.{y}"

        # Если не нашли — ищем в тексте родителей (несколько уровней вверх)
        if not date_str:
            node = a
            for _ in range(5):
                node = node.parent
                if node is None:
                    break
                node_text = node.get_text()
                date_str = parse_date_from_text(node_text)
                if date_str:
                    break

        # Если не нашли — ищем в href
        if not date_str:
            date_str = parse_date_from_text(href)

        # Заголовок — текст ближайшего родителя
        parent = a.find_parent(["p", "li", "div", "td", "span"])
        parent_text = parent.get_text(strip=True) if parent else ""
        title = parent_text[:100] if parent_text and len(parent_text) > 5 else filename

        fetched_at = datetime.now().strftime("%d.%m.%Y %H:%M")
        results.append({
            "url": full_url,
            "title": title,
            "filename": filename,
            "date": date_str,
            "fetched_at": fetched_at,
            "raw_title": parent_text
        })

    logger.info(f"Найдено файлов на сайте: {len(results)}")
    return results

def find_files_by_date(date_str: str) -> list[dict]:
    """Ищет файлы на сайте по дате (дд.мм.гггг)"""
    all_files = get_site_files()
    matched = [f for f in all_files if f.get("date") == date_str]
    return matched

# ============================================================
# РАБОТА С ДАННЫМИ
# ============================================================

def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {
        "subscribers": [],
        "admins": [ADMIN_ID],
        "schedules": {},
        "stats": {"sent": 0, "uploads": 0}
    }

def save_data(data):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def is_admin(user_id: int) -> bool:
    return user_id in load_data()["admins"]

# ============================================================
# КОМАНДЫ ПОЛЬЗОВАТЕЛЯ
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await update.message.reply_text(
        f"👋 Привет, {user.first_name}!\n\n"
        "📋 Я бот расписаний замен ЧПК.\n\n"
        "Команды:\n"
        "  /subscribe — подписаться на уведомления\n"
        "  /unsubscribe — отписаться\n"
        "  /schedule дд.мм.гггг — замены на дату с сайта\n"
        "  /today — замены на сегодня\n"
        "  /latest — последние загруженные файлы\n"
        "  /site — все файлы с сайта прямо сейчас\n"
        "  /help — помощь\n"
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 *Помощь*\n\n"
        "/subscribe — подписаться на рассылку\n"
        "/unsubscribe — отписаться\n"
        "/schedule `05.06.2025` — замены на дату с сайта\n"
        "/today — замены на сегодня\n"
        "/latest — последние файлы из базы бота\n"
        "/site — все файлы что сейчас есть на сайте\n\n"
        "*Если ты админ:*\n"
        "/admin — панель управления",
        parse_mode="Markdown"
    )

async def subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    data = load_data()
    if uid not in data["subscribers"]:
        data["subscribers"].append(uid)
        save_data(data)
        await update.message.reply_text("✅ Подписан на уведомления о новых заменах!")
    else:
        await update.message.reply_text("ℹ️ Ты уже подписан.")

async def unsubscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    data = load_data()
    if uid in data["subscribers"]:
        data["subscribers"].remove(uid)
        save_data(data)
        await update.message.reply_text("❌ Отписан от уведомлений.")
    else:
        await update.message.reply_text("ℹ️ Ты не был подписан.")

async def schedule_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ищет замены по дате на сайте: /schedule дд.мм.гггг"""
    if not context.args:
        await update.message.reply_text(
            "📅 Укажи дату: `/schedule дд.мм.гггг`\n"
            "Пример: `/schedule 05.06.2025`",
            parse_mode="Markdown"
        )
        return

    date_str = context.args[0].strip()
    try:
        datetime.strptime(date_str, "%d.%m.%Y")
    except ValueError:
        await update.message.reply_text("❗ Неверный формат. Используй: `дд.мм.гггг`", parse_mode="Markdown")
        return

    await _send_files_for_date(update, context, date_str)

async def today_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    date_str = datetime.now().strftime("%d.%m.%Y")
    await update.message.reply_text(f"🔍 Ищу замены на сегодня ({date_str})...")
    await _send_files_for_date(update, context, date_str)

async def _send_files_for_date(update: Update, context: ContextTypes.DEFAULT_TYPE, date_str: str):
    """Общая функция: ищет файлы на сайте по дате и отправляет пользователю"""
    msg = await update.message.reply_text(f"🔍 Ищу замены на {date_str} на сайте...")

    files = find_files_by_date(date_str)

    if not files:
        await msg.edit_text(
            f"📭 Замен на {date_str} не найдено на сайте.\n\n"
            f"🔗 Проверь сам: {SITE_URL}"
        )
        return

    await msg.edit_text(
        f"📂 Найдено файлов на {date_str}: *{len(files)}*",
        parse_mode="Markdown"
    )

    for i, f in enumerate(files, 1):
        name = f.get('filename') or f['title']
        fetched = f.get('fetched_at', '')
        caption = (
            f"📄 *Файл {i} из {len(files)}*\n"
            f"📅 Дата замен: {date_str}\n"
            f"📎 Имя файла: {name}\n"
            + (f"🕐 Найден на сайте: {fetched}\n" if fetched else "")
            + f"🔗 [Открыть на сайте]({f['url']})"
        )
        try:
            await update.message.reply_document(
                document=f["url"],
                caption=caption,
                parse_mode="Markdown"
            )
        except Exception as e:
            logger.error(f"Ошибка отправки файла {f['url']}: {e}")
            await update.message.reply_text(
                f"⚠️ Не удалось отправить файл {i}.\n"
                f"📎 Скачай вручную: {f['url']}"
            )

async def site_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает все файлы с сайта списком с кликабельными ссылками"""
    msg = await update.message.reply_text("🔍 Загружаю список файлов с сайта...")
    all_files = get_site_files()

    if not all_files:
        await msg.edit_text(f"📭 Файлов на сайте не найдено.\n🔗 {SITE_URL}")
        return

    by_date: dict[str, list] = {}
    no_date = []
    for f in all_files:
        d = f.get("date")
        if d:
            by_date.setdefault(d, []).append(f)
        else:
            no_date.append(f)

    text = f"📋 *Файлы на сайте* (всего: {len(all_files)}):\n\n"

    for date in sorted(by_date.keys(), key=lambda d: datetime.strptime(d, "%d.%m.%Y"), reverse=True):
        entries = by_date[date]
        text += f"📅 *{date}* — {len(entries)} файл(ов)\n"
        for i, f in enumerate(entries, 1):
            name = f.get("filename") or f["title"][:60]
            fetched = f.get("fetched_at", "")
            text += f"  {i}. [{name}]({f['url']})"
            if fetched:
                text += f" _({fetched})_"
            text += "\n"
        text += "\n"

    if no_date:
        text += f"📎 *Без даты* — {len(no_date)} файл(ов)\n"
        for f in no_date:
            name = f.get("filename") or f["title"][:60]
            fetched = f.get("fetched_at", "")
            text += f"  • [{name}]({f['url']})"
            if fetched:
                text += f" _({fetched})_"
            text += "\n"

    text += f"\n💡 Для поиска по дате: `/schedule дд.мм.гггг`"

    await msg.edit_text(text, parse_mode="Markdown", disable_web_page_preview=True)

async def latest_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Последние файлы из базы бота (загруженные вручную)"""
    data = load_data()
    if not data["schedules"]:
        await update.message.reply_text("📭 В базе бота файлов нет.\n\nИспользуй /site чтобы посмотреть файлы прямо с сайта.")
        return

    sorted_dates = sorted(data["schedules"].keys(),
                          key=lambda d: datetime.strptime(d, "%d.%m.%Y"), reverse=True)
    latest_date = sorted_dates[0]
    entries = data["schedules"][latest_date]

    await update.message.reply_text(
        f"📂 Последние замены — *{latest_date}* ({len(entries)} файл(ов)):",
        parse_mode="Markdown"
    )
    for i, entry in enumerate(entries, 1):
        caption = (
            f"📄 Файл {i} из {len(entries)}\n"
            f"📅 Дата: {latest_date}\n"
            f"📝 {entry.get('caption', 'Замены')}\n"
            f"🕐 Загружен: {entry.get('added_at', '—')}"
        )
        await update.message.reply_document(document=entry["file_id"], caption=caption)

# ============================================================
# ПАНЕЛЬ АДМИНА
# ============================================================

async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Нет доступа.")
        return

    data = load_data()
    keyboard = [
        [InlineKeyboardButton("🌐 Проверить сайт", callback_data="admin_check_site")],
        [InlineKeyboardButton("🔄 Проверить сейчас", callback_data="admin_monitor_now")],
        [InlineKeyboardButton("📣 Разослать с сайта", callback_data="admin_broadcast_site")],
        [InlineKeyboardButton("📤 Загрузить PDF вручную", callback_data="admin_upload")],
        [InlineKeyboardButton("📣 Разослать из базы", callback_data="admin_broadcast_menu")],
        [InlineKeyboardButton("📋 База бота", callback_data="admin_list")],
        [InlineKeyboardButton("👥 Подписчики", callback_data="admin_subscribers")],
        [InlineKeyboardButton("🗑 Удалить из базы", callback_data="admin_delete_menu")],
        [InlineKeyboardButton("➕ Добавить админа", callback_data="admin_add_admin")],
        [InlineKeyboardButton("📊 Статистика", callback_data="admin_stats")],
    ]
    await update.message.reply_text(
        f"🔧 *Панель администратора*\n\n"
        f"👥 Подписчиков: {len(data['subscribers'])}\n"
        f"📁 Дат в базе: {len(data['schedules'])}\n"
        f"📊 Отправлено: {data['stats']['sent']} файлов",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not is_admin(query.from_user.id):
        await query.edit_message_text("⛔ Нет доступа.")
        return

    data = load_data()
    action = query.data

    # --- Проверить сайт ---
    if action == "admin_check_site":
        await query.edit_message_text("🔍 Загружаю файлы с сайта...")
        all_files = get_site_files()
        if not all_files:
            await query.edit_message_text(
                f"📭 Файлов на сайте не найдено.\n🔗 {SITE_URL}"
            )
            return

        by_date: dict[str, list] = {}
        no_date = []
        for f in all_files:
            d = f.get("date")
            if d:
                by_date.setdefault(d, []).append(f)
            else:
                no_date.append(f)

        text = f"🌐 *Файлы на сайте* ({len(all_files)} шт.):\n\n"
        for date in sorted(by_date.keys(), key=lambda d: datetime.strptime(d, "%d.%m.%Y"), reverse=True):
            text += f"📅 *{date}* — {len(by_date[date])} файл(ов)\n"
        if no_date:
            text += f"📎 Без даты — {len(no_date)} шт.\n"

        await query.edit_message_text(
            text,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("◀️ Назад", callback_data="back_admin")
            ]])
        )

    # --- Разослать с сайта ---
    elif action == "admin_broadcast_site":
        await query.edit_message_text("🔍 Загружаю файлы с сайта...")
        all_files = get_site_files()
        if not all_files:
            await query.edit_message_text("📭 Файлов на сайте не найдено.")
            return

        by_date: dict[str, list] = {}
        for f in all_files:
            d = f.get("date")
            if d:
                by_date.setdefault(d, []).append(f)

        if not by_date:
            await query.edit_message_text("📭 Нет файлов с датами.")
            return

        keyboard = []
        for date in sorted(by_date.keys(), key=lambda d: datetime.strptime(d, "%d.%m.%Y"), reverse=True)[:10]:
            keyboard.append([InlineKeyboardButton(
                f"📅 {date} ({len(by_date[date])} файл.)",
                callback_data=f"bcast_site_{date}"
            )])
        keyboard.append([InlineKeyboardButton("◀️ Назад", callback_data="back_admin")])
        await query.edit_message_text(
            "📣 *Выбери дату для рассылки с сайта:*",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    elif action.startswith("bcast_site_"):
        date_str = action.replace("bcast_site_", "")
        files = find_files_by_date(date_str)
        subs = data["subscribers"]
        if not subs:
            await query.edit_message_text("👥 Нет подписчиков.")
            return
        if not files:
            await query.edit_message_text(f"📭 Файлов на {date_str} не нашлось.")
            return

        await query.edit_message_text(
            f"📣 Рассылаю замены на {date_str}\n"
            f"👥 Подписчиков: {len(subs)}, файлов: {len(files)}"
        )
        sent_ok = 0
        failed = 0
        for uid in subs:
            try:
                for i, f in enumerate(files, 1):
                    caption = (
                        f"🔔 *Новые замены!*\n"
                        f"📄 Файл {i} из {len(files)}\n"
                        f"📅 Дата: {date_str}\n"
                        f"📝 {f['title']}"
                    )
                    await context.bot.send_document(
                        chat_id=uid,
                        document=f["url"],
                        caption=caption,
                        parse_mode="Markdown"
                    )
                sent_ok += 1
                data["stats"]["sent"] += len(files)
            except Exception as e:
                logger.error(f"Ошибка отправки {uid}: {e}")
                failed += 1

        save_data(data)
        await context.bot.send_message(
            query.from_user.id,
            f"✅ Рассылка завершена!\n✔️ Доставлено: {sent_ok}\n❌ Ошибок: {failed}"
        )

    # --- Загрузить вручную ---
    elif action == "admin_upload":
        context.user_data["waiting_for"] = "pdf_upload"
        await query.edit_message_text(
            "📤 *Ручная загрузка*\n\n"
            "Отправь PDF-файл. В подписи к файлу укажи дату:\n"
            "Пример: `05.06.2025 Замены 1 смена`\n\n"
            "_(Если дату не указать — запишется сегодняшняя)_",
            parse_mode="Markdown"
        )

    elif action == "admin_list":
        if not data["schedules"]:
            await query.edit_message_text("📭 В базе бота файлов нет.")
            return
        sorted_dates = sorted(data["schedules"].keys(),
                               key=lambda d: datetime.strptime(d, "%d.%m.%Y"), reverse=True)
        text = "📋 *База бота:*\n\n"
        for date in sorted_dates[:15]:
            text += f"📅 `{date}` — {len(data['schedules'][date])} файл(ов)\n"
        await query.edit_message_text(text, parse_mode="Markdown")

    elif action == "admin_subscribers":
        subs = data["subscribers"]
        await query.edit_message_text(
            f"👥 *Подписчики:* {len(subs)}\n\n"
            + ("\n".join([f"• `{uid}`" for uid in subs[:30]]) if subs else "Никого нет"),
            parse_mode="Markdown"
        )

    elif action == "admin_broadcast_menu":
        if not data["schedules"]:
            await query.edit_message_text("📭 В базе бота файлов нет.")
            return
        sorted_dates = sorted(data["schedules"].keys(),
                               key=lambda d: datetime.strptime(d, "%d.%m.%Y"), reverse=True)
        keyboard = []
        for date in sorted_dates[:10]:
            keyboard.append([InlineKeyboardButton(
                f"📅 {date} ({len(data['schedules'][date])} файл.)",
                callback_data=f"broadcast_{date}"
            )])
        keyboard.append([InlineKeyboardButton("◀️ Назад", callback_data="back_admin")])
        await query.edit_message_text(
            "📣 *Рассылка из базы бота:*",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    elif action.startswith("broadcast_"):
        date_str = action.replace("broadcast_", "")
        entries = data["schedules"].get(date_str, [])
        subs = data["subscribers"]
        if not subs:
            await query.edit_message_text("👥 Нет подписчиков.")
            return

        await query.edit_message_text(f"📣 Рассылаю на {date_str} для {len(subs)} подписчиков...")
        sent_ok = 0
        failed = 0
        for uid in subs:
            try:
                for i, entry in enumerate(entries, 1):
                    caption = (
                        f"🔔 *Новые замены!*\n"
                        f"📄 Файл {i} из {len(entries)}\n"
                        f"📅 Дата: {date_str}\n"
                        f"📝 {entry.get('caption', 'Замены')}"
                    )
                    await context.bot.send_document(
                        chat_id=uid,
                        document=entry["file_id"],
                        caption=caption,
                        parse_mode="Markdown"
                    )
                sent_ok += 1
                data["stats"]["sent"] += len(entries)
            except Exception as e:
                logger.error(f"Ошибка {uid}: {e}")
                failed += 1
        save_data(data)
        await context.bot.send_message(
            query.from_user.id,
            f"✅ Готово! Доставлено: {sent_ok}, ошибок: {failed}"
        )

    elif action == "admin_delete_menu":
        if not data["schedules"]:
            await query.edit_message_text("📭 Нет файлов.")
            return
        sorted_dates = sorted(data["schedules"].keys(),
                               key=lambda d: datetime.strptime(d, "%d.%m.%Y"), reverse=True)
        keyboard = [[InlineKeyboardButton(f"🗑 {d}", callback_data=f"del_{d}")] for d in sorted_dates[:10]]
        keyboard.append([InlineKeyboardButton("◀️ Назад", callback_data="back_admin")])
        await query.edit_message_text(
            "🗑 *Удалить дату из базы:*",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    elif action.startswith("del_"):
        date_str = action.replace("del_", "")
        if date_str in data["schedules"]:
            del data["schedules"][date_str]
            save_data(data)
            await query.edit_message_text(f"✅ Дата {date_str} удалена.")
        else:
            await query.edit_message_text("❗ Дата не найдена.")

    elif action == "admin_add_admin":
        context.user_data["waiting_for"] = "new_admin_id"
        await query.edit_message_text(
            "➕ Отправь Telegram ID нового администратора (только цифры).",
            parse_mode="Markdown"
        )

    elif action == "admin_stats":
        text = (
            f"📊 *Статистика*\n\n"
            f"👥 Подписчиков: {len(data['subscribers'])}\n"
            f"👮 Администраторов: {len(data['admins'])}\n"
            f"📁 Дат в базе: {len(data['schedules'])}\n"
            f"📣 Отправлено: {data['stats']['sent']}\n"
            f"📤 Загружено вручную: {data['stats']['uploads']}\n"
            f"🌐 Сайт: {SITE_URL}"
        )
        await query.edit_message_text(text, parse_mode="Markdown")

    elif action == "admin_monitor_now":
        await query.edit_message_text("🔄 Запускаю проверку сайта вручную...")
        known_urls = get_known_urls()
        all_files = get_site_files()
        if not all_files:
            await context.bot.send_message(query.from_user.id, "❌ Сайт недоступен или файлов нет.")
            return
        new_files = [f for f in all_files if f["url"] not in known_urls]
        if not new_files:
            await context.bot.send_message(
                query.from_user.id,
                f"✅ Проверил сайт — новых файлов нет.\nВсего файлов на сайте: {len(all_files)}"
            )
        else:
            await context.bot.send_message(
                query.from_user.id,
                f"🔔 Найдено {len(new_files)} новых файлов! Рассылаю подписчикам..."
            )
            await monitor_site(context)

    elif action == "back_admin":
        keyboard = [
            [InlineKeyboardButton("🌐 Проверить сайт", callback_data="admin_check_site")],
            [InlineKeyboardButton("🔄 Проверить сейчас", callback_data="admin_monitor_now")],
            [InlineKeyboardButton("📣 Разослать с сайта", callback_data="admin_broadcast_site")],
            [InlineKeyboardButton("📤 Загрузить PDF вручную", callback_data="admin_upload")],
            [InlineKeyboardButton("📣 Разослать из базы", callback_data="admin_broadcast_menu")],
            [InlineKeyboardButton("📋 База бота", callback_data="admin_list")],
            [InlineKeyboardButton("👥 Подписчики", callback_data="admin_subscribers")],
            [InlineKeyboardButton("🗑 Удалить из базы", callback_data="admin_delete_menu")],
            [InlineKeyboardButton("➕ Добавить админа", callback_data="admin_add_admin")],
            [InlineKeyboardButton("📊 Статистика", callback_data="admin_stats")],
        ]
        await query.edit_message_text(
            "🔧 *Панель администратора*",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

# ============================================================
# ОБРАБОТКА ВХОДЯЩИХ СООБЩЕНИЙ
# ============================================================

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("⛔ Загружать файлы могут только администраторы.")
        return

    doc = update.message.document
    if not doc.file_name.lower().endswith(".pdf"):
        await update.message.reply_text("❗ Только PDF-файлы.")
        return

    file_id = doc.file_id
    caption_raw = update.message.caption or ""

    date_match = re.search(r"\b(\d{2}\.\d{2}\.\d{4})\b", caption_raw)
    date_str = date_match.group(1) if date_match else datetime.now().strftime("%d.%m.%Y")
    caption_clean = caption_raw.strip() or f"Замены от {date_str}"

    data = load_data()
    data["schedules"].setdefault(date_str, []).append({
        "file_id": file_id,
        "caption": caption_clean,
        "filename": doc.file_name,
        "added_at": datetime.now().strftime("%d.%m.%Y %H:%M"),
        "added_by": user_id
    })
    data["stats"]["uploads"] += 1
    save_data(data)

    count = len(data["schedules"][date_str])
    await update.message.reply_text(
        f"✅ *Файл сохранён в базу!*\n\n"
        f"📅 Дата: {date_str}\n"
        f"📄 Файл: {doc.file_name}\n"
        f"📂 Всего на эту дату: {count}\n\n"
        f"Используй /admin → Разослать из базы.",
        parse_mode="Markdown"
    )

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    waiting = context.user_data.get("waiting_for")

    if waiting == "new_admin_id" and is_admin(user_id):
        text = update.message.text.strip()
        if text.isdigit():
            new_id = int(text)
            data = load_data()
            if new_id not in data["admins"]:
                data["admins"].append(new_id)
                save_data(data)
                await update.message.reply_text(f"✅ Администратор `{new_id}` добавлен.", parse_mode="Markdown")
            else:
                await update.message.reply_text("ℹ️ Уже администратор.")
        else:
            await update.message.reply_text("❗ Нужны только цифры.")
        context.user_data.pop("waiting_for", None)
    else:
        await update.message.reply_text("Напиши /help для справки.")


# ============================================================
# МОНИТОРИНГ САЙТА
# ============================================================

MONITOR_INTERVAL = 30 * 60  # 30 минут в секундах

def get_known_urls() -> set:
    """Возвращает множество URL файлов, которые уже были замечены"""
    data = load_data()
    return set(data.get("known_urls", []))

def save_known_urls(urls: set):
    data = load_data()
    data["known_urls"] = list(urls)
    save_data(data)

async def monitor_site(context):
    """Проверяет сайт на новые файлы и уведомляет подписчиков"""
    logger.info("Мониторинг: проверяю сайт...")
    data = load_data()
    subs = data.get("subscribers", [])
    admins = data.get("admins", [ADMIN_ID])

    known_urls = get_known_urls()
    all_files = get_site_files()

    if not all_files:
        logger.info("Мониторинг: файлов не найдено или сайт недоступен")
        return

    # Находим новые файлы — те что ещё не видели
    new_files = [f for f in all_files if f["url"] not in known_urls]

    if not new_files:
        logger.info("Мониторинг: новых файлов нет")
        return

    logger.info(f"Мониторинг: найдено {len(new_files)} новых файлов!")

    # Запоминаем все URL чтобы не слать повторно
    all_urls = known_urls | {f["url"] for f in all_files}
    save_known_urls(all_urls)

    # Группируем новые файлы по датам
    by_date: dict[str, list] = {}
    no_date = []
    for f in new_files:
        d = f.get("date")
        if d:
            by_date.setdefault(d, []).append(f)
        else:
            no_date.append(f)

    # Уведомляем подписчиков
    notify_ids = list(set(subs + admins))
    for uid in notify_ids:
        try:
            is_adm = uid in admins
            header = (
                "🔔 *Новые замены на сайте ЧПК!*\n\n"
                if not is_adm
                else "🔔 *[Мониторинг] Новые файлы на сайте!*\n\n"
            )
            # Сначала текстовое уведомление
            text = header
            for date in sorted(by_date.keys(), key=lambda d: datetime.strptime(d, "%d.%m.%Y"), reverse=True):
                entries = by_date[date]
                text += f"📅 *{date}* — {len(entries)} файл(ов)\n"
                for i, f in enumerate(entries, 1):
                    name = f.get("filename") or f["title"][:50]
                    text += f"  {i}. {name}\n"
                text += "\n"
            if no_date:
                text += f"📎 Без даты — {len(no_date)} файл(ов)\n"

            await context.bot.send_message(uid, text, parse_mode="Markdown")

            # Затем сами файлы
            for i, f in enumerate(new_files, 1):
                name = f.get("filename") or f["title"]
                fetched = f.get("fetched_at", "")
                caption = (
                    f"📄 Файл {i} из {len(new_files)}\n"
                    f"📅 Дата замен: {f.get('date', '—')}\n"
                    f"📎 Имя файла: {name}\n"
                    + (f"🕐 Найден: {fetched}\n" if fetched else "")
                    + f"🔗 {f['url']}"
                )
                await context.bot.send_document(
                    chat_id=uid,
                    document=f["url"],
                    caption=caption
                )
                data["stats"]["sent"] += 1

        except Exception as e:
            logger.error(f"Мониторинг: ошибка отправки {uid}: {e}")

    save_data(data)
    logger.info(f"Мониторинг: уведомления разосланы {len(notify_ids)} получателям")

# ============================================================
# ЗАПУСК
# ============================================================

def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("subscribe", subscribe))
    app.add_handler(CommandHandler("unsubscribe", unsubscribe))
    app.add_handler(CommandHandler("schedule", schedule_cmd))
    app.add_handler(CommandHandler("today", today_cmd))
    app.add_handler(CommandHandler("latest", latest_cmd))
    app.add_handler(CommandHandler("site", site_cmd))
    app.add_handler(CommandHandler("admin", admin_panel))
    app.add_handler(CallbackQueryHandler(admin_callback))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    # Запускаем мониторинг сайта каждые 30 минут
    app.job_queue.run_repeating(
        monitor_site,
        interval=MONITOR_INTERVAL,
        first=60  # первая проверка через 1 минуту после запуска
    )

    logger.info("Бот запущен! Мониторинг сайта каждые 30 минут.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
