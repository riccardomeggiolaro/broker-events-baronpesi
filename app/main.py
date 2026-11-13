"""Pannello web del broker: dispositivi, certificati, canali, monitor live e storico."""
import asyncio
import hmac
import json
import os
import re
import shutil
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Form, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from . import broker, config, db, ingest, pki

BASE = Path(__file__).parent
DEVICE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{1,63}$")
PAGE_SIZE = 50
LOCAL_TZ = ZoneInfo(os.environ.get("TZ") or "Europe/Rome")


# ----------------------------------------------------------------------
# Monitor live: dal thread MQTT / log ai websocket
# ----------------------------------------------------------------------
class Hub:
    def __init__(self):
        self.loop: asyncio.AbstractEventLoop | None = None
        self.clients: set[asyncio.Queue] = set()

    def publish(self, event: dict) -> None:
        """Chiamabile da qualsiasi thread."""
        if self.loop is None:
            return
        self.loop.call_soon_threadsafe(self._fanout, event)

    def _fanout(self, event: dict) -> None:
        for q in list(self.clients):
            if q.qsize() < 500:
                q.put_nowait(event)


hub = Hub()
presence = broker.Presence(on_event=hub.publish)
broker.presence_ref = presence
ingestor = ingest.Ingest(on_message=hub.publish)
stop_event = threading.Event()


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init()
    hub.loop = asyncio.get_running_loop()
    threading.Thread(target=presence.run, name="presence", daemon=True).start()
    threading.Thread(target=ingest.purge_loop, args=(stop_event,), name="purge", daemon=True).start()
    ingestor.start()
    yield
    stop_event.set()
    presence.stop()
    ingestor.stop()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
templates = Jinja2Templates(directory=BASE / "templates")


# ----------------------------------------------------------------------
# Filtri per i template
# ----------------------------------------------------------------------
def _to_dt(value) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(LOCAL_TZ)


def fmt_dt(value, with_seconds: bool = True) -> str:
    dt = _to_dt(value)
    if dt is None:
        return "—"
    return dt.strftime("%d/%m/%Y %H:%M:%S" if with_seconds else "%d/%m/%Y %H:%M")


def fmt_ago(value) -> str:
    dt = _to_dt(value)
    if dt is None:
        return "mai"
    s = int((datetime.now(LOCAL_TZ) - dt).total_seconds())
    if s < 60:
        return "pochi secondi fa"
    if s < 3600:
        return f"{s // 60} min fa"
    if s < 86400:
        return f"{s // 3600} h fa"
    return f"{s // 86400} gg fa"


def fmt_size(n) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


templates.env.filters["dt"] = fmt_dt
templates.env.filters["ago"] = fmt_ago
templates.env.filters["size"] = fmt_size
templates.env.globals["broker_host"] = config.BROKER_HOST
templates.env.globals["broker_port"] = config.BROKER_PUBLIC_PORT
templates.env.globals["retention_days"] = config.RETENTION_DAYS
templates.env.globals["max_message_mb"] = config.MAX_MESSAGE_MB


def render(request: Request, name: str, **ctx) -> HTMLResponse:
    flash = request.session.pop("flash", None)
    return templates.TemplateResponse(request, name, {"flash": flash, "nav": name.split(".")[0], **ctx})


def flash(request: Request, text: str, kind: str = "ok") -> None:
    request.session["flash"] = {"text": text, "kind": kind}


def redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


# ----------------------------------------------------------------------
# Autenticazione (un solo amministratore, credenziali da variabili d'ambiente)
# ----------------------------------------------------------------------
PUBLIC_PATHS = ("/login", "/static/", "/healthz")
_failed: dict[str, list[float]] = {}


@app.middleware("http")
async def require_login(request: Request, call_next):
    path = request.url.path
    if not path.startswith(PUBLIC_PATHS) and not request.session.get("user"):
        if path.startswith("/api/"):
            return JSONResponse({"detail": "non autenticato"}, status_code=401)
        return redirect("/login")
    return await call_next(request)


# Aggiunto dopo il controllo di login così da esserne lo strato esterno (la sessione è già decodificata)
app.add_middleware(
    SessionMiddleware,
    secret_key=config.secret_key(),
    session_cookie="broker_session",
    max_age=12 * 3600,
    same_site="strict",
    https_only=os.environ.get("SESSION_HTTPS_ONLY", "0") == "1",
)


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    return fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "?")


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return render(request, "login.html", configured=bool(config.ADMIN_PASSWORD))


