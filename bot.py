"""VocabBot - a Telegram bot that builds an English vocabulary list.

The user sends English words or short phrases; the bot fixes typos, produces a
Russian translation with the main senses, and stores the entry per chat.
"""

import os
import re
import asyncio
import sqlite3
import logging
from datetime import datetime, timedelta, timezone

import anthropic
from anthropic import AsyncAnthropic
from pydantic import BaseModel

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, MessageHandler, CommandHandler,
    CallbackQueryHandler, ContextTypes, filters
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ["BOT_TOKEN"]

# The Anthropic client reads ANTHROPIC_API_KEY from the environment.
client = AsyncAnthropic()

MODEL = "claude-sonnet-5"
DB_PATH = os.environ.get("DB_PATH", "vocab.db")
MAX_WORDS_IN_PHRASE = 5


class WordAnalysis(BaseModel):
    """Schema the model is constrained to return."""

    corrected: str
    translation: str


# --------------------------------------------------------------------------- #
# Storage
# --------------------------------------------------------------------------- #

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
    """Insert a word. Returns True if it was new for this chat."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    now = datetime.now(timezone.utc).isoformat()
    try:
        c.execute(
            "INSERT OR IGNORE INTO words (chat_id, word, translation, added_at) "
            "VALUES (?, ?, ?, ?)",
            (chat_id, word.lower().strip(), translation, now)
        )
        conn.commit()
        inserted = c.rowcount > 0
    except sqlite3.Error as e:
        logger.error("save_word failed: %s", e)
        inserted = False
    finally:
        conn.close()
    return inserted


def get_words(chat_id, since):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "SELECT word, translation, added_at FROM words "
        "WHERE chat_id=? AND added_at>=? ORDER BY added_at DESC",
        (chat_id, since.isoformat())
    )
    rows = c.fetchall()
    conn.close()
    return rows


# --------------------------------------------------------------------------- #
# Language model
# --------------------------------------------------------------------------- #

PROMPT = (
    "You help a Russian speaker learn English.\n"
    "The input is an English word or a short phrase of up to five words.\n\n"
    "1. Fix typos and return the correct spelling in lower case. If the input "
    "is already correct, return it unchanged.\n"
    "2. Translate it into Russian, giving the main senses separated by commas "
    "or semicolons. No examples, no explanations, at most 200 characters.\n\n"
    "Input: {word}"
)


async def analyze_word(word: str) -> WordAnalysis:
    """Ask the model to correct and translate one word or phrase.

    Structured outputs constrain the response to the WordAnalysis schema, so
    there is no JSON parsing or markdown-fence stripping to get wrong.
    On any API failure the input is returned unchanged rather than lost.
    """
    try:
        response = await client.messages.parse(
            model=MODEL,
            max_tokens=400,
            messages=[{"role": "user", "content": PROMPT.format(word=word)}],
            output_format=WordAnalysis,
        )
        result = response.parsed_output
        return WordAnalysis(
            corrected=result.corrected.strip() or word,
            translation=result.translation.strip() or "—",
        )
    except anthropic.RateLimitError:
        logger.warning("rate limited while analysing %r", word)
    except anthropic.APIStatusError as e:
        logger.error("API error %s while analysing %r", e.status_code, word)
    except anthropic.APIConnectionError:
        logger.error("network error while analysing %r", word)
    return WordAnalysis(corrected=word, translation="—")


def is_english_phrase(text: str) -> bool:
    """Accept English words and phrases of up to five words.

    Letters, hyphens, apostrophes and spaces between words are allowed.
    """
    text = text.strip()
    if not text:
        return False
    if not re.fullmatch(r"[a-zA-Z\-' ]{2,60}", text):
        return False
    words = text.split()
    if not words or len(words) > MAX_WORDS_IN_PHRASE:
        return False
    return all(re.search(r"[a-zA-Z]", w) for w in words)


# --------------------------------------------------------------------------- #
# Handlers
# --------------------------------------------------------------------------- #

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not msg.text:
        return
    chat_id = msg.chat_id

    # Commas, semicolons and newlines separate entries. A space does not, so
    # that phrases such as "driving license" survive as one entry.
    candidates = [p.strip() for p in re.split(r"[,\n;]+", msg.text)]
    phrases = [p for p in candidates if p and is_english_phrase(p)]
    if not phrases:
        return

    # One network call per phrase, all in flight at once.
    results = await asyncio.gather(*(analyze_word(p) for p in phrases))

    new_words = []
    corrections = []
    for phrase, result in zip(phrases, results):
        if save_word(chat_id, result.corrected, result.translation):
            new_words.append((result.corrected, result.translation))
        if result.corrected.lower() != phrase.lower():
            corrections.append((phrase, result.corrected))

    # Quiet mode still stores everything, it just does not reply.
    if get_quiet(chat_id):
        return

    if not (new_words or corrections):
        return

    lines = []
    if corrections:
        lines.append("Исправлены опечатки:")
        lines += [f"  {orig} → {corr}" for orig, corr in corrections]
        lines.append("")
    if new_words:
        lines.append("Добавлено в словарик:")
        lines += [f"  {w} — {t}" for w, t in new_words]
    await msg.reply_text("\n".join(lines))


async def vocab_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("За сегодня", callback_data="vocab_today")],
        [InlineKeyboardButton("За эту неделю", callback_data="vocab_week")],
        [InlineKeyboardButton("За этот месяц", callback_data="vocab_month")],
        [InlineKeyboardButton("За всё время", callback_data="vocab_all")],
    ]
    await update.message.reply_text(
        "За какой период показать словарик?",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    now = datetime.now(timezone.utc)
    periods = {
        "vocab_today": ("сегодня", now.replace(hour=0, minute=0, second=0, microsecond=0)),
        "vocab_week": ("эту неделю", now - timedelta(days=now.weekday())),
        "vocab_month": ("этот месяц", now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)),
        "vocab_all": ("всё время", datetime(2000, 1, 1, tzinfo=timezone.utc)),
    }
    label, since = periods[query.data]

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
    enabled = not get_quiet(chat_id)
    set_quiet(chat_id, enabled)
    if enabled:
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
