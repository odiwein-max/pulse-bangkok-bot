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
SUMMARY_INTERVAL_MIN = int(os.getenv("SUMMARY_INTERVAL_MIN", "60"))

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

BLACKLIST_NAMES = {
    "admin",
    "support",
    "bot",
    "anonymous",
    "unknown",
    "test",
    "null",
    "none",
    "system",
    "moderator",
}

AREA_GEOFENCE = {
    "Sukhumvit": {"center": (13.7370, 100.5600), "radius_km": 2.0},
    "Thonglor": {"center": (13.7308, 100.5810), "radius_km": 1.0},
    "Ekkamai": {"center": (13.7197, 100.5856), "radius_km": 1.0},
    "Ari": {"center": (13.7799, 100.5450), "radius_km": 1.2},
    "Silom / Sathorn": {"center": (13.7240, 100.5300), "radius_km": 1.5},
    "Khao San / Old Town": {"center": (13.7589, 100.4970), "radius_km": 1.0},
    "Chinatown": {"center": (13.7396, 100.5098), "radius_km": 1.0},
}

# ================= DATABASE =================
conn = sqlite3.connect("db.sqlite3", check_same_thread=False)
conn.row_factory = sqlite3.Row
cur = conn.cursor()

cur.execute(
    """
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY,
        name TEXT
    )
    """
)

cur.execute(
    """
    CREATE TABLE IF NOT EXISTS checkins (
        user_id INTEGER,
        area TEXT,
        vibe TEXT,
        expires_at TEXT,
        source TEXT DEFAULT 'user'
    )
    """
)

cur.execute(
    """
    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT
    )
    """
)

# Migrations / defaults
cur.execute("PRAGMA table_info(checkins)")
cols = [c[1] for c in cur.fetchall()]
if "source" not in cols:
    cur.execute("ALTER TABLE checkins ADD COLUMN source TEXT DEFAULT 'user'")

cur.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('autopilot_enabled', 'on')")
conn.commit()

# ================= HELPERS =================
def now() -> datetime:
    return datetime.now(BANGKOK_TZ)


def compute_expiry(start: datetime, label: str) -> datetime:
    if label == "30 min":
        return start + timedelta(minutes=30)
    if label == "1 hour":
        return start + timedelta(hours=1)
    if label == "2 hours":
        return start + timedelta(hours=2)
    if label == "Tonight":
        tonight = start.replace(hour=23, minute=59, second=0, microsecond=0)
        if tonight <= start:
            tonight = start + timedelta(hours=4)
        return tonight
    return start + timedelta(hours=1)


def is_admin(user_id: int) -> bool:
    return str(user_id) in ADMIN_IDS


def cleanup() -> None:
    cur.execute("DELETE FROM checkins WHERE expires_at <= ?", (now().isoformat(),))
    conn.commit()


def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            ["🗺️ Open Map", "Check in"],
            ["My status", "End check-in"],
            ["Safety rules"],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def area_menu() -> ReplyKeyboardMarkup:
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


def vibe_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([[v] for v in VIBES], resize_keyboard=True, one_time_keyboard=True)


def duration_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([[d] for d in DURATIONS], resize_keyboard=True, one_time_keyboard=True)


def validate_name(name: str) -> bool:
    name = (name or "").strip()
    if len(name) < 2 or len(name) > 20:
        return False
    if name.lower() in BLACKLIST_NAMES:
        return False
    if not name.replace(" ", "").isalpha():
        return False
    return True


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
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


def suggest_area_from_location(lat: float, lon: float):
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


def get_setting(key: str, default=None):
    row = cur.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    cur.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
    conn.commit()


def autopilot_enabled() -> bool:
    return get_setting("autopilot_enabled", "on") == "on"


def summary_buttons(bot_username: str) -> InlineKeyboardMarkup:
    rows = []
    if LIVE_MAP_URL:
        rows.append([InlineKeyboardButton("Open Live Map 🗺️", url=LIVE_MAP_URL)])
    rows.append([InlineKeyboardButton("Check in on Telegram", url=f"https://t.me/{bot_username}")])
    return InlineKeyboardMarkup(rows)


