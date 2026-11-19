"""Terminale SSH verso gli in-out attraverso la connessione MQTT.

Il pannello fa da client SSH (asyncssh) e i byte della connessione viaggiano nel
tunnel MQTT (stessi topic dell'accesso remoto, `<id>/tunnel/req|res`): l'in-out li
inoltra al suo sshd locale (127.0.0.1:22). La sessione SSH è cifrata da un capo
all'altro, il tunnel trasporta solo byte.

    pannello -> in-out                         in-out -> pannello
    {"t":"ssh_open","id","key"}                {"t":"ssh_open","id","ok","user","error"}
    {"t":"ssh_data","id"} + byte               {"t":"ssh_data","id"} + byte
    {"t":"ssh_close","id"}                     {"t":"ssh_close","id"}

Autenticazione: il broker ha una sua chiave SSH (`DATA_DIR/ssh/id_ed25519`, creata
al primo uso; la privata non lascia mai il broker). La chiave pubblica viaggia in
`ssh_open`: l'in-out la installa da solo in authorized_keys dell'utente scelto
sull'in-out, con il vincolo from="127.0.0.1" (vale solo attraverso il tunnel), e
risponde con il nome dell'utente. Il terminale si apre così senza password.

Il trasporto è un oggetto asyncio che scrive direttamente nel tunnel: asyncssh lo
usa come `tunnel` (come farebbe con un salto SSH), senza socket locali.

Chiave dell'host: non viene verificata. Il canale è già autenticato dal certificato
client dell'in-out (mTLS su Mosquitto, un solo dispositivo per topic) e l'in-out si
collega solo al proprio 127.0.0.1.
"""
import asyncio
import os
import threading

import asyncssh

from . import config
from .tunnel import Tunnel

KEY_PATH = config.SSH_DIR / "id_ed25519"
OPEN_TIMEOUT = 20

_key_lock = threading.Lock()
_key: asyncssh.SSHKey | None = None


def private_key() -> asyncssh.SSHKey:
    """Chiave SSH del broker: letta dal volume dati o generata al primo uso."""
    global _key
    with _key_lock:
        if _key is None:
            if KEY_PATH.exists():
                _key = asyncssh.read_private_key(str(KEY_PATH))
            else:
                config.SSH_DIR.mkdir(parents=True, exist_ok=True)
                os.chmod(config.SSH_DIR, 0o700)
                key = asyncssh.generate_private_key("ssh-ed25519", comment="in-out-remote-broker")
                tmp = KEY_PATH.with_name(KEY_PATH.name + ".tmp")
                key.write_private_key(str(tmp))
                os.chmod(tmp, 0o600)
                tmp.replace(KEY_PATH)
                (config.SSH_DIR / "id_ed25519.pub").write_bytes(key.export_public_key())
                _key = key
        return _key


def public_key() -> str:
    """Chiave pubblica in formato OpenSSH, senza commento (l'in-out mette il suo)."""
    return " ".join(private_key().export_public_key().decode().split()[:2])


class SSHSessionError(Exception):
    pass


class _TunnelTransport(asyncio.Transport):
    """Trasporto asyncio sopra il tunnel MQTT: quello che asyncssh scrive diventa ssh_data
    verso l'in-out, quello che arriva dall'in-out viene passato al protocollo SSH."""

    def __init__(self, session: "TunnelSSH", protocol: asyncio.Protocol):
        super().__init__()
        self._session = session
        self._protocol = protocol
        self._closing = False

    def get_extra_info(self, name, default=None):
        if name == "peername":
            return (self._session.device_id, 22)
        if name == "sockname":
            return ("127.0.0.1", 0)
        return default

    def write(self, data) -> None:
        if self._closing or not data:
            return
        if not self._session.tunnel.send(self._session.device_id, {"t": "ssh_data", "id": self._session.sid}, bytes(data)):
            self.abort()

    def is_closing(self) -> bool:
        return self._closing

    def can_write_eof(self) -> bool:
        return False

    def pause_reading(self) -> None:
        pass

    def resume_reading(self) -> None:
        pass

    def get_write_buffer_size(self) -> int:
        return 0

    def set_write_buffer_limits(self, high=None, low=None) -> None:
        pass

    def close(self) -> None:
        if self._closing:
            return
        self._closing = True
        self._session.closed_locally()
        asyncio.get_running_loop().call_soon(self._protocol.connection_lost, None)

    def abort(self) -> None:
        self.close()

    def lost_from_device(self) -> None:
        """L'in-out ha chiuso la connessione (logout, sshd fermo, canale perso)."""
        if self._closing:
            return
        self._closing = True
        self._protocol.connection_lost(None)


