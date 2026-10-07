"""SQLite persistence. Each aggregate is stored as a JSON document plus a few indexed columns.

A single connection guarded by a re-entrant lock keeps things simple and safe for the small
worker pool that processes documents in the background.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from .settings import get_settings

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS claims (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS documents (
    id TEXT PRIMARY KEY,
    claim_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_documents_claim ON documents(claim_id);
CREATE TABLE IF NOT EXISTS expenses (
    id TEXT PRIMARY KEY,
    claim_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_expenses_claim ON expenses(claim_id);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    claim_id TEXT NOT NULL,
    data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_messages_claim ON messages(claim_id);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    claim_id TEXT NOT NULL,
    data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_events_claim ON events(claim_id);
CREATE TABLE IF NOT EXISTS submissions (
    reference TEXT PRIMARY KEY,
    claim_id TEXT NOT NULL,
    data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS counters (
    name TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _connection() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        settings = get_settings()
        _conn = sqlite3.connect(settings.db_path, check_same_thread=False, isolation_level=None)
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.executescript(SCHEMA)
    return _conn


def reset_connection() -> None:
    """Used by tests to point at a fresh database."""
    global _conn
    with _lock:
        if _conn is not None:
            _conn.close()
        _conn = None


@contextmanager
def locked() -> Iterator[sqlite3.Connection]:
    with _lock:
        yield _connection()


def lock() -> threading.RLock:
    return _lock


def _dump(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, default=str)


# ---------------------------------------------------------------- counters

def next_counter(name: str, start: int = 1) -> int:
    with locked() as c:
        row = c.execute("SELECT value FROM counters WHERE name=?", (name,)).fetchone()
        value = (row[0] + 1) if row else start
        c.execute("INSERT OR REPLACE INTO counters(name, value) VALUES(?, ?)", (name, value))
        return value


# ---------------------------------------------------------------- claims

def save_claim(claim: dict) -> None:
    with locked() as c:
        c.execute(
            "INSERT OR REPLACE INTO claims(id, created_at, data) VALUES(?, ?, ?)",
            (claim["id"], claim["createdAt"], _dump(claim)),
        )


def get_claim(claim_id: str) -> dict | None:
    with locked() as c:
        row = c.execute("SELECT data FROM claims WHERE id=?", (claim_id,)).fetchone()
        return json.loads(row[0]) if row else None


def list_claims() -> list[dict]:
    with locked() as c:
        rows = c.execute("SELECT data FROM claims ORDER BY created_at DESC").fetchall()
        return [json.loads(r[0]) for r in rows]


# ---------------------------------------------------------------- documents

def save_document(doc: dict) -> None:
    with locked() as c:
        c.execute(
            "INSERT OR REPLACE INTO documents(id, claim_id, seq, data) VALUES(?, ?, ?, ?)",
            (doc["id"], doc["claimId"], doc["seq"], _dump(doc)),
        )


def get_document(doc_id: str) -> dict | None:
    with locked() as c:
        row = c.execute("SELECT data FROM documents WHERE id=?", (doc_id,)).fetchone()
        return json.loads(row[0]) if row else None


def list_documents(claim_id: str) -> list[dict]:
    with locked() as c:
        rows = c.execute("SELECT data FROM documents WHERE claim_id=? ORDER BY seq", (claim_id,)).fetchall()
        return [json.loads(r[0]) for r in rows]


def count_documents(claim_id: str) -> int:
    with locked() as c:
        return c.execute("SELECT COUNT(*) FROM documents WHERE claim_id=?", (claim_id,)).fetchone()[0]


# ---------------------------------------------------------------- expenses

def save_expense(exp: dict) -> None:
    with locked() as c:
        c.execute(
            "INSERT OR REPLACE INTO expenses(id, claim_id, seq, data) VALUES(?, ?, ?, ?)",
            (exp["id"], exp["claimId"], exp["seq"], _dump(exp)),
        )


def get_expense(exp_id: str) -> dict | None:
    with locked() as c:
        row = c.execute("SELECT data FROM expenses WHERE id=?", (exp_id,)).fetchone()
        return json.loads(row[0]) if row else None


def list_expenses(claim_id: str) -> list[dict]:
    with locked() as c:
        rows = c.execute("SELECT data FROM expenses WHERE claim_id=? ORDER BY seq", (claim_id,)).fetchall()
        return [json.loads(r[0]) for r in rows]


def delete_expense(exp_id: str) -> None:
    with locked() as c:
        c.execute("DELETE FROM expenses WHERE id=?", (exp_id,))


# ---------------------------------------------------------------- chat + events

def add_message(claim_id: str, message: dict) -> dict:
    with locked() as c:
        message = {"createdAt": now_iso(), **message}
        cur = c.execute("INSERT INTO messages(claim_id, data) VALUES(?, ?)", (claim_id, _dump(message)))
        message["id"] = cur.lastrowid
        c.execute("UPDATE messages SET data=? WHERE id=?", (_dump(message), cur.lastrowid))
        return message


def list_messages(claim_id: str, limit: int = 500) -> list[dict]:
    with locked() as c:
        rows = c.execute(
            "SELECT data FROM messages WHERE claim_id=? ORDER BY id DESC LIMIT ?", (claim_id, limit)
        ).fetchall()
        return [json.loads(r[0]) for r in reversed(rows)]


def add_event(claim_id: str, event: dict) -> None:
    with locked() as c:
        c.execute("INSERT INTO events(claim_id, data) VALUES(?, ?)", (claim_id, _dump(event)))


def list_events(claim_id: str, limit: int = 200) -> list[dict]:
    with locked() as c:
        rows = c.execute(
            "SELECT data FROM events WHERE claim_id=? ORDER BY id DESC LIMIT ?", (claim_id, limit)
        ).fetchall()
        return [json.loads(r[0]) for r in reversed(rows)]


# ---------------------------------------------------------------- mock finance submissions

def save_submission(reference: str, claim_id: str, data: dict) -> None:
    with locked() as c:
        c.execute(
            "INSERT OR REPLACE INTO submissions(reference, claim_id, data) VALUES(?, ?, ?)",
            (reference, claim_id, _dump(data)),
        )


def get_submission(reference: str) -> dict | None:
    with locked() as c:
        row = c.execute("SELECT data FROM submissions WHERE reference=?", (reference,)).fetchone()
        return json.loads(row[0]) if row else None
