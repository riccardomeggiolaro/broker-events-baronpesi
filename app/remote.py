"""Accesso remoto agli in-out, sulla stessa porta del pannello (vedi `gateway.py`).

Ogni in-out ha il suo indirizzo `/<id>/...`: quello che arriva lì (pagine, API REST,
websocket) viene inoltrato all'in-out `<id>` attraverso la connessione MQTT (vedi
`tunnel.py`), togliendo il prefisso. Più in-out si possono aprire insieme, in schede
diverse dello stesso browser.

L'interfaccia dell'in-out usa percorsi assoluti (/api, /static, /login...), quindi:
- nelle pagine HTML si inietta `static/remote-shim.js`, che aggiunge il prefisso alle
  richieste fatte da JavaScript (fetch, XHR, WebSocket...) e separa il localStorage
  di ogni in-out (token di login compreso);
- le letture di `location.pathname` in pagine e script diventano `__inoutPath()`
  (il percorso senza prefisso), così i confronti tipo `=== '/reservation'` funzionano;
- una richiesta senza prefisso (link, immagine, `location.href = '/login'`) viene
  reindirizzata (307) all'in-out della pagina che l'ha fatta, ricavato dal Referer;
  per i websocket senza prefisso vale l'ultimo in-out aperto (cookie);
- redirect (Location) e cookie (Path) dell'in-out ricevono il prefisso.

Terminale SSH: `/__remote/terminal/<id>` apre una pagina con un terminale (xterm.js) collegato
al websocket `/__remote/ssh/<id>`; il proxy fa da client SSH verso l'in-out attraverso lo
stesso tunnel MQTT (vedi `ssh.py`).

Accesso: serve il login del pannello (stesso cookie di sessione, verificato con la stessa
chiave).
I cookie del pannello non vengono mai inoltrati all'in-out.
"""
import asyncio
import gzip
import html
import json
import re
from base64 import b64decode
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote, urlsplit

import itsdangerous
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response, StreamingResponse
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.staticfiles import StaticFiles
from starlette.websockets import WebSocket, WebSocketDisconnect

import asyncssh

from . import config, db
from .ssh import SSHSessionError, TunnelSSH
from .ssh_sessions import Recorder
from .tunnel import Tunnel

SESSION_COOKIE = "broker_session"
SESSION_MAX_AGE = 12 * 3600
# Ultimo in-out aperto: solo per i websocket aperti senza prefisso (senza Referer)
DEVICE_COOKIE = "inout_remote"
PRIVATE_COOKIES = {SESSION_COOKIE, DEVICE_COOKIE}
RESERVED = "/__remote/"
WS_OPEN_TIMEOUT = 15

# Intestazioni che non si inoltrano (gestite da ciascun lato della connessione)
HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailer",
    "trailers", "transfer-encoding", "upgrade", "content-length", "proxy-connection",
}
REQUEST_SKIP = HOP_BY_HOP | {"accept-encoding", "referer"}
WS_SKIP = HOP_BY_HOP | {
    "host", "origin", "referer", "accept-encoding", "sec-websocket-key", "sec-websocket-version",
    "sec-websocket-extensions", "sec-websocket-protocol", "sec-websocket-accept",
}

STATIC_DIR = Path(__file__).parent / "static"
SHIM_JS = (STATIC_DIR / "remote-shim.js").read_text(encoding="utf-8")
RE_HEAD = re.compile(rb"<head\b[^>]*>", re.IGNORECASE)
# Letture di location.pathname (non le assegnazioni): diventano il percorso senza prefisso
RE_PATHNAME = re.compile(rb"(?<![\w$.])(?:(?:window|document|self)\.)?location\.pathname\b(?!\s*=(?!=))")
PATHNAME_SUB = b"(window.__inoutPath?window.__inoutPath():location.pathname)"
RE_COOKIE_PATH = re.compile(r"(;\s*path\s*=\s*)(/[^;]*)", re.IGNORECASE)

tunnel = Tunnel()
_signer = itsdangerous.TimestampSigner(str(config.secret_key()))


@asynccontextmanager
async def lifespan(app):
    tunnel.start(asyncio.get_running_loop())
    yield
    tunnel.stop()


