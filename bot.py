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
ADMIN_IDS_RAW = os.getenv("ADMIN_USER_IDS", "")
SUMMARY_INTERVAL_MIN = int(os.getenv("SUMMARY_INTERVAL_MIN", "30"))

if not BOT_TOKEN:
    raise RuntimeError("Missing TELEGRAM_BOT_TOKEN")

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

ADMIN_IDS = {x.strip() for x in ADMIN_IDS_RAW.split(",") if x.strip()}

AREA_GEOFENCE = {
    "Sukhumvit": {"center": (13.7370, 100.5600), "radius_km": 2.0},
    "Thonglor": {"center": (13.7308, 100.5810), "radius_km": 1.0},
    "Ekkamai": {"center": (13.7197, 100.5856), "radius_km": 1.0},
    "Ari": {"center": (13.7799, 100.5450), "radius_km": 1.2},
    "Silom / Sathorn": {"center": (13.7240, 100.5300), "radius_km": 1.5},
    "Khao San / Old Town": {"center": (13.7589, 100.4970), "radius_km": 1.0},
    "Chinatown": {"center": (13.7396, 100.5098), "radius_km": 1.0},
}

BLACKLIST_NAMES = {
    "admin", "support", "bot", "anonymous", "unknown",
    "test", "null", "none", "system", "moderator"
}

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
    expires_at TEXT
)
""")

conn.commit()


# ================= HELPERS =================
def now():
    return datetime.now(BANGKOK_TZ)


def compute_expiry(start_dt: datetime, duration_label: str) -> datetime:
    if duration_label == "30 min":
        return start_dt + timedelta(minutes=30)
    if duration_label == "1 hour":
        return start_dt + timedelta(hours=1)
    if duration_label == "2 hours":
        return start_dt + timedelta(hours=2)
    if duration_label == "Tonight":
        tonight = start_dt.replace(hour=23, minute=59, second=0, microsecond=0)
        if tonight <= start_dt:
            tonight = start_dt + timedelta(hours=4)
        return tonight
    return start_dt + timedelta(hours=1)


def validate_name(name: str) -> bool:
    name = (name or "").strip()
    if len(name) < 2 or len(name) > 20:
        return False
    if name.lower() in BLACKLIST_NAMES:
        return False
    if not name.replace(" ", "").isalpha():
        return False
    return True


def is_admin(user_id: int) -> bool:
    return str(user_id) in ADMIN_IDS


def cleanup_expired_checkins():
    current = now().isoformat()
    cur.execute("DELETE FROM checkins WHERE expires_at <= ?", (current,))
    conn.commit()


def main_menu():
    return ReplyKeyboardMarkup(
        [
            ["Check in", "My status"],
            ["End check-in", "Safety rules"],
        ],
        resize_keyboard=True,
    )


def area_menu():
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("📍 Use my location", request_location=True)],
            ["Sukhumvit", "Thonglor"],
            ["Ekkamai", "Ari"],
            ["Silom / Sathorn"],
            ["Khao San / Old Town", "Chinatown"],
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def vibe_menu():
    return ReplyKeyboardMarkup([[v] for v in VIBES], resize_keyboard=True, one_time_keyboard=True)


def duration_menu():
    return ReplyKeyboardMarkup([[d] for d in DURATIONS], resize_keyboard=True, one_time_keyboard=True)


def haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(lat1))
        * math.cos(math.radians(lat2))
        * math.sin(dlon / 2) ** 2
    )
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return r * c


def suggest_area_from_location(lat, lon):
    matches = []
    for area, cfg in AREA_GEOFENCE.items():
        center_lat, center_lon = cfg["center"]
        radius_km = cfg["radius_km"]
        distance = haversine_km(lat, lon, center_lat, center_lon)
        if distance <= radius_km:
            matches.append((area, distance))

    if not matches:
        return None

    matches.sort(key=lambda x: x[1])
    return matches[0][0]


def format_summary_text():
    cleanup_expired_checkins()

    rows = cur.execute(
        "SELECT area, vibe, COUNT(*) FROM checkins GROUP BY area, vibe ORDER BY area, vibe"
    ).fetchall()

    if not rows:
        return (
            "👀 Pulse Bangkok — Live Now\n\n"
            "Not much activity yet in the pilot areas.\n\n"
            "Want to show up in the next update?\n"
            "Check in through the bot."
        )

    grouped = {}
    for area, vibe, count in rows:
        grouped.setdefault(area, [])
        grouped[area].append((vibe, count))

    vibe_map = {
        "Work": "working",
        "Social": "social",
        "Chill": "chilling",
        "Explore": "exploring",
        "Drinks": "drinks",
    }

    lines = ["🔥 Pulse Bangkok — Live Now", ""]
    for area in grouped:
        lines.append(area)
        for vibe, count in grouped[area]:
            lines.append(f"• {count} {vibe_map.get(vibe, vibe.lower())}")
        lines.append("")

    lines.append("Check in through the bot to appear in the next update.")
    return "\n".join(lines)


# ================= USER FLOW =================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Welcome to Pulse Bangkok\n\nSee what’s happening around you.",
        reply_markup=main_menu()
    )


async def safety(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Safety rules:\n"
        "• Share area only\n"
        "• No exact location\n"
        "• Public places only\n"
        "• End your check-in anytime",
        reply_markup=main_menu()
    )


async def checkin_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    row = cur.execute("SELECT name FROM users WHERE id = ?", (user_id,)).fetchone()

    if row and row[0]:
        context.user_data["name"] = row[0]
        await update.message.reply_text("Where are you around?", reply_markup=area_menu())
        return ASK_AREA

    await update.message.reply_text(
        "What should we call you?\n\nUse a simple English first name or nickname."
    )
    return ASK_NAME


async def get_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = (update.message.text or "").strip()

    if not validate_name(name):
        await update.message.reply_text("Invalid name. Use simple English letters only.")
        return ASK_NAME

    user_id = update.effective_user.id
    context.user_data["name"] = name
    cur.execute("INSERT OR REPLACE INTO users (id, name) VALUES (?, ?)", (user_id, name))
    conn.commit()

    await update.message.reply_text("Where are you around?", reply_markup=area_menu())
    return ASK_AREA


async def get_area(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.location:
        lat = update.message.location.latitude
        lon = update.message.location.longitude
        suggested = suggest_area_from_location(lat, lon)
        context.user_data["area"] = suggested if suggested else "Sukhumvit"
    else:
        area = (update.message.text or "").strip()
        if area not in AREAS:
            await update.message.reply_text("Please choose a valid area.", reply_markup=area_menu())
            return ASK_AREA
        context.user_data["area"] = area

    await update.message.reply_text("What’s your vibe?", reply_markup=vibe_menu())
    return ASK_VIBE


async def get_vibe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    vibe = (update.message.text or "").strip()

    if vibe not in VIBES:
        await update.message.reply_text("Please choose a valid vibe.", reply_markup=vibe_menu())
        return ASK_VIBE

    context.user_data["vibe"] = vibe
    await update.message.reply_text("How long?", reply_markup=duration_menu())
    return ASK_DURATION


async def finish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    duration = (update.message.text or "").strip()

    if duration not in DURATIONS:
        await update.message.reply_text("Please choose a valid duration.", reply_markup=duration_menu())
        return ASK_DURATION

    user_id = update.effective_user.id
    area = context.user_data["area"]
    vibe = context.user_data["vibe"]
    expires = compute_expiry(now(), duration)

    cleanup_expired_checkins()
    cur.execute("DELETE FROM checkins WHERE user_id = ?", (user_id,))
    cur.execute(
        "INSERT INTO checkins (user_id, area, vibe, expires_at) VALUES (?, ?, ?, ?)",
        (user_id, area, vibe, expires.isoformat()),
    )
    conn.commit()

    await update.message.reply_text(
        f"Checked in ✅\n\nArea: {area}\nVibe: {vibe}\nDuration: {duration}",
        reply_markup=main_menu()
    )
    return ConversationHandler.END


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    cleanup_expired_checkins()

    row = cur.execute(
        "SELECT area, vibe, expires_at FROM checkins WHERE user_id = ?",
        (user_id,),
    ).fetchone()

    if not row:
        await update.message.reply_text("No active check-in.", reply_markup=main_menu())
        return

    area, vibe, expires_at = row
    expires_time = datetime.fromisoformat(expires_at).strftime("%H:%M")

    await update.message.reply_text(
        f"Your status:\n\nArea: {area}\nVibe: {vibe}\nActive until: {expires_time}",
        reply_markup=main_menu()
    )


async def end(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    cur.execute("DELETE FROM checkins WHERE user_id = ?", (user_id,))
    conn.commit()

    await update.message.reply_text("Check-in ended.", reply_markup=main_menu())


# ================= ADMIN =================
async def admin_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return

    await update.message.reply_text(
        "Admin commands:\n\n"
        "/admin_help\n"
        "/admin_stats\n"
        "/admin_ignite Area|Vibe|Duration|Count\n"
        "Example: /admin_ignite Ari|Work|2 hours|3\n"
        "/admin_clear_ignite"
    )


async def admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return

    cleanup_expired_checkins()
    rows = cur.execute(
        "SELECT area, COUNT(*) FROM checkins GROUP BY area ORDER BY area"
    ).fetchall()

    if not rows:
        await update.message.reply_text("Stats:\nNo active check-ins.")
        return

    text = "Stats:\n"
    for area, count in rows:
        text += f"{area}: {count}\n"

    await update.message.reply_text(text)


async def admin_ignite(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return

    try:
        raw = update.message.text.replace("/admin_ignite", "", 1).strip()
        parts = [p.strip() for p in raw.split("|")]

        if len(parts) != 4:
            await update.message.reply_text(
                "Usage: /admin_ignite Area|Vibe|Duration|Count\n"
                "Example: /admin_ignite Ari|Work|2 hours|3"
            )
            return

        area, vibe, duration_text, count_text = parts

        if area not in AREAS:
            await update.message.reply_text(f"Invalid area. Use one of: {', '.join(AREAS)}")
            return

        if vibe not in VIBES:
            await update.message.reply_text(f"Invalid vibe. Use one of: {', '.join(VIBES)}")
            return

        if duration_text not in DURATIONS:
            await update.message.reply_text(f"Invalid duration. Use one of: {', '.join(DURATIONS)}")
            return

        count = int(count_text)
        if count < 1 or count > 50:
            await update.message.reply_text("Count must be between 1 and 50.")
            return

        cleanup_expired_checkins()
        expires = compute_expiry(now(), duration_text).isoformat()

        base_id = int(now().timestamp())
        for i in range(count):
            fake_user_id = 900000000 + base_id + i
            cur.execute(
                "INSERT INTO checkins (user_id, area, vibe, expires_at) VALUES (?, ?, ?, ?)",
                (fake_user_id, area, vibe, expires)
            )

        conn.commit()

        await update.message.reply_text(
            f"Ignited 🔥\nArea: {area}\nVibe: {vibe}\nDuration: {duration_text}\nCount: {count}"
        )

    except ValueError:
        await update.message.reply_text("Count must be a number.")
    except Exception as e:
        await update.message.reply_text(f"Error: {str(e)}")


async def admin_clear_ignite(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return

    cur.execute("""
        DELETE FROM checkins
        WHERE user_id NOT IN (SELECT id FROM users)
    """)
    conn.commit()

    await update.message.reply_text("Ignited activity cleared.")


# ================= SUMMARY JOB =================
async def post_summary(context: ContextTypes.DEFAULT_TYPE):
    if not CHANNEL_ID:
        return

    try:
        text = format_summary_text()
        await context.bot.send_message(chat_id=CHANNEL_ID, text=text)
    except Exception as e:
        print(f"Summary send failed: {e}")


# ================= MAIN =================
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[MessageHandler(filters.TEXT & filters.Regex("^Check in$"), checkin_start)],
        states={
            ASK_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_name)],
            ASK_AREA: [MessageHandler((filters.TEXT | filters.LOCATION) & ~filters.COMMAND, get_area)],
            ASK_VIBE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_vibe)],
            ASK_DURATION: [MessageHandler(filters.TEXT & ~filters.COMMAND, finish)],
        },
        fallbacks=[],
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin_help", admin_help))
    app.add_handler(CommandHandler("admin_stats", admin_stats))
    app.add_handler(CommandHandler("admin_ignite", admin_ignite))
    app.add_handler(CommandHandler("admin_clear_ignite", admin_clear_ignite))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("^Safety rules$"), safety))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("^My status$"), status))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("^End check-in$"), end))
    app.add_handler(conv)

    if app.job_queue:
        app.job_queue.run_repeating(post_summary, interval=SUMMARY_INTERVAL_MIN * 60, first=60)

    app.run_polling()


if __name__ == "__main__":
    main()
