import os
import math
import sqlite3
from datetime import datetime, timedelta, timezone
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
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
ADMIN_IDS = {x.strip() for x in os.getenv("ADMIN_USER_IDS", "").split(",")}
SUMMARY_INTERVAL_MIN = int(os.getenv("SUMMARY_INTERVAL_MIN", "30"))

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

cur.execute("""
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    name TEXT
)
""")

cur.execute("""
CREATE TABLE IF NOT EXISTS checkins (
    user_id INTEGER,
    area TEXT,
    vibe TEXT,
    expires_at TEXT,
    source TEXT DEFAULT 'user'
)
""")

# migration
cur.execute("PRAGMA table_info(checkins)")
cols = [c[1] for c in cur.fetchall()]
if "source" not in cols:
    cur.execute("ALTER TABLE checkins ADD COLUMN source TEXT DEFAULT 'user'")

conn.commit()

# ================= HELPERS =================
def now():
    return datetime.now(BANGKOK_TZ)

def compute_expiry(start, label):
    if label == "30 min":
        return start + timedelta(minutes=30)
    if label == "1 hour":
        return start + timedelta(hours=1)
    if label == "2 hours":
        return start + timedelta(hours=2)
    if label == "Tonight":
        return start.replace(hour=23, minute=59)
    return start + timedelta(hours=1)

def is_admin(user_id):
    return str(user_id) in ADMIN_IDS

def cleanup():
    cur.execute("DELETE FROM checkins WHERE expires_at <= ?", (now().isoformat(),))
    conn.commit()

def main_menu():
    return ReplyKeyboardMarkup([
        ["Check in", "My status"],
        ["End check-in", "Safety rules"]
    ], resize_keyboard=True)

# ================= FLOW =================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Welcome to Pulse", reply_markup=main_menu())

async def checkin_start(update, context):
    await update.message.reply_text("Name?")
    return ASK_NAME

async def get_name(update, context):
    name = update.message.text
    context.user_data["name"] = name
    cur.execute("INSERT OR REPLACE INTO users VALUES (?,?)",
                (update.effective_user.id, name))
    conn.commit()

    await update.message.reply_text("Area?", reply_markup=ReplyKeyboardMarkup(
        [["Sukhumvit","Thonglor"],["Ekkamai","Ari"],
         ["Silom / Sathorn"],["Khao San / Old Town","Chinatown"]],
        resize_keyboard=True))
    return ASK_AREA

async def get_area(update, context):
    context.user_data["area"] = update.message.text
    await update.message.reply_text("Vibe?", reply_markup=ReplyKeyboardMarkup(
        [[v] for v in VIBES], resize_keyboard=True))
    return ASK_VIBE

async def get_vibe(update, context):
    context.user_data["vibe"] = update.message.text
    await update.message.reply_text("Duration?", reply_markup=ReplyKeyboardMarkup(
        [[d] for d in DURATIONS], resize_keyboard=True))
    return ASK_DURATION

async def finish(update, context):
    user_id = update.effective_user.id
    area = context.user_data["area"]
    vibe = context.user_data["vibe"]
    duration = update.message.text

    expires = compute_expiry(now(), duration)

    cur.execute("DELETE FROM checkins WHERE user_id=?", (user_id,))
    cur.execute("INSERT INTO checkins VALUES (?,?,?,?,?)",
                (user_id, area, vibe, expires.isoformat(), "user"))
    conn.commit()

    await update.message.reply_text("Checked in ✅", reply_markup=main_menu())
    return ConversationHandler.END

async def status(update, context):
    row = cur.execute("SELECT area,vibe FROM checkins WHERE user_id=?",
                      (update.effective_user.id,)).fetchone()
    if not row:
        await update.message.reply_text("No active check-in")
    else:
        await update.message.reply_text(f"{row[0]} | {row[1]}")

async def end(update, context):
    cur.execute("DELETE FROM checkins WHERE user_id=?",
                (update.effective_user.id,))
    conn.commit()
    await update.message.reply_text("Ended")

# ================= ADMIN =================
async def admin_stats(update, context):
    if not is_admin(update.effective_user.id): return
    cleanup()
    rows = cur.execute("SELECT area,COUNT(*) FROM checkins GROUP BY area").fetchall()
    text = "Stats:\n" + "\n".join(f"{a}: {c}" for a,c in rows)
    await update.message.reply_text(text)

async def admin_ignite(update, context):
    if not is_admin(update.effective_user.id): return

    raw = update.message.text.replace("/admin_ignite","").strip()
    area, vibe, duration, count = [x.strip() for x in raw.split("|")]
    count = int(count)

    expires = compute_expiry(now(), duration).isoformat()

    base = int(now().timestamp())
    for i in range(count):
        cur.execute("INSERT INTO checkins VALUES (?,?,?,?,?)",
                    (900000000+base+i, area, vibe, expires, "ignite"))

    conn.commit()
    await update.message.reply_text("Ignited 🔥")

async def admin_clear_ignite(update, context):
    if not is_admin(update.effective_user.id): return

    cur.execute("DELETE FROM checkins WHERE source='ignite'")
    deleted = cur.rowcount
    conn.commit()

    await update.message.reply_text(f"Deleted {deleted} ignite checkins")

async def admin_reset_checkins(update, context):
    if not is_admin(update.effective_user.id): return

    cur.execute("DELETE FROM checkins")
    conn.commit()
    await update.message.reply_text("All checkins cleared")

async def admin_help(update, context):
    if not is_admin(update.effective_user.id): return

    await update.message.reply_text(
        "/admin_stats\n"
        "/admin_ignite Area|Vibe|Duration|Count\n"
        "/admin_clear_ignite\n"
        "/admin_reset_checkins"
    )

# ================= SUMMARY =================
async def summary(context):
    if not CHANNEL_ID: return

    cleanup()
    rows = cur.execute(
        "SELECT area,vibe,COUNT(*) FROM checkins GROUP BY area,vibe"
    ).fetchall()

    text = "🔥 Pulse Live\n\n"
    for r in rows:
        text += f"{r[0]} - {r[1]}: {r[2]}\n"

    await context.bot.send_message(chat_id=CHANNEL_ID, text=text)

# ================= MAIN =================
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[MessageHandler(filters.TEXT & filters.Regex("^Check in$"), checkin_start)],
        states={
            ASK_NAME:[MessageHandler(filters.TEXT, get_name)],
            ASK_AREA:[MessageHandler(filters.TEXT, get_area)],
            ASK_VIBE:[MessageHandler(filters.TEXT, get_vibe)],
            ASK_DURATION:[MessageHandler(filters.TEXT, finish)],
        },
        fallbacks=[]
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin_stats", admin_stats))
    app.add_handler(CommandHandler("admin_ignite", admin_ignite))
    app.add_handler(CommandHandler("admin_clear_ignite", admin_clear_ignite))
    app.add_handler(CommandHandler("admin_reset_checkins", admin_reset_checkins))
    app.add_handler(CommandHandler("admin_help", admin_help))

    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("^My status$"), status))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("^End check-in$"), end))

    app.add_handler(conv)

    if app.job_queue:
        app.job_queue.run_repeating(summary, interval=SUMMARY_INTERVAL_MIN*60, first=60)

    app.run_polling()

if __name__ == "__main__":
    main()