# ----------------------------------------------------------------------
# Sessione del pannello e in-out
# ----------------------------------------------------------------------
def _panel_user(conn) -> str | None:
    """Utente del pannello dal cookie di sessione (stesso formato di SessionMiddleware), o None."""
    raw = conn.cookies.get(SESSION_COOKIE)
    if not raw:
        return None
    try:
        data = _signer.unsign(raw.encode("utf-8"), max_age=SESSION_MAX_AGE)
        session = json.loads(b64decode(data))
    except (itsdangerous.BadSignature, ValueError):
        return None
    user = session.get("user") if isinstance(session, dict) else None
    return str(user) if user else None


def _logged_in(conn) -> bool:
    return _panel_user(conn) is not None


def _device_by_id(device_id: str) -> dict | None:
    if not config.REMOTE_ENABLED or not device_id or not config.DEVICE_ID_RE.match(device_id):
        return None
    row = db.q1("SELECT id, name, channel_open FROM devices WHERE id=?", device_id)
    return dict(row) if row else None


def _split(path: str) -> tuple[dict | None, str]:
    """`/<id>/resto` -> (dispositivo, `/resto`); (None, path) se il primo segmento non è un in-out."""
    first, sep, rest = path.lstrip("/").partition("/")
    device = _device_by_id(first)
    if device is None:
        return None, path
    return device, "/" + rest if sep else ""


def _referer_device(conn) -> dict | None:
    """In-out della pagina che ha fatto la richiesta (Referer sullo stesso indirizzo)."""
    ref = conn.headers.get("referer")
    if not ref:
        return None
    try:
        parts = urlsplit(ref)
    except ValueError:
        return None
    if parts.netloc != conn.headers.get("host"):
        return None
    return _split(parts.path)[0]


def _prefix(device: dict) -> str:
    return "/" + quote(device["id"], safe="")


def _panel_url(conn) -> str:
    """Indirizzo del pannello sulla stessa porta (vedi gateway.py): `/__panel` va sempre al
    pannello, anche quando la richiesta parte da una pagina dell'in-out."""
    return "/__panel"


def _page(request: Request, title: str, text: str, status: int, device: dict | None = None) -> HTMLResponse:
    panel = _panel_url(request)
    back = f"{panel}/devices/{quote(device['id'])}" if device else f"{panel}/devices"
    body = f"""<!doctype html><html lang="it"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{html.escape(title)}</title>
<style>body{{font-family:system-ui,sans-serif;background:#f4f5f7;color:#1d2330;margin:0;padding:16px}}
main{{max-width:560px;margin:10vh auto;background:#fff;border-radius:10px;padding:24px;box-shadow:0 1px 3px #0002}}
h1{{font-size:20px;margin:0 0 12px}}p{{line-height:1.5}}a{{color:#1f5fbf}}
@media (prefers-color-scheme: dark){{body{{background:#14171c;color:#e6e8eb}}main{{background:#1d2128}}a{{color:#7fb0ff}}}}</style>
</head><body><main><h1>{html.escape(title)}</h1><p>{text}</p>
<p><a href="{html.escape(back)}" rel="noreferrer">Torna al pannello</a></p></main></body></html>"""
    return HTMLResponse(body, status_code=status)


def _wants_html(request: Request, inner_path: str) -> bool:
    return "text/html" in request.headers.get("accept", "") and not inner_path.startswith("/api/")


def _error(request: Request, inner_path: str, status: int, title: str, text: str, device: dict | None = None) -> Response:
    if _wants_html(request, inner_path):
        return _page(request, title, text, status, device)
    return Response(json.dumps({"detail": f"{title}: {text}"}), status_code=status, media_type="application/json")


def _forward_cookies(value: str) -> str | None:
    """Toglie dal Cookie i cookie del pannello: l'in-out non deve mai riceverli."""
    parts = []
    for part in value.split(";"):
        name = part.split("=", 1)[0].strip()
        if name and name not in PRIVATE_COOKIES:
            parts.append(part.strip())
    return "; ".join(parts) or None


def _request_headers(items, skip: set[str]) -> list[list[str]]:
    out = []
    for key, value in items:
        k = key.lower()
        if k in skip:
            continue
        if k == "cookie":
            value = _forward_cookies(value)
            if value is None:
                continue
        out.append([key, value])
    return out


