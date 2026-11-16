"""Registrazione automatica degli in-out (HTTPS, porta 8884).

Un in-out senza certificato genera da solo la sua chiave privata (che non lascia
mai il dispositivo) e invia una richiesta CSR con il proprio client ID. Il
broker firma subito la richiesta, crea il dispositivo con il canale aperto e
ricarica Mosquitto: l'in-out può collegarsi sulla porta MQTT 8883.

Il servizio pubblica anche la CRL (/crl.pem): se la connessione MQTT viene
rifiutata, l'in-out la consulta e, se il suo certificato è stato revocato (es.
dispositivo eliminato dal pannello), si registra di nuovo da solo.

Il servizio usa il certificato del server Mosquitto (firmato dalla CA interna).
Al primo contatto l'in-out scarica la CA da /ca.crt senza verificarla (fiducia
al primo contatto), poi la usa per verificare il servizio e il broker.

Regole:
- dispositivo nuovo                                   -> certificato rilasciato
- stessa chiave di un certificato non revocato        -> rinnovo (quello in uso resta valido fino
                                                         alla scadenza, i precedenti vengono revocati)
- stessa chiave di un certificato revocato            -> rifiutato (key_revoked: l'in-out genera una nuova chiave)
- canale chiuso                                       -> rifiutato (channel_closed)
- altra chiave e il dispositivo ha già un certificato
  valido                                              -> rifiutato (already_enrolled): per sostituirlo
                                                         revoca il certificato dal pannello
"""
import threading
import time
from datetime import datetime, timezone

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.x509.oid import NameOID
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

from . import broker, config, db, pki

MAX_CSR_BYTES = 8 * 1024
RATE_WINDOW = 60
RATE_MAX = 20
# Motivo di revoca dei certificati sostituiti da un rinnovo (non blocca la chiave)
RENEWED = "rinnovato"

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
_rate: dict[str, list[float]] = {}
_rate_lock = threading.Lock()
# Una richiesta alla volta: evita due certificati per lo stesso dispositivo in parallelo
_enroll_lock = threading.Lock()


class EnrollRequest(BaseModel):
    client_id: str
    csr: str
    hostname: str = ""


def _error(status: int, code: str, detail: str) -> JSONResponse:
    return JSONResponse({"code": code, "detail": detail}, status_code=status)


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "?"


def _rate_limited(ip: str) -> bool:
    now = time.monotonic()
    with _rate_lock:
        hits = [t for t in _rate.get(ip, []) if now - t < RATE_WINDOW]
        hits.append(now)
        _rate[ip] = hits
        return len(hits) > RATE_MAX


def _valid_key(public_key) -> bool:
    if isinstance(public_key, ec.EllipticCurvePublicKey):
        return public_key.curve.key_size >= 256
    if isinstance(public_key, rsa.RSAPublicKey):
        return public_key.key_size >= 2048
    return False


@app.get("/healthz")
def healthz():
    return {"enroll": config.ENROLL_ENABLED}


@app.get("/ca.crt")
def ca_crt():
    return Response(pki.CA_CRT.read_bytes(), media_type="application/x-pem-file")


@app.get("/crl.pem")
def crl_pem():
    """CRL firmata dalla CA: l'in-out la usa per capire se il suo certificato è stato
    revocato (deve registrarsi di nuovo) o sospeso (canale chiuso: deve aspettare)."""
    return Response(pki.CRL.read_bytes(), media_type="application/x-pem-file")


