"""Configurazione letta dalle variabili d'ambiente."""
import os
import re
import secrets
from pathlib import Path


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
PKI_DIR = DATA_DIR / "pki"
ISSUED_DIR = PKI_DIR / "issued"
MOSQ_DIR = DATA_DIR / "mosquitto"
ATTACH_DIR = DATA_DIR / "attachments"
DB_PATH = DATA_DIR / "broker.sqlite3"

MOSQ_CONF = MOSQ_DIR / "mosquitto.conf"
MOSQ_ACL = MOSQ_DIR / "acl"
MOSQ_PID = MOSQ_DIR / "mosquitto.pid"
MOSQ_LOG = MOSQ_DIR / "mosquitto.log"

# Nome host pubblico del broker: va nel certificato del server (SAN) e deve
# coincidere con l'host impostato negli in-out.
BROKER_HOST = os.environ.get("BROKER_HOST", "localhost").strip()
# Porta pubblicata verso internet (solo informativa, il broker ascolta sempre su 8883)
BROKER_PUBLIC_PORT = _int("BROKER_PUBLIC_PORT", 8883)
BROKER_INTERNAL_PORT = 8883

ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")

RETENTION_DAYS = _int("RETENTION_DAYS", 90)
MAX_MESSAGE_MB = _int("MAX_MESSAGE_MB", 64)
CLIENT_CERT_DAYS = _int("CLIENT_CERT_DAYS", 365)

# Registrazione automatica degli in-out (servizio HTTPS separato dal pannello).
# Un in-out senza certificato si presenta con una richiesta CSR e riceve subito
# il certificato; per bloccarlo si chiude il canale dal pannello.
ENROLL_ENABLED = os.environ.get("ENROLL_ENABLED", "1") == "1"
ENROLL_INTERNAL_PORT = 8884
# Porta pubblicata (solo informativa, il servizio ascolta sempre su 8884)
ENROLL_PUBLIC_PORT = _int("ENROLL_PUBLIC_PORT", 8884)

# Identità interna del pannello (legge tutti i topic). I dispositivi non
# possono avere un ID che inizia con "_".
MONITOR_ID = "__monitor__"
# Topic di comando per ogni in-out (<id>/control): il pannello vi pubblica (retained)
# la richiesta di un nuovo certificato, l'in-out lo legge appena collegato.
CONTROL_SUFFIX = "control"


def control_topic(device_id: str) -> str:
    return f"{device_id}/{CONTROL_SUFFIX}"


# Accesso remoto all'in-out: richieste HTTP/websocket inoltrate attraverso la
# connessione MQTT (il pannello scrive su <id>/tunnel/req, l'in-out risponde su
# <id>/tunnel/res). Il proxy risponde su una porta dedicata, così l'interfaccia
# dell'in-out funziona dalla radice "/" come in rete locale.
TUNNEL_PREFIX = "tunnel"
TUNNEL_REQ_SUFFIX = f"{TUNNEL_PREFIX}/req"
TUNNEL_RES_SUFFIX = f"{TUNNEL_PREFIX}/res"
REMOTE_ENABLED = os.environ.get("REMOTE_ENABLED", "1") == "1"
REMOTE_INTERNAL_PORT = 8081
# Porta pubblicata del proxy remoto (con Caddy in HTTPS, in LAN in HTTP)
REMOTE_PUBLIC_PORT = _int("REMOTE_PUBLIC_PORT", 8443)
# Porta del pannello in HTTP (installazione in LAN), per i collegamenti dal proxy al pannello
WEB_PORT = _int("WEB_PORT", 8080)
# Tempo massimo di attesa della risposta dell'in-out (secondi)
REMOTE_TIMEOUT = _int("REMOTE_TIMEOUT", 60)
# Dimensione massima di una richiesta o risposta inoltrata
REMOTE_MAX_BYTES = min(32, MAX_MESSAGE_MB) * 1024 * 1024


# Terminale SSH verso gli in-out dal pannello (attraverso lo stesso tunnel MQTT).
# La chiave SSH del broker viene creata al primo uso in DATA_DIR/ssh.
SSH_ENABLED = os.environ.get("SSH_ENABLED", "1") == "1"
SSH_DIR = DATA_DIR / "ssh"
# Registrazione delle sessioni del terminale (tutto quello che compare a schermo, comandi compresi)
SSH_RECORD = os.environ.get("SSH_RECORD", "1") == "1"
SSH_RECORD_DIR = DATA_DIR / "ssh-sessions"
SSH_RECORD_MAX_BYTES = _int("SSH_RECORD_MAX_MB", 20) * 1024 * 1024
# Trasferimento file (SFTP) dalla pagina del terminale: dimensione massima di un file
SFTP_MAX_BYTES = _int("SFTP_MAX_MB", 200) * 1024 * 1024
# Chiusura automatica di un terminale senza tasti premuti per questi minuti (0 = mai)
SSH_IDLE_MINUTES = _int("SSH_IDLE_MINUTES", 30)


def tunnel_req_topic(device_id: str) -> str:
    return f"{device_id}/{TUNNEL_REQ_SUFFIX}"


def is_tunnel_topic(topic: str) -> bool:
    return topic.split("/", 1)[-1].startswith(TUNNEL_PREFIX + "/")


# ID dispositivo = CN del certificato = client ID = topic
DEVICE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{1,63}$")


def secret_key() -> str:
    """Chiave per firmare i cookie di sessione: da env o generata e salvata."""
    key = os.environ.get("SECRET_KEY")
    if key:
        return key
    path = DATA_DIR / "secret_key"
    if path.exists():
        return path.read_text().strip()
    key = secrets.token_urlsafe(48)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(key)
    path.chmod(0o600)
    return key