# ----------------------------------------------------------------------
# Riscritture delle risposte dell'in-out
# ----------------------------------------------------------------------
def _rewrite_location(value: str, prefix: str, host: str | None) -> str:
    """I redirect dell'in-out verso se stesso ricevono il prefisso /<id>."""
    try:
        parts = urlsplit(value)
    except ValueError:
        return value
    if parts.scheme or parts.netloc:
        if parts.hostname not in ("127.0.0.1", "localhost") and parts.netloc != host:
            return value  # redirect verso un altro sito
        path = parts.path or "/"
    elif value.startswith("/") and not value.startswith("//"):
        path = parts.path
    else:
        return value  # relativo: resta sotto il prefisso da solo
    if path == prefix or path.startswith(prefix + "/"):
        return value
    out = prefix + path
    if parts.query:
        out += "?" + parts.query
    if parts.fragment:
        out += "#" + parts.fragment
    return out


def _rewrite_set_cookie(value: str, prefix: str) -> str:
    """Cookie dell'in-out limitati al suo prefisso (in-out diversi non condividono cookie)."""
    return RE_COOKIE_PATH.sub(lambda m: m.group(1) + prefix + (m.group(2) if m.group(2) != "/" else "/"), value)


def _shim(prefix: str) -> bytes:
    return (
        "<script>window.__inoutPrefix=" + json.dumps(prefix).replace("<", "\\u003c") + ";\n"
        + SHIM_JS.replace("</script", "<\\/script") + "</script>"
    ).encode("utf-8")


def _rewrite_body(data: bytes, ctype: str, prefix: str) -> bytes:
    if "html" in ctype:
        data = RE_PATHNAME.sub(PATHNAME_SUB, data)
        shim = _shim(prefix)
        m = RE_HEAD.search(data)
        return data[:m.end()] + shim + data[m.end():] if m else shim + data
    return RE_PATHNAME.sub(PATHNAME_SUB, data)


def _rewritable(ctype: str) -> bool:
    return "text/html" in ctype or "javascript" in ctype


# ----------------------------------------------------------------------
# Apertura dal pannello
# ----------------------------------------------------------------------
async def open_device(request: Request):
    device_id = request.path_params["device_id"]
    if not _logged_in(request):
        return RedirectResponse(f"{_panel_url(request)}/login", status_code=303)
    if not config.REMOTE_ENABLED:
        return _page(request, "Accesso remoto disattivato", "Imposta <code>REMOTE_ENABLED=1</code> nel file .env del broker.", 403)
    device = _device_by_id(device_id)
    if not device:
        return _page(request, "In-out non trovato", f"Il dispositivo <b>{html.escape(device_id)}</b> non esiste.", 404)
    with db.tx():
        db.add_event("admin", device["id"], "aperto l'accesso remoto all'in-out")
    return RedirectResponse(_prefix(device) + "/", status_code=303)


async def remote_status(request: Request):
    if not _logged_in(request):
        return Response(json.dumps({"detail": "non autenticato"}), status_code=401, media_type="application/json")
    return Response(
        json.dumps({"tunnel_connected": tunnel.connected, "error": tunnel.last_error}),
        media_type="application/json",
    )


