"""Configurazione letta dalle variabili d'ambiente."""
import os
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
CLIENT_CERT_DAYS = _int("CLIENT_CERT_DAYS", 3650)

# Identità interna del pannello (legge tutti i topic). I dispositivi non
# possono avere un ID che inizia con "_".
MONITOR_ID = "__monitor__"


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
