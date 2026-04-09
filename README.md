# Telegram Search Bot

This project implements a Telegram bot that periodically searches Telegram content (site:t.me) using the Google Custom Search API and stores results in Firestore. It is designed to run on Render as a web service using long polling.

Features
- Google Custom Search integration restricted to `site:t.me`
- Firestore collections: `main/keywords`, `results`, `sent_links`
- Admin commands: `/status`, `/reset`, `/add <keyword>`, `/upload` (JSON file)
- Scheduler runs every 1 minute and respects a daily limit of 90 searches
- Deduplication of sent links

Environment variables (set in Render)
- `CUSTOM_GOOGLE_SEARCH_API`
- `SEARCH_ENGINE_ID`
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_ADMIN_USER_ID`
- `FIREBASE_KEY` (JSON string of Firebase service account)

Run locally
1. Create a Python 3.10+ virtualenv and install dependencies:
```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```
2. Populate environment variables (locally you can use a `.env` loader or export them).
3. Run the bot:
```bash
python bot.py
```

Render Deployment
1. Create a Web Service on Render using the `python` environment.
2. Set the start command to: `python bot.py`
3. Add the environment variables listed above in the Render dashboard.
4. Deploy. The service exposes `/` health endpoint and runs the bot using long polling.

Notes
- `FIREBASE_KEY` should be a JSON string; newlines in the private key should be escaped as `\\n` (the code will normalize them).
- The bot stores progress and prevents duplicate link sends via Firestore.
