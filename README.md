# broker-events-baronpesi

Broker MQTT (Mosquitto) per ricevere le pesate dagli **in-out**, con un pannello web per:

- **certificati**: CA interna, certificato del server, un certificato client per ogni in-out (zip pronto da caricare), rinnovo e revoca;
- **canali**: ogni dispositivo può pubblicare solo sul proprio topic; il canale si può chiudere e riaprire senza toccare l'in-out;
- **monitor live**: messaggi e connessioni in tempo reale;
- **storico**: ogni pesata viene archiviata con foto, PDF e CSV, consultabile e filtrabile; pulizia automatica dopo `RETENTION_DAYS` giorni;
- **stato**: dispositivi online/offline, eventi di connessione, tentativi rifiutati, log di Mosquitto.

```
 in-out ──MQTT/TLS 8883 (certificato client)──▶ ┌──────────── container "broker" ────────────┐
                                                │ Mosquitto ◀── ACL/CRL/SIGHUP ── pannello   │
 browser ──HTTPS 443──▶ Caddy ──▶ 8080 ───────▶ │            ──▶ subscriber "#" ──▶ SQLite   │
                                                │                                + allegati  │
                                                └────────────── volume /data ────────────────┘
```

## Avvio

Requisiti: una VPS con Docker, un nome DNS (es. `mqtt.tuodominio.it`) che punta alla VPS, porte **80**, **443** e **8883** aperte.

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

## Avvio in rete locale (senza dominio)

Per un PC/server Linux nella stessa LAN degli in-out: niente Caddy, pannello in HTTP sulla porta 8080, `BROKER_HOST` = IP del PC (deve essere fisso).

```bash
cp .env.example .env
```

Nel `.env`: `BROKER_HOST=192.168.1.50` (l'IP del PC), `ADMIN_PASSWORD=...`, `SESSION_HTTPS_ONLY=0`.

```bash
docker compose -f docker-compose.lan.yml up -d --build
```

Pannello: `http://192.168.1.50:8080`. Negli in-out: host `192.168.1.50`, porta `8883`.

## Collegare un in-out

1. Sull'in-out apri la configurazione del broker e copia il **Client ID / topic** (es. `in-out-impianto-1a2b3c4d`).
2. Nel pannello: **Dispositivi → Nuovo dispositivo**, incolla l'ID, dai un nome e crea. Scarica lo **zip dei certificati**.
3. Sull'in-out imposta:
   - Host: `BROKER_HOST`, porta `8883`
   - username e password vuoti
   - Certificato CA: `ca.crt`, certificato client: `client.crt`, chiave client: `client.key` (dallo zip)
4. Il dispositivo appare **online** e le pesate arrivano nel monitor live e nello storico.

L'identità del dispositivo è il CN del certificato: l'ACL gli permette di scrivere solo sul topic `<ID>` (e `<ID>/#`). Nessun dispositivo può leggere i messaggi degli altri.

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
| `BROKER_PUBLIC_PORT` | `8883` | Porta MQTT/TLS pubblicata |
| `ADMIN_USERNAME` / `ADMIN_PASSWORD` | `admin` / — | Accesso al pannello |
| `SESSION_HTTPS_ONLY` | `1` | Cookie di sessione solo su HTTPS |
| `RETENTION_DAYS` | `90` | Giorni di conservazione di messaggi ed eventi (`0` = per sempre) |
| `MAX_MESSAGE_MB` | `64` | Dimensione massima di un messaggio |
| `CLIENT_CERT_DAYS` | `3650` | Validità predefinita dei certificati client |
| `TZ` | `Europe/Rome` | Fuso orario per le date nel pannello |

Se cambi `BROKER_HOST`, il certificato del server viene rigenerato al riavvio. I certificati client restano validi, perché la CA non cambia.

## Dati e backup

Tutto è nel volume `broker_data` (`/data` nel container):

```
/data/pki/            CA (ca.key!), server, certificati emessi, crl.pem
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
  pki.py       CA, certificati, CRL, zip per gli in-out
  broker.py    mosquitto.conf, ACL, ricarica, scollegamento, lettura del log (stato online)
  ingest.py    subscriber MQTT interno che archivia i messaggi, pulizia dello storico
  db.py        schema SQLite
docker/        entrypoint e supervisord (Mosquitto + pannello nello stesso container)
```
