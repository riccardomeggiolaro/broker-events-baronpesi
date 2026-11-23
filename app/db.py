"""Database SQLite: dispositivi, certificati, messaggi, allegati, eventi di connessione."""
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
    id          TEXT PRIMARY KEY,           -- = CN del certificato = client ID = topic
    name        TEXT NOT NULL DEFAULT '',
    notes       TEXT NOT NULL DEFAULT '',
    channel_open INTEGER NOT NULL DEFAULT 1, -- 0 = canale chiuso (ACL rimossa, certificato ancora valido)
    renew_requested_at TEXT,                 -- nuovo certificato richiesto all'in-out dal pannello
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

-- Connessioni inoltrate dalla porta unica (app/mux.py): porta locale verso
-- Mosquitto/registrazione -> IP reale del client
CREATE TABLE IF NOT EXISTS mux_conns (
    local_port  INTEGER NOT NULL,
    ts          REAL NOT NULL,              -- epoch
    ip          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_mux_port ON mux_conns(local_port, ts);

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


# Colonne aggiunte dopo la prima versione: (tabella, colonna, definizione)
MIGRATIONS = [
    ("devices", "renew_requested_at", "TEXT"),
]


def init():
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    c = conn()
    c.executescript(SCHEMA)
    for table, column, decl in MIGRATIONS:
        if column not in {r["name"] for r in c.execute(f"PRAGMA table_info({table})")}:
            c.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


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


LOOPBACK = {"127.0.0.1", "::1", "::ffff:127.0.0.1"}


def mux_record(local_port: int, ip: str) -> None:
    conn().execute("INSERT INTO mux_conns(local_port, ts, ip) VALUES(?, ?, ?)", (local_port, time.time(), ip))


def mux_prune(max_age: float = 2 * 86400) -> None:
    conn().execute("DELETE FROM mux_conns WHERE ts < ?", (time.time() - max_age,))


def real_ip(ip: str, port: int | None, at: float | None = None, window: float = 5.0) -> str:
    """IP reale di una connessione arrivata dalla porta unica (sorgente locale `ip:port`).

    `at`: istante della connessione (per righe di log lette in ritardo); senza,
    vale l'ultima connessione registrata su quella porta (connessione ancora aperta).
    Le connessioni che non passano dalla porta unica restano invariate."""
    if ip not in LOOPBACK or not port:
        return ip
    if at is None:
        row = q1("SELECT ip FROM mux_conns WHERE local_port=? ORDER BY ts DESC LIMIT 1", port)
    else:
        row = q1(
            "SELECT ip FROM mux_conns WHERE local_port=? AND ts BETWEEN ? AND ? ORDER BY ts DESC LIMIT 1",
            port, at - window, at + window,
        )
    return row["ip"] if row else ip


def add_event(kind: str, device_id: str | None, detail: str = "", ts: str | None = None) -> None:
    conn().execute(
        "INSERT INTO events(ts, device_id, kind, detail) VALUES(?, ?, ?, ?)",
        (ts or now(), device_id, kind, detail),
    )