# ----------------------------------------------------------------------
# Inoltro HTTP
# ----------------------------------------------------------------------
async def proxy_http(request: Request):
    path = request.url.path
    query = f"?{request.url.query}" if request.url.query else ""
    device, inner = _split(path)

    if not _logged_in(request):
        if _wants_html(request, inner or path):
            return RedirectResponse(f"{_panel_url(request)}/login", status_code=303)
        return _error(request, inner or path, 401, "Non autenticato", "accedi al pannello del broker.")

    if device is None:
        # Percorso assoluto dell'in-out senza prefisso: va all'in-out della pagina che l'ha chiesto
        ref = _referer_device(request)
        if ref is not None:
            return RedirectResponse(_prefix(ref) + path + query, status_code=307)
        if path == "/":
            return RedirectResponse(f"{_panel_url(request)}/devices", status_code=303)
        return _error(request, path, 404, "Nessun in-out", "Apri l'accesso remoto dalla pagina del dispositivo nel pannello.")
    prefix = _prefix(device)
    if not inner:
        return RedirectResponse(prefix + "/" + query, status_code=308)
    if not device["channel_open"]:
        return _error(request, inner, 409, "Canale chiuso",
                      "L'in-out non è collegato al broker finché il canale è chiuso.", device)

    size = request.headers.get("content-length")
    if size and size.isdigit() and int(size) > config.REMOTE_MAX_BYTES:
        return _error(request, inner, 413, "Richiesta troppo grande", "supera il limite del tunnel.", device)
    body = await request.body()
    if len(body) > config.REMOTE_MAX_BYTES:
        return _error(request, inner, 413, "Richiesta troppo grande", "supera il limite del tunnel.", device)

    headers = _request_headers(request.headers.items(), REQUEST_SKIP)
    # Compressione solo gzip: il proxy deve poter decomprimere le pagine da riscrivere
    if "gzip" in request.headers.get("accept-encoding", ""):
        headers.append(["Accept-Encoding", "gzip"])
    header = {"t": "http", "method": request.method, "path": inner + query, "headers": headers}
    try:
        res, data = await tunnel.request(device["id"], header, body, timeout=config.REMOTE_TIMEOUT)
    except asyncio.TimeoutError:
        return _error(request, inner, 504, "L'in-out non risponde",
                      "Nessuna risposta attraverso il broker: l'in-out è spento, non collegato o non aggiornato "
                      "a una versione che supporta l'accesso remoto.", device)
    except ConnectionError as e:
        return _error(request, inner, 502, "Proxy non collegato al broker", html.escape(str(e)), device)
    if res.get("error"):
        return _error(request, inner, 502, "Errore dell'in-out", html.escape(str(res["error"])), device)

    raw_headers = [(str(k).lower(), str(v)) for k, v in res.get("headers") or []]
    ctype = next((v for k, v in raw_headers if k == "content-type"), "").lower()
    encoding = next((v for k, v in raw_headers if k == "content-encoding"), "").lower().strip()
    rewrite = _rewritable(ctype) and encoding in ("", "identity", "gzip") and request.method != "HEAD"
    if rewrite:
        try:
            if encoding == "gzip":
                data = gzip.decompress(data)
            data = _rewrite_body(data, ctype, prefix)
        except (OSError, EOFError):
            rewrite = False

    resp = Response(data, status_code=int(res.get("status") or 502))
    host = request.headers.get("host")
    for k, v in raw_headers:
        if k in HOP_BY_HOP:
            continue
        if rewrite and k in ("content-encoding", "etag"):
            continue
        if k == "location":
            v = _rewrite_location(v, prefix, host)
        elif k == "set-cookie":
            v = _rewrite_set_cookie(v, prefix)
        resp.raw_headers.append((k.encode("latin-1"), v.encode("latin-1", errors="replace")))
    if "text/html" in ctype:
        # Ultimo in-out aperto: per i websocket aperti senza prefisso
        resp.set_cookie(DEVICE_COOKIE, device["id"], max_age=SESSION_MAX_AGE, httponly=True,
                        samesite="strict", secure=request.url.scheme == "https")
    return resp


# ----------------------------------------------------------------------
# Inoltro websocket
# ----------------------------------------------------------------------
async def proxy_ws(ws: WebSocket):
    if not _logged_in(ws):
        await ws.close(code=4401)
        return
    device, inner = _split(ws.url.path)
    if device is None:
        # Senza prefisso: in-out della pagina (Referer) o, in mancanza, l'ultimo aperto
        device = _referer_device(ws) or _device_by_id(ws.cookies.get(DEVICE_COOKIE) or "")
        inner = ws.url.path
    if device is None or not device["channel_open"]:
        await ws.close(code=4404)
        return

    device_id = device["id"]
    sid, queue = tunnel.open_session(device_id)
    closed_by_device = False
    try:
        path = (inner or "/") + (f"?{ws.url.query}" if ws.url.query else "")
        protocols = [p.strip() for p in ws.headers.get("sec-websocket-protocol", "").split(",") if p.strip()]
        header = {
            "t": "ws_open", "id": sid, "path": path, "subprotocols": protocols,
            "headers": _request_headers(ws.headers.items(), WS_SKIP),
        }
        try:
            res, _ = await tunnel.request(device_id, header, timeout=WS_OPEN_TIMEOUT)
        except (asyncio.TimeoutError, ConnectionError):
            await ws.close(code=1011)
            closed_by_device = True
            return
        if not res.get("ok"):
            await ws.close(code=1011)
            closed_by_device = True
            return
        await ws.accept(subprotocol=res.get("subprotocol") or None)

        async def browser_to_device():
            while True:
                msg = await ws.receive()
                if msg["type"] == "websocket.disconnect":
                    return
                if msg.get("bytes") is not None:
                    tunnel.send(device_id, {"t": "ws_msg", "id": sid, "bin": True}, msg["bytes"])
                elif msg.get("text") is not None:
                    tunnel.send(device_id, {"t": "ws_msg", "id": sid, "bin": False}, msg["text"].encode("utf-8"))

        async def device_to_browser():
            nonlocal closed_by_device
            while True:
                head, data = await queue.get()
                if head.get("t") == "ws_close":
                    closed_by_device = True
                    code = head.get("code")
                    await ws.close(code=code if isinstance(code, int) and 1000 <= code < 5000 else 1000)
                    return
                if head.get("bin"):
                    await ws.send_bytes(data)
                else:
                    await ws.send_text(data.decode("utf-8", errors="replace"))

        tasks = [asyncio.create_task(browser_to_device()), asyncio.create_task(device_to_browser())]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        if not closed_by_device:
            tunnel.send(device_id, {"t": "ws_close", "id": sid, "code": 1000})
        tunnel.close_session(sid)


