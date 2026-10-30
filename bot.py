import os
import re
import asyncio
import sqlite3
import logging

from dotenv import load_dotenv
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    InputMediaAudio, InputMediaDocument,
)
from telegram.error import BadRequest, RetryAfter
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters,
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
    """)
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


async def send_episode(bot, chat_id, name, row):
    cap = f"{name} - Episode {row['number']}"
    if row["kind"] == "audio":
        await bot.send_audio(chat_id, row["file_id"], caption=cap)
    elif row["kind"] == "voice":
        await bot.send_voice(chat_id, row["file_id"], caption=cap)
    else:
        await bot.send_document(chat_id, row["file_id"], caption=cap)


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


async def send_chunk(bot, chat_id, name, chunk):
    """Send up to 10 episodes as one album. Falls back to one-by-one. Returns failed numbers."""
    failed = []
    kind = chunk[0]["kind"]
    if len(chunk) > 1 and kind in ("audio", "document"):
        cls = InputMediaAudio if kind == "audio" else InputMediaDocument
        media = [cls(media=r["file_id"], caption=f"{name} - Episode {r['number']}") for r in chunk]
        try:
            await with_retry(lambda: bot.send_media_group(chat_id, media))
            return failed
        except Exception:
            logging.exception("album failed, sending one by one")
    for r in chunk:
        try:
            await with_retry(lambda r=r: send_episode(bot, chat_id, name, r))
        except Exception:
            logging.exception("send failed for episode %s", r["number"])
            failed.append(r["number"])
        await asyncio.sleep(SEND_DELAY)
    return failed


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

    for chunk in make_chunks(rows):
        missing += await send_chunk(ctx.bot, msg.chat_id, s["name"], chunk)
        await asyncio.sleep(SEND_DELAY)

    if missing:
        missing = sorted(set(missing))
        shown = ", ".join(str(n) for n in missing[:30])
        more = f" (+{len(missing) - 30} more)" if len(missing) > 30 else ""
        await msg.reply_text(f"Not available: {shown}{more}")


async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Welcome!\nType a series name to find it.\n"
        "Then send an episode number (350) or a range (350-370).\n\n/help for more."
    )


async def help_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = (
        "🎧 How to use:\n"
        "• Type a series name (or /search name)\n"
        "• Pick the series, you'll see how many episodes are available\n"
        "• Send an episode number like 350, or a range like 350-370"
    )
    if is_admin(update):
        text += (
            "\n\n🔧 Admin:\n"
            "• /add Series Name - create the series and start uploading\n"
            "• /upload - choose an existing series to upload into\n"
            "• Then send audio files named like \"Ep - 01\" (number is read from the name;\n"
            "  files without a number get the next number automatically)\n"
            "• /setnext 350 - set the next episode number\n"
            "• /done - stop upload mode\n"
            "• /series - list all series\n"
            "• /rename Old Name | New Name\n"
            "• /delep Series Name | 5\n"
            "• /delseries Series Name"
        )
    await update.message.reply_text(text)


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


async def delete_episode(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    match = PIPE_RE.match(" ".join(ctx.args))
    if not match:
        await update.message.reply_text("Usage: /delep Series Name | 5")
        return
    name, number = match.group(1).strip(), int(match.group(2))
    c = db()
    cur = c.execute(
        "DELETE FROM episodes WHERE number=? AND series_id=(SELECT id FROM series WHERE name=?)",
        (number, name),
    )
    c.commit()
    c.close()
    await update.message.reply_text("🗑 Episode deleted." if cur.rowcount else "Episode not found.")


def main():
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise SystemExit("BOT_TOKEN is missing in .env")
    if not ADMINS:
        logging.warning("ADMIN_IDS is empty: nobody can upload episodes")
    init_db()

    app = Application.builder().token(token).build()
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
    app.add_handler(MessageHandler(filters.AUDIO | filters.VOICE | filters.Document.ALL, on_upload))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.run_polling()


if __name__ == "__main__":
    main()
