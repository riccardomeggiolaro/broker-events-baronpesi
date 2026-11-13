"""Gestione di Mosquitto: configurazione, ACL, ricarica e lettura del log.

Mosquitto gira nello stesso container (supervisord). Il pannello riscrive ACL e
CRL e invia SIGHUP: Mosquitto ricarica ACL, CRL e certificati e riapre il file
di log senza disconnettere i client.

Lo stato online/offline dei dispositivi si ricava dal log di Mosquitto, così
gli in-out non devono pubblicare nulla oltre alle pesate.
"""
import os
import re
import signal
import threading
import time
from datetime import datetime, timezone

from . import config, db, pki

LOG_MAX_BYTES = 10 * 1024 * 1024

MOSQUITTO_CONF = """# File generato dal pannello: le modifiche manuali vengono sovrascritte all'avvio
per_listener_settings false
persistence true
persistence_location {mosq_dir}/
autosave_interval 60
pid_file {pid}

log_dest stdout
log_dest file {log}
log_type error
log_type warning
log_type notice
log_type information
log_timestamp true
log_timestamp_format %Y-%m-%dT%H:%M:%S%z
connection_messages true

allow_anonymous false
acl_file {acl}

# Dimensione massima di un messaggio (pesata + foto + PDF in base64)
max_packet_size {max_packet}
# Coda per i client con sessione persistente (il pannello) quando sono offline
max_queued_messages 5000
max_queued_bytes 1073741824
max_inflight_messages 20

listener {port}
protocol mqtt
cafile {ca}
certfile {server_crt}
keyfile {server_key}
crlfile {crl}
tls_version tlsv1.2
require_certificate true
use_identity_as_username true
"""


def write_conf() -> None:
    config.MOSQ_DIR.mkdir(parents=True, exist_ok=True)
    text = MOSQUITTO_CONF.format(
        mosq_dir=config.MOSQ_DIR,
        pid=config.MOSQ_PID,
        log=config.MOSQ_LOG,
        acl=config.MOSQ_ACL,
        max_packet=config.MAX_MESSAGE_MB * 1024 * 1024,
        port=config.BROKER_INTERNAL_PORT,
        ca=pki.CA_CRT,
        server_crt=pki.SERVER_CRT,
        server_key=pki.SERVER_KEY,
        crl=pki.CRL,
    )
    config.MOSQ_CONF.write_text(text)


def write_acl() -> None:
    """ACL: il pannello legge tutto; ogni dispositivo con canale aperto scrive solo sul proprio topic."""
    lines = [
        "# File generato dal pannello: le modifiche manuali vengono sovrascritte",
        f"user {config.MONITOR_ID}",
        "topic read #",
        "",
    ]
    rows = db.q("SELECT id FROM devices WHERE channel_open=1 ORDER BY id")
    for row in rows:
        dev = row["id"]
        lines += [f"user {dev}", f"topic write {dev}", f"topic write {dev}/#", ""]
    tmp = config.MOSQ_ACL.with_suffix(".tmp")
    tmp.write_text("\n".join(lines) + "\n")
    tmp.chmod(0o600)
    tmp.replace(config.MOSQ_ACL)


def _mosquitto_pid() -> int | None:
    try:
        return int(config.MOSQ_PID.read_text().strip())
    except (FileNotFoundError, ValueError):
        return None


def is_running() -> bool:
    pid = _mosquitto_pid()
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def reload() -> bool:
    """Chiede a Mosquitto di ricaricare ACL, CRL e certificati (SIGHUP)."""
    pid = _mosquitto_pid()
    if not pid:
        return False
    try:
        os.kill(pid, signal.SIGHUP)
        return True
    except OSError:
        return False


def kick(device_id: str, timeout: float = 5) -> bool:
    """Disconnette un client collegandosi con il suo stesso client ID.

    Mosquitto non ha un comando per scollegare un client: con il takeover del
    client ID chiude la vecchia connessione. Il dispositivo, ricollegandosi,
    trova il certificato revocato/sospeso nella CRL e viene rifiutato; l'in-out
    tiene quindi le pesate nella sua coda invece di perderle.
    """
    import paho.mqtt.client as mqtt  # import locale: il bootstrap non ne ha bisogno

    done = threading.Event()
    c = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2, client_id=device_id, clean_session=True)
    c.tls_set(ca_certs=str(pki.CA_CRT), certfile=str(pki.MONITOR_CRT), keyfile=str(pki.MONITOR_KEY))
    c.on_connect = lambda *a: done.set()
    try:
        c.connect("localhost", config.BROKER_INTERNAL_PORT, keepalive=10)
        c.loop_start()
        ok = done.wait(timeout)
        c.disconnect()
        c.loop_stop()
        return ok
    except Exception as e:
        print(f"[broker] impossibile scollegare {device_id}: {e}", flush=True)
        return False