# ----------------------------------------------------------------------
# Stato della macchina (letto dalla pagina del dispositivo nel pannello)
# ----------------------------------------------------------------------
INFO_TIMEOUT = 10


def _no_store(resp: Response) -> Response:
    """Stato della macchina: letto dalla pagina del pannello (stessa porta), mai in cache."""
    resp.headers["Cache-Control"] = "no-store"
    return resp


async def device_info(request: Request):
    def reply(data: dict, status: int = 200) -> Response:
        return _no_store(Response(json.dumps(data), status_code=status, media_type="application/json"))

    if not _logged_in(request):
        return reply({"detail": "non autenticato"}, 401)
    device = _device_by_id(request.path_params["device_id"])
    if device is None:
        return reply({"detail": "dispositivo non trovato"}, 404)
    if not device["channel_open"]:
        return reply({"detail": "canale chiuso"}, 409)
    try:
        res, _ = await tunnel.request(device["id"], {"t": "info"}, timeout=INFO_TIMEOUT)
    except asyncio.TimeoutError:
        return reply({"detail": "l'in-out non risponde (spento, non collegato o versione senza questa funzione)"}, 504)
    except ConnectionError as e:
        return reply({"detail": f"proxy non collegato al broker: {e}"}, 502)
    if not res.get("ok"):
        return reply({"detail": str(res.get("error") or "errore dell'in-out")}, 502)
    data = res.get("data") if isinstance(res.get("data"), dict) else {}
    return reply({"device": device["id"], "data": data, "ssh_enabled": config.SSH_ENABLED})


# ----------------------------------------------------------------------
# Terminale SSH
# ----------------------------------------------------------------------
def _same_origin(conn) -> bool:
    """Websocket solo dalle pagine di questo stesso indirizzo (niente siti esterni con il cookie)."""
    origin = conn.headers.get("origin")
    if not origin:
        return False
    try:
        return urlsplit(origin).netloc == conn.headers.get("host")
    except ValueError:
        return False


def _term_size(value: str | None, default: int, low: int, high: int) -> int:
    try:
        return max(low, min(high, int(value or default)))
    except ValueError:
        return default


