import os
import re
import json
import sqlite3
import logging
from datetime import datetime, timedelta
from anthropic import Anthropic

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, MessageHandler, CommandHandler,
    CallbackQueryHandler, ContextTypes, filters
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ["BOT_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]

client = Anthropic(api_key=ANTHROPIC_API_KEY)
DB_PATH = "vocab.db"

MAX_WORDS_IN_PHRASE = 5

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS words (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            word TEXT NOT NULL,
            translation TEXT,
            added_at TEXT NOT NULL,
            UNIQUE(chat_id, word)
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS chat_settings (
            chat_id INTEGER PRIMARY KEY,
            quiet INTEGER NOT NULL DEFAULT 0
        )
    """)
    conn.commit()
    conn.close()

def get_quiet(chat_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT quiet FROM chat_settings WHERE chat_id=?", (chat_id,))
    row = c.fetchone()
    conn.close()
    return bool(row[0]) if row else False

def set_quiet(chat_id, value):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "INSERT INTO chat_settings (chat_id, quiet) VALUES (?, ?) "
        "ON CONFLICT(chat_id) DO UPDATE SET quiet=excluded.quiet",
        (chat_id, 1 if value else 0)
    )
    conn.commit()
    conn.close()

def save_word(chat_id, word, translation):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    now = datetime.utcnow().isoformat()
    try:
        c.execute(
            "INSERT OR IGNORE INTO words (chat_id, word, translation, added_at) VALUES (?, ?, ?, ?)",
            (chat_id, word.lower().strip(), translation, now)
        )
        conn.commit()
        inserted = c.rowcount > 0
    except Exception as e:
        logger.error(f"save_word error: {e}")
        inserted = False
    conn.close()
    return inserted

def get_words(chat_id, since):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "SELECT word, translation, added_at FROM words WHERE chat_id=? AND added_at>=? ORDER BY added_at DESC",
        (chat_id, since.isoformat())
    )
    rows = c.fetchall()
    conn.close()
    return rows

def analyze_word(word):
    """
    Отправляет слово/фразу в Claude.
    Возвращает dict: {"corrected": str, "translation": str}
    - corrected: исправленный вариант (с учётом опечаток), в нижнем регистре
    - translation: расширенный перевод с несколькими значениями
    """
    try:
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=400,
            messages=[{
                "role": "user",
                "content": (
                    "Ты — помощник для изучения английского языка. "
                    "Пользователь прислал английское слово или короткую фразу (до 5 слов). "
                    "Сделай две вещи:\n"
                    "1) Исправь опечатки, если они есть. Верни корректное написание в нижнем регистре "
                    "(имена собственные — с заглавной). Если ошибок нет — верни как есть.\n"
                    "2) Дай расширенный перевод на русский с несколькими основными значениями, "
                    "если они есть. Формат перевода: значения через запятую или точку с запятой "
                    "для разных смыслов. Коротко, без примеров и без пояснений. "
                    "Максимум 200 символов.\n\n"
                    "Ответь СТРОГО в формате JSON без markdown и без пояснений:\n"
                    '{"corrected": "...", "translation": "..."}\n\n'
                    f"Ввод: {word}"
                )
            }]
        )
        raw = response.content[0].text.strip()
        # На случай, если модель обернула JSON в ```json ... ```
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.MULTILINE).strip()
        data = json.loads(raw)
        corrected = str(data.get("corrected", word)).strip()
        translation = str(data.get("translation", "—")).strip()
        if not corrected:
            corrected = word
        if not translation:
            translation = "—"
        return {"corrected": corrected, "translation": translation}
    except Exception as e:
        logger.error(f"analyze_word error: {e}")
        return {"corrected": word, "translation": "—"}

def is_english_phrase(text):
    """
    Принимает английские слова и фразы до 5 слов.
    Допускает буквы, дефис, апостроф и пробелы между словами.
    """
    text = text.strip()
    if not text:
        return False
    if not re.fullmatch(r"[a-zA-Z\-' ]{2,60}", text):
        return False
    words = [w for w in text.split() if w]
    if len(words) == 0 or len(words) > MAX_WORDS_IN_PHRASE:
        return False
    # каждая часть должна содержать хотя бы одну букву
    for w in words:
        if not re.search(r"[a-zA-Z]", w):
            return False
    return True

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not msg.text:
        return
    text = msg.text.strip()
    chat_id = msg.chat_id
    # Разделители между разными словами/фразами — запятая, точка с запятой, перевод строки.
    # Пробел НЕ является разделителем, чтобы поддержать фразы типа "driving license".
    candidates = re.split(r"[,\n;]+", text)
    new_words = []
    corrections = []  # (оригинал, исправленный) — показать пользователю
    skipped = []
    for candidate in candidates:
        phrase = candidate.strip()
        if not phrase:
            continue
        if is_english_phrase(phrase):
            result = analyze_word(phrase)
            corrected = result["corrected"].strip()
            translation = result["translation"]
            inserted = save_word(chat_id, corrected, translation)
            if inserted:
                new_words.append((corrected, translation))
            if corrected.lower() != phrase.lower():
                corrections.append((phrase, corrected))
        else:
            skipped.append(phrase)

    # В тихом режиме всё обработали и сохранили, но не отвечаем.
    if get_quiet(chat_id):
        return

    if new_words or corrections:
        lines = []
        if corrections:
            lines.append("Исправлены опечатки:")
            for orig, corr in corrections:
                lines.append(f"  {orig} → {corr}")
            lines.append("")
        if new_words:
            lines.append("Добавлено в словарик:")
            for w, t in new_words:
                lines.append(f"  {w} — {t}")
        await msg.reply_text("\n".join(lines))

async def vocab_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("За сегодня", callback_data="vocab_today")],
        [InlineKeyboardButton("За эту неделю", callback_data="vocab_week")],
        [InlineKeyboardButton("За этот месяц", callback_data="vocab_month")],
        [InlineKeyboardButton("За всё время", callback_data="vocab_all")],
    ]
    await update.message.reply_text("За какой период показать словарик?",
                                    reply_markup=InlineKeyboardMarkup(keyboard))

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    now = datetime.utcnow()
    label_map = {
        "vocab_today": ("сегодня", now.replace(hour=0, minute=0, second=0)),
        "vocab_week":  ("эту неделю", now - timedelta(days=now.weekday())),
        "vocab_month": ("этот месяц", now.replace(day=1, hour=0, minute=0, second=0)),
        "vocab_all":   ("всё время", datetime(2000, 1, 1)),
    }
    label, since = label_map[query.data]
    words = get_words(chat_id, since)
    if not words:
        await query.edit_message_text(f"Слов за {label} пока нет.")
        return
    lines = [f"Словарик за {label} ({len(words)} слов):\n"]
    for i, (word, translation, added_at) in enumerate(words, 1):
        lines.append(f"{i}. {word} — {translation} ({added_at[:10]})")
    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:4000] + "\n\n... (список обрезан)"
    await query.edit_message_text(text)

async def quiet_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    new_value = not get_quiet(chat_id)
    set_quiet(chat_id, new_value)
    if new_value:
        await update.message.reply_text(
            "🔕 Тихий режим включён.\n"
            "Слова будут сохраняться и переводиться в фоне, без подтверждений.\n"
            "Посмотреть словарик: /vocab\n"
            "Выключить тихий режим: /quiet"
        )
    else:
        await update.message.reply_text(
            "🔔 Тихий режим выключен. Буду снова отвечать на каждое сообщение."
        )

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Я VocabBot.\n\n"
        "Пиши английские слова или короткие фразы (до 5 слов) — "
        "я исправлю опечатки, дам расширенный перевод и сохраню в словарик.\n\n"
        "Можно несколько за раз — через запятую, точку с запятой или с новой строки.\n\n"
        "Команды:\n"
        "/vocab — показать словарик\n"
        "/quiet — вкл/выкл тихий режим (работа в фоне без ответов)\n"
        "/start — эта справка"
    )

def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("vocab", vocab_command))
    app.add_handler(CommandHandler("quiet", quiet_command))
    app.add_handler(CallbackQueryHandler(button_handler, pattern="^vocab_"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    logger.info("VocabBot started")
    app.run_polling()

if __name__ == "__main__":
    main()