async def send_private_checkin_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bot_username = context.bot.username
    await update.message.reply_text(
        "Want to appear in the next Pulse update?\n\nOpen the live map or check in below 👇",
        reply_markup=summary_buttons(bot_username),
    )


# ================= AUTOPILOT =================
def get_time_bucket() -> str:
    hour = now().hour
    if 7 <= hour < 11:
        return "morning"
    if 11 <= hour < 16:
        return "midday"
    if 16 <= hour < 20:
        return "evening"
    if 20 <= hour <= 23 or 0 <= hour < 3:
        return "night"
    return "late_night"


def get_autopilot_profile():
    bucket = get_time_bucket()

    if bucket == "morning":
        return {
            "target_min": 22,
            "target_max": 30,
            "mix": [
                ("Ari", "Work", 6),
                ("Ari", "Chill", 3),
                ("Sukhumvit", "Work", 5),
                ("Ekkamai", "Work", 4),
                ("Ekkamai", "Chill", 3),
                ("Silom / Sathorn", "Work", 4),
                ("Chinatown", "Explore", 1),
            ],
        }

    if bucket == "midday":
        return {
            "target_min": 20,
            "target_max": 28,
            "mix": [
                ("Ari", "Work", 4),
                ("Sukhumvit", "Work", 4),
                ("Silom / Sathorn", "Work", 4),
                ("Chinatown", "Explore", 4),
                ("Khao San / Old Town", "Explore", 3),
                ("Ekkamai", "Chill", 3),
                ("Thonglor", "Social", 2),
                ("Sukhumvit", "Explore", 2),
            ],
        }

    if bucket == "evening":
        return {
            "target_min": 24,
            "target_max": 34,
            "mix": [
                ("Thonglor", "Social", 6),
                ("Ekkamai", "Social", 4),
                ("Ekkamai", "Drinks", 3),
                ("Silom / Sathorn", "Social", 4),
                ("Ari", "Chill", 3),
                ("Chinatown", "Explore", 4),
                ("Sukhumvit", "Explore", 3),
                ("Khao San / Old Town", "Explore", 2),
            ],
        }

    if bucket == "night":
        return {
            "target_min": 26,
            "target_max": 38,
            "mix": [
                ("Thonglor", "Drinks", 6),
                ("Thonglor", "Social", 5),
                ("Ekkamai", "Drinks", 4),
                ("Khao San / Old Town", "Social", 6),
                ("Khao San / Old Town", "Explore", 3),
                ("Chinatown", "Explore", 4),
                ("Chinatown", "Drinks", 2),
                ("Sukhumvit", "Social", 4),
            ],
        }

    return {
        "target_min": 3,
        "target_max": 6,
        "mix": [
            ("Sukhumvit", "Chill", 3),
            ("Thonglor", "Chill", 2),
            ("Khao San / Old Town", "Drinks", 1),
            ("Ari", "Chill", 1),
        ],
    }


def count_active_by_source():
    cleanup()
    rows = cur.execute("SELECT source, COUNT(*) as count FROM checkins GROUP BY source").fetchall()
    data = {"user": 0, "ignite": 0, "auto": 0}
    for row in rows:
        data[row["source"]] = row["count"]
    return data


def clear_auto_checkins() -> int:
    cur.execute("DELETE FROM checkins WHERE source = 'auto'")
    deleted = cur.rowcount
    conn.commit()
    return deleted