@app.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...)):
    ip = _client_ip(request)
    now = time.time()
    attempts = [t for t in _failed.get(ip, []) if now - t < 900]
    if len(attempts) >= 5:
        flash(request, "Troppi tentativi falliti: riprova tra qualche minuto.", "error")
        return redirect("/login")
    ok = bool(config.ADMIN_PASSWORD) and hmac.compare_digest(
        username.encode(), config.ADMIN_USERNAME.encode()
    ) & hmac.compare_digest(password.encode(), config.ADMIN_PASSWORD.encode())
    if not ok:
        attempts.append(now)
        _failed[ip] = attempts
        time.sleep(1)
        flash(request, "Credenziali non valide.", "error")
        return redirect("/login")
    _failed.pop(ip, None)
    request.session.clear()
    request.session["user"] = username
    return redirect("/")


@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    return redirect("/login")


@app.get("/healthz")
def healthz():
    return {"broker": broker.is_running(), "ingest": ingestor.connected}


# ----------------------------------------------------------------------
# Dashboard
# ----------------------------------------------------------------------
def _devices_with_state() -> list[dict]:
    online = presence.snapshot()
    rows = db.q(
        """SELECT d.*,
                  (SELECT COUNT(*) FROM messages m WHERE m.device_id=d.id) AS msg_count,
                  (SELECT MAX(received_at) FROM messages m WHERE m.device_id=d.id) AS last_msg_at,
                  (SELECT not_after FROM certificates c WHERE c.device_id=d.id AND c.revoked_at IS NULL
                     ORDER BY issued_at DESC LIMIT 1) AS cert_until
           FROM devices d ORDER BY d.name COLLATE NOCASE, d.id"""
    )
    out = []
    for r in rows:
        d = dict(r)
        d["online"] = d["id"] in online
        d["online_since"] = online.get(d["id"], {}).get("since")
        d["online_ip"] = online.get(d["id"], {}).get("ip")
        out.append(d)
    return out


def _status() -> dict:
    since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat(timespec="seconds")
    devices = _devices_with_state()
    online = presence.snapshot()
    known = {d["id"] for d in devices}
    unknown = [
        {"id": k, **v} for k, v in online.items() if k not in known and k != config.MONITOR_ID
    ]
    att = db.q1("SELECT COALESCE(SUM(size),0) AS s FROM attachments")["s"]
    try:
        db_size = config.DB_PATH.stat().st_size
    except FileNotFoundError:
        db_size = 0
    return {
        "broker_running": broker.is_running(),
        "ingest_connected": ingestor.connected,
        "ingest_error": ingestor.last_error,
        "devices": len(devices),
        "devices_online": sum(1 for d in devices if d["online"]),
        "messages_24h": db.q1("SELECT COUNT(*) AS n FROM messages WHERE received_at >= ?", since)["n"],
        "messages_total": db.q1("SELECT COUNT(*) AS n FROM messages")["n"],
        "storage": att + db_size,
        "unknown_online": unknown,
    }


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    recent = db.q(
        """SELECT m.*, d.name AS device_name,
                  (SELECT COUNT(*) FROM attachments a WHERE a.message_id=m.id) AS n_files
           FROM messages m LEFT JOIN devices d ON d.id=m.device_id
           ORDER BY m.id DESC LIMIT 10"""
    )
    events = db.q(
        "SELECT e.*, d.name AS device_name FROM events e LEFT JOIN devices d ON d.id=e.device_id ORDER BY e.id DESC LIMIT 15"
    )
    return render(
        request, "dashboard.html", status=_status(), devices=_devices_with_state(), recent=recent, events=events
    )


@app.get("/api/status")
def api_status():
    st = _status()
    st["online"] = {k: v for k, v in presence.snapshot().items() if k != config.MONITOR_ID}
    return st


# ----------------------------------------------------------------------
# Dispositivi e certificati
# ----------------------------------------------------------------------
def _get_device(device_id: str) -> dict:
    row = db.q1("SELECT * FROM devices WHERE id=?", device_id)
    if not row:
        raise HTTPException(404, "Dispositivo non trovato")
    return dict(row)


@app.get("/devices", response_class=HTMLResponse)
def devices_page(request: Request, new: str = ""):
    return render(
        request, "devices.html", devices=_devices_with_state(), prefill=new, default_days=config.CLIENT_CERT_DAYS
    )