async def terminal_page(request: Request):
    device_id = request.path_params["device_id"]
    if not _logged_in(request):
        return RedirectResponse(f"{_panel_url(request)}/login", status_code=303)
    if not config.REMOTE_ENABLED or not config.SSH_ENABLED:
        return _page(request, "Terminale disattivato",
                     "Imposta <code>REMOTE_ENABLED=1</code> e <code>SSH_ENABLED=1</code> nel file .env del broker.", 403)
    device = _device_by_id(device_id)
    if not device:
        return _page(request, "In-out non trovato", f"Il dispositivo <b>{html.escape(device_id)}</b> non esiste.", 404)
    name = device["name"] or device["id"]
    panel = f"{_panel_url(request)}/devices/{quote(device['id'])}"
    data = json.dumps({"device": device["id"], "name": name, "panel": panel}).replace("<", "\\u003c")
    v = int((STATIC_DIR / "terminal.js").stat().st_mtime)
    body = f"""<!doctype html><html lang="it"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Terminale · {html.escape(name)}</title>
<link rel="icon" type="image/png" href="/__remote/static/favicon.png">
<link rel="stylesheet" href="/__remote/static/xterm/xterm.css?v={v}">
<link rel="stylesheet" href="/__remote/static/terminal.css?v={v}">
</head><body>
<header class="bar">
  <div class="who"><span class="dot" id="dot"></span><strong>{html.escape(name)}</strong>
    <span class="muted" id="state">Collegamento…</span></div>
  <div class="actions">
    <button type="button" id="reconnect" hidden>Riconnetti</button>
    <button type="button" id="files-toggle" class="ghost" aria-expanded="false">File</button>
    <a href="{html.escape(panel)}">Pannello</a>
  </div>
</header>
<div class="body">
  <div id="term"></div>
  <aside id="files" hidden>
    <div class="files-head">
      <button type="button" id="files-up" class="ghost" title="Cartella superiore">↑</button>
      <input id="files-path" spellcheck="false" aria-label="Cartella">
      <button type="button" id="files-go" class="ghost">Vai</button>
    </div>
    <div class="files-actions">
      <label class="upload">Carica file<input type="file" id="files-upload" hidden></label>
      <span class="muted" id="files-state"></span>
    </div>
    <ul id="files-list"></ul>
  </aside>
</div>
<script>window.__terminal={data};</script>
<script src="/__remote/static/xterm/xterm.js?v={v}"></script>
<script src="/__remote/static/xterm/addon-fit.js?v={v}"></script>
<script src="/__remote/static/terminal.js?v={v}"></script>
<script src="/__remote/static/files.js?v={v}"></script>
</body></html>"""
    return HTMLResponse(body, headers={"Cache-Control": "no-store"})


async def ssh_ws(ws: WebSocket):
    if not _logged_in(ws) or not _same_origin(ws):
        await ws.close(code=4401)
        return
    if not config.SSH_ENABLED:
        await ws.close(code=4403)
        return
    device = _device_by_id(ws.path_params["device_id"])
    if device is None:
        await ws.close(code=4404)
        return
    await ws.accept()

    async def notify(kind: str, message: str = "", **extra) -> None:
        await ws.send_text(json.dumps({"type": kind, "message": message, **extra}))

    if not device["channel_open"]:
        await notify("error", "Canale chiuso: l'in-out non è collegato al broker finché il canale è chiuso.")
        await ws.close(code=1000)
        return

    cols = _term_size(ws.query_params.get("cols"), 80, 10, 500)
    rows = _term_size(ws.query_params.get("rows"), 24, 5, 200)
    session = TunnelSSH(tunnel, device["id"])
    loop = asyncio.get_running_loop()
    try:
        await notify("status", "Apertura della connessione SSH…")
        try:
            conn = await session.open()
            proc = await conn.create_process(
                term_type="xterm-256color", term_size=(cols, rows), encoding=None, stderr=asyncssh.STDOUT,
            )
        except SSHSessionError as e:
            await notify("error", str(e))
            await ws.close(code=1000)
            return
        except (OSError, asyncssh.Error) as e:
            await notify("error", f"apertura della shell non riuscita: {e}")
            await ws.close(code=1000)
            return
        panel_user = _panel_user(ws) or "?"
        with db.tx():
            db.add_event("admin", device["id"], f"aperto il terminale SSH remoto (utente {session.user}, da {panel_user})")
        recorder = Recorder(device["id"], session.sid or "", panel_user, session.user)
        opened_at = loop.time()
        last_input = opened_at
        reason = "chiuso dal browser"
        await notify("ready", user=session.user, idle_minutes=config.SSH_IDLE_MINUTES)

        async def browser_to_ssh():
            nonlocal last_input
            while True:
                msg = await ws.receive()
                if msg["type"] == "websocket.disconnect":
                    return
                if msg.get("bytes") is not None:
                    last_input = loop.time()
                    proc.stdin.write(msg["bytes"])
                elif msg.get("text"):
                    try:
                        ctrl = json.loads(msg["text"])
                    except ValueError:
                        continue
                    if isinstance(ctrl, dict) and ctrl.get("type") == "resize":
                        proc.change_terminal_size(
                            _term_size(str(ctrl.get("cols")), cols, 10, 500),
                            _term_size(str(ctrl.get("rows")), rows, 5, 200),
                        )

        async def ssh_to_browser():
            nonlocal reason
            while True:
                data = await proc.stdout.read(32 * 1024)
                if not data:
                    reason = "sessione terminata sull'in-out"
                    await notify("exit", "Sessione terminata.")
                    await ws.close(code=1000)
                    return
                recorder.write(data)
                await ws.send_bytes(data)

        async def idle_watch():
            nonlocal reason
            limit = config.SSH_IDLE_MINUTES * 60
            while True:
                await asyncio.sleep(min(30, limit))
                if loop.time() - last_input >= limit:
                    reason = f"chiuso per inattività ({config.SSH_IDLE_MINUTES} min)"
                    await notify("exit", f"Terminale chiuso dopo {config.SSH_IDLE_MINUTES} minuti senza attività.")
                    await ws.close(code=1000)
                    return

        tasks = [asyncio.create_task(browser_to_ssh()), asyncio.create_task(ssh_to_browser())]
        if config.SSH_IDLE_MINUTES > 0:
            tasks.append(asyncio.create_task(idle_watch()))
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            minutes = (loop.time() - opened_at) / 60
            summary = f"chiuso il terminale SSH remoto (utente {session.user}, da {panel_user}, " \
                      f"durata {_duration(minutes)}, {reason})"
            recorder.close(summary)
            with db.tx():
                db.add_event("admin", device["id"], summary)
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        await session.close()


