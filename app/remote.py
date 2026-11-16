"""Accesso remoto all'in-out (porta dedicata, REMOTE_PUBLIC_PORT).

Tutto quello che arriva su questa porta (pagine, API REST, websocket) viene inoltrato
all'in-out scelto dal pannello attraverso la connessione MQTT (vedi `tunnel.py`):
l'interfaccia dell'in-out funziona dalla radice "/" come in rete locale.

- Accesso: serve il login del pannello. Il cookie di sessione del pannello vale anche su
  questa porta (i cookie non distinguono le porte) e viene verificato con la stessa chiave.
- Scelta dell'in-out: `/__remote/open/<id>` (pulsante nella pagina del dispositivo) salva
  l'ID in un cookie; da lì ogni richiesta va a quell'in-out.
- I cookie del pannello non vengono mai inoltrati all'in-out.
"""
import asyncio
import html
import json
from base64 import b64decode
from contextlib import asynccontextmanager
from urllib.parse import quote

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
DEVICE_COOKIE = "inout_remote"
PRIVATE_COOKIES = {SESSION_COOKIE, DEVICE_COOKIE}
RESERVED = "/__remote/"
WS_OPEN_TIMEOUT = 15

# Intestazioni che non si inoltrano (gestite da ciascun lato della connessione)
HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailer",
    "trailers", "transfer-encoding", "upgrade", "content-length", "proxy-connection",
}
WS_SKIP = HOP_BY_HOP | {
    "host", "origin", "sec-websocket-key", "sec-websocket-version", "sec-websocket-extensions",
    "sec-websocket-protocol", "sec-websocket-accept",
}

tunnel = Tunnel()
_signer = itsdangerous.TimestampSigner(str(config.secret_key()))


@asynccontextmanager
async def lifespan(app):
    tunnel.start(asyncio.get_running_loop())
    yield
    tunnel.stop()


# ----------------------------------------------------------------------
# Sessione del pannello e in-out scelto
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


def _device(conn) -> dict | None:
    device_id = conn.cookies.get(DEVICE_COOKIE) or ""
    if not config.REMOTE_ENABLED or not config.DEVICE_ID_RE.match(device_id):
        return None
    row = db.q1("SELECT id, name, channel_open FROM devices WHERE id=?", device_id)
    return dict(row) if row else None


def _panel_url(request: Request) -> str:
    """Indirizzo del pannello sullo stesso host: HTTPS tramite Caddy, oppure HTTP sulla porta del pannello (LAN)."""
    host = request.url.hostname or config.BROKER_HOST
    if ":" in host:
        host = f"[{host}]"
    if request.url.scheme == "https":
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


def _wants_html(request: Request) -> bool:
    return "text/html" in request.headers.get("accept", "") and not request.url.path.startswith("/api/")


def _error(request: Request, status: int, title: str, text: str, device: dict | None = None) -> Response:
    if _wants_html(request):
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


def _local_location(value: str) -> str:
    """Un redirect assoluto verso l'indirizzo locale dell'in-out diventa relativo."""
    for prefix in ("http://127.0.0.1", "http://localhost", "https://127.0.0.1", "https://localhost"):
        if value.startswith(prefix):
            rest = value[len(prefix):]
            if rest.startswith(":"):
                rest = rest[rest.find("/"):] if "/" in rest else "/"
            return rest or "/"
    return value


# ----------------------------------------------------------------------
# Scelta dell'in-out
# ----------------------------------------------------------------------
async def open_device(request: Request):
    device_id = request.path_params["device_id"]
    if not _logged_in(request):
        return RedirectResponse(f"{_panel_url(request)}/login", status_code=303)
    if not config.REMOTE_ENABLED:
        return _page(request, "Accesso remoto disattivato", "Imposta <code>REMOTE_ENABLED=1</code> nel file .env del broker.", 403)
    row = db.q1("SELECT id FROM devices WHERE id=?", device_id) if config.DEVICE_ID_RE.match(device_id) else None
    if not row:
        return _page(request, "In-out non trovato", f"Il dispositivo <b>{html.escape(device_id)}</b> non esiste.", 404)
    with db.tx():
        db.add_event("admin", device_id, "aperto l'accesso remoto all'in-out")
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie(
        DEVICE_COOKIE, device_id, max_age=SESSION_MAX_AGE, httponly=True, samesite="strict",
        secure=request.url.scheme == "https",
    )
    return resp