@app.post("/devices")
def create_device(
    request: Request,
    device_id: str = Form(...),
    name: str = Form(""),
    notes: str = Form(""),
    days: int = Form(config.CLIENT_CERT_DAYS),
):
    device_id = device_id.strip()
    if not DEVICE_ID_RE.match(device_id):
        flash(request, "ID non valido: usa 2-64 caratteri tra lettere, numeri, punto, trattino e underscore "
                       "(non può iniziare con un simbolo).", "error")
        return redirect("/devices")
    if db.q1("SELECT 1 FROM devices WHERE id=?", device_id):
        flash(request, f"Esiste già un dispositivo con ID {device_id}.", "error")
        return redirect(f"/devices/{device_id}")
    days = max(1, min(days, 365 * 30))
    with db.tx() as c:
        c.execute(
            "INSERT INTO devices(id, name, notes, created_at) VALUES(?,?,?,?)",
            (device_id, name.strip(), notes.strip(), db.now()),
        )
        pki.issue_device_cert(device_id, days)
        db.add_event("admin", device_id, "dispositivo creato, certificato rilasciato e canale aperto")
    broker.apply()
    flash(request, "Dispositivo creato. Scarica i certificati e caricali nella pagina broker dell'in-out.")
    return redirect(f"/devices/{device_id}")


@app.get("/devices/{device_id}", response_class=HTMLResponse)
def device_page(request: Request, device_id: str):
    device = _get_device(device_id)
    online = presence.snapshot().get(device_id)
    certs = db.q("SELECT * FROM certificates WHERE device_id=? ORDER BY issued_at DESC", device_id)
    certs = [dict(c) | {"has_key": pki.issued_paths(c["serial"])[1].exists()} for c in certs]
    events = db.q("SELECT * FROM events WHERE device_id=? ORDER BY id DESC LIMIT 30", device_id)
    messages = db.q(
        """SELECT m.*, (SELECT COUNT(*) FROM attachments a WHERE a.message_id=m.id) AS n_files
           FROM messages m WHERE device_id=? ORDER BY id DESC LIMIT 15""",
        device_id,
    )
    count = db.q1("SELECT COUNT(*) AS n FROM messages WHERE device_id=?", device_id)["n"]
    return render(
        request, "device.html", device=device, online=online, certs=certs, events=events,
        messages=messages, msg_count=count, default_days=config.CLIENT_CERT_DAYS,
    )


@app.post("/devices/{device_id}/edit")
def edit_device(request: Request, device_id: str, name: str = Form(""), notes: str = Form("")):
    _get_device(device_id)
    db.conn().execute("UPDATE devices SET name=?, notes=? WHERE id=?", (name.strip(), notes.strip(), device_id))
    flash(request, "Dati salvati.")
    return redirect(f"/devices/{device_id}")


@app.post("/devices/{device_id}/channel")
def set_channel(request: Request, device_id: str, open: int = Form(...)):
    _get_device(device_id)
    with db.tx() as c:
        c.execute("UPDATE devices SET channel_open=? WHERE id=?", (1 if open else 0, device_id))
        db.add_event("admin", device_id, "canale aperto" if open else "canale chiuso")
    broker.apply(kick_devices=[] if open else [device_id])
    flash(request, "Canale aperto: il dispositivo si ricollegherà al prossimo tentativo e invierà le pesate in coda."
          if open else "Canale chiuso: il dispositivo è stato scollegato e non può ricollegarsi. "
                       "L'in-out tiene le pesate in coda e le invierà alla riapertura.")
    return redirect(f"/devices/{device_id}")


@app.post("/devices/{device_id}/certificates")
def new_certificate(request: Request, device_id: str, days: int = Form(config.CLIENT_CERT_DAYS),
                    revoke_previous: int = Form(0)):
    _get_device(device_id)
    days = max(1, min(days, 365 * 30))
    with db.tx() as c:
        previous = [r["serial"] for r in c.execute(
            "SELECT serial FROM certificates WHERE device_id=? AND revoked_at IS NULL", (device_id,))]
        serial = pki.issue_device_cert(device_id, days)
        if revoke_previous:
            for s in previous:
                pki.revoke(s, "sostituito")
        db.add_event("admin", device_id, f"nuovo certificato {serial[:12]}…"
                     + (f", revocati {len(previous)} precedenti" if revoke_previous and previous else ""))
    broker.apply(kick_devices=[device_id] if revoke_previous and previous else [])
    flash(request, "Nuovo certificato rilasciato. Scarica lo zip e caricalo sull'in-out.")
    return redirect(f"/devices/{device_id}")


