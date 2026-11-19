"""Tunnel HTTP/websocket verso l'API degli in-out attraverso la connessione MQTT.

L'in-out resta sempre collegato al broker: il pannello gli invia le richieste HTTP
(e i messaggi websocket) su `<id>/tunnel/req` e l'in-out le esegue sulla sua API
locale, rispondendo su `<id>/tunnel/res`. Così l'in-out si controlla da remoto
senza aprire porte verso di lui.

Ogni messaggio MQTT è: intestazione JSON su una riga, "\\n", corpo binario.

    pannello -> in-out                         in-out -> pannello
    {"t":"http","id","method","path","headers"} {"t":"http","id","status","headers"} | {"t":"http","id","error"}
    {"t":"ws_open","id","path","headers","subprotocols"}  {"t":"ws_open","id","ok","subprotocol","error"}
    {"t":"ws_msg","id","bin"} + dati           {"t":"ws_msg","id","bin"} + dati
    {"t":"ws_close","id","code"}               {"t":"ws_close","id","code"}

`{"t":"info","id"}` chiede all'in-out lo stato della macchina (versione, disco, memoria, SSH).
Sullo stesso canale passa il terminale SSH (messaggi ssh_open/ssh_data/ssh_close, vedi `ssh.py`).

Si usa QoS 0: sulla stessa connessione TCP i messaggi arrivano in ordine e, se la
connessione cade, le richieste in corso falliscono comunque per timeout.
"""
import asyncio
import json
import os
import ssl
import uuid

import paho.mqtt.client as mqtt

from . import config, pki


def encode(header: dict, body: bytes = b"") -> bytes:
    return json.dumps(header, separators=(",", ":"), ensure_ascii=True).encode() + b"\n" + body


def decode(payload: bytes) -> tuple[dict, bytes]:
    head, _, body = payload.partition(b"\n")
    header = json.loads(head)
    if not isinstance(header, dict):
        raise ValueError("intestazione non valida")
    return header, body


class Tunnel:
    """Client MQTT del proxy remoto: invia le richieste e smista le risposte alle coroutine in attesa."""

    def __init__(self):
        self.loop: asyncio.AbstractEventLoop | None = None
        self.client: mqtt.Client | None = None
        self.connected = False
        self.last_error: str | None = None
        self._waiters: dict[str, asyncio.Future] = {}
        self._sockets: dict[str, asyncio.Queue] = {}
        # id della richiesta -> dispositivo: una risposta vale solo se arriva dal topic giusto
        self._owner: dict[str, str] = {}

    # ------------------------------------------------------------------
    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop
        client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"__tunnel-{os.getpid()}-{uuid.uuid4().hex[:6]}",
            clean_session=True,
        )
        client.tls_set(
            ca_certs=str(pki.CA_CRT),
            certfile=str(pki.MONITOR_CRT),
            keyfile=str(pki.MONITOR_KEY),
            cert_reqs=ssl.CERT_REQUIRED,
        )
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message
        client.reconnect_delay_set(min_delay=1, max_delay=10)
        client.connect_async("localhost", config.BROKER_INTERNAL_PORT, keepalive=30)
        client.loop_start()
        self.client = client

    def stop(self) -> None:
        if self.client:
            self.client.disconnect()
            self.client.loop_stop()

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            self.connected = False
            self.last_error = f"connessione al broker rifiutata: {reason_code}"
            return
        self.connected = True
        self.last_error = None
        client.subscribe(f"+/{config.TUNNEL_RES_SUFFIX}", qos=0)

    def _on_disconnect(self, client, userdata, flags, reason_code, properties):
        self.connected = False
        if reason_code.is_failure:
            self.last_error = f"disconnesso dal broker: {reason_code}"

    def _on_message(self, client, userdata, msg: mqtt.MQTTMessage):
        device_id = msg.topic.split("/")[0]
        try:
            header, body = decode(msg.payload)
        except (ValueError, UnicodeDecodeError):
            return
        if self.loop is not None:
            self.loop.call_soon_threadsafe(self._dispatch, device_id, header, body)

    def _dispatch(self, device_id: str, header: dict, body: bytes) -> None:
        sid = str(header.get("id") or "")
        if not sid or self._owner.get(sid) != device_id:
            return
        kind = header.get("t")
        if kind in ("http", "ws_open", "ssh_open", "info"):
            fut = self._waiters.pop(sid, None)
            if fut is not None and not fut.done():
                fut.set_result((header, body))
        elif kind in ("ws_msg", "ws_close", "ssh_data", "ssh_close"):
            q = self._sockets.get(sid)
            if q is not None:
                q.put_nowait((header, body))

    # ------------------------------------------------------------------
    def send(self, device_id: str, header: dict, body: bytes = b"") -> bool:
        client = self.client
        if client is None or not self.connected:
            return False
        info = client.publish(config.tunnel_req_topic(device_id), encode(header, body), qos=0)
        return info.rc == mqtt.MQTT_ERR_SUCCESS

    async def request(self, device_id: str, header: dict, body: bytes = b"", timeout: float = 30) -> tuple[dict, bytes]:
        """Invia una richiesta e attende la risposta con lo stesso id. TimeoutError se non arriva."""
        sid = header.setdefault("id", uuid.uuid4().hex)
        fut = asyncio.get_running_loop().create_future()
        self._waiters[sid] = fut
        self._owner[sid] = device_id
        try:
            if not self.send(device_id, header, body):
                raise ConnectionError(self.last_error or "proxy non collegato al broker")
            return await asyncio.wait_for(fut, timeout)
        finally:
            self._waiters.pop(sid, None)
            if sid not in self._sockets:
                self._owner.pop(sid, None)

    def open_session(self, device_id: str) -> tuple[str, asyncio.Queue]:
        sid = uuid.uuid4().hex
        q: asyncio.Queue = asyncio.Queue()
        self._sockets[sid] = q
        self._owner[sid] = device_id
        return sid, q

    def close_session(self, sid: str) -> None:
        self._sockets.pop(sid, None)
        self._owner.pop(sid, None)

