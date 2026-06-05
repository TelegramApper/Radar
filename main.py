import logging
import os
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional

from telegram import Update, User
from telegram.constants import ChatType, ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(
    format="%(asctime)s | %(name)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("fc26_elite_tracker")

OWNER_ID = int(os.getenv("OWNER_ID", "813607344"))
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
DB_PATH = os.getenv("DB_PATH", "/data/fc26_elite_tracker.db")
TIMEZONE_OFFSET_HOURS = int(os.getenv("TIMEZONE_OFFSET_HOURS", "3"))
SUSPECT_THRESHOLD = int(os.getenv("SUSPECT_THRESHOLD", "15"))
TOP_LIMIT = int(os.getenv("TOP_LIMIT", "20"))
MESSAGE_LOG_LIMIT = int(os.getenv("MESSAGE_LOG_LIMIT", "1000"))

DEFAULT_KEYWORDS = [
    "Prendete",
    "Tua",
    "Vostra",
    "Vieni",
    "Vai",
    "Andate",
    "Let's go",
    "Go",
    "Gift",
]

THREE_DIGIT_RE = re.compile(r"(?<!\d)([1-9]\d{2})(?!\d)")


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def now_local() -> datetime:
    return now_utc() + timedelta(hours=TIMEZONE_OFFSET_HOURS)


def iso_now() -> str:
    return now_utc().isoformat()


def ensure_parent(path: str) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)


@dataclass
class TargetPlayer:
    user_id: int
    username: Optional[str]
    display_name: str