# ----------------------------------------------------------------------
# Trasferimento file (SFTP sulla stessa connessione SSH)
# ----------------------------------------------------------------------
def _json(data: dict, status: int = 200) -> Response:
    return Response(json.dumps(data), status_code=status, media_type="application/json",
                    headers={"Cache-Control": "no-store"})


async def _sftp_device(request: Request):
    """Controlli comuni: login, origine (pagina del terminale), dispositivo con canale aperto."""
    if not _logged_in(request):
        return None, _json({"detail": "non autenticato: accedi di nuovo al pannello"}, 401)
    if not config.SSH_ENABLED:
        return None, _json({"detail": "terminale disattivato sul broker"}, 403)
    if request.method != "GET" and not _same_origin(request):
        return None, _json({"detail": "origine non consentita"}, 403)
    device = _device_by_id(request.path_params["device_id"])
    if device is None:
        return None, _json({"detail": "dispositivo non trovato"}, 404)
    if not device["channel_open"]:
        return None, _json({"detail": "canale chiuso"}, 409)
    return device, None


async def _sftp_open(device_id: str):
    session = TunnelSSH(tunnel, device_id)
    conn = await session.open()
    try:
        sftp = await conn.start_sftp_client()
    except (OSError, asyncssh.Error) as e:
        await session.close()
        raise SSHSessionError(f"SFTP non disponibile sull'in-out: {e}")
    return session, sftp


def _sftp_error(e: Exception) -> Response:
    if isinstance(e, asyncssh.SFTPNoSuchFile):
        return _json({"detail": "file o cartella inesistente"}, 404)
    if isinstance(e, asyncssh.SFTPPermissionDenied):
        return _json({"detail": "permesso negato"}, 403)
    return _json({"detail": str(e) or type(e).__name__}, 502)


async def files_list(request: Request):
    device, err = await _sftp_device(request)
    if err:
        return err
    path = request.query_params.get("path") or "."
    try:
        session, sftp = await _sftp_open(device["id"])
    except SSHSessionError as e:
        return _json({"detail": str(e)}, 502)
    try:
        real = await sftp.realpath(path)
        entries = []
        for item in await sftp.readdir(real):
            if item.filename in (".", ".."):
                continue
            a = item.attrs
            entries.append({
                "name": item.filename,
                "dir": a.type == asyncssh.FILEXFER_TYPE_DIRECTORY,
                "link": a.type == asyncssh.FILEXFER_TYPE_SYMLINK,
                "size": a.size,
                "mtime": a.mtime,
            })
        entries.sort(key=lambda e: (not e["dir"], e["name"].lower()))
        return _json({"path": real, "entries": entries, "user": session.user})
    except (OSError, asyncssh.Error) as e:
        return _sftp_error(e)
    finally:
        sftp.exit()
        await session.close()