def apply(kick_devices: list[str] | tuple = ()) -> None:
    """Riscrive CRL e ACL, ricarica il broker e scollega i dispositivi indicati.

    Con MQTT 3.1.1 Mosquitto conferma (PUBACK) anche le pubblicazioni negate
    dall'ACL: un dispositivo bloccato solo via ACL perderebbe le pesate. Per
    questo il blocco avviene prima sulla CRL (nuove connessioni rifiutate), poi
    si scollega il client e solo dopo si aggiorna l'ACL.
    """
    pki.write_crl()
    reload()
    online = [d for d in kick_devices if presence_ref and presence_ref.is_online(d)]
    if online:
        time.sleep(0.3)  # lascia a Mosquitto il tempo di rileggere la CRL
        for dev in online:
            kick(dev)
    write_acl()
    reload()


presence_ref: "Presence | None" = None


def bootstrap() -> None:
    pki.bootstrap()
    write_conf()
    write_acl()


# ----------------------------------------------------------------------
# Lettura del log: stato dei dispositivi
# ----------------------------------------------------------------------
TS_RE = r"^(?P<ts>\S+): "
RE_CONNECT = re.compile(TS_RE + r"New client connected from (?P<ip>\S+?):\d+ as (?P<cid>\S+) \(.*?u'(?P<user>[^']*)'")
RE_CONNECT_NOUSER = re.compile(TS_RE + r"New client connected from (?P<ip>\S+?):\d+ as (?P<cid>\S+) \(")
RE_DISCONNECT = re.compile(
    TS_RE + r"Client (?P<cid>\S+) (?:disconnected|closed its connection|has exceeded timeout|"
    r"already connected, closing old connection)(?P<rest>.*)$"
)
RE_SOCKET_ERR = re.compile(TS_RE + r"(?:Socket error on client|Bad socket read/write on client) (?P<cid>\S+?),")
RE_DENIED = re.compile(TS_RE + r"Denied PUBLISH from (?P<cid>\S+) .*?'(?P<topic>[^']*)'")
RE_TLS_ERR = re.compile(TS_RE + r"(?:OpenSSL Error\[\d+\]: |Client connection from (?P<ip>\S+?) failed: )(?P<err>.*)$")
RE_START = re.compile(TS_RE + r"mosquitto version \S+ starting")
RE_NEWCONN = re.compile(TS_RE + r"New connection from (?P<ip>\S+?):\d+ on port")

DISCONNECT_REASONS = [
    ("already connected, closing old connection", "scollegato (connessione sostituita)"),
    ("closed its connection", "connessione chiusa dal dispositivo"),
    ("has exceeded timeout", "nessuna risposta (timeout keepalive)"),
    ("disconnected", "disconnesso"),
    ("Socket error", "errore di rete"),
    ("Bad socket", "errore di rete"),
]


def _disconnect_reason(line: str) -> str:
    for needle, text in DISCONNECT_REASONS:
        if needle in line:
            return text
    return line.split(": ", 1)[-1].strip()


def _parse_ts(ts: str) -> str:
    try:
        return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S%z").astimezone(timezone.utc).isoformat(timespec="seconds")
    except ValueError:
        return db.now()


