import logging
import os
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from telegram import Update, User
from telegram.constants import ChatType
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
TIMEZONE_OFFSET_HOURS = int(os.getenv("TIMEZONE_OFFSET_HOURS", "2"))
SUSPECT_THRESHOLD = int(os.getenv("SUSPECT_THRESHOLD", "15"))
TOP_LIMIT = int(os.getenv("TOP_LIMIT", "20"))
MESSAGE_LOG_LIMIT = int(os.getenv("MESSAGE_LOG_LIMIT", "1000"))

ADD_ONLY_USER_IDS = {
    848523015,
    8025004558,
    2029428163,
    8353991451,
    8825145872,
    7629122327,
}

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
        conn = sqlite3.connect(self.path, check_same_thread=False)
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
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS notes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    player_user_id INTEGER NOT NULL,
                    note_text TEXT NOT NULL,
                    issuer_user_id INTEGER NOT NULL,
                    issuer_name TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (player_user_id) REFERENCES tracked_players(user_id)
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

    def add_note(self, player_user_id: int, note_text: str, issuer_user_id: int, issuer_name: str):
        with closing(self.connect()) as conn, conn:
            conn.execute(
                """
                INSERT INTO notes(player_user_id, note_text, issuer_user_id, issuer_name, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (player_user_id, note_text[:1000], issuer_user_id, issuer_name[:100], iso_now()),
            )

    def get_notes_for_player(self, player_user_id: int):
        with closing(self.connect()) as conn:
            return conn.execute(
                """
                SELECT id, note_text, issuer_user_id, issuer_name, created_at
                FROM notes
                WHERE player_user_id = ?
                ORDER BY created_at ASC
                """,
                (player_user_id,),
            ).fetchall()

    def get_recent_notes_for_player(self, player_user_id: int, limit: int = 3):
        with closing(self.connect()) as conn:
            return conn.execute(
                """
                SELECT id, note_text, issuer_user_id, issuer_name, created_at
                FROM notes
                WHERE player_user_id = ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (player_user_id, limit),
            ).fetchall()[::-1]

    def delete_notes_by_indexes(self, player_user_id: int, indexes: list[int]) -> int:
        notes = self.get_notes_for_player(player_user_id)
        if not notes:
            return 0
        ids_to_delete = []
        for idx in indexes:
            if 1 <= idx <= len(notes):
                ids_to_delete.append(notes[idx - 1]["id"])
        ids_to_delete = list(dict.fromkeys(ids_to_delete))
        if not ids_to_delete:
            return 0
        with closing(self.connect()) as conn, conn:
            qmarks = ",".join("?" for _ in ids_to_delete)
            cur = conn.execute(
                f"DELETE FROM notes WHERE player_user_id = ? AND id IN ({qmarks})",
                [player_user_id, *ids_to_delete],
            )
            return cur.rowcount

    def players_with_notes_summary(self):
        with closing(self.connect()) as conn:
            return conn.execute(
                """
                SELECT p.user_id, p.username, p.display_name,
                       COUNT(n.id) AS notes_count,
                       MAX(n.created_at) AS last_note_at
                FROM tracked_players p
                JOIN notes n ON n.player_user_id = p.user_id
                GROUP BY p.user_id, p.username, p.display_name
                ORDER BY COALESCE(p.username, p.display_name) COLLATE NOCASE
                """
            ).fetchall()

    def all_notes_grouped(self):
        with closing(self.connect()) as conn:
            players = conn.execute(
                """
                SELECT DISTINCT p.user_id, p.username, p.display_name
                FROM tracked_players p
                JOIN notes n ON n.player_user_id = p.user_id
                ORDER BY COALESCE(p.username, p.display_name) COLLATE NOCASE
                """
            ).fetchall()
            result = []
            for p in players:
                notes = conn.execute(
                    """
                    SELECT id, note_text, issuer_name, created_at
                    FROM notes
                    WHERE player_user_id = ?
                    ORDER BY created_at ASC
                    """,
                    (p["user_id"],),
                ).fetchall()
                result.append({"player": p, "notes": notes})
            return result


