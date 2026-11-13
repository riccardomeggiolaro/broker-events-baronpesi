"""Subscriber interno: riceve tutti i messaggi dal broker e li archivia.

Usa una sessione persistente (clean_session=False) e QoS 1: se il pannello è
fermo, Mosquitto tiene in coda i messaggi e li consegna alla riconnessione.
I duplicati (possibili con QoS 1) vengono scartati grazie al message_id.
"""
import base64
import binascii
import json
import re
import shutil
import ssl
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import paho.mqtt.client as mqtt

from . import config, db, pki

KIND_EXT = {"image": ".png", "pdf": ".pdf", "csv": ".csv"}
SAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")
MAX_TEXT_BODY = 256 * 1024


def _safe_name(name: str, fallback: str) -> str:
    name = SAFE_RE.sub("_", (name or "").strip()).strip("._") or fallback
    return name[:120]


def message_dir(device_id: str, received_at: str, row_id: int) -> Path:
    """Cartella degli allegati di un messaggio, relativa a ATTACH_DIR."""
    return Path(_safe_name(device_id, "unknown")) / received_at[:7] / str(row_id)


class Ingest:
    def __init__(self, on_message=None):
        self.on_message = on_message  # callback(summary_dict) per il monitor live
        self.connected = False
        self.last_error: str | None = None
        self.received = 0
        self.client: mqtt.Client | None = None

    # ------------------------------------------------------------------
    def start(self) -> None:
        client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=config.MONITOR_ID,
            clean_session=False,
        )
        client.tls_set(
            ca_certs=str(pki.CA_CRT),
            certfile=str(pki.MONITOR_CRT),
            keyfile=str(pki.MONITOR_KEY),
            cert_reqs=ssl.CERT_REQUIRED,
        )
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message
        client.reconnect_delay_set(min_delay=1, max_delay=30)
        client.connect_async("localhost", config.BROKER_INTERNAL_PORT, keepalive=30)
        client.loop_start()
        self.client = client

    def stop(self) -> None:
        if self.client:
            self.client.disconnect()
            self.client.loop_stop()

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            self.connected = False
            self.last_error = f"connessione rifiutata: {reason_code}"
            return
        self.connected = True
        self.last_error = None
        client.subscribe("#", qos=1)

    def _on_disconnect(self, client, userdata, flags, reason_code, properties):
        self.connected = False
        if reason_code.is_failure:
            self.last_error = f"disconnesso: {reason_code}"

    def _on_message(self, client, userdata, msg: mqtt.MQTTMessage):
        try:
            summary = self.store(msg.topic, msg.payload)
            self.received += 1
            if summary and self.on_message:
                self.on_message(summary)
        except Exception as e:
            # Il messaggio è comunque confermato al broker: lo si segnala nel registro eventi
            self.last_error = f"errore archiviando un messaggio su {msg.topic}: {e}"
            print(f"[ingest] {self.last_error}", flush=True)
            try:
                db.add_event("admin", msg.topic.split("/")[0], self.last_error)
            except Exception:
                pass

    # ------------------------------------------------------------------
    def store(self, topic: str, payload: bytes) -> dict | None:
        device_id = topic.split("/")[0]
        received_at = db.now()
        size = len(payload)
        data = None
        try:
            data = json.loads(payload)
        except (ValueError, UnicodeDecodeError):
            pass

        files: list[dict] = []
        is_json = isinstance(data, (dict, list))
        if isinstance(data, dict):
            for f in data.get("files") or []:
                if isinstance(f, dict) and isinstance(f.get("data"), str):
                    files.append(f)
            body_obj = dict(data)
            if "files" in body_obj:
                body_obj["files"] = [
                    {k: v for k, v in f.items() if k != "data"} for f in files
                ]
            body = json.dumps(body_obj, ensure_ascii=False)
            message_id = str(data.get("message_id") or "") or None
        elif is_json:
            body = json.dumps(data, ensure_ascii=False)
            message_id = None
        else:
            message_id = None
            body = payload[:MAX_TEXT_BODY].decode("utf-8", errors="replace") if size <= MAX_TEXT_BODY else None

        meta = data if isinstance(data, dict) else {}
        with db.tx() as c:
            cur = c.execute(
                """INSERT OR IGNORE INTO messages
                   (device_id, topic, received_at, message_id, event_type, instance_name, weigher_name, size, is_json, body)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    device_id, topic, received_at, message_id,
                    _str(meta.get("event_type")), _str(meta.get("instance_name")), _str(meta.get("weigher_name")),
                    size, 1 if is_json else 0, body,
                ),
            )
            if cur.rowcount == 0:
                return None  # duplicato (stesso message_id)
            row_id = cur.lastrowid
            rel_dir = message_dir(device_id, received_at, row_id)
            abs_dir = config.ATTACH_DIR / rel_dir
            saved = []
            if not is_json and body is None:
                # Payload binario o molto grande non JSON: salvato come file
                saved.append(self._save(abs_dir, rel_dir, "payload.bin", payload, "file", "application/octet-stream"))
            for i, f in enumerate(files):
                kind = f.get("type") if f.get("type") in KIND_EXT else "file"
                name = _safe_name(f.get("name"), f"allegato-{i + 1}{KIND_EXT.get(kind, '')}")
                try:
                    content = base64.b64decode(f["data"], validate=False)
                except (binascii.Error, ValueError):
                    continue
                ctype = f.get("content_type") or "application/octet-stream"
                saved.append(self._save(abs_dir, rel_dir, f"{i + 1:02d}-{name}", content, kind, ctype))
            for a in saved:
                c.execute(
                    "INSERT INTO attachments(message_id, kind, name, content_type, path, size) VALUES(?,?,?,?,?,?)",
                    (row_id, a["kind"], a["name"], a["content_type"], a["path"], a["size"]),
                )
        return {
            "type": "message",
            "id": row_id,
            "device_id": device_id,
            "topic": topic,
            "received_at": received_at,
            "event_type": meta.get("event_type"),
            "instance_name": meta.get("instance_name"),
            "weigher_name": meta.get("weigher_name"),
            "size": size,
            "files": [{"kind": a["kind"], "name": a["name"]} for a in saved],
        }

    @staticmethod
    def _save(abs_dir: Path, rel_dir: Path, name: str, content: bytes, kind: str, ctype: str) -> dict:
        abs_dir.mkdir(parents=True, exist_ok=True)
        (abs_dir / name).write_bytes(content)
        return {"kind": kind, "name": name, "content_type": ctype, "path": str(rel_dir / name), "size": len(content)}


def _str(v) -> str | None:
    return None if v is None else str(v)


# ----------------------------------------------------------------------
# Pulizia dello storico
# ----------------------------------------------------------------------
def remove_attachments(msg_id: int) -> None:
    """Cancella dal disco la cartella degli allegati di un messaggio."""
    for a in db.q("SELECT path FROM attachments WHERE message_id=?", msg_id):
        d = (config.ATTACH_DIR / a["path"]).parent
        if d.resolve().is_relative_to(config.ATTACH_DIR.resolve()) and d != config.ATTACH_DIR:
            shutil.rmtree(d, ignore_errors=True)


def purge_old(days: int = config.RETENTION_DAYS) -> int:
    if days <= 0:
        return 0
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    rows = db.q("SELECT id FROM messages WHERE received_at < ?", cutoff)
    for r in rows:
        remove_attachments(r["id"])
    with db.tx() as c:
        c.execute("DELETE FROM messages WHERE received_at < ?", (cutoff,))
        c.execute("DELETE FROM events WHERE ts < ?", (cutoff,))
    # Rimuove le cartelle mensili rimaste vuote
    if config.ATTACH_DIR.exists():
        for month_dir in config.ATTACH_DIR.glob("*/*"):
            try:
                month_dir.rmdir()
            except OSError:
                pass
    return len(rows)


def purge_loop(stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            n = purge_old()
            if n:
                print(f"[purge] eliminati {n} messaggi più vecchi di {config.RETENTION_DAYS} giorni", flush=True)
        except Exception as e:
            print(f"[purge] errore: {e}", flush=True)
        stop.wait(6 * 3600)
