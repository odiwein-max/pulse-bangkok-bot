import os
import math
import random
import sqlite3
import threading
from datetime import datetime, timedelta, timezone

from flask import Flask, jsonify

from telegram import (
    Update,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    ConversationHandler,
    filters,
)

# ================= CONFIG =================
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID")
LIVE_MAP_URL = os.getenv("LIVE_MAP_URL")

ADMIN_IDS = {x.strip() for x in os.getenv("ADMIN_USER_IDS", "").split(",") if x.strip()}
SUMMARY_INTERVAL_MIN = int(os.getenv("SUMMARY_INTERVAL_MIN", "30"))

BANGKOK_TZ = timezone(timedelta(hours=7))

ASK_NAME, ASK_AREA, ASK_VIBE, ASK_DURATION = range(4)

AREAS = [
    "Sukhumvit",
    "Thonglor",
    "Ekkamai",
    "Ari",
    "Silom / Sathorn",
    "Khao San / Old Town",
    "Chinatown",
]

VIBES = ["Work", "Social", "Chill", "Explore", "Drinks"]
DURATIONS = ["30 min", "1 hour", "2 hours", "Tonight"]

# ================= DATABASE =================
conn = sqlite3.connect("db.sqlite3", check_same_thread=False)
conn.row_factory = sqlite3.Row
cur = conn.cursor()

cur.execute("""
CREATE TABLE IF NOT EXISTS checkins (
    user_id INTEGER,
    area TEXT,
    vibe TEXT,
    expires_at TEXT,
    source TEXT DEFAULT 'user'
)
""")

conn.commit()

# ================= HELPERS =================
def now():
    return datetime.now(BANGKOK_TZ)

def compute_expiry(label):
    if label == "30 min":
        return now() + timedelta(minutes=30)
    if label == "1 hour":
        return now() + timedelta(hours=1)
    if label == "2 hours":
        return now() + timedelta(hours=2)
    return now() + timedelta(hours=4)

def cleanup():
    cur.execute("DELETE FROM checkins WHERE expires_at <= ?", (now().isoformat(),))
    conn.commit()

def summary_buttons(bot_username):
    buttons = [[InlineKeyboardButton("Check in 📍", url=f"https://t.me/{bot_username}")]]
    if LIVE_MAP_URL:
        buttons.append([InlineKeyboardButton("Open Live Map 🗺️", url=LIVE_MAP_URL)])
    return InlineKeyboardMarkup(buttons)

# ================= HEATMAP API =================
app_flask = Flask(__name__)

@app_flask.route("/api/heatmap")
def heatmap():
    cleanup()

    rows = cur.execute("""
        SELECT area, vibe, COUNT(*) as count
        FROM checkins
        GROUP BY area, vibe
    """).fetchall()

    centers = {
        "Sukhumvit": (13.7370, 100.5600),
        "Thonglor": (13.7308, 100.5810),
        "Ekkamai": (13.7197, 100.5856),
        "Ari": (13.7799, 100.5450),
        "Silom / Sathorn": (13.7240, 100.5300),
        "Khao San / Old Town": (13.7589, 100.4970),
        "Chinatown": (13.7396, 100.5098),
    }

    grouped = {}

    for row in rows:
        area = row["area"]
        vibe = row["vibe"]
        count = row["count"]

        if area not in grouped:
            grouped[area] = {"total": 0, "vibes": {}}

        grouped[area]["total"] += count
        grouped[area]["vibes"][vibe] = count

    result = []

    for area, data in grouped.items():
        lat, lng = centers.get(area, (13.7563, 100.5018))
        result.append({
            "area": area,
            "lat": lat,
            "lng": lng,
            "total": data["total"],
            "vibes": data["vibes"]
        })

    return jsonify(result)

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    app_flask.run(host="0.0.0.0", port=port)

# ================= SUMMARY =================
def format_summary():
    cleanup()

    rows = cur.execute("""
    SELECT area, vibe, COUNT(*) as count
    FROM checkins
    GROUP BY area, vibe
    """).fetchall()

    if not rows:
        return "No activity right now 👀"

    grouped = {}
    for row in rows:
        grouped.setdefault(row["area"], []).append((row["vibe"], row["count"]))

    lines = ["🔥 Pulse Bangkok — Live Now", ""]

    for area, vibes in grouped.items():
        lines.append(area)
        for vibe, count in vibes:
            lines.append(f"• {count} {vibe}")
        lines.append("")

    return "\n".join(lines)

async def summary_job(context: ContextTypes.DEFAULT_TYPE):
    if not CHANNEL_ID:
        return

    text = format_summary()
    keyboard = summary_buttons(context.bot.username)

    await context.bot.send_message(
        chat_id=CHANNEL_ID,
        text=text,
        reply_markup=keyboard,
    )

# ================= CHECKIN =================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Welcome 👋",
        reply_markup=ReplyKeyboardMarkup([["Check in"]], resize_keyboard=True)
    )

async def checkin_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Your name?")
    return ASK_NAME

async def get_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["name"] = update.message.text
    await update.message.reply_text("Choose area", reply_markup=ReplyKeyboardMarkup([AREAS], resize_keyboard=True))
    return ASK_AREA

async def get_area(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["area"] = update.message.text
    await update.message.reply_text("Choose vibe", reply_markup=ReplyKeyboardMarkup([VIBES], resize_keyboard=True))
    return ASK_VIBE

async def get_vibe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["vibe"] = update.message.text
    await update.message.reply_text("Duration", reply_markup=ReplyKeyboardMarkup([DURATIONS], resize_keyboard=True))
    return ASK_DURATION

async def finish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    expires = compute_expiry(update.message.text)

    cur.execute("DELETE FROM checkins WHERE user_id = ?", (user_id,))
    cur.execute(
        "INSERT INTO checkins VALUES (?, ?, ?, ?, ?)",
        (user_id, context.user_data["area"], context.user_data["vibe"], expires.isoformat(), "user"),
    )
    conn.commit()

    await update.message.reply_text("Checked in ✅")
    return ConversationHandler.END

# ================= MAIN =================
def main():
    threading.Thread(target=run_flask).start()

    app = Application.builder().token(BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[MessageHandler(filters.TEXT & filters.Regex("^Check in$"), checkin_start)],
        states={
            ASK_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_name)],
            ASK_AREA: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_area)],
            ASK_VIBE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_vibe)],
            ASK_DURATION: [MessageHandler(filters.TEXT & ~filters.COMMAND, finish)],
        },
        fallbacks=[],
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(conv)

    if app.job_queue:
        app.job_queue.run_repeating(summary_job, interval=SUMMARY_INTERVAL_MIN * 60, first=10)

    app.run_polling()

if __name__ == "__main__":
    main()