db = Database(DB_PATH)


def display_handle(row_or_stats) -> str:
    username = row_or_stats["username"] if isinstance(row_or_stats, sqlite3.Row) else row_or_stats.get("username")
    display_name = row_or_stats["display_name"] if isinstance(row_or_stats, sqlite3.Row) else row_or_stats.get("display_name")
    return f"@{username}" if username else display_name


def issuer_name_from_user(user: User) -> str:
    if user.username:
        return user.username
    return (user.full_name or user.first_name or str(user.id)).strip()


def format_date_only(iso_text: str) -> str:
    try:
        dt = datetime.fromisoformat(iso_text)
        return dt.date().isoformat()
    except Exception:
        return iso_text[:10]


def humanize_last_seen(iso_text: Optional[str]) -> str:
    if not iso_text:
        return "Unknown"
    try:
        dt = datetime.fromisoformat(iso_text)
        diff = now_utc() - dt
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
    except Exception:
        return iso_text


def message_is_contribution(text: str, keywords: list[str]) -> bool:
    lowered = text.lower()
    has_keyword = any(kw.lower() in lowered for kw in keywords)
    has_three_digit = False
    for match in THREE_DIGIT_RE.findall(text):
        try:
            num = int(match)
            if 100 <= num <= 999:
                has_three_digit = True
                break
        except Exception:
            pass
    return has_keyword or has_three_digit


