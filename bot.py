import os
import re
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
 
def translate_word(word):
    try:
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=100,
            messages=[{
                "role": "user",
                "content": (
                    f"Переведи английское слово или фразу на русский язык. "
                    f"Ответь ТОЛЬКО переводом, без пояснений и без кавычек.\n\n"
                    f"Слово: {word}"
                )
            }]
        )
        return response.content[0].text.strip()
    except Exception as e:
        logger.error(f"translate_word error: {e}")
        return "—"
 
def is_english_word(text):
    text = text.strip()
    return bool(re.fullmatch(r"[a-zA-Z\-']{2,40}", text))
 
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not msg.text:
        return
    text = msg.text.strip()
    chat_id = msg.chat_id
    candidates = re.split(r"[,\n;]+", text)
    new_words = []
    for candidate in candidates:
        word = candidate.strip()
        if is_english_word(word):
            translation = translate_word(word)
            inserted = save_word(chat_id, word, translation)
            if inserted:
                new_words.append((word, translation))
    if new_words:
        lines = ["Добавлено в словарик:"]
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
 
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Я VocabBot.\n\n"
        "Просто пиши английские слова в этот чат — я их переведу и сохраню.\n\n"
        "Команды:\n"
        "/vocab — показать словарик\n"
        "/start — эта справка"
    )
 
def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("vocab", vocab_command))
    app.add_handler(CallbackQueryHandler(button_handler, pattern="^vocab_"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    logger.info("VocabBot started")
    app.run_polling()
 
if __name__ == "__main__":
    main()