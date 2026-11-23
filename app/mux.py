"""Porta unica: smista le connessioni TLS tra Mosquitto, registrazione e pannello.

Tutto il traffico arriva su una sola porta pubblica (443). Il TLS NON viene
decifrato: dal ClientHello si legge solo l'ALPN dichiarato dal client e la
connessione viene inoltrata così com'è:

    ALPN "mqtt"              -> Mosquitto 127.0.0.1:8883 (mTLS con il certificato client)
    ALPN "baronpesi-enroll"  -> registrazione 127.0.0.1:8884 (certificato della CA interna)
    altro (browser, ACME)    -> MUX_WEB_UPSTREAM (Caddy, Let's Encrypt) con PROXY protocol v2

Così mTLS, CRL e fiducia nella CA interna restano invariati.

IP reale: Mosquitto e la registrazione vedono la connessione arrivare da
127.0.0.1:<porta>. Prima di collegarsi il mux annota nel DB porta locale -> IP
del client; il lettore del log di Mosquitto e la registrazione lo recuperano
con `db.real_ip`. Verso Caddy l'IP viaggia nel PROXY protocol.
"""
import asyncio
import ipaddress
import socket
import struct
import sys

from . import config, db

HANDSHAKE_TIMEOUT = 10        # secondi per ricevere il ClientHello
MAX_HELLO = 64 * 1024         # dimensione massima accettata del ClientHello
CLOSE_GRACE = 10              # secondi concessi all'altra direzione dopo una chiusura
BUF = 64 * 1024

PP2_SIGNATURE = b"\r\n\r\n\x00\r\nQUIT\n"


# ----------------------------------------------------------------------
# ClientHello
# ----------------------------------------------------------------------
class NotTLS(Exception):
    pass


async def read_client_hello(reader: asyncio.StreamReader) -> tuple[bytes, list[str]]:
    """Legge i record TLS fino al ClientHello completo.

    Ritorna (byte letti, da inoltrare tali e quali; protocolli ALPN offerti).
    Solleva NotTLS se il client non parla TLS: i byte letti sono in `args[0]`."""
    raw = bytearray()
    hs = bytearray()
    need = None
    while need is None or len(hs) < need:
        head = await reader.readexactly(5)
        raw += head
        if head[0] != 0x16 or head[1] != 0x03:
            raise NotTLS(bytes(raw))
        length = struct.unpack("!H", head[3:5])[0]
        body = await reader.readexactly(length)
        raw += body
        hs += body
        if need is None and len(hs) >= 4:
            if hs[0] != 0x01:  # non è un ClientHello
                raise NotTLS(bytes(raw))
            need = 4 + int.from_bytes(hs[1:4], "big")
        if len(raw) > MAX_HELLO:
            raise NotTLS(bytes(raw))
    try:
        alpn = parse_alpn(bytes(hs[4:need]))
    except (IndexError, struct.error, UnicodeDecodeError):
        alpn = []
    return bytes(raw), alpn


def parse_alpn(hello: bytes) -> list[str]:
    """Estrae l'estensione ALPN (16) dal corpo di un ClientHello."""
    p = 2 + 32                                   # versione + random
    p += 1 + hello[p]                            # session id
    p += 2 + struct.unpack("!H", hello[p:p + 2])[0]   # cipher suites
    p += 1 + hello[p]                            # compressione
    if p + 2 > len(hello):
        return []
    end = p + 2 + struct.unpack("!H", hello[p:p + 2])[0]
    p += 2
    while p + 4 <= end:
        ext_type, ext_len = struct.unpack("!HH", hello[p:p + 4])
        p += 4
        if ext_type == 16:
            data = hello[p:p + ext_len]
            out, q = [], 2
            while q < len(data):
                n = data[q]
                out.append(data[q + 1:q + 1 + n].decode("ascii"))
                q += 1 + n
            return out
        p += ext_len
    return []


# ----------------------------------------------------------------------
# PROXY protocol v2 (verso Caddy)
# ----------------------------------------------------------------------
def proxy_v2_header(src: tuple, dst: tuple) -> bytes:
    try:
        s_ip = ipaddress.ip_address(src[0])
        d_ip = ipaddress.ip_address(dst[0])
    except (ValueError, IndexError):
        return PP2_SIGNATURE + b"\x20\x00\x00\x00"  # LOCAL: nessun indirizzo
    if isinstance(s_ip, ipaddress.IPv6Address) and s_ip.ipv4_mapped:
        s_ip = s_ip.ipv4_mapped
    if isinstance(d_ip, ipaddress.IPv6Address) and d_ip.ipv4_mapped:
        d_ip = d_ip.ipv4_mapped
    if s_ip.version != d_ip.version:
        d_ip = ipaddress.ip_address("0.0.0.0") if s_ip.version == 4 else ipaddress.ip_address("::")
    fam = 0x11 if s_ip.version == 4 else 0x21     # TCP su IPv4 / IPv6
    addrs = s_ip.packed + d_ip.packed + struct.pack("!HH", src[1], dst[1])
    return PP2_SIGNATURE + bytes([0x21, fam]) + struct.pack("!H", len(addrs)) + addrs


