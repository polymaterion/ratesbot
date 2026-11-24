import asyncio
import logging
import os
import sqlite3
import xml.etree.ElementTree as ET
from datetime import time
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests
from dotenv import load_dotenv
from telegram import Update
from telegram.error import Forbidden, TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    Defaults,
)

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
DAILY_TIME = os.getenv("DAILY_TIME", "09:00").strip()
TIMEZONE_NAME = os.getenv("TIMEZONE", "Europe/Moscow").strip()
DB_PATH = Path(os.getenv("DB_PATH", str(Path(__file__).with_name("subscribers.sqlite3"))))
CBR_URL = "https://www.cbr.ru/scripts/XML_daily.asp"

try:
    ADMIN_IDS = {
        int(item.strip())
        for item in os.getenv("ADMIN_IDS", "").split(",")
        if item.strip()
    }
except ValueError as exc:
    raise RuntimeError("ADMIN_IDS должен содержать Telegram ID через запятую") from exc

try:
    INITIAL_USD_TMT_RATE = Decimal(
        os.getenv("INITIAL_USD_TMT_RATE", "3.5").replace(",", ".")
    )
except InvalidOperation as exc:
    raise RuntimeError("INITIAL_USD_TMT_RATE должен быть числом, например 3.5") from exc

if not BOT_TOKEN:
    raise RuntimeError("Укажите BOT_TOKEN в файле .env")
if not INITIAL_USD_TMT_RATE.is_finite() or INITIAL_USD_TMT_RATE <= 0:
    raise RuntimeError("INITIAL_USD_TMT_RATE должен быть числом больше нуля")

try:
    TIMEZONE = ZoneInfo(TIMEZONE_NAME)
except ZoneInfoNotFoundError as exc:
    raise RuntimeError(f"Неизвестный часовой пояс: {TIMEZONE_NAME}") from exc

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


def init_db() -> None:
    """Создаёт таблицы подписок и настроек, если их ещё нет."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS subscribers (chat_id INTEGER PRIMARY KEY)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
            ("usd_tmt_rate", str(INITIAL_USD_TMT_RATE)),
        )


def get_tmt_rate() -> Decimal:
    """Возвращает количество TMT за 1 USD."""
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = ?", ("usd_tmt_rate",)
        ).fetchone()
    return Decimal(row[0]) if row else INITIAL_USD_TMT_RATE


def set_tmt_rate(rate: Decimal) -> None:
    """Сохраняет количество TMT за 1 USD."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            ("usd_tmt_rate", str(rate)),
        )


def add_subscriber(chat_id: int) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO subscribers (chat_id) VALUES (?)", (chat_id,)
        )


def remove_subscriber(chat_id: int) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM subscribers WHERE chat_id = ?", (chat_id,))


def get_subscribers() -> list[int]:
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute("SELECT chat_id FROM subscribers").fetchall()
    return [int(row[0]) for row in rows]


def format_number(value: Decimal, places: int = 4) -> str:
    return f"{value:,.{places}f}".replace(",", " ").replace(".", ",")


