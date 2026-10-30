import os
import re
import time
import asyncio
import sqlite3
import logging

from dotenv import load_dotenv
from telegram import (
    BotCommand, BotCommandScopeChat, Update, InlineKeyboardButton, InlineKeyboardMarkup,
    InputMediaAudio, InputMediaDocument,
)
from telegram.error import BadRequest, Forbidden, RetryAfter
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ApplicationHandlerStop, ContextTypes, TypeHandler, filters,
)

BASE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE, ".env"))
DB_PATH = os.path.join(BASE, "episodes.db")
ADMINS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x}

MAX_RANGE = 50       # max episodes sent per request
SEND_DELAY = 1.0     # seconds between albums / single files
ALBUM_SIZE = 10      # Telegram allows up to 10 files in one album

NUM_RE = re.compile(r"^\s*(\d+)\s*(?:(?:-|–|to)\s*(\d+))?\s*$", re.I)
PIPE_RE = re.compile(r"^\s*(.+?)\s*\|\s*(\d+)\s*$")
EP_RE = re.compile(r"ep(?:isode)?\.?\s*[-_:#]?\s*(\d+)", re.I)   # "Ep - 01", "Ep 02", "Episode 3"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


# ---------- database ----------
def db():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    c = db()
    c.executescript("""
        CREATE TABLE IF NOT EXISTS series(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE COLLATE NOCASE NOT NULL
        );
        CREATE TABLE IF NOT EXISTS episodes(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            series_id INTEGER NOT NULL,
            number INTEGER NOT NULL,
            file_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            UNIQUE(series_id, number)
        );
        CREATE TABLE IF NOT EXISTS viewing(
            user_id INTEGER PRIMARY KEY,
            series_id INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS uploading(
            user_id INTEGER PRIMARY KEY,
            series_id INTEGER NOT NULL,
            next_number INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS users(
            user_id INTEGER PRIMARY KEY,
            name TEXT,
            username TEXT,
            joined TEXT DEFAULT CURRENT_TIMESTAMP,
            active INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS settings(
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS deletions(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            delete_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_deletions_at ON deletions(delete_at);
        CREATE TABLE IF NOT EXISTS authorized(
            user_id INTEGER PRIMARY KEY,
            name TEXT,
            added TEXT DEFAULT CURRENT_TIMESTAMP,
            expires_at REAL
        );
    """)
    try:
        c.execute("ALTER TABLE series ADD COLUMN hits INTEGER NOT NULL DEFAULT 0")
    except sqlite3.OperationalError:
        pass  # column already exists
    c.commit()
    c.close()


def is_admin_id(uid) -> bool:
    return uid in ADMINS


def is_admin(update: Update) -> bool:
    return update.effective_user is not None and update.effective_user.id in ADMINS


def info_text(sid):
    c = db()
    s = c.execute("SELECT name FROM series WHERE id=?", (sid,)).fetchone()
    if not s:
        c.close()
        return None
    st = c.execute(
        "SELECT COUNT(*) AS n, MIN(number) AS lo, MAX(number) AS hi FROM episodes WHERE series_id=?",
        (sid,),
    ).fetchone()
    c.close()
    if st["n"] == 0:
        return f"🎧 {s['name']}\nNo episodes yet."
    return (
        f"🎧 {s['name']}\n"
        f"📚 Episodes available: {st['n']} (No. {st['lo']} to {st['hi']})\n\n"
        f"Send an episode number, like: 350\n"
        f"or a range, like: 350-370\n"
        f"and I'll send them."
    )


def set_viewing(uid, sid):
    c = db()
    c.execute("INSERT OR REPLACE INTO viewing(user_id, series_id) VALUES(?,?)", (uid, sid))
    c.commit()
    c.close()


def get_viewing(uid):
    c = db()
    r = c.execute("SELECT series_id FROM viewing WHERE user_id=?", (uid,)).fetchone()
    c.close()
    return r["series_id"] if r else None


# ---------- user side ----------
async def do_search(update: Update, query: str):
    c = db()
    found = c.execute(
        "SELECT id, name FROM series WHERE name LIKE ? ORDER BY name LIMIT 20",
        (f"%{query}%",),
    ).fetchall()
    c.close()

    if not found:
        await update.message.reply_text("No series found. Try another name.")
        return
    if len(found) == 1:
        set_viewing(update.effective_user.id, found[0]["id"])
        await update.message.reply_text(info_text(found[0]["id"]))
        return
    buttons = [[InlineKeyboardButton(r["name"], callback_data=f"s:{r['id']}")] for r in found]
    await update.message.reply_text("Pick a series:", reply_markup=InlineKeyboardMarkup(buttons))


def get_setting(key, default):
    c = db()
    r = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    c.close()
    return r["value"] if r else default


def set_setting(key, value):
    c = db()
    c.execute("INSERT OR REPLACE INTO settings(key, value) VALUES(?,?)", (key, str(value)))
    c.commit()
    c.close()


