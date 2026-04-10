import os
import json
import logging
import threading
import asyncio
import time
from datetime import datetime, timezone

from flask import Flask, jsonify
import requests
from duckduckgo_search import ddg

import firebase_admin
from firebase_admin import credentials, firestore

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# Logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# Environment
CUSTOM_GOOGLE_SEARCH_API = os.environ.get("CUSTOM_GOOGLE_SEARCH_API")
SEARCH_ENGINE_ID = os.environ.get("SEARCH_ENGINE_ID")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_ADMIN_USER_ID = int(os.environ.get("TELEGRAM_ADMIN_USER_ID")) if os.environ.get("TELEGRAM_ADMIN_USER_ID") else None
FIREBASE_KEY = os.environ.get("FIREBASE_KEY")

if not TELEGRAM_BOT_TOKEN:
    logger.error("TELEGRAM_BOT_TOKEN not set in environment")
    raise SystemExit("Missing TELEGRAM_BOT_TOKEN")

app = Flask(__name__)

db = None

def init_firestore():
    global db
    if db is not None:
        return db
    if not FIREBASE_KEY:
        logger.error("FIREBASE_KEY not set; continuing without Firestore")
        return None
    # FIREBASE_KEY may be JSON string with escaped newlines; try to parse robustly
    key_str = FIREBASE_KEY
    if key_str.startswith("'") and key_str.endswith("'"):
        key_str = key_str[1:-1]
    try:
        key_json = json.loads(key_str)
    except Exception:
        # attempt literal eval fallback
        import ast
        key_json = ast.literal_eval(key_str)
    # Replace escaped newlines in private_key
    if "private_key" in key_json:
        key_json["private_key"] = key_json["private_key"].replace('\\n', '\n')

    cred = credentials.Certificate(key_json)
    try:
        firebase_admin.initialize_app(cred)
    except ValueError:
        # already initialized
        pass
    db = firestore.client()
    logger.info("Initialized Firestore")
    ensure_keywords_doc()
    return db

def ensure_keywords_doc():
    d = db.collection("main").document("keywords")
    doc = d.get()
    today = datetime.now(timezone.utc).date().isoformat()
    if not doc.exists:
        d.set({
            "list": [],
            "current_index": 0,
            "last_reset_date": today,
            "daily_count": 0,
        })
        logger.info("Created keywords document")

def get_keywords_doc():
    d = db.collection("main").document("keywords")
    doc = d.get()
    if not doc.exists:
        ensure_keywords_doc()
        doc = d.get()
    return d, doc.to_dict()

def reset_daily_if_needed(doc_ref, data):
    today = datetime.now(timezone.utc).date().isoformat()
    if data.get("last_reset_date") != today:
        doc_ref.update({
            "last_reset_date": today,
            "daily_count": 0,
        })
        data["last_reset_date"] = today
        data["daily_count"] = 0
        logger.info("Daily counters reset for new day")

def increment_daily_count(doc_ref, data):
    new_count = (data.get("daily_count") or 0) + 1
    doc_ref.update({"daily_count": new_count})
    data["daily_count"] = new_count

def advance_index(doc_ref, data):
    keywords = data.get("list") or []
    if not keywords:
        return
    next_index = (data.get("current_index") or 0) + 1
    if next_index >= len(keywords):
        next_index = 0
    doc_ref.update({"current_index": next_index})
    data["current_index"] = next_index

def get_current_keyword(doc_ref, data):
    keywords = data.get("list") or []
    if not keywords:
        return None
    idx = data.get("current_index") or 0
    if idx >= len(keywords):
        idx = 0
        doc_ref.update({"current_index": 0})
        data["current_index"] = 0
    return keywords[idx]

def search_google(keyword):
    # Use duckduckgo_search.ddg to perform the site-scoped query and return
    # results in the same structure expected by the rest of the pipeline.
    q = f"site:t.me {keyword}"
    try:
        items = ddg(q, max_results=10)
        results = []
        if items:
            for it in items:
                results.append({
                    "title": it.get("title"),
                    "link": it.get("href") or it.get("link"),
                    "snippet": it.get("body") or it.get("snippet") or "",
                })
        logger.info(f"Search for '{keyword}' returned {len(results)} items (DuckDuckGo)")
        return results, None
    except Exception as e:
        logger.exception("Search failed: %s", e)
        return [], str(e)

async def process_search_cycle(bot):
    if db is None:
        logger.warning("Firestore not initialized; skipping search cycle")
        return
    doc_ref, data = get_keywords_doc()
    reset_daily_if_needed(doc_ref, data)
    if (data.get("daily_count") or 0) >= 90:
        logger.info("Daily search limit reached (%s). Skipping until next day.", data.get("daily_count"))
        return
    keyword = get_current_keyword(doc_ref, data)
    if not keyword:
        logger.info("No keywords configured")
        return
    logger.info("Performing search for keyword: %s", keyword)
    results, err = search_google(keyword)

    # Send a brief summary to the admin for each completed search (success or error)
    try:
        if TELEGRAM_ADMIN_USER_ID:
            if err:
                summary = f"⚠️ Search for '{keyword}' failed:\n{err}"
            else:
                if results:
                    summary = f"🔎 Search complete for '{keyword}' — {len(results)} result(s) found. Sending unique links now."
                else:
                    summary = f"🔎 Search complete for '{keyword}' — no results found."
            await bot.send_message(chat_id=TELEGRAM_ADMIN_USER_ID, text=summary)
    except Exception:
        logger.exception("Failed to send search summary to admin for keyword: %s", keyword)

    if err:
        # don't attempt to process individual results when there was an error
        increment_daily_count(doc_ref, data)
        advance_index(doc_ref, data)
        return

    for r in results:
        link = r.get("link")
        sent_q = db.collection("sent_links").where("link", "==", link).limit(1).get()
        if sent_q and len(sent_q) > 0:
            logger.debug("Link already sent: %s", link)
            continue
        # Save to results
        db.collection("results").add({
            "keyword": keyword,
            "title": r.get("title"),
            "link": link,
            "snippet": r.get("snippet"),
            "created_at": firestore.SERVER_TIMESTAMP,
        })
        db.collection("sent_links").add({
            "link": link,
            "created_at": firestore.SERVER_TIMESTAMP,
        })
        # Send to admin
        try:
            msg = f"🔍 Keyword: {keyword}\n\n{r.get('title')}\n{link}\n\n{r.get('snippet') or ''}"
            if TELEGRAM_ADMIN_USER_ID:
                await bot.send_message(chat_id=TELEGRAM_ADMIN_USER_ID, text=msg)
                logger.info("Sent result to admin: %s", link)
        except Exception:
            logger.exception("Failed to send telegram message for link: %s", link)
    # update counts and advance index
    increment_daily_count(doc_ref, data)
    advance_index(doc_ref, data)

