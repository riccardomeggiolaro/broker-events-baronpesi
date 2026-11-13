"""CA interna: certificati del server, del pannello e dei dispositivi, revoche (CRL).

Tutte le chiavi sono ECDSA P-256. La CA firma:
- il certificato del server Mosquitto (SAN = BROKER_HOST + localhost)
- il certificato interno del pannello (CN = __monitor__)
- un certificato client per ogni dispositivo (CN = ID del dispositivo)
"""
import io
import ipaddress
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from . import config, db

CA_KEY = config.PKI_DIR / "ca.key"
CA_CRT = config.PKI_DIR / "ca.crt"
SERVER_KEY = config.PKI_DIR / "server.key"
SERVER_CRT = config.PKI_DIR / "server.crt"
MONITOR_KEY = config.PKI_DIR / "monitor.key"
MONITOR_CRT = config.PKI_DIR / "monitor.crt"
CRL = config.PKI_DIR / "crl.pem"

CA_DAYS = 365 * 30
SERVER_DAYS = 365 * 10
CRL_DAYS = 365 * 10  # la CRL viene comunque rigenerata a ogni avvio e a ogni revoca


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _write_key(path: Path, key) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    tmp.chmod(0o600)
    tmp.replace(path)


def _write_cert(path: Path, cert: x509.Certificate) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    tmp.chmod(0o644)
    tmp.replace(path)


def load_cert(path: Path) -> x509.Certificate:
    return x509.load_pem_x509_certificate(path.read_bytes())


def _load_ca():
    key = serialization.load_pem_private_key(CA_KEY.read_bytes(), password=None)
    return key, load_cert(CA_CRT)


def ensure_ca() -> bool:
    """Crea la CA se manca. Ritorna True se è stata creata."""
    if CA_KEY.exists() and CA_CRT.exists():
        return False
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "Baron Pesi MQTT CA"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Baron Pesi"),
    ])
    now = _now()
    ski = x509.SubjectKeyIdentifier.from_public_key(key.public_key())
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=CA_DAYS))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True, content_commitment=False, key_encipherment=False,
                data_encipherment=False, key_agreement=False, key_cert_sign=True,
                crl_sign=True, encipher_only=False, decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(ski, critical=False)
        .sign(key, hashes.SHA256())
    )
    _write_key(CA_KEY, key)
    _write_cert(CA_CRT, cert)
    return True


def _issue(cn: str, days: int, server_names: list[str] | None = None):
    """Firma un nuovo certificato. Ritorna (chiave, certificato)."""
    ca_key, ca_cert = _load_ca()
    key = ec.generate_private_key(ec.SECP256R1())
    now = _now()
    builder = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)]))
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=days))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True, content_commitment=False, key_encipherment=False,
                data_encipherment=False, key_agreement=True, key_cert_sign=False,
                crl_sign=False, encipher_only=False, decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False
        )
    )
    if server_names is not None:
        sans = []
        for n in server_names:
            try:
                sans.append(x509.IPAddress(ipaddress.ip_address(n)))
            except ValueError:
                sans.append(x509.DNSName(n))
        builder = builder.add_extension(x509.SubjectAlternativeName(sans), critical=False)
        builder = builder.add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False
        )
    else:
        builder = builder.add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]), critical=False
        )
    return key, builder.sign(ca_key, hashes.SHA256())


def server_names() -> list[str]:
    names = [config.BROKER_HOST, "localhost", "127.0.0.1"]
    return list(dict.fromkeys(n for n in names if n))


def ensure_server_cert(force: bool = False) -> bool:
    """Crea (o ricrea se l'host è cambiato) il certificato del broker."""
    if not force and SERVER_CRT.exists() and SERVER_KEY.exists():
        cert = load_cert(SERVER_CRT)
        try:
            san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
            current = {str(v) for v in san.get_values_for_type(x509.DNSName)}
            current |= {str(v) for v in san.get_values_for_type(x509.IPAddress)}
        except x509.ExtensionNotFound:
            current = set()
        expiring = cert.not_valid_after_utc < _now() + timedelta(days=30)
        if set(server_names()) <= current and not expiring:
            return False
    key, cert = _issue(config.BROKER_HOST, SERVER_DAYS, server_names())
    _write_key(SERVER_KEY, key)
    _write_cert(SERVER_CRT, cert)
    return True


def ensure_monitor_cert() -> None:
    if MONITOR_CRT.exists() and MONITOR_KEY.exists():
        if load_cert(MONITOR_CRT).not_valid_after_utc > _now() + timedelta(days=30):
            return
    key, cert = _issue(config.MONITOR_ID, SERVER_DAYS)
    _write_key(MONITOR_KEY, key)
    _write_cert(MONITOR_CRT, cert)


# ----------------------------------------------------------------------
# Certificati dei dispositivi
# ----------------------------------------------------------------------
def issued_paths(serial: str) -> tuple[Path, Path]:
    return config.ISSUED_DIR / f"{serial}.crt", config.ISSUED_DIR / f"{serial}.key"