def protect_on() -> bool:
    return get_setting("protect", "1") == "1"      # default: ON


def autodelete_minutes() -> int:
    try:
        return max(0, int(get_setting("autodelete", "0")))   # default: OFF
    except ValueError:
        return 0


def fmt_minutes(m: int) -> str:
    if m % 60 == 0:
        h = m // 60
        return f"{h} hour" + ("s" if h != 1 else "")
    return f"{m} minute" + ("s" if m != 1 else "")


def schedule_deletions(chat_id, message_ids, minutes):
    if not message_ids or minutes <= 0:
        return
    t = time.time() + minutes * 60
    c = db()
    c.executemany(
        "INSERT INTO deletions(chat_id, message_id, delete_at) VALUES(?,?,?)",
        [(chat_id, i, t) for i in message_ids],
    )
    c.commit()
    c.close()


async def cleanup_loop(app: Application):
    """Every 30 seconds, delete the messages whose time is up (survives bot restarts)."""
    while True:
        try:
            await asyncio.sleep(30)
            c = db()
            rows = c.execute(
                "SELECT id, chat_id, message_id FROM deletions WHERE delete_at <= ? ORDER BY id LIMIT 300",
                (time.time(),),
            ).fetchall()
            c.close()
            done = []
            for r in rows:
                try:
                    await app.bot.delete_message(r["chat_id"], r["message_id"])
                except RetryAfter as e:
                    await asyncio.sleep(e.retry_after + 1)
                    break                      # try the rest on the next round
                except Exception:
                    pass                       # already deleted, too old, or chat blocked
                done.append((r["id"],))
                await asyncio.sleep(0.05)
            if done:
                c = db()
                c.executemany("DELETE FROM deletions WHERE id=?", done)
                c.commit()
                c.close()
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.exception("cleanup loop error")


async def send_episode(bot, chat_id, name, row, protect=False):
    cap = f"{name} - Episode {row['number']}"
    if row["kind"] == "audio":
        return await bot.send_audio(chat_id, row["file_id"], caption=cap, protect_content=protect)
    if row["kind"] == "voice":
        return await bot.send_voice(chat_id, row["file_id"], caption=cap, protect_content=protect)
    return await bot.send_document(chat_id, row["file_id"], caption=cap, protect_content=protect)


async def with_retry(fn):
    try:
        return await fn()
    except RetryAfter as e:
        await asyncio.sleep(e.retry_after + 1)
        return await fn()


def make_chunks(rows):
    """Group consecutive episodes of the same kind into albums of up to 10 (voice is sent alone)."""
    out, cur = [], []
    for r in rows:
        if cur and (r["kind"] != cur[0]["kind"] or r["kind"] == "voice" or len(cur) >= ALBUM_SIZE):
            out.append(cur)
            cur = []
        cur.append(r)
    if cur:
        out.append(cur)
    return out


async def send_chunk(bot, chat_id, name, chunk, protect=False):
    """Send up to 10 episodes as one album. Falls back to one-by-one.
    Returns (failed episode numbers, sent message ids)."""
    failed, sent = [], []
    kind = chunk[0]["kind"]
    if len(chunk) > 1 and kind in ("audio", "document"):
        cls = InputMediaAudio if kind == "audio" else InputMediaDocument
        media = [cls(media=r["file_id"], caption=f"{name} - Episode {r['number']}") for r in chunk]
        try:
            msgs = await with_retry(lambda: bot.send_media_group(chat_id, media, protect_content=protect))
            sent.extend(m.message_id for m in msgs)
            return failed, sent
        except Exception:
            logging.exception("album failed, sending one by one")
    for r in chunk:
        try:
            m = await with_retry(lambda r=r: send_episode(bot, chat_id, name, r, protect))
            sent.append(m.message_id)
        except Exception:
            logging.exception("send failed for episode %s", r["number"])
            failed.append(r["number"])
        await asyncio.sleep(SEND_DELAY)
    return failed, sent