def create_auto_checkins() -> int:
    if not autopilot_enabled():
        return 0

    cleanup()
    counts = count_active_by_source()
    real_count = counts.get("user", 0)

    profile = get_autopilot_profile()
    target = random.randint(profile["target_min"], profile["target_max"])

    clear_auto_checkins()

    if real_count >= 30:
        return 0

    needed = max(0, target - real_count)
    if needed <= 0:
        return 0

    weighted_pool = []
    for area, vibe, weight in profile["mix"]:
        weighted_pool.extend([(area, vibe)] * weight)

    created = 0
    base = int(now().timestamp())

    for i in range(needed):
        area, vibe = random.choice(weighted_pool)

        bucket = get_time_bucket()
        if bucket == "morning":
            duration_label = random.choice(["30 min", "1 hour", "1 hour", "2 hours"])
        elif bucket == "midday":
            duration_label = random.choice(["30 min", "1 hour", "2 hours"])
        elif bucket == "evening":
            duration_label = random.choice(["1 hour", "2 hours", "2 hours"])
        elif bucket == "night":
            duration_label = random.choice(["1 hour", "2 hours", "Tonight"])
        else:
            duration_label = random.choice(["30 min", "1 hour"])

        expires = compute_expiry(now(), duration_label).isoformat()
        fake_user_id = 800000000 + base + i

        cur.execute(
            "INSERT INTO checkins (user_id, area, vibe, expires_at, source) VALUES (?, ?, ?, ?, ?)",
            (fake_user_id, area, vibe, expires, "auto"),
        )
        created += 1

    conn.commit()
    return created


# ================= SUMMARY STYLE =================
def vibe_emoji(vibe: str) -> str:
    return {
        "Work": "💻",
        "Social": "🧑‍🤝‍🧑",
        "Chill": "☕",
        "Explore": "🗺️",
        "Drinks": "🍻",
    }.get(vibe, "•")


def vibe_label(vibe: str) -> str:
    return {
        "Work": "working",
        "Social": "social",
        "Chill": "chilling",
        "Explore": "exploring",
        "Drinks": "out for drinks",
    }.get(vibe, vibe.lower())


def area_status(total: int) -> str:
    if total == 0:
        return "quiet"
    if total <= 2:
        return "starting"
    if total <= 5:
        return "active"
    return "hot"


def format_summary_text() -> str:
    cleanup()
    rows = cur.execute(
        "SELECT area, vibe, COUNT(*) as count FROM checkins GROUP BY area, vibe ORDER BY area, vibe"
    ).fetchall()

    total_row = cur.execute("SELECT COUNT(*) as total FROM checkins").fetchone()
    total_count = total_row["total"] if total_row else 0

    if not rows:
        return (
            "👀 Pulse Bangkok — Live Now\n\n"
            "The city feels quiet right now.\n\n"
            "Want to appear in the next update?\n"
            "👇 Open the live map or check in below"
        )

    grouped = {}
    for row in rows:
        area = row["area"]
        vibe = row["vibe"]
        count = row["count"]
        grouped.setdefault(area, [])
        grouped[area].append((vibe, count))

    active_areas = len(grouped)

    intro_options = [
        f"👀 {total_count} people live right now across {active_areas} Bangkok areas",
        f"🔥 Bangkok is moving right now — {total_count} people currently checked in",
        f"📍 Live Pulse update — {total_count} people active right now",
    ]
    intro = random.choice(intro_options)

    lines = ["🔥 Pulse Bangkok — Live Now", "", intro, ""]

    for area, vibes in grouped.items():
        area_total = sum(count for _, count in vibes)
        status = area_status(area_total)

        if status == "hot":
            header = f"{area} is 🔥 hot right now"
        elif status == "active":
            header = f"{area} is active"
        elif status == "starting":
            header = f"{area} is starting up"
        else:
            header = f"{area} is quiet for now 👀"

        lines.append(header + ":")

        vibes_sorted = sorted(vibes, key=lambda x: x[1], reverse=True)
        for vibe, count in vibes_sorted:
            emoji = vibe_emoji(vibe)
            label = vibe_label(vibe)
            lines.append(f"• {count} {label} {emoji}")

        lines.append("")

    if active_areas >= 4:
        lines.append("Bangkok feels pretty alive right now.")
    elif active_areas >= 2:
        lines.append("A few areas are starting to pick up.")
    else:
        lines.append("Most of the action is still concentrated in one zone.")

    lines.append("")
    lines.append("👇 Open the live map or check in below")
    return "\n".join(lines)