def issue_device_cert(device_id: str, days: int) -> str:
    """Rilascia un certificato per il dispositivo, lo registra e ritorna il seriale."""
    key, cert = _issue(device_id, days)
    serial = format(cert.serial_number, "x")
    crt_path, key_path = issued_paths(serial)
    _write_key(key_path, key)
    _write_cert(crt_path, cert)
    db.conn().execute(
        "INSERT INTO certificates(serial, device_id, not_before, not_after, issued_at) VALUES(?,?,?,?,?)",
        (
            serial,
            device_id,
            cert.not_valid_before_utc.isoformat(timespec="seconds"),
            cert.not_valid_after_utc.isoformat(timespec="seconds"),
            db.now(),
        ),
    )
    return serial


def revoke(serial: str, reason: str = "") -> None:
    db.conn().execute(
        "UPDATE certificates SET revoked_at=?, revoke_reason=? WHERE serial=? AND revoked_at IS NULL",
        (db.now(), reason, serial),
    )
    # La chiave privata di un certificato revocato non serve più
    _, key_path = issued_paths(serial)
    key_path.unlink(missing_ok=True)


def write_crl() -> None:
    ca_key, ca_cert = _load_ca()
    now = _now()
    builder = (
        x509.CertificateRevocationListBuilder()
        .issuer_name(ca_cert.subject)
        .last_update(now - timedelta(minutes=5))
        .next_update(now + timedelta(days=CRL_DAYS))
    )
    for row in db.q("SELECT serial, revoked_at FROM certificates WHERE revoked_at IS NOT NULL"):
        revoked = (
            x509.RevokedCertificateBuilder()
            .serial_number(int(row["serial"], 16))
            .revocation_date(datetime.fromisoformat(row["revoked_at"]))
            .build()
        )
        builder = builder.add_revoked_certificate(revoked)
    # Canale chiuso: i certificati validi del dispositivo sono "sospesi" (certificateHold),
    # una revoca temporanea che sparisce dalla CRL quando il canale viene riaperto.
    held = db.q(
        """SELECT c.serial FROM certificates c JOIN devices d ON d.id = c.device_id
           WHERE c.revoked_at IS NULL AND d.channel_open = 0"""
    )
    for row in held:
        revoked = (
            x509.RevokedCertificateBuilder()
            .serial_number(int(row["serial"], 16))
            .revocation_date(now)
            .add_extension(x509.CRLReason(x509.ReasonFlags.certificate_hold), critical=False)
            .build()
        )
        builder = builder.add_revoked_certificate(revoked)
    crl = builder.sign(ca_key, hashes.SHA256())
    tmp = CRL.with_suffix(".tmp")
    tmp.write_bytes(crl.public_bytes(serialization.Encoding.PEM))
    tmp.chmod(0o644)
    tmp.replace(CRL)


def device_bundle(device_id: str, serial: str) -> bytes:
    """Zip con ca.crt, client.crt e client.key (i nomi usati dalla pagina broker dell'in-out)."""
    crt_path, key_path = issued_paths(serial)
    if not key_path.exists():
        raise FileNotFoundError("Chiave privata non disponibile per questo certificato")
    readme = (
        f"Certificati MQTT per il dispositivo {device_id}\n\n"
        f"Broker:   {config.BROKER_HOST}:{config.BROKER_PUBLIC_PORT} (TLS)\n"
        f"Client ID / topic: {device_id}\n\n"
        "Nella configurazione del broker dell'in-out:\n"
        f"  - Host: {config.BROKER_HOST}\n"
        f"  - Porta: {config.BROKER_PUBLIC_PORT}\n"
        "  - Username e password: vuoti\n"
        "  - Certificato CA: ca.crt\n"
        "  - Certificato client: client.crt\n"
        "  - Chiave client: client.key\n"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("ca.crt", CA_CRT.read_bytes())
        z.writestr("client.crt", crt_path.read_bytes())
        z.writestr("client.key", key_path.read_bytes())
        z.writestr("LEGGIMI.txt", readme)
    return buf.getvalue()


def cert_info(path: Path) -> dict:
    cert = load_cert(path)
    return {
        "subject": cert.subject.rfc4514_string(),
        "serial": format(cert.serial_number, "x"),
        "not_before": cert.not_valid_before_utc,
        "not_after": cert.not_valid_after_utc,
        "fingerprint": cert.fingerprint(hashes.SHA256()).hex(":").upper(),
    }


def bootstrap() -> None:
    config.PKI_DIR.mkdir(parents=True, exist_ok=True)
    config.ISSUED_DIR.mkdir(parents=True, exist_ok=True)
    if ensure_ca():
        print("[pki] CA creata", flush=True)
    if ensure_server_cert():
        print(f"[pki] certificato server creato per {', '.join(server_names())}", flush=True)
    ensure_monitor_cert()
    write_crl()