class TunnelSSH:
    """Una connessione SSH verso un in-out, trasportata dal tunnel MQTT.

    asyncssh usa questo oggetto come `tunnel`: `create_connection` restituisce un
    trasporto che scrive nel tunnel, mentre un task passa al protocollo SSH i byte
    che arrivano dall'in-out.
    """

    def __init__(self, tunnel: Tunnel, device_id: str):
        self.tunnel = tunnel
        self.device_id = device_id
        self.sid: str | None = None
        self.user: str | None = None
        self.conn: asyncssh.SSHClientConnection | None = None
        self._queue: asyncio.Queue | None = None
        self._transport: _TunnelTransport | None = None
        self._reader: asyncio.Task | None = None
        self._closed_by_device = False

    async def create_connection(self, protocol_factory, host, port):
        protocol = protocol_factory()
        transport = _TunnelTransport(self, protocol)
        self._transport = transport
        protocol.connection_made(transport)
        self._reader = asyncio.create_task(self._device_to_ssh(transport, protocol))
        return transport, protocol

    async def _device_to_ssh(self, transport: _TunnelTransport, protocol) -> None:
        try:
            while True:
                head, data = await self._queue.get()
                if head.get("t") == "ssh_close":
                    self._closed_by_device = True
                    transport.lost_from_device()
                    return
                if head.get("t") == "ssh_data" and data and not transport.is_closing():
                    protocol.data_received(data)
        except asyncio.CancelledError:
            pass

    def closed_locally(self) -> None:
        """asyncssh ha chiuso la connessione: lo si dice all'in-out."""
        if self.sid and not self._closed_by_device:
            self._closed_by_device = True  # una sola notifica
            self.tunnel.send(self.device_id, {"t": "ssh_close", "id": self.sid})

    async def open(self) -> asyncssh.SSHClientConnection:
        sid, queue = self.tunnel.open_session(self.device_id)
        self.sid = sid
        self._queue = queue
        try:
            res, _ = await self.tunnel.request(
                self.device_id, {"t": "ssh_open", "id": sid, "key": public_key()}, timeout=OPEN_TIMEOUT,
            )
        except asyncio.TimeoutError:
            self._closed_by_device = True
            raise SSHSessionError("l'in-out non risponde: è spento, non collegato o non aggiornato "
                                  "a una versione con il terminale remoto")
        except ConnectionError as e:
            self._closed_by_device = True
            raise SSHSessionError(f"proxy non collegato al broker: {e}")
        if not res.get("ok"):
            self._closed_by_device = True
            raise SSHSessionError(str(res.get("error") or "apertura rifiutata dall'in-out"))
        self.user = str(res.get("user") or "root")

        try:
            self.conn = await asyncio.wait_for(asyncssh.connect(
                self.device_id, 22,
                tunnel=self,
                username=self.user,
                client_keys=[private_key()],
                known_hosts=None,
                agent_path=None,
                config=None,
                preferred_auth="publickey",
                keepalive_interval=30,
            ), OPEN_TIMEOUT)
        except asyncssh.PermissionDenied:
            await self.close()
            raise SSHSessionError(f"accesso SSH negato per l'utente '{self.user}': controlla che sshd "
                                  "accetti le chiavi (PubkeyAuthentication) e, per root, PermitRootLogin")
        except (OSError, asyncssh.Error, asyncio.TimeoutError) as e:
            await self.close()
            raise SSHSessionError(f"connessione SSH non riuscita: {e or type(e).__name__}")
        return self.conn

    async def close(self) -> None:
        if self.conn is not None:
            self.conn.close()
            try:
                await asyncio.wait_for(self.conn.wait_closed(), 3)
            except (asyncio.TimeoutError, OSError, asyncssh.Error):
                pass
            self.conn = None
        if self._transport is not None:
            self._transport.close()
        if self._reader is not None:
            self._reader.cancel()
            await asyncio.gather(self._reader, return_exceptions=True)
            self._reader = None
        if self.sid:
            self.closed_locally()
            self.tunnel.close_session(self.sid)
            self.sid = None
