"""Accesso remoto agli in-out (porta dedicata, REMOTE_PUBLIC_PORT).

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

Accesso: serve il login del pannello. Il cookie di sessione del pannello vale anche su
questa porta (i cookie non distinguono le porte) e viene verificato con la stessa chiave.
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
from starlette.responses import HTMLResponse, RedirectResponse, Response
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from . import config, db
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

SHIM_JS = (Path(__file__).parent / "static" / "remote-shim.js").read_text(encoding="utf-8")
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
def _logged_in(conn) -> bool:
    """Verifica il cookie di sessione del pannello (stesso formato di SessionMiddleware)."""
    raw = conn.cookies.get(SESSION_COOKIE)
    if not raw:
        return False
    try:
        data = _signer.unsign(raw.encode("utf-8"), max_age=SESSION_MAX_AGE)
        session = json.loads(b64decode(data))
    except (itsdangerous.BadSignature, ValueError):
        return False
    return isinstance(session, dict) and bool(session.get("user"))


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
    """Indirizzo del pannello sullo stesso host: HTTPS tramite Caddy, oppure HTTP sulla porta del pannello (LAN)."""
    host = conn.url.hostname or config.BROKER_HOST
    if ":" in host:
        host = f"[{host}]"
    if conn.url.scheme in ("https", "wss"):
        return f"https://{host}"
    return f"http://{host}:{config.WEB_PORT}"


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
<p><a href="{html.escape(back)}">Torna al pannello</a></p></main></body></html>"""
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


METHODS = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]

app = Starlette(
    lifespan=lifespan,
    routes=[
        Route(RESERVED + "open/{device_id}", open_device, methods=["GET"]),
        Route(RESERVED + "status", remote_status, methods=["GET"]),
        Route("/{path:path}", proxy_http, methods=METHODS),
        WebSocketRoute("/{path:path}", proxy_ws),
    ],
)
