import os
import math
import sqlite3
from datetime import datetime, timedelta, timezone
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, ConversationHandler

# ================= CONFIG =================
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID")
ADMIN_IDS = os.getenv("ADMIN_USER_IDS", "")

BANGKOK_TZ = timezone(timedelta(hours=7))

ASK_NAME, ASK_AREA, ASK_VIBE, ASK_DURATION = range(4)

AREAS = [
    "Sukhumvit", "Thonglor", "Ekkamai", "Ari",
    "Silom / Sathorn", "Khao San / Old Town", "Chinatown"
]

VIBES = ["Work", "Social", "Chill", "Explore", "Drinks"]
DURATIONS = ["30 min", "1 hour", "2 hours", "Tonight"]

# ================= DATABASE =================
conn = sqlite3.connect("db.sqlite3", check_same_thread=False)
cur = conn.cursor()

cur.execute("CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, name TEXT)")
cur.execute("""CREATE TABLE IF NOT EXISTS checkins (
    user_id INTEGER,
    area TEXT,
    vibe TEXT,
    expires_at TEXT
)""")
conn.commit()

# ================= HELPERS =================

def now():
    return datetime.now(BANGKOK_TZ)

def validate_name(name):
    if len(name) < 2 or len(name) > 20:
        return False
    if not name.replace(" ", "").isalpha():
        return False
    return True

def main_menu():
    return ReplyKeyboardMarkup([
        ["Check in", "My status"],
        ["End check-in"],
        ["Safety rules"]
    ], resize_keyboard=True)

# ================= HANDLERS =================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Welcome to Pulse Bangkok\n\nSee what's happening around you.",
        reply_markup=main_menu()
    )

async def safety(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Safety rules:\n• Area only\n• No exact location\n• Public places only",
        reply_markup=main_menu()
    )

async def checkin_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("What should we call you?")
    return ASK_NAME

async def get_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.message.text
    if not validate_name(name):
        await update.message.reply_text("Invalid name. Use simple English letters.")
        return ASK_NAME

    context.user_data["name"] = name
    await update.message.reply_text("Where are you around?",
        reply_markup=ReplyKeyboardMarkup([
            [KeyboardButton("📍 Use my location", request_location=True)],
            AREAS[:2], AREAS[2:4], AREAS[4:6], [AREAS[6]]
        ], resize_keyboard=True)
    )
    return ASK_AREA

async def get_area(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.location:
        context.user_data["area"] = "Sukhumvit"
    else:
        context.user_data["area"] = update.message.text

    await update.message.reply_text("What's your vibe?",
        reply_markup=ReplyKeyboardMarkup([[v] for v in VIBES], resize_keyboard=True))
    return ASK_VIBE

async def get_vibe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["vibe"] = update.message.text

    await update.message.reply_text("How long?",
        reply_markup=ReplyKeyboardMarkup([[d] for d in DURATIONS], resize_keyboard=True))
    return ASK_DURATION

async def finish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    duration = update.message.text
    user_id = update.effective_user.id

    expires = now() + timedelta(hours=1)

    cur.execute("DELETE FROM checkins WHERE user_id=?", (user_id,))
    cur.execute("INSERT INTO checkins VALUES (?, ?, ?, ?)",
                (user_id, context.user_data["area"], context.user_data["vibe"], expires.isoformat()))
    conn.commit()

    await update.message.reply_text(
        f"Checked in:\n{context.user_data['area']} | {context.user_data['vibe']}",
        reply_markup=main_menu()
    )
    return ConversationHandler.END

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    row = cur.execute("SELECT area, vibe FROM checkins WHERE user_id=?", (user_id,)).fetchone()

    if not row:
        await update.message.reply_text("No active check-in.", reply_markup=main_menu())
    else:
        await update.message.reply_text(f"{row[0]} | {row[1]}", reply_markup=main_menu())

async def end(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    cur.execute("DELETE FROM checkins WHERE user_id=?", (user_id,))
    conn.commit()

    await update.message.reply_text("Check-in ended.", reply_markup=main_menu())

# ================= ADMIN =================

def is_admin(user_id):
    return str(user_id) in ADMIN_IDS

async def admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return

    rows = cur.execute("SELECT area, COUNT(*) FROM checkins GROUP BY area").fetchall()

    text = "Stats:\n"
    for r in rows:
        text += f"{r[0]}: {r[1]}\n"

    await update.message.reply_text(text)

async def admin_ignite(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return

    cur.execute("INSERT INTO checkins VALUES (?, ?, ?, ?)",
                (999999, "Ari", "Work", (now()+timedelta(hours=1)).isoformat()))
    conn.commit()

    await update.message.reply_text("Area ignited 🔥")

# ================= MAIN =================

def main():
    app = Application.builder().token(BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[MessageHandler(filters.TEXT & filters.Regex("^Check in$"), checkin_start)],
        states={
            ASK_NAME: [MessageHandler(filters.TEXT, get_name)],
            ASK_AREA: [MessageHandler(filters.TEXT | filters.LOCATION, get_area)],
            ASK_VIBE: [MessageHandler(filters.TEXT, get_vibe)],
            ASK_DURATION: [MessageHandler(filters.TEXT, finish)],
        },
        fallbacks=[]
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("^Safety rules$"), safety))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("^My status$"), status))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("^End check-in$"), end))
    app.add_handler(CommandHandler("admin_stats", admin_stats))
    app.add_handler(CommandHandler("admin_ignite", admin_ignite))
    app.add_handler(conv)

    app.run_polling()

if __name__ == "__main__":
    main()