async def is_admin(chat_id: int, user_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    try:
        admins = await context.bot.get_chat_administrators(chat_id)
        return any(a.user.id == user_id for a in admins)
    except Exception:
        return False


async def can_add_only(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user_id = update.effective_user.id
    if user_id in ADD_ONLY_USER_IDS:
        return True
    return False


async def is_authorized(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user_id = update.effective_user.id
    if user_id == OWNER_ID:
        return True
    chat = update.effective_chat
    if chat and chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
        return await is_admin(chat.id, user_id, context)
    return False


async def is_authorized_for_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    return await is_authorized(update, context) or await can_add_only(update, context)


def get_target_from_reply(update: Update) -> Optional[TargetPlayer]:
    msg = update.effective_message
    if not msg or not msg.reply_to_message or not msg.reply_to_message.from_user:
        return None
    u = msg.reply_to_message.from_user
    return TargetPlayer(
        user_id=u.id,
        username=(u.username.lower() if u.username else None),
        display_name=(u.full_name or u.first_name or str(u.id)).strip(),
    )


def find_target_by_username_arg(username: str) -> Optional[TargetPlayer]:
    row = db.find_player_by_username(username)
    if not row:
        return None
    return TargetPlayer(
        user_id=row["user_id"],
        username=row["username"],
        display_name=row["display_name"],
    )


async def send_private_text(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    try:
        await context.bot.send_message(chat_id=update.effective_user.id, text=text)
        return True
    except Exception:
        return False


async def add_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_authorized_for_add(update, context):
        return
    msg = update.effective_message
    target_user = None
    if msg.reply_to_message and msg.reply_to_message.from_user:
        target_user = msg.reply_to_message.from_user
    elif context.args and context.args[0].startswith("@"):
        row = db.find_player_by_username(context.args[0])
        if row:
            class TempUser:
                id = row["user_id"]
                username = row["username"]
                full_name = row["display_name"]
                first_name = row["display_name"]
            target_user = TempUser()
        else:
            return
    if not target_user:
        return
    inserted = db.upsert_player(target_user, update.effective_user.id)
    handle = f"@{target_user.username}" if getattr(target_user, "username", None) else (target_user.full_name or target_user.first_name)
    if inserted:
        await msg.reply_text(f"{handle} è stato aggiunto al sistema di monitoraggio Elite.")
    else:
        await msg.reply_text(f"{handle} è già presente nel sistema di monitoraggio.")


async def remove_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_authorized(update, context):
        return
    msg = update.effective_message
    target = get_target_from_reply(update)
    if not target and context.args and context.args[0].startswith("@"):
        target = find_target_by_username_arg(context.args[0])
    if not target:
        return
    db.remove_player(target.user_id)


async def ch_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_authorized(update, context):
        return
    msg = update.effective_message
    target = get_target_from_reply(update)
    if not target and context.args and context.args[0].startswith("@"):
        target = find_target_by_username_arg(context.args[0])
    if not target:
        return
    db.refresh_ranks()
    stats = db.player_stats(target.user_id)
    handle = f"@{stats['username']}" if stats["username"] else stats["display_name"]
    lines = [
        f"Player: {handle}",
        "",
        f"Total Contributions: {stats['total']}",
        f"Current Rank: #{stats['current_rank'] or '-'}",
        f"Daily Contributions: {stats['daily']}",
        f"Weekly Contributions: {stats['weekly']}",
        f"Last Seen: {humanize_last_seen(stats['last_seen_at'])}",
        f"Tracked Since: {format_date_only(stats['added_at'])}",
    ]
    recent_notes = db.get_recent_notes_for_player(target.user_id, 3)
    if recent_notes:
        lines += ["", "Recent Notes:"]
        for n in recent_notes:
            lines.append(f"- {format_date_only(n['created_at'])} | {n['note_text']} | by {n['issuer_name']}")
    await send_private_text(update, context, "\n".join(lines))


async def msg_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_authorized(update, context):
        return
    if update.effective_chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return

    target = get_target_from_reply(update)
    if not target:
        await update.effective_message.reply_text("Reply to the player's message, then use .msg or .msg 50.")
        return

    limit = 20
    if context.args and context.args[0].isdigit():
        limit = max(1, min(100, int(context.args[0])))

    rows = db.recent_messages_by_chat(target.user_id, update.effective_chat.id, limit)
    handle = f"@{target.username}" if target.username else target.display_name

    if not rows:
        await update.effective_message.reply_text(f"No stored messages found for {handle} in this group.")
        return

    lines = [f"Last Messages for {handle}", ""]
    for i, r in enumerate(rows, start=1):
        marker = "+1" if r["contribution_count"] else "0"
        lines.append(f"{i}. [{format_date_time_local(r['created_at'])}] ({marker}) {r['message_text']}")

    for chunk in split_text_chunks("\n".join(lines)):
        await update.effective_message.reply_text(chunk)


async def msg_all_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_authorized(update, context):
        return
    if update.effective_chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return

    target = get_target_from_reply(update)
    if not target:
        await update.effective_message.reply_text("Reply to the player's message, then use .msg_all or .msg_all 50.")
        return

    limit = 20
    if context.args and context.args[0].isdigit():
        limit = max(1, min(100, int(context.args[0])))

    rows = db.recent_messages_any_user(target.user_id, limit)
    handle = f"@{target.username}" if target.username else target.display_name

    if not rows:
        await update.effective_message.reply_text(f"No stored messages found for {handle}.")
        return

    lines = [f"Last Messages for {handle} (All Groups)", ""]
    for i, r in enumerate(rows, start=1):
        marker = "+1" if r["contribution_count"] else "0"
        lines.append(f"{i}. [{format_date_time_local(r['created_at'])}] ({marker}) {r['message_text']}")

    for chunk in split_text_chunks("\n".join(lines)):
        await update.effective_message.reply_text(chunk)


async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_authorized(update, context):
        return
    msg = update.effective_message
    target = get_target_from_reply(update)
    if not target and context.args and context.args[0].startswith("@"):
        target = find_target_by_username_arg(context.args[0])
    if not target:
        return
    db.refresh_ranks()
    stats = db.player_stats(target.user_id)
    handle = f"@{stats['username']}" if stats["username"] else stats["display_name"]
    tracked_since = format_date_only(stats["added_at"])
    try:
        start = datetime.fromisoformat(stats["added_at"]) if stats["added_at"] else now_utc()
        days = max(1, (now_utc() - start).days + 1)
    except Exception:
        days = 1
    avg_daily = round(stats["total"] / days)
    lines = [
        f"Player: {handle}",
        "",
        f"Tracked Since: {tracked_since}",
        f"Total Contributions: {stats['total']}",
        f"Average Daily Contributions: {avg_daily}",
        f"Highest Rank: #{stats['highest_rank'] or '-'}",
        f"Current Rank: #{stats['current_rank'] or '-'}",
        f"Last Seen: {humanize_last_seen(stats['last_seen_at'])}",
    ]
    notes = db.get_notes_for_player(target.user_id)
    if notes:
        lines += ["", "All Notes:"]
        for i, n in enumerate(notes, start=1):
            lines.append(f"{i}. {format_date_only(n['created_at'])} | {n['note_text']} | by {n['issuer_name']}")
    await send_private_text(update, context, "\n".join(lines))


async def top_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_authorized(update, context):
        return
    ranked = db.refresh_ranks()[:TOP_LIMIT]
    lines = ["Elite Contribution Ranking", ""]
    for item in ranked:
        handle = f"@{item['username']}" if item["username"] else item["display_name"]
        lines.append(f"#{item['rank']} {handle} - {item['total']}")
    await send_private_text(update, context, "\n".join(lines))


async def suspects_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_authorized(update, context):
        return
    ranked = db.refresh_ranks()
    suspects = [r for r in ranked if r["total"] <= SUSPECT_THRESHOLD]
    lines = ["Potential Inactive Players", ""]
    for item in suspects:
        handle = f"@{item['username']}" if item["username"] else item["display_name"]
        lines.append(f"{handle} - {item['total']} contributions")
    await send_private_text(update, context, "\n".join(lines))


async def list_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_authorized(update, context):
        return
    rows = db.tracked_players()
    lines = [f"Tracked Players ({len(rows)})", ""]
    for r in rows:
        lines.append(display_handle(r))
    await send_private_text(update, context, "\n".join(lines))


async def addkw_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_authorized(update, context):
        return
    if not context.args:
        return
    db.add_keyword(" ".join(context.args).strip(), update.effective_user.id)


async def removekw_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_authorized(update, context):
        return
    if not context.args:
        return
    db.remove_keyword(" ".join(context.args).strip())


async def keywords_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_authorized(update, context):
        return
    lines = ["Keywords", ""] + db.get_keywords()
    await send_private_text(update, context, "\n".join(lines))


async def notes_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_authorized(update, context):
        return
    args = context.args
    if not args:
        rows = db.players_with_notes_summary()
        if not rows:
            await send_private_text(update, context, "No notes found.")
            return
        lines = ["Players With Notes", ""]
        for r in rows:
            handle = f"@{r['username']}" if r["username"] else r["display_name"]
            lines.append(f"{handle} — {r['notes_count']} notes")
        await send_private_text(update, context, "\n".join(lines))
        return
    if len(args) == 1 and args[0].lower() == "full":
        grouped = db.all_notes_grouped()
        if not grouped:
            await send_private_text(update, context, "No notes found.")
            return
        lines = ["All Player Notes", ""]
        for block in grouped:
            p = block["player"]
            handle = f"@{p['username']}" if p["username"] else p["display_name"]
            lines.append(handle)
            for i, n in enumerate(block["notes"], start=1):
                lines.append(f"{i}. {format_date_only(n['created_at'])} | {n['note_text']} | by {n['issuer_name']}")
            lines.append("")
        await send_private_text(update, context, "\n".join(lines).strip())
        return
    if len(args) == 1 and args[0].startswith("@"):
        target = find_target_by_username_arg(args[0])
        if not target:
            await send_private_text(update, context, "Player not found.")
            return
        notes = db.get_notes_for_player(target.user_id)
        handle = f"@{target.username}" if target.username else target.display_name
        if not notes:
            await send_private_text(update, context, f"No notes for {handle}.")
            return
        lines = [f"Notes for {handle}", ""]
        for i, n in enumerate(notes, start=1):
            lines.append(f"{i}. {format_date_only(n['created_at'])} | {n['note_text']} | by {n['issuer_name']}")
        await send_private_text(update, context, "\n".join(lines))
        return
    await send_private_text(update, context, "Usage:\n/notes\n/notes full\n/notes @username")


async def handle_dot_commands(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg or not msg.text or update.effective_chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return
    text = msg.text.strip()

    if text.lower() == ".add":
        if not await is_authorized_for_add(update, context):
            return
        target = get_target_from_reply(update)
        if not target:
            return
        class TempUser:
            id = target.user_id
            username = target.username
            full_name = target.display_name
            first_name = target.display_name
        inserted = db.upsert_player(TempUser(), update.effective_user.id)
        handle = f"@{target.username}" if target.username else target.display_name
        if inserted:
            await msg.reply_text(f"{handle} è stato aggiunto al sistema di monitoraggio Elite.")
        else:
            await msg.reply_text(f"{handle} è già presente nel sistema di monitoraggio.")
        return

    if text.lower().startswith(".note"):
        if not await is_authorized(update, context):
            return
        target = get_target_from_reply(update)
        if not target or not db.is_tracked(target.user_id):
            return
        note_text = text[5:].strip()
        if not note_text:
            return
        issuer = update.effective_user
        db.add_note(target.user_id, note_text, issuer.id, issuer_name_from_user(issuer))
        await msg.reply_text("Nota aggiunta con successo.")
        return

    if text.lower().startswith(".delnote"):
        if not await is_authorized(update, context):
            return
        target = get_target_from_reply(update)
        if not target or not db.is_tracked(target.user_id):
            return
        parts = text.split()[1:]
        indexes = [int(p) for p in parts if p.isdigit()]
        if not indexes:
            return
        deleted = db.delete_notes_by_indexes(target.user_id, indexes)
        if deleted == 1:
            await msg.reply_text("Nota rimossa con successo.")
        elif deleted > 1:
            await msg.reply_text("Note rimosse con successo.")
        return

    if text.lower() == ".remove":
        if not await is_authorized(update, context):
            return
        target = get_target_from_reply(update)
        if target:
            db.remove_player(target.user_id)
        return

    if text.lower() == ".ch":
        if not await is_authorized(update, context):
            return
        context.args = []
        await ch_command(update, context)
        return

    if text.lower().startswith(".msg"):
        if not await is_authorized(update, context):
            return
        parts = text.split()[1:]
        context.args = ["", *parts] if parts else []
        await msg_command(update, context)
        return

    if text.lower() == ".history":
        if not await is_authorized(update, context):
            return
        context.args = []
        await history_command(update, context)
        return


async def track_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg or not msg.text or msg.text.startswith("/") or msg.text.startswith("."):
        return
    user = msg.from_user
    if not user or not db.is_tracked(user.id):
        return
    db.update_seen(user)
    is_contrib = message_is_contribution(msg.text, db.get_keywords())
    db.add_message(user.id, update.effective_chat.id, msg.message_id, msg.text, is_contrib)


def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is missing")
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("add", add_command))
    app.add_handler(CommandHandler("remove", remove_command))
    app.add_handler(CommandHandler("ch", ch_command))
    app.add_handler(CommandHandler("msg", msg_command))
    app.add_handler(CommandHandler("msg_all", msg_all_command))
    app.add_handler(CommandHandler("history", history_command))
    app.add_handler(CommandHandler("top", top_command))
    app.add_handler(CommandHandler("suspects", suspects_command))
    app.add_handler(CommandHandler("list", list_command))
    app.add_handler(CommandHandler("addkw", addkw_command))
    app.add_handler(CommandHandler("removekw", removekw_command))
    app.add_handler(CommandHandler("keywords", keywords_command))
    app.add_handler(CommandHandler("notes", notes_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_dot_commands))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, track_messages))
    app.add_error_handler(error_handler)
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