class Presence:
    """Stato in memoria dei client connessi, ricostruito leggendo il log."""

    def __init__(self, on_event=None):
        self.online: dict[str, dict] = {}
        self.on_event = on_event  # callback(event_dict) per il monitor live
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._last_ip = ""  # IP dell'ultima connessione TCP, per i rifiuti TLS

    def snapshot(self) -> dict[str, dict]:
        with self._lock:
            return dict(self.online)

    def is_online(self, device_id: str) -> bool:
        with self._lock:
            return device_id in self.online

    def _emit(self, kind: str, device_id: str | None, detail: str, ts: str, record: bool):
        if not record:
            return
        if device_id == config.MONITOR_ID:
            return
        try:
            db.add_event(kind, device_id, detail, ts)
            if kind == "connect" and device_id:
                db.conn().execute(
                    "UPDATE devices SET last_seen_at=?, last_ip=? WHERE id=?",
                    (ts, detail.removeprefix("da ").split(" ")[0], device_id),
                )
            elif kind == "disconnect" and device_id:
                db.conn().execute("UPDATE devices SET last_seen_at=? WHERE id=?", (ts, device_id))
        except Exception as e:  # il log non deve mai fermarsi per un errore di DB
            print(f"[presence] errore DB: {e}", flush=True)
        if self.on_event:
            self.on_event({"type": "presence", "kind": kind, "device_id": device_id, "detail": detail, "ts": ts})

    def handle_line(self, line: str, record: bool) -> None:
        m = RE_START.match(line)
        if m:
            with self._lock:
                self.online.clear()
            self._emit("admin", None, "broker avviato", _parse_ts(m["ts"]), record)
            return
        m = RE_NEWCONN.match(line)
        if m:
            self._last_ip = m["ip"]
            return
        m = RE_CONNECT.match(line) or RE_CONNECT_NOUSER.match(line)
        if m:
            cid = m["cid"]
            ts = _parse_ts(m["ts"])
            if "user" in m.groupdict() and m["user"] == config.MONITOR_ID and cid != config.MONITOR_ID:
                return  # connessione del pannello per scollegare un dispositivo (kick)
            with self._lock:
                self.online[cid] = {"since": ts, "ip": m["ip"]}
            self._emit("connect", cid, f"da {m['ip']}", ts, record)
            return
        m = RE_DISCONNECT.match(line) or RE_SOCKET_ERR.match(line)
        if m:
            cid = m["cid"]
            ts = _parse_ts(m["ts"])
            with self._lock:
                was = self.online.pop(cid, None)
            if was is not None:
                detail = _disconnect_reason(line)
                self._emit("disconnect", cid, detail, ts, record)
            return
        m = RE_DENIED.match(line)
        if m:
            self._emit("denied", m["cid"], f"publish negato su '{m['topic']}'", _parse_ts(m["ts"]), record)
            return
        m = RE_TLS_ERR.match(line)
        if m:
            err = m["err"].strip()
            if "certificate verify failed" in err:
                err = "certificato non valido, revocato o sospeso"
            elif "peer did not return a certificate" in err:
                err = "nessun certificato client"
            ip = m.groupdict().get("ip") or self._last_ip
            self._emit("denied", None, f"connessione rifiutata da {ip or '?'}: {err}", _parse_ts(m["ts"]), record)

    # ------------------------------------------------------------------
    def run(self) -> None:
        """Segue il file di log (come `tail -F`), ruotandolo quando supera LOG_MAX_BYTES."""
        saved_inode = db.kv_get("log_inode")
        saved_offset = int(db.kv_get("log_offset", "0") or 0)
        f = None
        inode = None
        last_save = 0.0
        while not self._stop.is_set():
            if f is None:
                try:
                    f = open(config.MOSQ_LOG, "r", encoding="utf-8", errors="replace")
                except FileNotFoundError:
                    time.sleep(1)
                    continue
                inode = str(os.fstat(f.fileno()).st_ino)
                # Rilegge da capo per ricostruire lo stato; registra solo le righe nuove
                record_from = saved_offset if inode == saved_inode else 0
            pos = f.tell()
            line = f.readline()
            if line and line.endswith("\n"):
                self.handle_line(line.rstrip("\n"), record=pos >= record_from)
                continue
            if line:  # riga incompleta: aspetta il resto
                f.seek(pos)
            # Fine file: salva la posizione, controlla rotazione/troncamento
            now = time.monotonic()
            if now - last_save > 2:
                db.kv_set("log_inode", inode)
                db.kv_set("log_offset", f.tell())
                saved_inode, saved_offset = inode, f.tell()
                last_save = now
            try:
                st = os.stat(config.MOSQ_LOG)
            except FileNotFoundError:
                st = None
            if st is None or str(st.st_ino) != inode or st.st_size < f.tell():
                f.close()
                f = None
                saved_inode, saved_offset = None, 0
                continue
            if st.st_size > LOG_MAX_BYTES and is_running():
                self._rotate()
            time.sleep(0.5)

    def _rotate(self) -> None:
        old = config.MOSQ_LOG.with_suffix(".log.1")
        try:
            os.replace(config.MOSQ_LOG, old)
        except FileNotFoundError:
            return
        reload()  # Mosquitto riapre il file di log su SIGHUP

    def stop(self) -> None:
        self._stop.set()


def tail_log(lines: int = 300) -> list[str]:
    out: list[str] = []
    for path in (config.MOSQ_LOG.with_suffix(".log.1"), config.MOSQ_LOG):
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                out.extend(f.readlines()[-lines:])
        except FileNotFoundError:
            pass
    return [l.rstrip("\n") for l in out[-lines:]]