async def send_range(update: Update, ctx: ContextTypes.DEFAULT_TYPE, sid, a, b):
    if a > b:
        a, b = b, a
    msg = update.message
    c = db()
    s = c.execute("SELECT name FROM series WHERE id=?", (sid,)).fetchone()
    if not s:
        c.close()
        await msg.reply_text("That series is no longer available. Please search again.")
        return

    if b - a + 1 > MAX_RANGE:
        b = a + MAX_RANGE - 1
        await msg.reply_text(
            f"⚠️ Max {MAX_RANGE} episodes at a time. Sending {a}-{b}.\n"
            f"Send {b + 1}-{b + MAX_RANGE} for the next batch."
        )

    rows = c.execute(
        "SELECT number, file_id, kind FROM episodes "
        "WHERE series_id=? AND number BETWEEN ? AND ? ORDER BY number",
        (sid, a, b),
    ).fetchall()
    c.close()

    if not rows:
        await msg.reply_text("Those episodes are not available.")
        return

    have = {r["number"] for r in rows}
    missing = [n for n in range(a, b + 1) if n not in have]

    protect = protect_on()
    mins = autodelete_minutes()
    for chunk in make_chunks(rows):
        failed, ids = await send_chunk(ctx.bot, msg.chat_id, s["name"], chunk, protect)
        missing += failed
        schedule_deletions(msg.chat_id, ids, mins)
        await asyncio.sleep(SEND_DELAY)

    if mins:
        try:
            note = await ctx.bot.send_message(
                msg.chat_id,
                f"⏳ For privacy, these episodes will be deleted from this chat in {fmt_minutes(mins)}. "
                "Listen now!",
                protect_content=protect,
            )
            schedule_deletions(msg.chat_id, [note.message_id], mins)
        except Exception:
            logging.warning("could not send the auto-delete notice")

    c = db()
    c.execute("UPDATE series SET hits = hits + 1 WHERE id=?", (sid,))
    c.commit()
    c.close()

    if missing:
        missing = sorted(set(missing))
        shown = ", ".join(str(n) for n in missing[:30])
        more = f" (+{len(missing) - 30} more)" if len(missing) > 30 else ""
        await msg.reply_text(f"Not available: {shown}{more}")


async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if is_admin(update):
        try:
            await ctx.bot.set_my_commands(
                ADMIN_COMMANDS, scope=BotCommandScopeChat(update.effective_chat.id)
            )
        except Exception:
            logging.warning("could not set admin command menu")
    await update.message.reply_text(
        "👋 Welcome!\nType a series name to find it.\n"
        "Then send an episode number (350) or a range (350-370).\n\n/help for more."
    )


USER_COMMANDS = [
    BotCommand("start", "Start the bot"),
    BotCommand("help", "How to use the bot"),
    BotCommand("search", "Search a series"),
]
ADMIN_COMMANDS = USER_COMMANDS + [
    BotCommand("admin", "Show all admin tools"),
    BotCommand("add", "Create a series and start uploading"),
    BotCommand("upload", "Choose a series to upload into"),
    BotCommand("setnext", "Set the next episode number"),
    BotCommand("done", "Finish upload mode"),
    BotCommand("series", "List all series"),
    BotCommand("info", "Episodes available and missing"),
    BotCommand("stats", "Users, series, episodes, top series"),
    BotCommand("allow", "Give a user access"),
    BotCommand("revoke", "Remove a user's access"),
    BotCommand("authorized", "List users with access"),
    BotCommand("private", "Only authorized users on/off"),
    BotCommand("allowall", "Authorize everyone who used the bot"),
    BotCommand("privacy", "Privacy settings"),
    BotCommand("protect", "Block forwarding and saving"),
    BotCommand("autodelete", "Auto-delete sent episodes"),
    BotCommand("broadcast", "Send a message to all users"),
    BotCommand("rename", "Rename a series"),
    BotCommand("delep", "Delete episode(s)"),
    BotCommand("delseries", "Delete a series"),
]

USER_HELP = (
    "🎧 How to use:\n"
    "• Type a series name (or /search name)\n"
    "• Pick the series, you'll see how many episodes are available\n"
    "• Send an episode number like 350, or a range like 350-370\n"
    f"• Up to {MAX_RANGE} episodes per request"
)

ADMIN_HELP = """🔧 Admin tools

📤 Uploading
/add Series Name - create a series and start uploading
/upload - pick an existing series to upload into
Then send the audio files named like "Ep - 01" (the number is read from the name; a file without a number gets the next number)
/setnext 350 - change the next episode number
/done - finish upload mode

🗂 Manage
/series - list all series with episode counts
/info Series Name - episodes available and missing numbers
/rename Old Name | New Name
/delep Series Name | 5 - delete one episode
/delep Series Name | 5-10 - delete a range
/delseries Series Name - delete a series

🔑 Access (private bot)
/allow 123456789 - give a user access (the ID is in their access request)
/allow 123456789 30 - access for 30 days only
/revoke 123456789 - remove access
/authorized - list users who have access
/private on|off - only authorized users can use the bot
/allowall - authorize everyone who already used the bot

🔒 Privacy
/privacy - show the privacy settings
/protect on|off - block forwarding and saving of episodes
/autodelete 60 - delete sent episodes after 60 minutes (/autodelete off to disable)

📊 Users
/stats - users, series, episodes, most requested
/broadcast Your message - send a message to all users"""


async def help_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = USER_HELP
    if is_admin(update):
        text += "\n\n" + ADMIN_HELP
    await update.message.reply_text(text)


async def admin_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if is_admin(update):
        await update.message.reply_text(ADMIN_HELP)