@app.get("/devices/{device_id}/certificates/{serial}/bundle.zip")
def download_bundle(device_id: str, serial: str):
    row = db.q1("SELECT * FROM certificates WHERE serial=? AND device_id=?", serial, device_id)
    if not row or row["revoked_at"]:
        raise HTTPException(404, "Certificato non disponibile")
    try:
        data = pki.device_bundle(device_id, serial)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    db.add_event("admin", device_id, f"scaricato il pacchetto certificati {serial[:12]}…")
    return Response(
        data, media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{device_id}-certificati.zip"'},
    )


@app.post("/devices/{device_id}/certificates/{serial}/revoke")
def revoke_certificate(request: Request, device_id: str, serial: str):
    row = db.q1("SELECT * FROM certificates WHERE serial=? AND device_id=?", serial, device_id)
    if not row:
        raise HTTPException(404)
    with db.tx():
        pki.revoke(serial, "revocato dall'amministratore")
        db.add_event("admin", device_id, f"certificato {serial[:12]}… revocato")
    broker.apply(kick_devices=[device_id])
    flash(request, "Certificato revocato: il dispositivo non potrà più collegarsi con questo certificato.")
    return redirect(f"/devices/{device_id}")


@app.post("/devices/{device_id}/delete")
def delete_device(request: Request, device_id: str, delete_messages: int = Form(0)):
    _get_device(device_id)
    with db.tx() as c:
        for r in c.execute("SELECT serial FROM certificates WHERE device_id=? AND revoked_at IS NULL", (device_id,)).fetchall():
            pki.revoke(r["serial"], "dispositivo eliminato")
        c.execute("DELETE FROM devices WHERE id=?", (device_id,))
        if delete_messages:
            c.execute("DELETE FROM messages WHERE device_id=?", (device_id,))
        db.add_event("admin", device_id, "dispositivo eliminato, certificati revocati")
    if delete_messages:
        shutil.rmtree(config.ATTACH_DIR / ingest.message_dir(device_id, "0000000", 0).parts[0], ignore_errors=True)
    broker.apply(kick_devices=[device_id])
    flash(request, f"Dispositivo {device_id} eliminato.")
    return redirect("/devices")


# ----------------------------------------------------------------------
# Storico messaggi
# ----------------------------------------------------------------------
def _local_day_to_utc(day: str, end: bool) -> str | None:
    try:
        d = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=LOCAL_TZ)
    except ValueError:
        return None
    if end:
        d += timedelta(days=1)
    return d.astimezone(timezone.utc).isoformat(timespec="seconds")


@app.get("/messages", response_class=HTMLResponse)
def messages_page(request: Request, device: str = "", event_type: str = "", date_from: str = "",
                  date_to: str = "", q: str = "", page: int = 1):
    where, args = [], []
    if device:
        where.append("m.device_id = ?")
        args.append(device)
    if event_type:
        where.append("m.event_type = ?")
        args.append(event_type)
    if date_from and (v := _local_day_to_utc(date_from, False)):
        where.append("m.received_at >= ?")
        args.append(v)
    if date_to and (v := _local_day_to_utc(date_to, True)):
        where.append("m.received_at < ?")
        args.append(v)
    if q:
        where.append("(m.body LIKE ? OR m.message_id = ? OR m.weigher_name LIKE ? OR m.instance_name LIKE ?)")
        like = f"%{q}%"
        args += [like, q, like, like]
    sql_where = ("WHERE " + " AND ".join(where)) if where else ""
    total = db.q1(f"SELECT COUNT(*) AS n FROM messages m {sql_where}", *args)["n"]
    page = max(1, page)
    rows = db.q(
        f"""SELECT m.id, m.device_id, m.topic, m.received_at, m.message_id, m.event_type, m.instance_name,
                   m.weigher_name, m.size, d.name AS device_name,
                   (SELECT COUNT(*) FROM attachments a WHERE a.message_id=m.id) AS n_files
            FROM messages m LEFT JOIN devices d ON d.id=m.device_id {sql_where}
            ORDER BY m.id DESC LIMIT ? OFFSET ?""",
        *args, PAGE_SIZE, (page - 1) * PAGE_SIZE,
    )
    devices = db.q("SELECT DISTINCT device_id FROM messages ORDER BY device_id")
    types = db.q("SELECT DISTINCT event_type FROM messages WHERE event_type IS NOT NULL ORDER BY event_type")
    filters = {"device": device, "event_type": event_type, "date_from": date_from, "date_to": date_to, "q": q}
    return render(
        request, "messages.html", rows=rows, total=total, page=page,
        pages=max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE), filters=filters,
        devices=[r["device_id"] for r in devices], types=[r["event_type"] for r in types],
    )


