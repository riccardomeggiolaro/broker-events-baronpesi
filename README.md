# broker-events-baronpesi

Broker MQTT (Mosquitto) per ricevere le pesate dagli **in-out**, con un pannello web per:

- **registrazione automatica**: l'in-out si configura solo con l'host del broker, genera la sua chiave e riceve da solo il certificato client, che rinnova prima della scadenza;
- **certificati**: CA interna, certificato del server, un certificato client per ogni in-out (la chiave privata resta sull'in-out), rinnovo, sostituzione su richiesta e revoca;
- **canali**: ogni dispositivo può pubblicare solo sul proprio topic; il canale si può chiudere e riaprire senza toccare l'in-out;
- **monitor live**: messaggi e connessioni in tempo reale; tutte le pagine del pannello si aggiornano da sole (nuovi dispositivi, stato online, certificati, eventi, messaggi) senza ricaricare;
- **storico**: ogni pesata viene archiviata con foto, PDF e CSV, consultabile e filtrabile; pulizia automatica dopo `RETENTION_DAYS` giorni;
- **stato**: dispositivi online/offline, eventi di connessione, tentativi rifiutati, log di Mosquitto;
- **accesso remoto**: dal pannello si apre l'interfaccia dell'in-out (pagine, API REST e websocket) attraverso la connessione MQTT, senza aprire porte verso l'impianto;
- **terminale SSH**: per ogni in-out un terminale nel browser, sempre attraverso la connessione MQTT, con accesso a chiave senza password.

```
                         ┌──────────────── container "broker" ─────────────────┐
 in-out  ALPN mqtt ───┐  │  porta unica ──▶ 8883 Mosquitto ◀── ACL/CRL ── pannello│
 in-out  ALPN enroll ─┼─▶│  443 (mux)   ──▶ 8884 registrazione (CSR)            │
 browser (h2/http1.1)─┘  │      │                    subscriber "#" ──▶ SQLite   │
                         │      └─ PROXY v2 ─▶ Caddy ──▶ 8080 pannello          │
                         │                  proxy remoto /<ID>/ ◀─▶ tunnel MQTT │
                         └──────────────────── volume /data ───────────────────┘
```

Su internet basta una porta: la **443**. Il container smista le connessioni TLS in base all'ALPN
dichiarato dal client, senza decifrarle (vedi [Porta unica](#porta-unica-443)). Le porte separate
8883/8884 restano disponibili per gli in-out non ancora aggiornati.

## Avvio

Requisiti: una VPS con Docker, un nome DNS (es. `mqtt.tuodominio.it`) che punta alla VPS, porte **80** e **443** aperte (più **8883** e **8884** finché ci sono in-out non aggiornati alla porta unica).

```bash
cp .env.example .env
```

```bash
nano .env        # BROKER_HOST e ADMIN_PASSWORD
```

```bash
docker compose up -d --build
```

Al primo avvio vengono creati automaticamente la CA, il certificato del server (per `BROKER_HOST`) e la configurazione di Mosquitto. Il pannello è su `https://BROKER_HOST` (certificato Let's Encrypt gestito da Caddy).

> Se hai già un reverse proxy, togli il servizio `caddy` dal compose e pubblica la porta 8080 del servizio `broker` solo verso il proxy. Senza HTTPS imposta `SESSION_HTTPS_ONLY=0`.

## Porta unica (443)

Con `docker-compose.yml` la 443 pubblica è del container `broker` (`app/mux.py`). Per ogni
connessione legge dal ClientHello TLS solo l'**ALPN** e la inoltra così com'è:

| ALPN offerto dal client | Destinazione | TLS |
|---|---|---|
| `mqtt` | Mosquitto (8883 interna) | mTLS con il certificato client, CRL: invariato |
| `baronpesi-enroll` | registrazione (8884 interna) | certificato della CA interna: invariato |
| altro (browser: `h2`, `http/1.1`; ACME) | Caddy, con PROXY protocol v2 | Let's Encrypt |

Il TLS non viene terminato dal mux, quindi certificati client, revoche e sospensioni funzionano
come sulle porte separate. L'IP reale del client resta visibile ovunque: verso Caddy viaggia nel
PROXY protocol; per Mosquitto e la registrazione il mux annota nel DB *porta locale → IP* prima di
collegarsi, e pannello e registrazione lo recuperano (eventi, `last_ip`, limite di richieste).

Cosa cambia negli in-out:

- porta MQTT e porta di registrazione: `443`;
- connessione MQTT con ALPN `mqtt` (paho: `ctx.set_alpn_protocols(["mqtt"])` e `client.tls_set_context(ctx)`);
- tutte le richieste di registrazione (`/ca.crt`, `/enroll`, `/crl.pem`) con ALPN `baronpesi-enroll`
  (`ssl.SSLContext.set_alpn_protocols(["baronpesi-enroll"])`, anche per il primo download della CA).

Senza ALPN una connessione sulla 443 arriva al pannello. Se la registrazione è arrivata dalla porta
unica, la risposta di `/enroll` contiene `"mqtt_port": 443` e `"mqtt_alpn": "mqtt"`.

Quando tutti gli in-out sono aggiornati, togli le righe `8883` e `8884` dai `ports` del compose.
In rete locale (`docker-compose.lan.yml`) la porta unica non è attiva (`MUX_ENABLED=0`).

## Avvio in rete locale (senza dominio)

Per un PC/server Linux nella stessa LAN degli in-out: niente Caddy, pannello in HTTP sulla porta 8080, `BROKER_HOST` = IP del PC (deve essere fisso).

```bash
cp .env.example .env
```

Nel `.env`: `BROKER_HOST=192.168.1.50` (l'IP del PC), `ADMIN_PASSWORD=...`, `SESSION_HTTPS_ONLY=0`.

```bash
docker compose -f docker-compose.lan.yml up -d --build
```

Pannello e accesso remoto agli in-out: `http://192.168.1.50:8080`. Negli in-out: host `192.168.1.50` (porte `8883` e `8884`).

## Collegare un in-out (registrazione automatica)

Sull'in-out, nella pagina **Broker MQTT**, imposta solo:

- Host: `BROKER_HOST` (lo stesso nome o IP presente nel certificato del server)
- Porta MQTT: `443`, porta registrazione: `443`, con ALPN (vedi [Porta unica](#porta-unica-443)). In-out non aggiornati o in rete locale: `8883` e `8884`

Al salvataggio l'in-out:

1. scarica la CA del broker da `https://BROKER_HOST:8884/ca.crt` e la salva (**fiducia al primo contatto**): da quel momento verifica sempre quella;
2. genera la sua chiave privata (EC P-256, non lascia mai l'in-out) e invia una richiesta CSR con il suo client ID;
3. riceve subito il certificato (validità `CLIENT_CERT_DAYS`, default 365 giorni) e si collega su `8883`.

Nel pannello il dispositivo compare da solo con il **canale aperto**. L'in-out controlla la scadenza ogni ora e rinnova il certificato (con la stessa chiave) quando mancano meno di 60 giorni.

| Situazione | Risposta del servizio di registrazione |
|---|---|
| dispositivo nuovo | certificato rilasciato, dispositivo creato con canale aperto |
| stessa chiave di un certificato valido | rinnovo: quello in uso resta valido fino alla scadenza, i precedenti vengono revocati |
| canale chiuso | rifiutata: **per bloccare un in-out chiudi il canale** |
| dispositivo eliminato o certificato revocato dal pannello | l'in-out, rifiutato da Mosquitto, controlla la CRL (`https://BROKER_HOST:8884/crl.pem`), elimina il certificato e si registra di nuovo da solo (con il canale chiuso invece aspetta) |
| chiave di un certificato revocato dall'amministratore | rifiutata: l'in-out genera una nuova chiave e riprova |
| altra chiave, ma il dispositivo ha già un certificato valido (es. in-out reinstallato) | rifiutata: revoca il vecchio certificato dal pannello e l'in-out si registra da solo al tentativo successivo (ogni 30 s) |

### Nuovo certificato dal pannello

Per un in-out registrato in automatico, nella pagina del dispositivo il pulsante **Nuovo certificato all'in-out**:

1. pubblica un comando *retained* sul topic `<ID>/control`, che l'in-out legge appena collegato (anche se al momento è spento);
2. l'in-out genera una **nuova chiave**, ritira il nuovo certificato sulla porta 8884 e si ricollega;
3. in quel momento i certificati precedenti vengono revocati e il comando viene cancellato.

Finché l'in-out non lo ritira, il dispositivo mostra la richiesta **in attesa** (annullabile). L'in-out resta sempre collegato, anche senza pesate da inviare, così riceve i comandi subito.

La registrazione automatica è l'unico modo per rilasciare i certificati: il pannello non genera più chiavi né zip da caricare a mano. Con `ENROLL_ENABLED=0` nuovi in-out non possono registrarsi e i certificati non vengono rinnovati (quelli già rilasciati restano validi fino alla scadenza).

L'identità del dispositivo è il CN del certificato: l'ACL gli permette di scrivere solo sul topic `<ID>` (e `<ID>/#`) e di leggere solo i comandi su `<ID>/control`. Nessun dispositivo può leggere i messaggi degli altri.

## Accesso remoto all'in-out

Nella pagina del dispositivo, **Apri l'in-out da remoto** apre in una nuova scheda l'interfaccia web dell'in-out, come se fossi in rete locale: pagine, chiamate alle API REST e websocket (pesi in tempo reale, log…). Non serve aprire porte verso l'impianto: tutto passa nella connessione MQTT/TLS che l'in-out tiene già aperta.

```
browser ──▶ https://BROKER_HOST/<ID>/... ──▶ proxy remoto ──MQTT <ID>/tunnel/req──▶ in-out ──▶ http://127.0.0.1:<porta API>/...
        ◀───────────────────────────────────────────────────────MQTT <ID>/tunnel/res◀──
```

- **Un indirizzo per ogni in-out**: `https://BROKER_HOST/<ID>/` (in LAN `http://IP:8080/<ID>/`). Si possono tenere aperti più in-out insieme, in schede diverse dello stesso browser, ognuno con il suo login.
- **Stessa porta del pannello**: `app/gateway.py` smista le richieste. `/__panel/...` va sempre al pannello, `/__remote/...` e `/<ID>/...` al proxy remoto, le richieste senza prefisso fatte da una pagina dell'in-out (Referer `/<ID>/...`) all'in-out, il resto al pannello. Un in-out con lo stesso nome di una sezione del pannello (`devices`, `login`, `static`...) non è raggiungibile da remoto.
- **Accesso**: serve il login del pannello (lo stesso cookie di sessione). Poi l'in-out chiede il suo login come sempre.
- L'interfaccia dell'in-out usa percorsi assoluti (`/api`, `/static`, `/login`…). Il proxy la adatta al prefisso `/<ID>` senza modificare l'in-out:
  - nelle pagine HTML inietta `app/static/remote-shim.js`, che aggiunge il prefisso a `fetch`, XHR, WebSocket, EventSource, `window.open` e `history`, e tiene separato il `localStorage` di ogni in-out (chiavi `/<ID>:token`…);
  - nelle pagine e negli script riscrive le letture di `location.pathname` nel percorso senza prefisso, così i controlli tipo `=== '/reservation'` funzionano;
  - le richieste senza prefisso (link, immagini, `location.href = '/login'`) vengono reindirizzate all'in-out della pagina che le ha fatte (dal `Referer`);
  - i redirect e i cookie dell'in-out ricevono il prefisso.
- I cookie del pannello non vengono mai inoltrati all'in-out. Ogni apertura è registrata negli eventi del dispositivo.
- Limiti: richieste e risposte fino a 32 MB (e comunque entro `MAX_MESSAGE_MB`), attesa massima `REMOTE_TIMEOUT` secondi. Le risposte in streaming (download progressivi, SSE) arrivano solo quando sono complete.
- Serve l'in-out aggiornato alla versione con l'accesso remoto (modulo `remote_tunnel`). Sull'in-out si può disattivare con `app_api.message_broker.remote_access = false` in `config.json`.

Il canale deve essere aperto: l'ACL permette all'in-out di leggere `<ID>/tunnel/req` e di rispondere su `<ID>/tunnel/res`; il pannello è l'unico che può scrivere sulle richieste. Questi messaggi non finiscono nello storico.

## Terminale SSH dell'in-out

Nella pagina del dispositivo, **Apri il terminale SSH** apre in una nuova scheda un terminale (xterm.js) collegato alla shell dell'in-out. Anche l'SSH passa nella connessione MQTT/TLS già aperta dall'in-out: sull'impianto non si apre nessuna porta.

```
browser ──wss://BROKER_HOST/__remote/ssh/<ID>──▶ proxy remoto (client SSH) ──MQTT <ID>/tunnel/req──▶ in-out ──▶ sshd 127.0.0.1:22
        ◀── byte del terminale ──────────────────────────────────────────────MQTT <ID>/tunnel/res◀──
```

- **Un terminale per ogni in-out**: `https://BROKER_HOST/__remote/terminal/<ID>`. Se ne possono aprire più di uno, anche verso in-out diversi, in schede separate.
- **SSH cifrato da un capo all'altro**: il proxy remoto fa da client SSH (asyncssh) e il tunnel trasporta solo i byte della connessione verso il sshd dell'in-out.
- **Accesso con chiave, senza password**: il broker ha una sua chiave SSH (`/data/ssh/id_ed25519`, creata al primo uso; la privata non lascia mai il broker). La chiave pubblica viaggia con ogni apertura del terminale: l'in-out la installa da solo in `authorized_keys` dell'utente scelto nelle sue impostazioni, con il vincolo `from="127.0.0.1,::1"`, cioè valida solo per le connessioni arrivate dal tunnel.
- **Sull'in-out** (pagina Broker MQTT): *Consenti il terminale SSH* (attivo di default; se lo disattivi la chiave viene tolta da `authorized_keys`) e *Utente SSH* (default `root`). Serve un in-out Linux con sshd attivo, che accetti le chiavi: per `root` serve `PermitRootLogin prohibit-password` (il default di Debian e Ubuntu) o `yes`.
- **Accesso al terminale**: serve il login del pannello; il websocket accetta solo connessioni dalla pagina del terminale (controllo dell'`Origin`). Ogni apertura è registrata negli eventi del dispositivo con l'utente usato.
- **Windows**: se OpenSSH Server manca, l'in-out ne avvia l'installazione alla prima apertura (serve che giri come amministratore) e chiede di riprovare dopo qualche minuto. La chiave va in `C:\ProgramData\ssh\administrators_authorized_keys` per gli amministratori, altrimenti in `%USERPROFILE%\.ssh\authorized_keys`. Con utente `root` (default) si usa l'utente con cui gira l'in-out.
- **File**: il pulsante *File* della pagina del terminale apre un pannello SFTP sulla stessa connessione: sfoglia le cartelle, scarica un file con un clic, carica file nella cartella aperta (fino a `SFTP_MAX_MB`). Ogni trasferimento è registrato negli eventi.
- **Registrazioni**: tutto quello che compare nel terminale (comandi compresi) viene salvato in `/data/ssh-sessions/<ID>/` e si scarica dalla pagina del dispositivo; restano `RETENTION_DAYS` giorni. Negli eventi: apertura e chiusura con utente del pannello, durata e motivo.
- **Inattività**: un terminale senza tasti premuti per `SSH_IDLE_MINUTES` minuti (default 30) viene chiuso.
- **Chiave del broker**: nella pagina *Broker* c'è l'impronta e il pulsante *Rigenera chiave SSH*; ogni in-out sostituisce la vecchia alla prossima apertura.
- **Stato della macchina**: nella pagina del dispositivo il riquadro *Macchina* mostra versione dell'in-out, sistema, uptime, CPU, memoria, disco e se il server SSH è pronto, letti dall'in-out attraverso il tunnel.
- Si disattiva sul broker con `SSH_ENABLED=0`. Se si perde `/data/ssh` il broker crea una nuova chiave e gli in-out la installano alla prossima apertura, sostituendo la vecchia.

## Canali, revoca e pesate in coda

Con MQTT 3.1.1 Mosquitto conferma (PUBACK) anche i messaggi negati dall'ACL: bloccare un dispositivo solo con l'ACL farebbe perdere le pesate. Per questo:

- **Chiudi canale**: il certificato viene messo in sospensione nella CRL (`certificateHold`) e il dispositivo viene scollegato. Non può ricollegarsi, quindi l'in-out tiene le pesate nella sua coda su disco. **Apri canale** toglie la sospensione e l'in-out invia tutto quello che ha accumulato.
- **Revoca**: il certificato finisce definitivamente nella CRL e il dispositivo viene scollegato. Per riattivarlo serve un nuovo certificato.
- Tutte le modifiche vengono applicate a caldo (SIGHUP): gli altri dispositivi restano collegati.

Il pannello si iscrive a `#` con sessione persistente e QoS 1: se il pannello è fermo, Mosquitto tiene in coda i messaggi e li consegna alla ripartenza. I duplicati vengono scartati tramite `message_id`.

## Configurazione (`.env`)

| Variabile | Default | Descrizione |
|---|---|---|
| `BROKER_HOST` | — | Nome DNS del broker: va nel certificato del server e nel campo Host degli in-out |
| `MUX_PUBLIC_PORT` | `443` | Porta unica pubblicata (in-out e pannello, smistati per ALPN) |
| `MUX_ENABLED` | `0` (`1` in `docker-compose.yml`) | Porta unica attiva |
| `MUX_WEB_UPSTREAM` | `caddy:443` | Dove inoltrare il traffico dei browser |
| `MUX_WEB_PROXY_PROTOCOL` | `1` | Invia l'IP del client a `MUX_WEB_UPSTREAM` con il PROXY protocol v2 |
| `MUX_MAX_CONNECTIONS` | `2000` | Connessioni contemporanee massime sulla porta unica |
| `BROKER_PUBLIC_PORT` | `8883` | Porta MQTT/TLS separata pubblicata |
| `ADMIN_USERNAME` / `ADMIN_PASSWORD` | `admin` / — | Accesso al pannello |
| `SESSION_HTTPS_ONLY` | `1` | Cookie di sessione solo su HTTPS |
| `RETENTION_DAYS` | `90` | Giorni di conservazione di messaggi ed eventi (`0` = per sempre) |
| `MAX_MESSAGE_MB` | `64` | Dimensione massima di un messaggio |
| `CLIENT_CERT_DAYS` | `365` | Validità dei certificati client (gli in-out li rinnovano da soli) |
| `ENROLL_ENABLED` | `1` | Registrazione automatica degli in-out sulla porta 8884 |
| `ENROLL_PUBLIC_PORT` | `8884` | Porta di registrazione pubblicata |
| `REMOTE_ENABLED` | `1` | Pulsante di accesso remoto agli in-out nel pannello |
| `REMOTE_TIMEOUT` | `60` | Secondi di attesa della risposta dell'in-out |
| `SSH_ENABLED` | `1` | Terminale SSH verso gli in-out nel pannello (richiede `REMOTE_ENABLED=1`) |
| `SSH_IDLE_MINUTES` | `30` | Chiusura del terminale dopo questi minuti senza attività (0 = mai) |
| `SSH_RECORD` | `1` | Registrazione delle sessioni del terminale in `/data/ssh-sessions` |
| `SSH_RECORD_MAX_MB` | `20` | Dimensione massima di una registrazione |
| `SFTP_MAX_MB` | `200` | Dimensione massima di un file scaricato o caricato dalla pagina del terminale |
| `TZ` | `Europe/Rome` | Fuso orario per le date nel pannello |

Se cambi `BROKER_HOST`, il certificato del server viene rigenerato al riavvio. I certificati client restano validi, perché la CA non cambia.

## Dati e backup

Tutto è nel volume `broker_data` (`/data` nel container):

```
/data/pki/            CA (ca.key!), server, certificati emessi, crl.pem
/data/ssh/            chiave SSH del broker per il terminale degli in-out
/data/ssh-sessions/   registrazioni delle sessioni del terminale
/data/mosquitto/      mosquitto.conf, acl, persistenza, log (generati)
/data/broker.sqlite3  dispositivi, certificati, messaggi, eventi
/data/attachments/    foto, PDF e CSV delle pesate
```

**Fai il backup di `/data/pki`**: senza la chiave della CA bisogna rilasciare nuovi certificati a tutti gli in-out.

```bash
docker run --rm -v broker-events-baronpesi_broker_data:/data -v "$PWD":/backup debian tar czf /backup/broker-data.tgz -C / data
```

## Sviluppo

```
app/
  main.py      pannello FastAPI (pagine, API, websocket live)
  pki.py       CA, certificati, CRL
  broker.py    mosquitto.conf, ACL, ricarica, scollegamento, lettura del log (stato online)
  ingest.py    subscriber MQTT interno che archivia i messaggi, pulizia dello storico
  enroll.py    servizio HTTPS di registrazione automatica degli in-out (porta 8884)
  mux.py       porta unica 443: smista per ALPN tra Mosquitto, registrazione e Caddy
  gateway.py   porta unica 8080: smista tra pannello e accesso remoto
  remote.py    accesso remoto agli in-out su /<ID>/: proxy HTTP/websocket attraverso MQTT
  tunnel.py    protocollo del tunnel su MQTT (<ID>/tunnel/req e <ID>/tunnel/res)
  ssh.py       terminale SSH: chiave del broker e connessione SSH trasportata dal tunnel
  ssh_sessions.py  registrazioni delle sessioni del terminale
  static/xterm/  xterm.js 5.5.0 e addon-fit 0.10.0 (licenza MIT) per la pagina del terminale
  db.py        schema SQLite
docker/        entrypoint e supervisord (Mosquitto, pannello, registrazione e porta unica nello stesso container)
```