# ================= API =================
app_flask = Flask(__name__)


@app_flask.after_request
def add_cors_headers(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return response


@app_flask.route("/api/health", methods=["GET", "OPTIONS"])
def api_health():
    return jsonify({"ok": True, "service": "pulse-bangkok-bot"})


@app_flask.route("/api/heatmap", methods=["GET", "OPTIONS"])
def api_heatmap():
    cleanup()

    rows = cur.execute(
        """
        SELECT area, vibe, COUNT(*) as count
        FROM checkins
        GROUP BY area, vibe
        """
    ).fetchall()

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
    for area in AREAS:
        lat, lng = AREA_GEOFENCE[area]["center"]
        radius_km = AREA_GEOFENCE[area]["radius_km"]

        if area in grouped:
            result.append(
                {
                    "area": area,
                    "lat": lat,
                    "lng": lng,
                    "radius_km": radius_km,
                    "total": grouped[area]["total"],
                    "vibes": grouped[area]["vibes"],
                }
            )
        else:
            result.append(
                {
                    "area": area,
                    "lat": lat,
                    "lng": lng,
                    "radius_km": radius_km,
                    "total": 0,
                    "vibes": {},
                }
            )

    return jsonify(
        {
            "updated_at": now().isoformat(),
            "areas": result,
        }
    )


def run_flask():
    port = int(os.environ.get("PORT", 8080))
    app_flask.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)


# ================= USER FLOW =================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_type = update.effective_chat.type
    if chat_type in ("group", "supergroup"):
        await send_private_checkin_prompt(update, context)
        return

    user_id = update.effective_user.id
    row = cur.execute("SELECT name FROM users WHERE id = ?", (user_id,)).fetchone()

    if row and row["name"]:
        text = f"Welcome back, {row['name']} 👋\n\nOpen the map or use the menu below."
    else:
        text = "Welcome to Pulse Bangkok 👋\n\nOpen the map or use the menu below to check in."

    await update.message.reply_text(
        text,
        reply_markup=main_menu(),
    )

    if LIVE_MAP_URL:
        await update.message.reply_text(
            "Quick access 👇",
            reply_markup=summary_buttons(context.bot.username),
        )