async def track_user(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Remember every private-chat user once (used for /stats and /broadcast)."""
    u = update.effective_user
    chat = update.effective_chat
    if not u or u.is_bot or not chat or chat.type != "private":
        return
    seen = ctx.application.bot_data.setdefault("seen", set())
    if u.id in seen:
        return
    seen.add(u.id)
    c = db()
    c.execute(
        "INSERT INTO users(user_id, name, username) VALUES(?,?,?) "
        "ON CONFLICT(user_id) DO UPDATE SET name=excluded.name, username=excluded.username, active=1",
        (u.id, u.full_name, u.username),
    )
    c.execute("UPDATE authorized SET name=? WHERE user_id=?", (u.full_name, u.id))
    c.commit()
    c.close()


async def search_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = " ".join(ctx.args).strip()
    if not q:
        await update.message.reply_text("Usage: /search series name")
        return
    await do_search(update, q)


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    t = (update.message.text or "").strip()
    if not t:
        return
    m = NUM_RE.match(t)
    if m:
        sid = get_viewing(update.effective_user.id)
        if sid:
            a = int(m.group(1))
            b = int(m.group(2)) if m.group(2) else a
            uid = update.effective_user.id
            active = ctx.application.bot_data.setdefault("sending", set())
            if uid in active:
                await update.message.reply_text("⏳ Still sending your previous episodes. Please wait.")
                return
            active.add(uid)

            async def job():
                try:
                    await send_range(update, ctx, sid, a, b)
                finally:
                    active.discard(uid)

            # run in background so other users are not blocked while files are being sent
            ctx.application.create_task(job(), update=update)
            return
    await do_search(update, t)


async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    parts = q.data.split(":")
    uid = q.from_user.id

    if parts[0] in ("ap", "rj"):
        await handle_access_button(q, ctx, parts, uid)
        return

    if parts[0] == "s":
        sid = int(parts[1])
        text = info_text(sid)
        try:
            if not text:
                await q.edit_message_text("Series not found.")
                return
            set_viewing(uid, sid)
            await q.edit_message_text(text)
        except BadRequest:
            pass

    elif parts[0] == "u" and is_admin_id(uid):
        sid = int(parts[1])
        c = db()
        s = c.execute("SELECT name FROM series WHERE id=?", (sid,)).fetchone()
        if not s:
            c.close()
            return
        nxt = (c.execute("SELECT MAX(number) AS m FROM episodes WHERE series_id=?", (sid,)).fetchone()["m"] or 0) + 1
        c.execute(
            "INSERT OR REPLACE INTO uploading(user_id, series_id, next_number) VALUES(?,?,?)",
            (uid, sid, nxt),
        )
        c.commit()
        c.close()
        try:
            await q.edit_message_text(
                f"📤 Upload mode: {s['name']}\nNext episode: {nxt}\n\n"
                "Send the audio files in order. /setnext 350 to change the number, /done when finished."
            )
        except BadRequest:
            pass


# ---------- admin side ----------
async def add_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    name = " ".join(ctx.args).strip()
    if not name:
        await update.message.reply_text("Usage: /add Series Name")
        return
    c = db()
    c.execute("INSERT OR IGNORE INTO series(name) VALUES(?)", (name,))
    s = c.execute("SELECT id, name FROM series WHERE name=?", (name,)).fetchone()
    nxt = (c.execute("SELECT MAX(number) AS m FROM episodes WHERE series_id=?", (s["id"],)).fetchone()["m"] or 0) + 1
    c.execute(
        "INSERT OR REPLACE INTO uploading(user_id, series_id, next_number) VALUES(?,?,?)",
        (update.effective_user.id, s["id"], nxt),
    )
    c.commit()
    c.close()
    await update.message.reply_text(
        f"📤 Upload mode: {s['name']}\nNext episode: {nxt}\n\n"
        "Send the audio files in order. /setnext 350 to change the number, /done when finished."
    )


async def upload_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    c = db()
    rows = c.execute("SELECT id, name FROM series ORDER BY name LIMIT 50").fetchall()
    c.close()
    if not rows:
        await update.message.reply_text("No series yet. Create one with: /add Series Name")
        return
    buttons = [[InlineKeyboardButton(r["name"], callback_data=f"u:{r['id']}")] for r in rows]
    await update.message.reply_text(
        "Choose the series to upload into (or create a new one with /add Series Name):",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def setnext_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    if not ctx.args or not ctx.args[0].isdigit():
        await update.message.reply_text("Usage: /setnext 350")
        return
    n = int(ctx.args[0])
    c = db()
    cur = c.execute("UPDATE uploading SET next_number=? WHERE user_id=?", (n, update.effective_user.id))
    c.commit()
    c.close()
    if cur.rowcount:
        await update.message.reply_text(f"Next episode number: {n}")
    else:
        await update.message.reply_text("Choose a series first: /upload or /add Series Name")


async def done_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    c = db()
    c.execute("DELETE FROM uploading WHERE user_id=?", (update.effective_user.id,))
    c.commit()
    c.close()
    await update.message.reply_text("✅ Upload mode finished.")


async def on_upload(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    m = update.message
    if not is_admin(update):
        await m.reply_text("Only the admin can upload episodes.")
        return

    fname = ""
    if m.audio:
        file_id, kind = m.audio.file_id, "audio"
        fname = m.audio.file_name or m.audio.title or ""
    elif m.voice:
        file_id, kind = m.voice.file_id, "voice"
    elif m.document:
        file_id, kind = m.document.file_id, "document"
        fname = m.document.file_name or ""
    else:
        return

    uid = update.effective_user.id
    c = db()
    st = c.execute("SELECT series_id, next_number FROM uploading WHERE user_id=?", (uid,)).fetchone()
    if not st:
        c.close()
        await m.reply_text("First choose a series: /upload or /add Series Name")
        return
    s = c.execute("SELECT name FROM series WHERE id=?", (st["series_id"],)).fetchone()
    if not s:
        c.execute("DELETE FROM uploading WHERE user_id=?", (uid,))
        c.commit()
        c.close()
        await m.reply_text("That series was deleted. Choose again: /upload")
        return

    found = EP_RE.search(fname)
    n = int(found.group(1)) if found else st["next_number"]   # number from file name, else auto
    old = c.execute(
        "SELECT id FROM episodes WHERE series_id=? AND number=?", (st["series_id"], n)
    ).fetchone()
    if old:
        c.execute("UPDATE episodes SET file_id=?, kind=? WHERE id=?", (file_id, kind, old["id"]))
    else:
        c.execute(
            "INSERT INTO episodes(series_id, number, file_id, kind) VALUES(?,?,?,?)",
            (st["series_id"], n, file_id, kind),
        )
    c.execute("UPDATE uploading SET next_number=? WHERE user_id=?", (n + 1, uid))
    c.commit()
    c.close()

    try:
        await m.reply_text(f"{'♻️' if old else '✅'} {s['name']} - Episode {n}")
    except Exception:
        logging.warning("reply failed (flood control?) but episode %s was saved", n)


async def list_series(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    c = db()
    rows = c.execute(
        "SELECT s.name, COUNT(e.id) AS n FROM series s "
        "LEFT JOIN episodes e ON e.series_id = s.id GROUP BY s.id ORDER BY s.name"
    ).fetchall()
    c.close()
    if not rows:
        await update.message.reply_text("No series yet. Create one with: /add Series Name")
        return
    text = "\n".join(f"• {r['name']} ({r['n']})" for r in rows)
    for i in range(0, len(text), 4000):
        await update.message.reply_text(text[i:i + 4000])


async def rename_series(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    raw = " ".join(ctx.args)
    if "|" not in raw:
        await update.message.reply_text("Usage: /rename Old Name | New Name")
        return
    old, new = (x.strip() for x in raw.split("|", 1))
    c = db()
    try:
        cur = c.execute("UPDATE series SET name=? WHERE name=?", (new, old))
        c.commit()
        ok = cur.rowcount > 0
    except sqlite3.IntegrityError:
        c.close()
        await update.message.reply_text("A series with that new name already exists.")
        return
    c.close()
    await update.message.reply_text("✅ Renamed." if ok else "Series not found.")


async def delete_series(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    name = " ".join(ctx.args).strip()
    if not name:
        await update.message.reply_text("Usage: /delseries Series Name")
        return
    c = db()
    s = c.execute("SELECT id FROM series WHERE name=?", (name,)).fetchone()
    if not s:
        c.close()
        await update.message.reply_text("Series not found.")
        return
    c.execute("DELETE FROM episodes WHERE series_id=?", (s["id"],))
    c.execute("DELETE FROM viewing WHERE series_id=?", (s["id"],))
    c.execute("DELETE FROM uploading WHERE series_id=?", (s["id"],))
    c.execute("DELETE FROM series WHERE id=?", (s["id"],))
    c.commit()
    c.close()
    await update.message.reply_text("🗑 Series deleted.")


DEL_RE = re.compile(r"^\s*(.+?)\s*\|\s*(\d+)(?:\s*-\s*(\d+))?\s*$")


async def delete_episode(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    match = DEL_RE.match(" ".join(ctx.args))
    if not match:
        await update.message.reply_text("Usage: /delep Series Name | 5   or   /delep Series Name | 5-10")
        return
    name = match.group(1).strip()
    a = int(match.group(2))
    b = int(match.group(3) or a)
    if a > b:
        a, b = b, a
    c = db()
    cur = c.execute(
        "DELETE FROM episodes WHERE number BETWEEN ? AND ? "
        "AND series_id=(SELECT id FROM series WHERE name=?)",
        (a, b, name),
    )
    c.commit()
    c.close()
    await update.message.reply_text(
        f"🗑 Deleted {cur.rowcount} episode(s)." if cur.rowcount else "No matching episodes found."
    )


def compact_ranges(nums):
    """[1,2,3,5,7,8] -> '1-3, 5, 7-8'"""
    out, start, prev = [], None, None
    for n in nums:
        if start is None:
            start = prev = n
        elif n == prev + 1:
            prev = n
        else:
            out.append((start, prev))
            start = prev = n
    if start is not None:
        out.append((start, prev))
    return ", ".join(str(a) if a == b else f"{a}-{b}" for a, b in out)


async def info_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    name = " ".join(ctx.args).strip()
    if not name:
        await update.message.reply_text("Usage: /info Series Name")
        return
    c = db()
    s = c.execute("SELECT id, name, hits FROM series WHERE name=?", (name,)).fetchone()
    if not s:
        like = c.execute(
            "SELECT id, name, hits FROM series WHERE name LIKE ? ORDER BY name LIMIT 2", (f"%{name}%",)
        ).fetchall()
        if len(like) == 1:
            s = like[0]
        elif len(like) > 1:
            c.close()
            await update.message.reply_text("More than one series matches. Type the full name (see /series).")
            return
    if not s:
        c.close()
        await update.message.reply_text("Series not found.")
        return
    nums = [r["number"] for r in c.execute(
        "SELECT number FROM episodes WHERE series_id=? ORDER BY number", (s["id"],)
    ).fetchall()]
    c.close()

    if not nums:
        await update.message.reply_text(f"🎧 {s['name']}\nNo episodes yet.")
        return
    have = set(nums)
    missing = [n for n in range(nums[0], nums[-1] + 1) if n not in have]
    text = (
        f"🎧 {s['name']}\n"
        f"Total: {len(nums)} episodes (No. {nums[0]} to {nums[-1]})\n"
        f"Requests so far: {s['hits']}\n\n"
        f"✅ Available: {compact_ranges(nums)}\n\n"
        f"⚠️ Missing: {compact_ranges(missing) if missing else 'none'}"
    )
    for i in range(0, len(text), 4000):
        await update.message.reply_text(text[i:i + 4000])


async def stats_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    c = db()
    u = c.execute("SELECT COUNT(*) AS n, COALESCE(SUM(active), 0) AS a FROM users").fetchone()
    n_series = c.execute("SELECT COUNT(*) AS n FROM series").fetchone()["n"]
    n_eps = c.execute("SELECT COUNT(*) AS n FROM episodes").fetchone()["n"]
    n_auth = c.execute(
        "SELECT COUNT(*) AS n FROM authorized WHERE expires_at IS NULL OR expires_at > ?", (time.time(),)
    ).fetchone()["n"]
    top = c.execute(
        "SELECT name, hits FROM series WHERE hits > 0 ORDER BY hits DESC LIMIT 5"
    ).fetchall()
    c.close()
    text = (
        "📊 Stats\n"
        f"👥 Users: {u['n']} ({u['a']} active)\n"
        f"🔑 Authorized: {n_auth}\n"
        f"📚 Series: {n_series}\n"
        f"🎧 Episodes: {n_eps}"
    )
    if top:
        text += "\n\n🔥 Most requested:\n" + "\n".join(
            f"{i}. {r['name']} ({r['hits']})" for i, r in enumerate(top, 1)
        )
    await update.message.reply_text(text)


async def broadcast_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    parts = (update.message.text or "").split(None, 1)
    if len(parts) < 2 or not parts[1].strip():
        await update.message.reply_text("Usage: /broadcast Your message")
        return
    text = parts[1].strip()
    admin_chat = update.effective_chat.id

    c = db()
    ids = [r["user_id"] for r in c.execute("SELECT user_id FROM users WHERE active=1").fetchall()]
    c.close()
    ids = [i for i in ids if auth_status(i) == "ok"]
    if not ids:
        await update.message.reply_text("No users yet.")
        return
    await update.message.reply_text(f"📣 Sending to {len(ids)} users...")

    async def job():
        ok, fail, blocked = 0, 0, []
        for uid in ids:
            try:
                await with_retry(lambda uid=uid: ctx.bot.send_message(uid, text))
                ok += 1
            except Forbidden:
                blocked.append(uid)
                fail += 1
            except Exception:
                fail += 1
            await asyncio.sleep(0.06)   # about 16 messages/second, under Telegram's limit
        if blocked:
            cc = db()
            cc.executemany("UPDATE users SET active=0 WHERE user_id=?", [(b,) for b in blocked])
            cc.commit()
            cc.close()
        await ctx.bot.send_message(
            admin_chat, f"✅ Broadcast finished.\nDelivered: {ok}\nFailed or blocked: {fail}"
        )

    ctx.application.create_task(job(), update=update)


def private_on() -> bool:
    return get_setting("private", "1") == "1"      # default: ON (only authorized users)


def auth_status(uid) -> str:
    """Returns 'ok', 'expired' or 'no'."""
    if uid in ADMINS or not private_on():
        return "ok"
    c = db()
    r = c.execute("SELECT expires_at FROM authorized WHERE user_id=?", (uid,)).fetchone()
    c.close()
    if not r:
        return "no"
    if r["expires_at"] is None or r["expires_at"] > time.time():
        return "ok"
    return "expired"


async def request_access(u, ctx: ContextTypes.DEFAULT_TYPE):
    pending = ctx.application.bot_data.setdefault("requested", set())
    if u.id in pending:
        return
    pending.add(u.id)
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Approve", callback_data=f"ap:{u.id}"),
        InlineKeyboardButton("❌ Reject", callback_data=f"rj:{u.id}"),
    ]])
    text = (
        "🔑 Access request\n"
        f"Name: {u.full_name}\n"
        f"Username: @{u.username or '-'}\n"
        f"ID: {u.id}\n\n"
        "Approve gives access with no expiry. For a limited time use /allow ID DAYS."
    )
    for aid in ADMINS:
        try:
            await ctx.bot.send_message(aid, text, reply_markup=kb)
        except Exception:
            logging.warning("could not send access request to admin %s", aid)


async def gate(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Runs before everything else. Blocks anyone who is not authorized."""
    u = update.effective_user
    chat = update.effective_chat
    if not u or u.is_bot or not chat or chat.type != "private":
        raise ApplicationHandlerStop          # the bot only works in private chats
    status = auth_status(u.id)
    if status == "ok":
        return

    q = update.callback_query
    if q and q.data == "req":
        await q.answer()
        await request_access(u, ctx)
        try:
            await q.edit_message_text("✅ Request sent to the admin. You will get a message when you are approved.")
        except Exception:
            pass
        raise ApplicationHandlerStop
    if q:
        await q.answer("🔒 Not authorized", show_alert=True)
        raise ApplicationHandlerStop

    msg = update.effective_message
    if msg:
        last = ctx.application.bot_data.setdefault("notice_at", {})
        now = time.time()
        if now - last.get(u.id, 0) > 30:          # do not answer the same person more than every 30 s
            last[u.id] = now
            head = "⏳ Your access has expired." if status == "expired" else "🔒 This bot is private."
            await msg.reply_text(
                f"{head}\nYour ID: {u.id}\n\nTap below to ask the admin for access.",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("🔑 Request access", callback_data="req")]]
                ),
            )
    raise ApplicationHandlerStop