# ----------------------------------------------------------------------
# Inoltro
# ----------------------------------------------------------------------
def _route(alpn: list[str]) -> str:
    if config.ALPN_MQTT in alpn:
        return "mqtt"
    if config.ALPN_ENROLL in alpn:
        return "enroll"
    return "web"


def _split_hostport(value: str) -> tuple[str, int]:
    host, _, port = value.rpartition(":")
    return host.strip("[]"), int(port)


async def _open_local(port: int, client_ip: str):
    """Connessione a un servizio locale, annotando prima porta locale -> IP del client."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.setblocking(False)
        sock.bind(("127.0.0.1", 0))
        await asyncio.to_thread(db.mux_record, sock.getsockname()[1], client_ip)
        await asyncio.get_running_loop().sock_connect(sock, ("127.0.0.1", port))
    except BaseException:
        sock.close()
        raise
    return await asyncio.open_connection(sock=sock)


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while True:
            data = await reader.read(BUF)
            if not data:
                break
            writer.write(data)
            await writer.drain()
        if writer.can_write_eof():
            writer.write_eof()
    except (ConnectionError, OSError):
        pass


def _close(writer: asyncio.StreamWriter) -> None:
    try:
        writer.close()
    except Exception:
        pass


def _keepalive(writer: asyncio.StreamWriter) -> None:
    sock = writer.get_extra_info("socket")
    if sock is not None:
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        except OSError:
            pass


class Mux:
    def __init__(self):
        self.active = 0
        self.web_host, self.web_port = _split_hostport(config.MUX_WEB_UPSTREAM)

    async def handle(self, c_reader: asyncio.StreamReader, c_writer: asyncio.StreamWriter) -> None:
        peer = c_writer.get_extra_info("peername") or ("?", 0)
        local = c_writer.get_extra_info("sockname") or ("0.0.0.0", 0)
        client_ip = peer[0]
        if client_ip.startswith("::ffff:"):
            client_ip = client_ip[7:]
        if self.active >= config.MUX_MAX_CONNECTIONS:
            _close(c_writer)
            return
        self.active += 1
        u_writer = None
        try:
            try:
                first, alpn = await asyncio.wait_for(read_client_hello(c_reader), HANDSHAKE_TIMEOUT)
            except NotTLS as e:
                first, alpn = e.args[0], []
            route = _route(alpn)
            if route == "mqtt":
                u_reader, u_writer = await _open_local(config.BROKER_INTERNAL_PORT, client_ip)
            elif route == "enroll":
                u_reader, u_writer = await _open_local(config.ENROLL_INTERNAL_PORT, client_ip)
            else:
                u_reader, u_writer = await asyncio.open_connection(self.web_host, self.web_port)
                if config.MUX_WEB_PROXY_PROTOCOL:
                    u_writer.write(proxy_v2_header((client_ip, peer[1]), local))
            _keepalive(c_writer)
            _keepalive(u_writer)
            u_writer.write(first)
            await u_writer.drain()

            up = asyncio.create_task(_pipe(c_reader, u_writer))
            down = asyncio.create_task(_pipe(u_reader, c_writer))
            done, pending = await asyncio.wait({up, down}, return_when=asyncio.FIRST_COMPLETED)
            if pending:
                await asyncio.wait(pending, timeout=CLOSE_GRACE)
                for t in pending:
                    t.cancel()
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, ConnectionError, OSError):
            pass
        except Exception as e:
            print(f"[mux] errore da {client_ip}: {e!r}", flush=True)
        finally:
            self.active -= 1
            if u_writer is not None:
                _close(u_writer)
            _close(c_writer)


async def _prune_loop() -> None:
    while True:
        try:
            await asyncio.to_thread(db.mux_prune)
        except Exception as e:
            print(f"[mux] pulizia DB: {e}", flush=True)
        await asyncio.sleep(3600)


async def serve() -> None:
    mux = Mux()
    server = await asyncio.start_server(
        mux.handle, host=None, port=config.MUX_INTERNAL_PORT, reuse_address=True, backlog=512,
    )
    print(
        f"[mux] porta unica in ascolto su {config.MUX_INTERNAL_PORT} "
        f"(mqtt -> {config.BROKER_INTERNAL_PORT}, enroll -> {config.ENROLL_INTERNAL_PORT}, "
        f"web -> {config.MUX_WEB_UPSTREAM})",
        flush=True,
    )
    asyncio.create_task(_prune_loop())
    async with server:
        await server.serve_forever()


def main() -> None:
    if not config.MUX_ENABLED:
        print("[mux] porta unica disattivata (MUX_ENABLED=0)", flush=True)
        sys.exit(0)
    db.init()
    asyncio.run(serve())


if __name__ == "__main__":
    main()