@app.get("/messages/{msg_id}", response_class=HTMLResponse)
def message_page(request: Request, msg_id: int):
    m = db.q1("SELECT m.*, d.name AS device_name FROM messages m LEFT JOIN devices d ON d.id=m.device_id WHERE m.id=?", msg_id)
    if not m:
        raise HTTPException(404, "Messaggio non trovato")
    atts = db.q("SELECT * FROM attachments WHERE message_id=? ORDER BY id", msg_id)
    body = None
    if m["is_json"] and m["body"]:
        try:
            body = json.loads(m["body"])
        except ValueError:
            body = None
    weighing = body.get("weighing") if isinstance(body, dict) else None
    header = {k: v for k, v in body.items() if k not in ("weighing", "files")} if isinstance(body, dict) else {}
    csv_preview = {}
    for a in atts:
        if a["kind"] == "csv" and a["size"] < 512 * 1024:
            try:
                text = (config.ATTACH_DIR / a["path"]).read_text(encoding="utf-8-sig", errors="replace")
                sep = ";" if text.count(";") > text.count(",") else ","
                csv_preview[a["id"]] = [line.split(sep) for line in text.splitlines()[:50]]
            except OSError:
                pass
    return render(
        request, "message.html", m=m, atts=atts, body=body, weighing=weighing, header=header,
        pretty=json.dumps(body, indent=2, ensure_ascii=False) if body is not None else (m["body"] or ""),
        csv_preview=csv_preview,
    )


@app.post("/messages/{msg_id}/delete")
def delete_message(request: Request, msg_id: int):
    m = db.q1("SELECT * FROM messages WHERE id=?", msg_id)
    if m:
        ingest.remove_attachments(msg_id)
        db.conn().execute("DELETE FROM messages WHERE id=?", (msg_id,))
        flash(request, "Messaggio eliminato.")
    return redirect("/messages")


@app.get("/attachments/{att_id}")
def attachment(att_id: int, download: int = 0):
    a = db.q1("SELECT * FROM attachments WHERE id=?", att_id)
    if not a:
        raise HTTPException(404)
    path = (config.ATTACH_DIR / a["path"]).resolve()
    if not path.is_relative_to(config.ATTACH_DIR.resolve()) or not path.exists():
        raise HTTPException(404, "File non trovato")
    inline_ok = a["content_type"] in ("image/png", "image/jpeg", "application/pdf")
    return FileResponse(
        path, media_type=a["content_type"] if inline_ok else "application/octet-stream",
        filename=a["name"], content_disposition_type="attachment" if download or not inline_ok else "inline",
    )


# ----------------------------------------------------------------------
# Monitor live
# ----------------------------------------------------------------------
@app.get("/live", response_class=HTMLResponse)
def live_page(request: Request):
    devices = _devices_with_state()
    return render(request, "live.html", devices=devices, names={d["id"]: d["name"] or d["id"] for d in devices})


@app.websocket("/ws/live")
async def ws_live(ws: WebSocket):
    if not ws.session.get("user"):
        await ws.close(code=4401)
        return
    await ws.accept()
    q: asyncio.Queue = asyncio.Queue()
    hub.clients.add(q)
    try:
        await ws.send_json({"type": "hello", "online": {k: v for k, v in presence.snapshot().items()
                                                        if k != config.MONITOR_ID}})
        while True:
            event = await q.get()
            await ws.send_json(event)
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        hub.clients.discard(q)


# ----------------------------------------------------------------------
# Broker: CA, certificato server, log
# ----------------------------------------------------------------------
@app.get("/broker", response_class=HTMLResponse)
def broker_page(request: Request):
    revoked = db.q("SELECT * FROM certificates WHERE revoked_at IS NOT NULL ORDER BY revoked_at DESC LIMIT 50")
    return render(
        request, "broker.html",
        ca=pki.cert_info(pki.CA_CRT), server=pki.cert_info(pki.SERVER_CRT), server_names=pki.server_names(),
        status=_status(), log=broker.tail_log(300), revoked=revoked,
    )


@app.get("/ca.crt")
def download_ca():
    return FileResponse(pki.CA_CRT, media_type="application/x-x509-ca-cert", filename="ca.crt")


@app.post("/broker/reload")
def reload_broker(request: Request):
    broker.apply()
    flash(request, "ACL e lista dei certificati revocati riscritte, broker ricaricato.")
    return redirect("/broker")