@app.post("/enroll")
def enroll(body: EnrollRequest, request: Request):
    ip = _client_ip(request)
    if not config.ENROLL_ENABLED:
        return _error(503, "disabled", "Registrazione automatica disattivata sul broker")
    if _rate_limited(ip):
        return _error(429, "rate_limited", "Troppe richieste, riprova tra un minuto")

    device_id = body.client_id.strip()
    if not config.DEVICE_ID_RE.match(device_id):
        return _error(400, "invalid_id", "Client ID non valido")
    if len(body.csr) > MAX_CSR_BYTES:
        return _error(400, "invalid_csr", "Richiesta CSR troppo grande")
    try:
        csr = x509.load_pem_x509_csr(body.csr.encode())
    except ValueError:
        return _error(400, "invalid_csr", "Richiesta CSR non leggibile")
    if not csr.is_signature_valid:
        return _error(400, "invalid_csr", "Firma della richiesta CSR non valida")
    cns = csr.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
    if len(cns) != 1 or cns[0].value != device_id:
        return _error(400, "invalid_csr", "Il CN della richiesta deve coincidere con il client ID")
    public_key = csr.public_key()
    if not _valid_key(public_key):
        return _error(400, "invalid_csr", "Chiave non supportata (serve EC P-256 o RSA 2048+)")
    key_der = pki.public_key_der(public_key)
    hostname = body.hostname.strip()[:100]

    with _enroll_lock, db.tx() as c:
        device = c.execute("SELECT * FROM devices WHERE id=?", (device_id,)).fetchone()
        if device and not device["channel_open"]:
            db.add_event("denied", device_id, f"registrazione rifiutata da {ip}: canale chiuso")
            return _error(403, "channel_closed", "Canale chiuso sul broker: il dispositivo è bloccato")

        certs = c.execute("SELECT * FROM certificates WHERE device_id=?", (device_id,)).fetchall()
        same_key = [r for r in certs if pki.issued_public_key(r["serial"]) == key_der]
        if any(r["revoked_at"] and r["revoke_reason"] != RENEWED for r in same_key):
            db.add_event("denied", device_id, f"registrazione rifiutata da {ip}: chiave di un certificato revocato")
            return _error(403, "key_revoked", "Questa chiave appartiene a un certificato revocato: generane una nuova")

        now = datetime.now(timezone.utc)
        active = [r for r in certs if not r["revoked_at"] and datetime.fromisoformat(r["not_after"]) > now]
        renew = [r for r in same_key if not r["revoked_at"]]
        if not renew and active:
            db.add_event("denied", device_id, f"registrazione rifiutata da {ip}: il dispositivo ha già un certificato valido")
            return _error(409, "already_enrolled",
                          "Il dispositivo ha già un certificato valido con un'altra chiave: "
                          "revocalo dal pannello del broker per registrarlo di nuovo")

        if not device:
            c.execute(
                "INSERT INTO devices(id, name, notes, created_at) VALUES(?,?,?,?)",
                (device_id, hostname, "", db.now()),
            )
        serial, cert = pki.sign_device_csr(device_id, public_key, config.CLIENT_CERT_DAYS)
        # Il certificato in uso resta valido fino alla scadenza: se l'in-out non riesce a
        # salvare quello nuovo può continuare a collegarsi. I precedenti vengono revocati.
        for r in sorted(renew, key=lambda r: r["issued_at"])[:-1]:
            pki.revoke(r["serial"], RENEWED)
        c.execute("UPDATE devices SET last_ip=? WHERE id=?", (ip, device_id))
        if renew:
            db.add_event("admin", device_id, f"certificato rinnovato automaticamente da {ip} ({serial[:12]}…)")
        elif device:
            db.add_event("admin", device_id, f"nuovo certificato rilasciato automaticamente a {ip} ({serial[:12]}…)")
        else:
            db.add_event("admin", device_id, f"dispositivo registrato automaticamente da {ip} ({serial[:12]}…), canale aperto")

    broker.apply()
    print(f"[enroll] certificato {serial[:12]}… rilasciato a {device_id} ({ip})", flush=True)
    return {
        "device_id": device_id,
        "serial": serial,
        "not_after": cert.not_valid_after_utc.isoformat(timespec="seconds"),
        "certificate": cert.public_bytes(serialization.Encoding.PEM).decode(),
        "ca": pki.CA_CRT.read_text(),
        "mqtt_port": config.BROKER_PUBLIC_PORT,
    }