async def scheduler_loop(bot):
    logger.info("Async scheduler started")
    while True:
        try:
            await process_search_cycle(bot)
        except Exception:
            logger.exception("Error during scheduled search cycle")
        await asyncio.sleep(60)

# --- Telegram handlers ---
awaiting_upload = set()

def restricted_admin(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if TELEGRAM_ADMIN_USER_ID and user and user.id != TELEGRAM_ADMIN_USER_ID:
            await update.message.reply_text("Unauthorized")
            return
        return await func(update, context)
    return wrapper


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Telegram Search Bot running.")


@restricted_admin
async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if db is None:
        await update.message.reply_text("Firestore not configured")
        return
    doc_ref, data = get_keywords_doc()
    total = len(data.get("list") or [])
    idx = data.get("current_index") or 0
    searches = data.get("daily_count") or 0
    msg = f"Total keywords: {total}\nCurrent index: {idx}\nSearches today: {searches}"
    await update.message.reply_text(msg)


@restricted_admin
async def reset_index(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if db is None:
        await update.message.reply_text("Firestore not configured")
        return
    doc_ref, data = get_keywords_doc()
    doc_ref.update({"current_index": 0})
    await update.message.reply_text("Keyword index reset to 0")


@restricted_admin
async def add_keyword(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or ""
    parts = text.split(" ", 1)
    if len(parts) < 2:
        await update.message.reply_text("Usage: /add <keyword text>")
        return
    kw = parts[1].strip()
    doc_ref, data = get_keywords_doc()
    kws = data.get("list") or []
    if kw in kws:
        await update.message.reply_text("Keyword already exists")
        return
    kws.append(kw)
    doc_ref.update({"list": kws})
    await update.message.reply_text(f"Added keyword: {kw}")


@restricted_admin
async def upload_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Please send a JSON file containing an array of keywords (e.g. [\"kw1\", \"kw2\"])."
    )
    awaiting_upload.add(update.effective_chat.id)


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id not in awaiting_upload:
        return
    if TELEGRAM_ADMIN_USER_ID and update.effective_user.id != TELEGRAM_ADMIN_USER_ID:
        await update.message.reply_text("Unauthorized")
        return
    doc = update.message.document
    file = await doc.get_file()
    data_bytes = await file.download_as_bytearray()
    try:
        arr = json.loads(data_bytes.decode("utf-8"))
        if not isinstance(arr, list):
            raise ValueError("JSON is not an array")
        # sanitize
        cleaned = []
        for it in arr:
            if not isinstance(it, str):
                continue
            s = it.strip()
            if s:
                cleaned.append(s)
        if not cleaned:
            await update.message.reply_text("No valid keywords found in file")
            awaiting_upload.discard(chat_id)
            return
        # merge with existing
        doc_ref, data = get_keywords_doc()
        existing = data.get("list") or []
        merged = existing + cleaned
        # dedupe while preserving order
        seen = set()
        deduped = []
        for k in merged:
            if k not in seen:
                seen.add(k)
                deduped.append(k)
        doc_ref.update({"list": deduped})
        await update.message.reply_text(f"Uploaded {len(cleaned)} keywords. Total now: {len(deduped)}")
    except Exception as e:
        logger.exception("Failed to process uploaded file")
        await update.message.reply_text(f"Failed to process file: {e}")
    finally:
        awaiting_upload.discard(chat_id)

def start_flask():
    port = int(os.environ.get("PORT", 8080))

    @app.route("/", methods=["GET"])
    def health():
        return jsonify({"status": "ok"})

    logger.info("Starting Flask webserver on port %s", port)
    app.run(host="0.0.0.0", port=port)


def main():
    global db
    db = init_firestore()

    application = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    async def post_init(app):
        logger.info("Application initialized, starting scheduler")
        app.create_task(scheduler_loop(app.bot))

    application.post_init = post_init

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("status", status))
    application.add_handler(CommandHandler("reset", reset_index))
    application.add_handler(CommandHandler("add", add_keyword))
    application.add_handler(CommandHandler("upload", upload_start))
    application.add_handler(MessageHandler(filters.Document.ALL, handle_document))

    # Start Flask in a separate thread so PTB can run the asyncio loop
    t = threading.Thread(target=start_flask, daemon=True)
    t.start()

    logger.info("Starting Telegram Application (long polling)")
    application.run_polling()


if __name__ == '__main__':
    main()
