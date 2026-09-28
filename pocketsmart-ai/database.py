"""database.py - SQLite storage for users and recommendation history."""
import json
import os
import sqlite3
from contextlib import contextmanager
from typing import Optional

DB_PATH = os.getenv("DB_PATH", "pocketsmart.db")


@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with get_db() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                email TEXT NOT NULL UNIQUE,
                hashed_password TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id),
                category TEXT NOT NULL,
                budget REAL NOT NULL,
                inputs TEXT NOT NULL,
                result TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_history_user ON history(user_id, created_at);
            """
        )


# ---------- users ----------
def create_user(username: str, email: str, hashed_password: str) -> int:
    with get_db() as db:
        cur = db.execute(
            "INSERT INTO users (username, email, hashed_password) VALUES (?, ?, ?)",
            (username, email.lower(), hashed_password),
        )
        return cur.lastrowid


def get_user_by_username(username: str) -> Optional[dict]:
    with get_db() as db:
        row = db.execute(
            "SELECT * FROM users WHERE username = ? COLLATE NOCASE", (username,)
        ).fetchone()
        return dict(row) if row else None


# ---------- history ----------
def save_history(user_id: int, category: str, budget: float, inputs: dict, result: dict) -> int:
    with get_db() as db:
        cur = db.execute(
            "INSERT INTO history (user_id, category, budget, inputs, result) VALUES (?, ?, ?, ?, ?)",
            (user_id, category, budget, json.dumps(inputs), json.dumps(result)),
        )
        return cur.lastrowid


def _row_to_history(row: sqlite3.Row) -> dict:
    item = dict(row)
    item["inputs"] = json.loads(item["inputs"])
    item["result"] = json.loads(item["result"])
    return item


def list_history(user_id: int, category: Optional[str] = None, limit: int = 50) -> list:
    query = "SELECT * FROM history WHERE user_id = ?"
    params: list = [user_id]
    if category:
        query += " AND category = ?"
        params.append(category)
    query += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    with get_db() as db:
        return [_row_to_history(r) for r in db.execute(query, params).fetchall()]


def get_history_item(user_id: int, item_id: int) -> Optional[dict]:
    with get_db() as db:
        row = db.execute(
            "SELECT * FROM history WHERE id = ? AND user_id = ?", (item_id, user_id)
        ).fetchone()
        return _row_to_history(row) if row else None


def count_by_category(user_id: int) -> dict:
    counts = {"home": 0, "party": 0, "jewelry": 0}
    with get_db() as db:
        for row in db.execute(
            "SELECT category, COUNT(*) AS n FROM history WHERE user_id = ? GROUP BY category",
            (user_id,),
        ):
            counts[row["category"]] = row["n"]
    return counts