async def files_download(request: Request):
    device, err = await _sftp_device(request)
    if err:
        return err
    path = request.query_params.get("path") or ""
    if not path:
        return _json({"detail": "percorso mancante"}, 400)
    try:
        session, sftp = await _sftp_open(device["id"])
    except SSHSessionError as e:
        return _json({"detail": str(e)}, 502)
    try:
        attrs = await sftp.stat(path)
        if attrs.type == asyncssh.FILEXFER_TYPE_DIRECTORY:
            raise ValueError("è una cartella")
        if attrs.size is not None and attrs.size > config.SFTP_MAX_BYTES:
            raise ValueError(f"file troppo grande (limite {config.SFTP_MAX_BYTES // 1048576} MB, SFTP_MAX_MB)")
        f = await sftp.open(path, "rb")
    except ValueError as e:
        sftp.exit()
        await session.close()
        return _json({"detail": str(e)}, 400)
    except (OSError, asyncssh.Error) as e:
        sftp.exit()
        await session.close()
        return _sftp_error(e)

    async def body():
        try:
            while True:
                chunk = await f.read(256 * 1024)
                if not chunk:
                    break
                yield chunk
        finally:
            await f.close()
            sftp.exit()
            await session.close()

    with db.tx():
        db.add_event("admin", device["id"], f"scaricato dall'in-out {path} ({_panel_user(request)})")
    name = path.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1] or "file"
    headers = {"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}", "Cache-Control": "no-store"}
    if attrs.size is not None:
        headers["Content-Length"] = str(attrs.size)
    return StreamingResponse(body(), media_type="application/octet-stream", headers=headers)


async def files_upload(request: Request):
    device, err = await _sftp_device(request)
    if err:
        return err
    folder = request.query_params.get("path") or "."
    size = request.headers.get("content-length")
    if size and size.isdigit() and int(size) > config.SFTP_MAX_BYTES + 64 * 1024:
        return _json({"detail": f"file troppo grande (limite {config.SFTP_MAX_BYTES // 1048576} MB, SFTP_MAX_MB)"}, 413)
    form = await request.form(max_part_size=config.SFTP_MAX_BYTES)
    upload = form.get("file")
    if upload is None or not getattr(upload, "filename", None):
        return _json({"detail": "nessun file"}, 400)
    name = upload.filename.replace("\\", "/").rsplit("/", 1)[-1]
    if name in ("", ".", ".."):
        return _json({"detail": "nome del file non valido"}, 400)
    try:
        session, sftp = await _sftp_open(device["id"])
    except SSHSessionError as e:
        return _json({"detail": str(e)}, 502)
    try:
        real = await sftp.realpath(folder)
        target = real.rstrip("/") + "/" + name
        written = 0
        async with sftp.open(target, "wb") as f:
            while True:
                chunk = await upload.read(256 * 1024)
                if not chunk:
                    break
                await f.write(chunk)
                written += len(chunk)
        with db.tx():
            db.add_event("admin", device["id"], f"caricato sull'in-out {target} ({written} byte, {_panel_user(request)})")
        return _json({"path": target, "size": written})
    except (OSError, asyncssh.Error) as e:
        return _sftp_error(e)
    finally:
        await upload.close()
        sftp.exit()
        await session.close()


def _duration(minutes: float) -> str:
    if minutes < 1:
        return "meno di un minuto"
    if minutes < 60:
        return f"{int(minutes)} min"
    return f"{int(minutes // 60)} h {int(minutes % 60)} min"


METHODS = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]

app = Starlette(
    lifespan=lifespan,
    routes=[
        Route(RESERVED + "open/{device_id}", open_device, methods=["GET"]),
        Route(RESERVED + "status", remote_status, methods=["GET"]),
        Route(RESERVED + "info/{device_id}", device_info, methods=["GET"]),
        Route(RESERVED + "terminal/{device_id}", terminal_page, methods=["GET"]),
        WebSocketRoute(RESERVED + "ssh/{device_id}", ssh_ws),
        Route(RESERVED + "files/{device_id}/list", files_list, methods=["GET"]),
        Route(RESERVED + "files/{device_id}/download", files_download, methods=["GET"]),
        Route(RESERVED + "files/{device_id}/upload", files_upload, methods=["POST"]),
        Mount(RESERVED + "static", StaticFiles(directory=STATIC_DIR), name="static"),
        Route("/{path:path}", proxy_http, methods=METHODS),
        WebSocketRoute("/{path:path}", proxy_ws),
    ],
)