async def close_device(request: Request):
    device = _device(request)
    resp = RedirectResponse(
        f"{_panel_url(request)}/devices/{quote(device['id'])}" if device else f"{_panel_url(request)}/devices",
        status_code=303,
    )
    resp.delete_cookie(DEVICE_COOKIE)
    return resp


async def remote_status(request: Request):
    if not _logged_in(request):
        return Response(json.dumps({"detail": "non autenticato"}), status_code=401, media_type="application/json")
    device = _device(request)
    return Response(
        json.dumps({"device": device["id"] if device else None, "tunnel_connected": tunnel.connected,
                    "error": tunnel.last_error}),
        media_type="application/json",
    )


# ----------------------------------------------------------------------
# Inoltro HTTP
# ----------------------------------------------------------------------
async def proxy_http(request: Request):
    if not _logged_in(request):
        if _wants_html(request):
            return RedirectResponse(f"{_panel_url(request)}/login", status_code=303)
        return _error(request, 401, "Non autenticato", "accedi al pannello del broker.")
    device = _device(request)
    if device is None:
        return _error(request, 400, "Nessun in-out selezionato",
                      "Apri l'accesso remoto dalla pagina del dispositivo nel pannello.")
    if not device["channel_open"]:
        return _error(request, 409, "Canale chiuso",
                      "L'in-out non è collegato al broker finché il canale è chiuso.", device)

    size = request.headers.get("content-length")
    if size and size.isdigit() and int(size) > config.REMOTE_MAX_BYTES:
        return _error(request, 413, "Richiesta troppo grande", "supera il limite del tunnel.", device)
    body = await request.body()
    if len(body) > config.REMOTE_MAX_BYTES:
        return _error(request, 413, "Richiesta troppo grande", "supera il limite del tunnel.", device)

    path = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    header = {
        "t": "http",
        "method": request.method,
        "path": path,
        "headers": _request_headers(request.headers.items(), HOP_BY_HOP),
    }
    try:
        res, data = await tunnel.request(device["id"], header, body, timeout=config.REMOTE_TIMEOUT)
    except asyncio.TimeoutError:
        return _error(request, 504, "L'in-out non risponde",
                      "Nessuna risposta attraverso il broker: l'in-out è spento, non collegato o non aggiornato "
                      "a una versione che supporta l'accesso remoto.", device)
    except ConnectionError as e:
        return _error(request, 502, "Proxy non collegato al broker", html.escape(str(e)), device)
    if res.get("error"):
        return _error(request, 502, "Errore dell'in-out", html.escape(str(res["error"])), device)

    resp = Response(data, status_code=int(res.get("status") or 502))
    for key, value in res.get("headers") or []:
        k = str(key).lower()
        if k in HOP_BY_HOP:
            continue
        if k == "location":
            value = _local_location(str(value))
        resp.raw_headers.append((k.encode("latin-1"), str(value).encode("latin-1")))
    return resp


# ----------------------------------------------------------------------
# Inoltro websocket
# ----------------------------------------------------------------------
async def proxy_ws(ws: WebSocket):
    if not _logged_in(ws):
        await ws.close(code=4401)
        return
    device = _device(ws)
    if device is None or not device["channel_open"]:
        await ws.close(code=4404)
        return

    device_id = device["id"]
    sid, queue = tunnel.open_session(device_id)
    closed_by_device = False
    try:
        path = ws.url.path + (f"?{ws.url.query}" if ws.url.query else "")
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
        Route(RESERVED + "close", close_device, methods=["GET", "POST"]),
        Route(RESERVED + "status", remote_status, methods=["GET"]),
        Route("/{path:path}", proxy_http, methods=METHODS),
        WebSocketRoute("/{path:path}", proxy_ws),
    ],
)