def get_rates_message() -> str:
    """Получает USD/RUB у ЦБ РФ и рассчитывает USD/TMT и TMT/RUB."""
    response = requests.get(
        CBR_URL,
        headers={"User-Agent": "CurrencyTelegramBot/1.0"},
        timeout=20,
    )
    response.raise_for_status()
    root = ET.fromstring(response.content)
    usd_rub = None

    for currency in root.findall("Valute"):
        if currency.findtext("CharCode") == "USD":
            nominal = Decimal((currency.findtext("Nominal") or "1").replace(",", "."))
            value = Decimal((currency.findtext("Value") or "").replace(",", "."))
            if nominal == 0:
                raise ValueError("ЦБ РФ вернул нулевой номинал USD")
            usd_rub = value / nominal
            break

    if usd_rub is None:
        raise ValueError("В ответе ЦБ РФ не найден курс USD")

    usd_tmt = get_tmt_rate()
    tmt_rub = usd_rub / usd_tmt
    rate_date = root.attrib.get("Date", "")
    date_line = f"на {rate_date}" if rate_date else "по последним данным"

    return (
        f"Курсы валют {date_line}\n\n"
        f"1 USD = {format_number(usd_rub, 2)} RUB\n"
        f"1 USD = {format_number(usd_tmt, 4)} TMT\n"
        f"1 TMT = {format_number(tmt_rub, 4)} RUB"
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat is None or update.message is None:
        return
    add_subscriber(update.effective_chat.id)
    await update.message.reply_text(
        "Подписка на ежедневные курсы включена.\n\n"
        "Команды:\n"
        "/rates — получить курсы сейчас\n"
        "/stop — отключить ежедневную рассылку\n"
        "/help — помощь"
    )


async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat is None or update.message is None:
        return
    remove_subscriber(update.effective_chat.id)
    await update.message.reply_text("Ежедневная рассылка отключена.")


async def rates(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return
    try:
        message = await asyncio.to_thread(get_rates_message)
    except Exception:
        logger.exception("Не удалось получить курсы валют")
        await update.message.reply_text(
            "Не удалось получить курс Банка России. Попробуйте позднее."
        )
        return
    await update.message.reply_text(message)


async def tmt_rate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Администраторская команда: /tmt_rate 18 означает 1 USD = 18 TMT."""
    if update.message is None or update.effective_user is None:
        return
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("Команда доступна только администратору.")
        return
    if len(context.args) != 1:
        await update.message.reply_text(
            "Использование: /tmt_rate 18\n"
            f"Текущий курс: 1 USD = {format_number(get_tmt_rate(), 4)} TMT"
        )
        return
    try:
        new_rate = Decimal(context.args[0].replace(",", "."))
        if not new_rate.is_finite() or new_rate <= 0:
            raise InvalidOperation
    except (InvalidOperation, ValueError):
        await update.message.reply_text(
            "Укажите положительное число. Пример: /tmt_rate 18"
        )
        return

    set_tmt_rate(new_rate)
    await update.message.reply_text(
        "Курс сохранён.\n"
        f"1 USD = {format_number(new_rate, 4)} TMT\n"
        "Курс TMT/RUB будет пересчитан автоматически."
    )
    logger.info(
        "Администратор %s изменил USD/TMT на %s", update.effective_user.id, new_rate
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return
    help_text = (
        "/start — подписаться на ежедневную рассылку\n"
        "/rates — получить курсы сейчас\n"
        "/stop — отписаться от рассылки"
    )
    if update.effective_user and update.effective_user.id in ADMIN_IDS:
        help_text += "\n/tmt_rate 18 — установить курс: 1 USD = 18 TMT"
    await update.message.reply_text(help_text)


async def daily_broadcast(context: ContextTypes.DEFAULT_TYPE) -> None:
    subscribers = get_subscribers()
    if not subscribers:
        logger.info("Подписчиков нет — ежедневная отправка пропущена")
        return
    try:
        message = await asyncio.to_thread(get_rates_message)
    except Exception:
        logger.exception("Ошибка при получении курсов для ежедневной рассылки")
        message = (
            "Не удалось получить курс Банка России. "
            "Попробуйте запросить его позднее командой /rates."
        )
    for chat_id in subscribers:
        try:
            await context.bot.send_message(chat_id=chat_id, text=message)
        except Forbidden:
            remove_subscriber(chat_id)
        except TelegramError:
            logger.exception("Не удалось отправить сообщение в чат %s", chat_id)


def parse_daily_time() -> time:
    try:
        hour_text, minute_text = DAILY_TIME.split(":", maxsplit=1)
        hour, minute = int(hour_text), int(minute_text)
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError
        return time(hour=hour, minute=minute, tzinfo=TIMEZONE)
    except ValueError as exc:
        raise RuntimeError(
            "DAILY_TIME задаётся в формате HH:MM, например 09:00"
        ) from exc


def main() -> None:
    init_db()
    if not ADMIN_IDS:
        logger.warning("ADMIN_IDS не задан: команда /tmt_rate будет недоступна")

    application: Application = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .defaults(Defaults(tzinfo=TIMEZONE))
        .build()
    )
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("rates", rates))
    application.add_handler(CommandHandler("tmt_rate", tmt_rate))
    application.add_handler(CommandHandler("stop", stop))
    application.add_handler(CommandHandler("help", help_command))

    if application.job_queue is None:
        raise RuntimeError(
            'Планировщик не установлен. Установите зависимости: '
            'pip install "python-telegram-bot[job-queue]"'
        )
    application.job_queue.run_daily(
        daily_broadcast,
        time=parse_daily_time(),
        name="daily_currency_rates",
    )
    logger.info(
        "Бот запущен. Ежедневная отправка: %s (%s)", DAILY_TIME, TIMEZONE_NAME
    )
    application.run_polling()


if __name__ == "__main__":
    main()