async def open_map(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if LIVE_MAP_URL:
        await update.message.reply_text(
            f"Open the live map:\n{LIVE_MAP_URL}",
            reply_markup=main_menu(),
        )
    else:
        await update.message.reply_text(
            "Map URL not configured.",
            reply_markup=main_menu(),
        )


async def safety(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Safety rules:\n"
        "• Share area only\n"
        "• No exact location\n"
        "• Public places only\n"
        "• End your check-in anytime",
        reply_markup=main_menu(),
    )


async def checkin_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_type = update.effective_chat.type
    if chat_type in ("group", "supergroup"):
        await send_private_checkin_prompt(update, context)
        return ConversationHandler.END

    user_id = update.effective_user.id
    row = cur.execute("SELECT name FROM users WHERE id = ?", (user_id,)).fetchone()

    if row and row["name"]:
        context.user_data["name"] = row["name"]
        await update.message.reply_text("Where are you around? 👇", reply_markup=area_menu())
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

    await update.message.reply_text("Where are you around? 👇", reply_markup=area_menu())
    return ASK_AREA


async def get_area(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.location:
        lat = update.message.location.latitude
        lon = update.message.location.longitude
        suggested = suggest_area_from_location(lat, lon)
        context.user_data["area"] = suggested if suggested else "Sukhumvit"
    else:
        text = (update.message.text or "").strip()

        if text not in AREAS:
            await update.message.reply_text(
                "Please choose an area from the list 👇",
                reply_markup=area_menu(),
            )
            return ASK_AREA

        context.user_data["area"] = text

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
        await update.message.reply_text(
            "Please choose a valid duration.",
            reply_markup=duration_menu(),
        )
        return ASK_DURATION

    user_id = update.effective_user.id
    area = context.user_data["area"]
    vibe = context.user_data["vibe"]
    expires = compute_expiry(now(), duration)

    cleanup()
    cur.execute("DELETE FROM checkins WHERE user_id = ?", (user_id,))
    cur.execute(
        "INSERT INTO checkins (user_id, area, vibe, expires_at, source) VALUES (?, ?, ?, ?, ?)",
        (user_id, area, vibe, expires.isoformat(), "user"),
    )
    conn.commit()

    await update.message.reply_text(
        f"Checked in ✅\n\nArea: {area}\nVibe: {vibe}\nDuration: {duration}",
        reply_markup=main_menu(),
    )
    return ConversationHandler.END


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    cleanup()

    row = cur.execute(
        "SELECT area, vibe, expires_at FROM checkins WHERE user_id = ?",
        (user_id,),
    ).fetchone()

    if not row:
        await update.message.reply_text("No active check-in.", reply_markup=main_menu())
        return

    area, vibe, expires_at = row["area"], row["vibe"], row["expires_at"]
    expires_time = datetime.fromisoformat(expires_at).strftime("%H:%M")

    await update.message.reply_text(
        f"Your status:\n\nArea: {area}\nVibe: {vibe}\nActive until: {expires_time}",
        reply_markup=main_menu(),
    )


async def end(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    cur.execute("DELETE FROM checkins WHERE user_id = ?", (user_id,))
    conn.commit()
    await update.message.reply_text("Check-in ended.", reply_markup=main_menu())


# ================= GROUP ONBOARDING =================
async def welcome_new_members(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type not in ("group", "supergroup"):
        return

    if not update.message or not update.message.new_chat_members:
        return

    bot_id = context.bot.id
    new_members = update.message.new_chat_members
    human_members = [m for m in new_members if not m.is_bot and m.id != bot_id]

    if not human_members:
        return

    bot_username = context.bot.username
    text = (
        "Welcome to Pulse Bangkok 👋\n\n"
        "This group shows what’s happening around the city in real time.\n\n"
        "👇 Open the live map or check in below:"
    )

    await update.message.reply_text(
        text,
        reply_markup=summary_buttons(bot_username),
    )


# ================= ADMIN =================
async def admin_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return

    await update.message.reply_text(
        "Admin commands:\n\n"
        "/admin_help\n"
        "/admin_stats\n"
        "/admin_ignite Area|Vibe|Duration|Count\n"
        "/admin_clear_ignite\n"
        "/admin_clear_auto\n"
        "/admin_reset_checkins\n"
        "/admin_toggle_autopilot on\n"
        "/admin_toggle_autopilot off\n"
        "/admin_autopilot_status\n"
        "/admin_run_autopilot\n"
        "/get_chat_id"
    )


async def admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return

    cleanup()

    rows = cur.execute(
        "SELECT area, source, COUNT(*) as count FROM checkins GROUP BY area, source ORDER BY area, source"
    ).fetchall()

    totals = cur.execute(
        "SELECT source, COUNT(*) as count FROM checkins GROUP BY source ORDER BY source"
    ).fetchall()

    if not rows:
        await update.message.reply_text("Stats:\nNo active check-ins.")
        return

    lines = ["Stats:", ""]
    lines.append("Totals by source:")
    total_map = {"user": 0, "auto": 0, "ignite": 0}
    for row in totals:
        total_map[row["source"]] = row["count"]

    lines.append(f"• Real users: {total_map['user']}")
    lines.append(f"• Autopilot: {total_map['auto']}")
    lines.append(f"• Ignite: {total_map['ignite']}")
    lines.append("")

    grouped = {}
    for row in rows:
        area = row["area"]
        source = row["source"]
        count = row["count"]
        grouped.setdefault(area, {"user": 0, "auto": 0, "ignite": 0})
        grouped[area][source] = count

    for area, data in grouped.items():
        lines.append(area)
        lines.append(f"• Real: {data['user']}")
        lines.append(f"• Auto: {data['auto']}")
        lines.append(f"• Ignite: {data['ignite']}")
        lines.append("")

    await update.message.reply_text("\n".join(lines))


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

        cleanup()
        expires = compute_expiry(now(), duration_text).isoformat()
        base = int(now().timestamp())

        for i in range(count):
            fake_user_id = 900000000 + base + i
            cur.execute(
                "INSERT INTO checkins (user_id, area, vibe, expires_at, source) VALUES (?, ?, ?, ?, ?)",
                (fake_user_id, area, vibe, expires, "ignite"),
            )

        conn.commit()
        await update.message.reply_text(
            f"Ignited 🔥\nArea: {area}\nVibe: {vibe}\nDuration: {duration_text}\nCount: {count}"
        )

    except Exception as e:
        await update.message.reply_text(f"Error: {str(e)}")


async def admin_clear_ignite(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return

    cur.execute("DELETE FROM checkins WHERE source = 'ignite'")
    deleted = cur.rowcount
    conn.commit()
    await update.message.reply_text(f"Deleted {deleted} ignite check-ins.")


async def admin_clear_auto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return

    deleted = clear_auto_checkins()
    await update.message.reply_text(f"Deleted {deleted} autopilot check-ins.")


async def admin_reset_checkins(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return

    cur.execute("DELETE FROM checkins")
    deleted = cur.rowcount
    conn.commit()
    await update.message.reply_text(f"All check-ins cleared. Deleted {deleted} total rows.")


async def admin_toggle_autopilot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return

    raw = update.message.text.replace("/admin_toggle_autopilot", "", 1).strip().lower()
    if raw not in {"on", "off"}:
        await update.message.reply_text("Usage: /admin_toggle_autopilot on OR /admin_toggle_autopilot off")
        return

    set_setting("autopilot_enabled", raw)
    await update.message.reply_text(f"Autopilot is now {raw}.")


async def admin_autopilot_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return

    profile = get_autopilot_profile()
    counts = count_active_by_source()
    enabled = "on" if autopilot_enabled() else "off"

    await update.message.reply_text(
        f"Autopilot status: {enabled}\n"
        f"Time bucket: {get_time_bucket()}\n"
        f"Target range: {profile['target_min']}–{profile['target_max']}\n"
        f"Real users: {counts.get('user', 0)}\n"
        f"Auto: {counts.get('auto', 0)}\n"
        f"Ignite: {counts.get('ignite', 0)}"
    )


async def admin_run_autopilot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return

    created = create_auto_checkins()
    await update.message.reply_text(f"Autopilot ran. Created {created} auto check-ins.")


async def get_chat_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return

    chat = update.effective_chat
    await update.message.reply_text(f"Chat title: {chat.title}\nChat ID: {chat.id}")


# ================= SUMMARY JOB =================
async def summary_job(context: ContextTypes.DEFAULT_TYPE):
    if not CHANNEL_ID:
        return

    create_auto_checkins()

    try:
        text = format_summary_text()
        keyboard = summary_buttons(context.bot.username)

        await context.bot.send_message(
            chat_id=CHANNEL_ID,
            text=text,
            reply_markup=keyboard,
        )
    except Exception as e:
        print(f"Summary send failed: {e}")


# ================= MAIN =================
def main():
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()

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
    app.add_handler(CommandHandler("admin_clear_auto", admin_clear_auto))
    app.add_handler(CommandHandler("admin_reset_checkins", admin_reset_checkins))
    app.add_handler(CommandHandler("admin_toggle_autopilot", admin_toggle_autopilot))
    app.add_handler(CommandHandler("admin_autopilot_status", admin_autopilot_status))
    app.add_handler(CommandHandler("admin_run_autopilot", admin_run_autopilot))
    app.add_handler(CommandHandler("get_chat_id", get_chat_id))

    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, welcome_new_members))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("^Safety rules$"), safety))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("^My status$"), status))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("^End check-in$"), end))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("^🗺️ Open Map$"), open_map))
    app.add_handler(conv)

    if app.job_queue:
        app.job_queue.run_repeating(summary_job, interval=SUMMARY_INTERVAL_MIN * 60, first=10)

    app.run_polling()


if __name__ == "__main__":
    main()
