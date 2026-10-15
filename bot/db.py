"""使用者／群組／主動排程資料表（一般 sqlite3，跟 LangGraph 的資料庫分開）。"""
import sqlite3
import threading
from datetime import datetime

import config
from bot import clock

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id           TEXT PRIMARY KEY,
    display_name      TEXT,
    is_friend         INTEGER DEFAULT 0,
    affection         INTEGER,
    first_seen        TEXT,
    last_user_msg_at  TEXT,
    user_msg_count    INTEGER DEFAULT 0,
    active_days       INTEGER DEFAULT 0,
    last_active_date  TEXT,
    gain_date         TEXT,
    gain_today        INTEGER DEFAULT 0,
    last_decay_date   TEXT,
    last_proactive_at TEXT,
    proactive_state   TEXT DEFAULT 'none'   -- none / waiting / replied / ignored
);
CREATE TABLE IF NOT EXISTS groups (
    group_id  TEXT PRIMARY KEY,
    active    INTEGER DEFAULT 1,
    joined_at TEXT
);
CREATE TABLE IF NOT EXISTS proactive_schedule (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      TEXT,
    plan_date    TEXT,
    scheduled_at TEXT,
    trigger      TEXT,      -- miss / birthday / event
    detail       TEXT,
    status       TEXT DEFAULT 'pending'  -- pending / sent / skipped / expired
);
CREATE TABLE IF NOT EXISTS push_log (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT,
    sent_at TEXT,
    month   TEXT
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


class DB:
    def __init__(self, path=config.DB_PATH):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(_SCHEMA)
        self.lock = threading.Lock()

    def execute(self, sql, params=()):
        with self.lock:
            return self.conn.execute(sql, params)

    def one(self, sql, params=()):
        with self.lock:
            row = self.conn.execute(sql, params).fetchone()
        return dict(row) if row else None

    def all(self, sql, params=()):
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    # ---------- users ----------
    def get_user(self, user_id: str) -> dict:
        user = self.one("SELECT * FROM users WHERE user_id=?", (user_id,))
        if user is None:
            self.execute(
                "INSERT INTO users (user_id, affection, first_seen) VALUES (?, ?, ?)",
                (user_id, config.AFFECTION_INIT, clock.now().isoformat()),
            )
            user = self.one("SELECT * FROM users WHERE user_id=?", (user_id,))
        return user

    def update_user(self, user_id: str, **fields):
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        self.execute(f"UPDATE users SET {cols} WHERE user_id=?", (*fields.values(), user_id))

    def friends(self) -> list[dict]:
        return self.all("SELECT * FROM users WHERE is_friend=1")

    def record_user_message(self, user_id: str, now: datetime):
        """使用者傳訊時更新活躍統計，並把等待中的主動訊息標記為已回覆。"""
        user = self.get_user(user_id)
        today = now.date().isoformat()
        fields = {
            "last_user_msg_at": now.isoformat(),
            "user_msg_count": user["user_msg_count"] + 1,
        }
        if user["last_active_date"] != today:
            fields["last_active_date"] = today
            fields["active_days"] = user["active_days"] + 1
        if user["proactive_state"] == "waiting":
            fields["proactive_state"] = "replied"
        self.update_user(user_id, **fields)

    # ---------- groups ----------
    def set_group(self, group_id: str, active: bool):
        self.execute(
            "INSERT INTO groups (group_id, active, joined_at) VALUES (?, ?, ?) "
            "ON CONFLICT(group_id) DO UPDATE SET active=excluded.active",
            (group_id, int(active), clock.now().isoformat()),
        )

    # ---------- push budget ----------
    def log_push(self, user_id: str, now: datetime):
        self.execute(
            "INSERT INTO push_log (user_id, sent_at, month) VALUES (?, ?, ?)",
            (user_id, now.isoformat(), now.strftime("%Y-%m")),
        )

    def pushes_this_month(self, now: datetime) -> int:
        row = self.one("SELECT COUNT(*) AS n FROM push_log WHERE month=?", (now.strftime("%Y-%m"),))
        return row["n"]

    # ---------- meta ----------
    def get_meta(self, key: str):
        row = self.one("SELECT value FROM meta WHERE key=?", (key,))
        return row["value"] if row else None

    def set_meta(self, key: str, value: str):
        self.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
