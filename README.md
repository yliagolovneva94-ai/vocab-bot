# VocabBot

A Telegram bot that turns everyday reading into a personal English vocabulary list.

You send it an English word or a short phrase. It corrects typos, returns a Russian
translation with the main senses, stores the entry for your chat, and gives the list
back on request for today, this week, this month or all time.

The bot replies in Russian because it translates English into Russian for a Russian
speaker. The code and this document are in English.

## Why it exists

I read in English every day and kept losing new words in the margins of other apps.
A Telegram chat is where the words already appear, so the vocabulary list belongs there
too. Quiet mode exists for the same reason: while reading, you want the word captured
without a reply interrupting you.

## How it works

- **Structured outputs.** The model is constrained to a Pydantic schema with
  `messages.parse`, so the reply is a validated object rather than a JSON string that
  has to be scraped out of markdown fences.
- **Non-blocking.** Handlers use the asynchronous Anthropic client. Sending several
  words at once dispatches one request per word concurrently with `asyncio.gather`,
  so the bot keeps serving other chats while translations are in flight.
- **Degrades instead of failing.** If the API is rate limited or unreachable, the word
  is stored unchanged with a placeholder translation rather than dropped.
- **Per-chat storage.** SQLite, one row per chat and word, with a uniqueness constraint
  so the same word is never stored twice for the same chat.

## Stack

Python, python-telegram-bot, the Anthropic Python SDK with Claude Sonnet 5, SQLite,
deployed on Railway.

## Running it

```bash
pip install -r requirements.txt
export BOT_TOKEN=...          # from @BotFather
export ANTHROPIC_API_KEY=...  # from console.anthropic.com
python bot.py
```

Copy `.env.example` if you prefer a file. No secrets are read from anywhere else.

## Commands

| Command | What it does |
|---|---|
| `/start` | Short help message |
| `/vocab` | Show the vocabulary list, with buttons for the period |
| `/quiet` | Toggle quiet mode: words are still saved, the bot stops replying |

## Known limitations

- The SQLite file lives on the container filesystem. On Railway that storage is
  ephemeral, so a redeploy resets the vocabulary unless a volume is mounted. Setting
  `DB_PATH` to a mounted volume fixes this.
- Input is restricted to Latin letters, hyphens and apostrophes, up to five words, so
  a phrase with a digit or punctuation is ignored rather than stored.
- `run_polling` means a single instance. Running two at once would double-process
  updates.
