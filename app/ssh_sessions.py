"""Registrazioni delle sessioni del terminale SSH.

Ogni sessione aperta dal pannello viene salvata in
`DATA_DIR/ssh-sessions/<id dispositivo>/<AAAAMMGG-HHMMSS>-<sessione>.log`: un'intestazione
(chi, quando, utente sull'in-out) e poi tutto quello che è comparso nel terminale, comandi
digitati compresi (la shell li ripete a schermo), così com'è (sequenze di colore incluse:
si rivede con `cat` o `less -R`). Si ferma a SSH_RECORD_MAX_MB per sessione e le
registrazioni più vecchie di RETENTION_DAYS giorni vengono eliminate.
"""
import re
import time
from datetime import datetime
from pathlib import Path

from . import config

RE_NAME = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{8}\.log$")


def device_dir(device_id: str) -> Path:
    return config.SSH_RECORD_DIR / device_id


def cleanup(device_id: str) -> None:
    if config.RETENTION_DAYS <= 0:
        return
    limit = time.time() - config.RETENTION_DAYS * 86400
    for f in device_dir(device_id).glob("*.log"):
        try:
            if f.stat().st_mtime < limit:
                f.unlink()
        except OSError:
            pass


def list_sessions(device_id: str, limit: int = 30) -> list[dict]:
    d = device_dir(device_id)
    if not d.is_dir():
        return []
    out = []
    for f in sorted(d.glob("*.log"), reverse=True)[:limit]:
        if not RE_NAME.match(f.name):
            continue
        try:
            st = f.stat()
            with f.open("r", encoding="utf-8", errors="replace") as fh:
                head = fh.readline().strip()
        except OSError:
            continue
        out.append({"name": f.name, "size": st.st_size, "modified": st.st_mtime, "header": head.lstrip("# ")})
    return out


def session_path(device_id: str, name: str) -> Path | None:
    if not RE_NAME.match(name or ""):
        return None
    path = device_dir(device_id) / name
    return path if path.is_file() else None


class Recorder:
    def __init__(self, device_id: str, sid: str, panel_user: str, ssh_user: str):
        self.file = None
        self.written = 0
        self.truncated = False
        if not config.SSH_RECORD:
            return
        try:
            d = device_dir(device_id)
            d.mkdir(parents=True, exist_ok=True)
            cleanup(device_id)
            self.path = d / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{sid[:8]}.log"
            self.file = self.path.open("ab")
            self.file.write(
                f"# Terminale SSH su {device_id} · utente {ssh_user} · aperto da {panel_user} "
                f"il {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}\n".encode()
            )
        except OSError as e:
            print(f"[ssh] registrazione della sessione non riuscita: {e}", flush=True)
            self.file = None

    def write(self, data: bytes) -> None:
        if self.file is None or self.truncated:
            return
        room = config.SSH_RECORD_MAX_BYTES - self.written
        if len(data) > room:
            data = data[:max(room, 0)]
            self.truncated = True
        try:
            self.file.write(data)
            self.written += len(data)
            if self.truncated:
                self.file.write(b"\n# [registrazione interrotta: raggiunto il limite SSH_RECORD_MAX_MB]\n")
        except OSError:
            self.close()

    def close(self, footer: str = "") -> None:
        if self.file is None:
            return
        try:
            if footer:
                self.file.write(f"\n# {footer}\n".encode())
            self.file.close()
        except OSError:
            pass
        self.file = None
