"""Porta unica per il browser: pannello e accesso remoto agli in-out sulla 8080.

Il pannello (`app.main`) e il proxy remoto (`app.remote`) girano nello stesso processo e
questo smistatore sceglie a chi passare ogni richiesta. Sia il pannello sia l'interfaccia
dell'in-out usano percorsi assoluti (/login, /static, /api...), quindi l'ordine conta:

1. `/__panel/...`   -> pannello, senza il prefisso. È l'indirizzo con cui il proxy remoto
                       rimanda al pannello (login, "Torna al pannello"): da una pagina
                       dell'in-out un `/login` qualsiasi andrebbe invece all'in-out.
2. `/__remote/...`  -> proxy remoto (apertura, terminale, file, stato della macchina).
3. Referer da una pagina `/<id>/...` di un in-out -> proxy remoto, che reindirizza la
   richiesta senza prefisso all'in-out giusto (link, immagini, `location.href = '/login'`).
4. primo segmento del percorso usato dal pannello (`/login`, `/devices`, `/static`...) -> pannello.
5. `/<id>/...` di un in-out registrato -> proxy remoto.
6. altro: websocket al proxy remoto (in-out dell'ultimo aperto), HTTP al pannello.

Un in-out con lo stesso nome di una sezione del pannello (es. `devices`) resta
raggiungibile solo dal pannello: al punto 4 vince il pannello.
"""
from contextlib import AsyncExitStack
from urllib.parse import unquote, urlsplit

from . import main, remote

PANEL_PREFIX = "/__panel"
REMOTE_PREFIX = remote.RESERVED  # "/__remote/"


def _first_segment(path: str) -> str:
    return path.lstrip("/").partition("/")[0]


def _panel_segments() -> set[str]:
    """Primi segmenti dei percorsi del pannello, ricavati dalle sue rotte."""
    out = set()
    for route in main.app.routes:
        path = getattr(route, "path", "") or ""
        first = _first_segment(path)
        if first and not first.startswith("{"):
            out.add(first)
    return out


PANEL_SEGMENTS = _panel_segments()


def _header(scope, name: bytes) -> str | None:
    for k, v in scope.get("headers") or ():
        if k == name:
            return v.decode("latin-1")
    return None


def _is_device(segment: str) -> bool:
    return bool(segment) and remote._device_by_id(unquote(segment)) is not None


def _referer_is_device(scope) -> bool:
    ref = _header(scope, b"referer")
    if not ref:
        return False
    try:
        parts = urlsplit(ref)
    except ValueError:
        return False
    if parts.netloc != _header(scope, b"host"):
        return False
    first = _first_segment(parts.path)
    return first not in PANEL_SEGMENTS and _is_device(first)


def _strip_panel_prefix(scope) -> dict:
    path = scope["path"][len(PANEL_PREFIX):] or "/"
    scope = dict(scope)
    scope["path"] = path
    raw = scope.get("raw_path")
    if raw is not None:
        scope["raw_path"] = raw[len(PANEL_PREFIX):] or b"/"
    return scope


def route(scope) -> tuple[str, dict]:
    """("panel" | "remote", scope da passare all'app scelta)."""
    path = scope["path"]
    if path == PANEL_PREFIX or path.startswith(PANEL_PREFIX + "/"):
        return "panel", _strip_panel_prefix(scope)
    if path.startswith(REMOTE_PREFIX):
        return "remote", scope
    if _referer_is_device(scope):
        return "remote", scope
    first = _first_segment(path)
    if first in PANEL_SEGMENTS or path == "/":
        return "panel", scope
    if _is_device(first):
        return "remote", scope
    if scope["type"] == "websocket":
        return "remote", scope
    return "panel", scope


async def _lifespan(receive, send):
    stack = AsyncExitStack()
    await receive()  # lifespan.startup
    try:
        await stack.enter_async_context(main.app.router.lifespan_context(main.app))
        await stack.enter_async_context(remote.app.router.lifespan_context(remote.app))
    except Exception as exc:  # noqa: BLE001
        await stack.aclose()
        await send({"type": "lifespan.startup.failed", "message": str(exc)})
        return
    await send({"type": "lifespan.startup.complete"})
    await receive()  # lifespan.shutdown
    await stack.aclose()
    await send({"type": "lifespan.shutdown.complete"})


async def app(scope, receive, send):
    if scope["type"] == "lifespan":
        await _lifespan(receive, send)
        return
    target, scope = route(scope)
    await (remote.app if target == "remote" else main.app)(scope, receive, send)
