"""Database SQLite: dispositivi, certificati, messaggi, allegati, eventi di connessione."""
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
    id          TEXT PRIMARY KEY,           -- = CN del certificato = client ID = topic
    name        TEXT NOT NULL DEFAULT '',
    notes       TEXT NOT NULL DEFAULT '',
    channel_open INTEGER NOT NULL DEFAULT 1, -- 0 = canale chiuso (ACL rimossa, certificato ancora valido)
    created_at  TEXT NOT NULL,
    last_seen_at TEXT,
    last_ip     TEXT
);

CREATE TABLE IF NOT EXISTS certificates (
    serial      TEXT PRIMARY KEY,           -- esadecimale
    device_id   TEXT NOT NULL,
    not_before  TEXT NOT NULL,
    not_after   TEXT NOT NULL,
    issued_at   TEXT NOT NULL,
    revoked_at  TEXT,
    revoke_reason TEXT
);
CREATE INDEX IF NOT EXISTS ix_cert_device ON certificates(device_id);

CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id   TEXT NOT NULL,
    topic       TEXT NOT NULL,
    received_at TEXT NOT NULL,
    message_id  TEXT UNIQUE,                -- message_id dell'in-out (deduplica QoS 1)
    event_type  TEXT,
    instance_name TEXT,
    weigher_name TEXT,
    size        INTEGER NOT NULL,
    is_json     INTEGER NOT NULL DEFAULT 1,
    body        TEXT                        -- JSON senza i contenuti base64 degli allegati
);
CREATE INDEX IF NOT EXISTS ix_msg_device ON messages(device_id, received_at);
CREATE INDEX IF NOT EXISTS ix_msg_received ON messages(received_at);

CREATE TABLE IF NOT EXISTS attachments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id  INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL,              -- image | pdf | csv | file
    name        TEXT NOT NULL,
    content_type TEXT NOT NULL,
    path        TEXT NOT NULL,              -- relativo a ATTACH_DIR
    size        INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_att_msg ON attachments(message_id);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    device_id   TEXT,
    kind        TEXT NOT NULL,              -- connect | disconnect | denied | admin
    detail      TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_evt_device ON events(device_id, ts);

CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

_local = threading.local()


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def conn() -> sqlite3.Connection:
    """Una connessione per thread (sqlite3 non condivide le connessioni tra thread)."""
    c = getattr(_local, "conn", None)
    if c is None:
        c = _connect()
        _local.conn = c
    return c


@contextmanager
def tx():
    c = conn()
    c.execute("BEGIN IMMEDIATE")
    try:
        yield c
        c.execute("COMMIT")
    except BaseException:
        c.execute("ROLLBACK")
        raise


def init():
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn().executescript(SCHEMA)


def q(sql: str, *args) -> list:
    return conn().execute(sql, args).fetchall()


def q1(sql: str, *args):
    return conn().execute(sql, args).fetchone()


def kv_get(key: str, default=None):
    row = q1("SELECT value FROM kv WHERE key=?", key)
    return row["value"] if row else default


def kv_set(key: str, value) -> None:
    conn().execute(
        "INSERT INTO kv(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, None if value is None else str(value)),
    )


def add_event(kind: str, device_id: str | None, detail: str = "", ts: str | None = None) -> None:
    conn().execute(
        "INSERT INTO events(ts, device_id, kind, detail) VALUES(?, ?, ?, ?)",
        (ts or now(), device_id, kind, detail),
    )