async def handle_access_button(q, ctx: ContextTypes.DEFAULT_TYPE, parts, uid):
    if not is_admin_id(uid):
        return
    target = int(parts[1])
    if parts[0] == "ap":
        name = ""
        try:
            name = (await ctx.bot.get_chat(target)).full_name or ""
        except Exception:
            pass
        c = db()
        c.execute(
            "INSERT OR REPLACE INTO authorized(user_id, name, expires_at) VALUES(?,?,NULL)", (target, name)
        )
        c.commit()
        c.close()
        ctx.application.bot_data.setdefault("requested", set()).discard(target)
        result = f"✅ Approved: {name or target}"
        try:
            await ctx.bot.send_message(target, "✅ You now have access. Type /start to begin.")
        except Exception:
            pass
    else:
        result = f"❌ Rejected: {target}"
        try:
            await ctx.bot.send_message(target, "Your access request was declined.")
        except Exception:
            pass
    try:
        await q.edit_message_text(result)
    except BadRequest:
        pass


async def allow_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    usage = "Usage: /allow 123456789   or   /allow 123456789 30   (30 = days)"
    if not ctx.args or not ctx.args[0].isdigit():
        await update.message.reply_text(usage)
        return
    uid = int(ctx.args[0])
    days = None
    if len(ctx.args) > 1:
        if not ctx.args[1].isdigit() or not 1 <= int(ctx.args[1]) <= 3650:
            await update.message.reply_text(usage)
            return
        days = int(ctx.args[1])
    expires = time.time() + days * 86400 if days else None
    name = ""
    try:
        name = (await ctx.bot.get_chat(uid)).full_name or ""
    except Exception:
        pass
    c = db()
    c.execute(
        "INSERT OR REPLACE INTO authorized(user_id, name, expires_at) VALUES(?,?,?)", (uid, name, expires)
    )
    c.commit()
    c.close()
    ctx.application.bot_data.setdefault("requested", set()).discard(uid)
    until = f"for {days} day(s)" if days else "with no expiry"
    await update.message.reply_text(f"✅ Access granted to {name or uid} ({uid}) {until}.")
    try:
        await ctx.bot.send_message(uid, "✅ You now have access. Type /start to begin.")
    except Exception:
        await update.message.reply_text("(Could not message them. They need to open the bot and press Start.)")