class Database:
    def __init__(self, path: str):
        ensure_parent(path)
        self.path = path
        self._init_db()

    def connect(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        with closing(self.connect()) as conn, conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS tracked_players (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    display_name TEXT,
                    added_at TEXT NOT NULL,
                    added_by INTEGER,
                    last_seen_at TEXT,
                    highest_rank INTEGER,
                    last_rank INTEGER
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS contributions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    message_text TEXT NOT NULL,
                    chat_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL,
                    contribution_count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (user_id) REFERENCES tracked_players(user_id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS keywords (
                    keyword TEXT PRIMARY KEY,
                    added_at TEXT NOT NULL,
                    added_by INTEGER
                )
                """
            )
            existing = conn.execute("SELECT COUNT(*) AS c FROM keywords").fetchone()["c"]
            if existing == 0:
                conn.executemany(
                    "INSERT OR IGNORE INTO keywords(keyword, added_at, added_by) VALUES (?, ?, ?)",
                    [(kw, iso_now(), OWNER_ID) for kw in DEFAULT_KEYWORDS],
                )

    def upsert_player(self, user: User, added_by: int):
        username = user.username.lower() if user.username else None
        display_name = (user.full_name or user.first_name or str(user.id)).strip()
        with closing(self.connect()) as conn, conn:
            exists = conn.execute(
                "SELECT user_id FROM tracked_players WHERE user_id = ?",
                (user.id,),
            ).fetchone()
            if exists:
                conn.execute(
                    "UPDATE tracked_players SET username = ?, display_name = ? WHERE user_id = ?",
                    (username, display_name, user.id),
                )
                return False
            conn.execute(
                """
                INSERT INTO tracked_players(user_id, username, display_name, added_at, added_by, last_seen_at, highest_rank, last_rank)
                VALUES (?, ?, ?, ?, ?, ?, NULL, NULL)
                """,
                (user.id, username, display_name, iso_now(), added_by, iso_now()),
            )
            return True

    def remove_player(self, user_id: int):
        with closing(self.connect()) as conn, conn:
            deleted = conn.execute(
                "DELETE FROM tracked_players WHERE user_id = ?",
                (user_id,),
            ).rowcount
            return deleted > 0

    def update_seen(self, user: User):
        with closing(self.connect()) as conn, conn:
            conn.execute(
                "UPDATE tracked_players SET username = ?, display_name = ?, last_seen_at = ? WHERE user_id = ?",
                (
                    user.username.lower() if user.username else None,
                    (user.full_name or user.first_name or str(user.id)).strip(),
                    iso_now(),
                    user.id,
                ),
            )

    def is_tracked(self, user_id: int) -> bool:
        with closing(self.connect()) as conn:
            row = conn.execute(
                "SELECT 1 FROM tracked_players WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            return row is not None

    def add_message(self, user_id: int, chat_id: int, message_id: int, text: str, is_contribution: bool):
        with closing(self.connect()) as conn, conn:
            conn.execute(
                """
                INSERT INTO contributions(user_id, message_text, chat_id, message_id, contribution_count, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (user_id, text[:4000], chat_id, message_id, 1 if is_contribution else 0, iso_now()),
            )
            conn.execute(
                "UPDATE tracked_players SET last_seen_at = ? WHERE user_id = ?",
                (iso_now(), user_id),
            )
            conn.execute(
                """
                DELETE FROM contributions
                WHERE id IN (
                    SELECT id FROM contributions
                    WHERE user_id = ?
                    ORDER BY created_at DESC
                    LIMIT -1 OFFSET ?
                )
                """,
                (user_id, MESSAGE_LOG_LIMIT),
            )

    def get_keywords(self) -> list[str]:
        with closing(self.connect()) as conn:
            rows = conn.execute("SELECT keyword FROM keywords ORDER BY keyword COLLATE NOCASE").fetchall()
            return [r["keyword"] for r in rows]

    def add_keyword(self, keyword: str, added_by: int) -> bool:
        with closing(self.connect()) as conn, conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO keywords(keyword, added_at, added_by) VALUES (?, ?, ?)",
                (keyword, iso_now(), added_by),
            )
            return cur.rowcount > 0

    def remove_keyword(self, keyword: str) -> bool:
        with closing(self.connect()) as conn, conn:
            cur = conn.execute("DELETE FROM keywords WHERE lower(keyword) = lower(?)", (keyword,))
            return cur.rowcount > 0

    def find_player_by_username(self, username: str):
        norm = username.lstrip("@").lower()
        with closing(self.connect()) as conn:
            return conn.execute(
                "SELECT * FROM tracked_players WHERE lower(username) = ?",
                (norm,),
            ).fetchone()

    def player_stats(self, user_id: int):
        start_day = now_local().replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(hours=TIMEZONE_OFFSET_HOURS)
        start_week_local = now_local() - timedelta(days=now_local().weekday())
        start_week = start_week_local.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(hours=TIMEZONE_OFFSET_HOURS)
        with closing(self.connect()) as conn:
            total = conn.execute(
                "SELECT COALESCE(SUM(contribution_count), 0) AS c FROM contributions WHERE user_id = ?",
                (user_id,),
            ).fetchone()["c"]
            daily = conn.execute(
                "SELECT COALESCE(SUM(contribution_count), 0) AS c FROM contributions WHERE user_id = ? AND created_at >= ?",
                (user_id, start_day.isoformat()),
            ).fetchone()["c"]
            weekly = conn.execute(
                "SELECT COALESCE(SUM(contribution_count), 0) AS c FROM contributions WHERE user_id = ? AND created_at >= ?",
                (user_id, start_week.isoformat()),
            ).fetchone()["c"]
            first = conn.execute(
                "SELECT added_at, last_seen_at, username, display_name, highest_rank, last_rank FROM tracked_players WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            return {
                "total": total,
                "daily": daily,
                "weekly": weekly,
                "added_at": first["added_at"] if first else None,
                "last_seen_at": first["last_seen_at"] if first else None,
                "username": first["username"] if first else None,
                "display_name": first["display_name"] if first else None,
                "highest_rank": first["highest_rank"] if first else None,
                "current_rank": first["last_rank"] if first else None,
            }

    def ranked_players(self):
        with closing(self.connect()) as conn:
            rows = conn.execute(
                """
                SELECT p.user_id, p.username, p.display_name,
                       COALESCE(SUM(c.contribution_count), 0) AS total
                FROM tracked_players p
                LEFT JOIN contributions c ON c.user_id = p.user_id
                GROUP BY p.user_id, p.username, p.display_name
                ORDER BY total DESC, p.added_at ASC
                """
            ).fetchall()
            ranked = []
            for i, row in enumerate(rows, start=1):
                ranked.append({**dict(row), "rank": i})
            return ranked

    def refresh_ranks(self):
        ranked = self.ranked_players()
        with closing(self.connect()) as conn, conn:
            for item in ranked:
                existing = conn.execute(
                    "SELECT highest_rank FROM tracked_players WHERE user_id = ?",
                    (item["user_id"],),
                ).fetchone()
                prev_best = existing["highest_rank"] if existing else None
                new_best = item["rank"] if prev_best is None else min(prev_best, item["rank"])
                conn.execute(
                    "UPDATE tracked_players SET last_rank = ?, highest_rank = ? WHERE user_id = ?",
                    (item["rank"], new_best, item["user_id"]),
                )
        return ranked

    def recent_messages(self, user_id: int, limit: int = 20):
        with closing(self.connect()) as conn:
            return conn.execute(
                """
                SELECT message_text, contribution_count, created_at
                FROM contributions
                WHERE user_id = ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (user_id, limit),
            ).fetchall()

    def tracked_players(self):
        with closing(self.connect()) as conn:
            return conn.execute(
                "SELECT * FROM tracked_players ORDER BY COALESCE(username, display_name) COLLATE NOCASE"
            ).fetchall()


db = Database(DB_PATH)


def escape_md(text: str) -> str:
    chars = r"_*[]()~`>#+-=|{}.!"
    out = text
    for ch in chars:
        out = out.replace(ch, f"\\{ch}")
    return out


def display_handle(row_or_stats) -> str:
    username = row_or_stats["username"] if isinstance(row_or_stats, sqlite3.Row) else row_or_stats.get("username")
    display_name = row_or_stats["display_name"] if isinstance(row_or_stats, sqlite3.Row) else row_or_stats.get("display_name")
    if username:
        return f"@{username}"
    return display_name or "Unknown"


def humanize_delta(iso_ts: Optional[str]) -> str:
    if not iso_ts:
        return "Unknown"
    then = datetime.fromisoformat(iso_ts)
    diff = now_utc() - then
    seconds = int(diff.total_seconds())
    if seconds < 60:
        return "just now"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} minute{'s' if minutes != 1 else ''} ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    days = hours // 24
    return f"{days} day{'s' if days != 1 else ''} ago"


def is_contribution(text: str, keywords: Iterable[str]) -> bool:
    if not text:
        return False
    normalized = text.casefold()
    has_keyword = any(kw.casefold() in normalized for kw in keywords)
    has_three_digit = any(100 <= int(m.group(1)) <= 999 for m in THREE_DIGIT_RE.finditer(text))
    return has_keyword or has_three_digit


async def is_authorized(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return False
    if user.id == OWNER_ID:
        return True
    if chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP}:
        return False
    try:
        member = await context.bot.get_chat_member(chat.id, user.id)
        return member.status in {"administrator", "creator"}
    except Exception as exc:
        logger.warning("Admin check failed: %s", exc)
        return False


async def reject_unauthorized(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if await is_authorized(update, context):
        return False
    return True


async def resolve_target(update: Update, context: ContextTypes.DEFAULT_TYPE) -> Optional[TargetPlayer]:
    message = update.effective_message
    args = context.args
    if message and message.reply_to_message and message.reply_to_message.from_user:
        u = message.reply_to_message.from_user
        return TargetPlayer(
            user_id=u.id,
            username=u.username.lower() if u.username else None,
            display_name=u.full_name or u.first_name,
        )
    if args:
        username = args[0].lstrip("@").lower()
        row = db.find_player_by_username(username)
        if row:
            return TargetPlayer(
                user_id=row["user_id"],
                username=row["username"],
                display_name=row["display_name"],
            )
    return None


async def send_private_or_notice(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    user = update.effective_user
    if update.effective_chat and update.effective_chat.type in {ChatType.GROUP, ChatType.SUPERGROUP}:
        await update.effective_message.reply_text("Report sent privately.")
    try:
        await context.bot.send_message(chat_id=user.id, text=text, parse_mode=ParseMode.MARKDOWN_V2)
    except Exception:
        await update.effective_message.reply_text(
            "I couldn't send you a private message. Start the bot in private first, then try again."
        )


async def add_player(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await reject_unauthorized(update, context):
        return
    message = update.effective_message
    target_user = None

    if message.reply_to_message and message.reply_to_message.from_user:
        target_user = message.reply_to_message.from_user
    elif context.args:
        username = context.args[0].lstrip("@")
        await message.reply_text(
            "Use reply + .add for guaranteed accuracy, or let the player send a message first so the bot can learn their User ID."
        )
        row = db.find_player_by_username(username)
        if row:
            await message.reply_text(f"@{row['username']} is already known. Use reply + .add for a safe add.")
        return
    else:
        await message.reply_text("Usage: /add by reply, or .add as a reply to the player's message.")
        return

    created = db.upsert_player(target_user, update.effective_user.id)
    handle = f"@{target_user.username}" if target_user.username else target_user.full_name
    if created:
        await message.reply_text(f"{handle} added to Elite Tracking System")
    else:
        await message.reply_text(f"{handle} is already tracked")


async def remove_player(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await reject_unauthorized(update, context):
        return
    target = await resolve_target(update, context)
    if not target:
        await update.effective_message.reply_text("Use /remove @username for tracked usernames, or reply + .remove.")
        return
    removed = db.remove_player(target.user_id)
    handle = f"@{target.username}" if target.username else target.display_name
    await update.effective_message.reply_text(
        f"{handle} removed from Elite Tracking System" if removed else f"{handle} is not tracked"
    )


async def check_player(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await reject_unauthorized(update, context):
        return
    target = await resolve_target(update, context)
    if not target:
        await update.effective_message.reply_text("Use /ch @username or reply + .ch")
        return
    db.refresh_ranks()
    stats = db.player_stats(target.user_id)
    if stats["added_at"] is None:
        await update.effective_message.reply_text("Player is not tracked")
        return
    text = (
        f"*Player:* {escape_md(display_handle(stats))}\n\n"
        f"*Total Contributions:* {stats['total']}\n\n"
        f"*Current Rank:* #{stats['current_rank'] or '-'}\n\n"
        f"*Daily Contributions:* {stats['daily']}\n\n"
        f"*Weekly Contributions:* {stats['weekly']}\n\n"
        f"*Last Seen:* {escape_md(humanize_delta(stats['last_seen_at']))}\n\n"
        f"*Tracked Since:* {escape_md(stats['added_at'][:10])}"
    )
    await send_private_or_notice(update, context, text)


async def player_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await reject_unauthorized(update, context):
        return
    target = await resolve_target(update, context)
    if not target:
        await update.effective_message.reply_text("Use /msg @username [20|50|100] or reply + .msg")
        return
    limit = 20
    if context.args:
        try:
            maybe_num = int(context.args[-1])
            if maybe_num in {20, 50, 100}:
                limit = maybe_num
        except Exception:
            pass
    rows = db.recent_messages(target.user_id, limit=limit)
    if not rows:
        await update.effective_message.reply_text("No messages found for this player")
        return
    lines = [
        f"*Last {len(rows)} messages for* {escape_md(display_handle({'username': target.username, 'display_name': target.display_name}))}\n"
    ]
    for i, row in enumerate(rows, start=1):
        marker = "\\+1" if row["contribution_count"] else "0"
        body = escape_md(row["message_text"][:300])
        ts = escape_md(datetime.fromisoformat(row["created_at"]).strftime("%Y-%m-%d %H:%M"))
        lines.append(f"{i}\\. \\[{ts}\\] \\({marker}\\) {body}")
    await send_private_or_notice(update, context, "\n".join(lines))


async def player_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await reject_unauthorized(update, context):
        return
    target = await resolve_target(update, context)
    if not target:
        await update.effective_message.reply_text("Use /history @username or reply + .history")
        return
    db.refresh_ranks()
    stats = db.player_stats(target.user_id)
    if not stats["added_at"]:
        await update.effective_message.reply_text("Player is not tracked")
        return
    tracked_since = datetime.fromisoformat(stats["added_at"])
    days = max(1, (now_utc() - tracked_since).days + 1)
    avg_daily = round(stats["total"] / days)
    text = (
        f"*Player:* {escape_md(display_handle(stats))}\n\n"
        f"*Tracked Since:* {escape_md(stats['added_at'][:10])}\n\n"
        f"*Total Contributions:* {stats['total']}\n\n"
        f"*Average Daily Contributions:* {avg_daily}\n\n"
        f"*Highest Rank:* #{stats['highest_rank'] or '-'}\n\n"
        f"*Current Rank:* #{stats['current_rank'] or '-'}\n\n"
        f"*Last Seen:* {escape_md(humanize_delta(stats['last_seen_at']))}"
    )
    await send_private_or_notice(update, context, text)


async def top_players(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await reject_unauthorized(update, context):
        return
    ranked = db.refresh_ranks()[:TOP_LIMIT]
    if not ranked:
        await update.effective_message.reply_text("No tracked players yet")
        return
    lines = ["Elite Contribution Ranking", ""]
    for item in ranked:
        lines.append(f"#{item['rank']} {display_handle(item)} - {item['total']}")
    await update.effective_message.reply_text("\n".join(lines))


async def suspects(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await reject_unauthorized(update, context):
        return
    ranked = db.refresh_ranks()
    suspects_rows = [x for x in ranked if x["total"] <= SUSPECT_THRESHOLD]
    if not suspects_rows:
        await update.effective_message.reply_text("No potential inactive players right now")
        return
    lines = ["Potential Inactive Players", ""]
    for item in suspects_rows:
        lines.append(f"{display_handle(item)} - {item['total']} contributions")
    await update.effective_message.reply_text("\n".join(lines))


async def list_players(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await reject_unauthorized(update, context):
        return
    rows = db.tracked_players()
    lines = [f"Tracked Players ({len(rows)})", ""]
    lines.extend(display_handle(r) for r in rows)
    await update.effective_message.reply_text("\n".join(lines))


async def add_keyword(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await reject_unauthorized(update, context):
        return
    if not context.args:
        await update.effective_message.reply_text("Usage: /addkw Keyword")
        return
    keyword = " ".join(context.args).strip()
    created = db.add_keyword(keyword, update.effective_user.id)
    await update.effective_message.reply_text("Keyword added" if created else "Keyword already exists")


async def remove_keyword(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await reject_unauthorized(update, context):
        return
    if not context.args:
        await update.effective_message.reply_text("Usage: /removekw Keyword")
        return
    keyword = " ".join(context.args).strip()
    removed = db.remove_keyword(keyword)
    await update.effective_message.reply_text("Keyword removed" if removed else "Keyword not found")


async def list_keywords(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await reject_unauthorized(update, context):
        return
    kws = db.get_keywords()
    await update.effective_message.reply_text("Current Keywords\n\n" + "\n".join(kws))


async def start_private(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text("Bot is active.")


async def track_group_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    user = update.effective_user
    chat = update.effective_chat
    if not message or not user or not chat:
        return
    if chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP}:
        return
    if message.text and message.text.startswith(("/", ".")):
        return
    if not db.is_tracked(user.id):
        return
    text = message.text or message.caption or ""
    db.update_seen(user)
    contribution = is_contribution(text, db.get_keywords())
    db.add_message(user.id, chat.id, message.message_id, text, contribution)


async def dot_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.effective_message.text or "").strip().lower()
    if text == ".add":
        await add_player(update, context)
    elif text == ".remove":
        await remove_player(update, context)
    elif text == ".ch":
        await check_player(update, context)
    elif text == ".msg":
        await player_messages(update, context)
    elif text == ".history":
        await player_history(update, context)


def build_app() -> Application:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is missing")
    application = Application.builder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start_private))
    application.add_handler(CommandHandler("add", add_player))
    application.add_handler(CommandHandler("remove", remove_player))
    application.add_handler(CommandHandler("ch", check_player))
    application.add_handler(CommandHandler("msg", player_messages))
    application.add_handler(CommandHandler("history", player_history))
    application.add_handler(CommandHandler("top", top_players))
    application.add_handler(CommandHandler("suspects", suspects))
    application.add_handler(CommandHandler("list", list_players))
    application.add_handler(CommandHandler("addkw", add_keyword))
    application.add_handler(CommandHandler("removekw", remove_keyword))
    application.add_handler(CommandHandler("keywords", list_keywords))
    application.add_handler(
        MessageHandler(filters.TEXT & filters.Regex(r"^\.(add|remove|ch|msg|history)$"), dot_router)
    )
    application.add_handler(
        MessageHandler((filters.TEXT | filters.CaptionRegex(r".+")) & ~filters.COMMAND, track_group_messages)
    )
    return application


def main():
    app = build_app()
    logger.info("Starting bot with DB at %s", DB_PATH)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