async def revoke_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    if not ctx.args or not ctx.args[0].isdigit():
        await update.message.reply_text("Usage: /revoke 123456789")
        return
    uid = int(ctx.args[0])
    c = db()
    cur = c.execute("DELETE FROM authorized WHERE user_id=?", (uid,))
    c.commit()
    c.close()
    await update.message.reply_text("🚫 Access removed." if cur.rowcount else "That user was not in the list.")


async def authorized_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    c = db()
    rows = c.execute("SELECT user_id, name, expires_at FROM authorized ORDER BY added DESC").fetchall()
    c.close()
    if not rows:
        await update.message.reply_text("No authorized users yet. Use /allow ID or wait for access requests.")
        return
    now = time.time()
    lines = []
    for r in rows:
        if r["expires_at"] is None:
            exp = "no expiry"
        else:
            exp = "expires " + time.strftime("%Y-%m-%d", time.localtime(r["expires_at"]))
            if r["expires_at"] <= now:
                exp += " (EXPIRED)"
        lines.append(f"{r['user_id']} - {r['name'] or '?'} - {exp}")
    text = f"🔑 Authorized users: {len(rows)}\n\n" + "\n".join(lines)
    for i in range(0, len(text), 4000):
        await update.message.reply_text(text[i:i + 4000])


async def private_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    arg = ctx.args[0].lower() if ctx.args else ""
    if arg in ("on", "off"):
        set_setting("private", "1" if arg == "on" else "0")
    state = "ON (only authorized users can use the bot)" if private_on() else "OFF (everyone can use the bot)"
    await update.message.reply_text(f"🔑 Private mode: {state}\n\nChange with /private on or /private off")


async def allowall_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    c = db()
    cur = c.execute("INSERT OR IGNORE INTO authorized(user_id, name) SELECT user_id, name FROM users")
    c.commit()
    c.close()
    await update.message.reply_text(f"✅ Authorized {cur.rowcount} existing user(s), with no expiry.")


def privacy_text():
    mins = autodelete_minutes()
    return (
        "🔒 Privacy settings\n"
        f"• Content protection (no forwarding/saving): {'ON' if protect_on() else 'OFF'}\n"
        f"• Auto-delete sent episodes: {('after ' + fmt_minutes(mins)) if mins else 'OFF'}\n\n"
        "Change with:\n/protect on|off\n/autodelete 60   (minutes, max 1440)\n/autodelete off"
    )


async def privacy_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if is_admin(update):
        await update.message.reply_text(privacy_text())


async def protect_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    arg = ctx.args[0].lower() if ctx.args else ""
    if arg not in ("on", "off"):
        await update.message.reply_text("Usage: /protect on   or   /protect off\n\n" + privacy_text())
        return
    set_setting("protect", "1" if arg == "on" else "0")
    await update.message.reply_text(privacy_text())


async def autodelete_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    arg = ctx.args[0].lower() if ctx.args else ""
    if arg in ("off", "0"):
        set_setting("autodelete", "0")
    elif arg.isdigit() and 1 <= int(arg) <= 1440:
        set_setting("autodelete", int(arg))
    else:
        await update.message.reply_text(
            "Usage: /autodelete 60   (1 to 1440 minutes)\n       /autodelete off\n\n" + privacy_text()
        )
        return
    await update.message.reply_text(privacy_text())


async def post_init(app: Application):
    app.bot_data["cleaner"] = asyncio.create_task(cleanup_loop(app))
    try:
        await app.bot.set_my_commands(USER_COMMANDS)
    except Exception:
        logging.warning("could not set the command menu")
    for aid in ADMINS:
        try:
            await app.bot.set_my_commands(ADMIN_COMMANDS, scope=BotCommandScopeChat(aid))
        except Exception:
            logging.warning("could not set admin menu for %s (they must press /start first)", aid)


def main():
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise SystemExit("BOT_TOKEN is missing in .env")
    if not ADMINS:
        logging.warning("ADMIN_IDS is empty: nobody can upload episodes")
    init_db()

    app = Application.builder().token(token).post_init(post_init).build()
    app.add_handler(TypeHandler(Update, gate), group=-2)
    app.add_handler(TypeHandler(Update, track_user), group=-1)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("search", search_cmd))
    app.add_handler(CommandHandler("add", add_cmd))
    app.add_handler(CommandHandler("upload", upload_cmd))
    app.add_handler(CommandHandler("setnext", setnext_cmd))
    app.add_handler(CommandHandler("done", done_cmd))
    app.add_handler(CommandHandler("series", list_series))
    app.add_handler(CommandHandler("rename", rename_series))
    app.add_handler(CommandHandler("delseries", delete_series))
    app.add_handler(CommandHandler("delep", delete_episode))
    app.add_handler(CommandHandler("admin", admin_cmd))
    app.add_handler(CommandHandler("info", info_cmd))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(CommandHandler("broadcast", broadcast_cmd))
    app.add_handler(CommandHandler("allow", allow_cmd))
    app.add_handler(CommandHandler("revoke", revoke_cmd))
    app.add_handler(CommandHandler("authorized", authorized_cmd))
    app.add_handler(CommandHandler("private", private_cmd))
    app.add_handler(CommandHandler("allowall", allowall_cmd))
    app.add_handler(CommandHandler("privacy", privacy_cmd))
    app.add_handler(CommandHandler("protect", protect_cmd))
    app.add_handler(CommandHandler("autodelete", autodelete_cmd))
    app.add_handler(MessageHandler(filters.AUDIO | filters.VOICE | filters.Document.ALL, on_upload))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.run_polling()


if __name__ == "__main__":
    main()
