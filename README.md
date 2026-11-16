# broker-events-baronpesi

Broker MQTT (Mosquitto) per ricevere le pesate dagli **in-out**, con un pannello web per:

- **registrazione automatica**: l'in-out si configura solo con l'host del broker, genera la sua chiave e riceve da solo il certificato client, che rinnova prima della scadenza;
- **certificati**: CA interna, certificato del server, un certificato client per ogni in-out (la chiave privata resta sull'in-out), rinnovo, sostituzione su richiesta e revoca;
- **canali**: ogni dispositivo può pubblicare solo sul proprio topic; il canale si può chiudere e riaprire senza toccare l'in-out;
- **monitor live**: messaggi e connessioni in tempo reale; tutte le pagine del pannello si aggiornano da sole (nuovi dispositivi, stato online, certificati, eventi, messaggi) senza ricaricare;
- **storico**: ogni pesata viene archiviata con foto, PDF e CSV, consultabile e filtrabile; pulizia automatica dopo `RETENTION_DAYS` giorni;
- **stato**: dispositivi online/offline, eventi di connessione, tentativi rifiutati, log di Mosquitto;
- **accesso remoto**: dal pannello si apre l'interfaccia dell'in-out (pagine, API REST e websocket) attraverso la connessione MQTT, senza aprire porte verso l'impianto.

```
 in-out ──HTTPS 8884 (registrazione, CSR)────▶ ┌──────────── container "broker" ────────────┐
 in-out ──MQTT/TLS 8883 (certificato client)──▶ │ Mosquitto ◀── ACL/CRL/SIGHUP ── pannello   │
 browser ──HTTPS 443──▶ Caddy ──▶ 8080 ───────▶ │            ──▶ subscriber "#" ──▶ SQLite   │
 browser ──HTTPS 8443─▶ Caddy ──▶ 8081 ───────▶ │ proxy remoto ◀─▶ <ID>/tunnel/req|res ◀─▶ in-out│
                                                │                                + allegati  │
                                                └────────────── volume /data ────────────────┘
```

## Avvio

Requisiti: una VPS con Docker, un nome DNS (es. `mqtt.tuodominio.it`) che punta alla VPS, porte **80**, **443**, **8883**, **8884** e **8443** (accesso remoto agli in-out) aperte.

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

Pannello: `http://192.168.1.50:8080`, accesso remoto agli in-out su `http://192.168.1.50:8443`. Negli in-out: host `192.168.1.50` (porte `8883` e `8884`).

## Collegare un in-out (registrazione automatica)

Sull'in-out, nella pagina **Broker MQTT**, imposta solo:

- Host: `BROKER_HOST` (lo stesso nome o IP presente nel certificato del server)
- Porta MQTT: `8883`, porta registrazione: `8884`

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
browser ──▶ https://BROKER_HOST:8443/... ──▶ proxy remoto ──MQTT <ID>/tunnel/req──▶ in-out ──▶ http://127.0.0.1:<porta API>
        ◀──────────────────────────────────────────────────MQTT <ID>/tunnel/res◀──
```

- **Porta dedicata** (`REMOTE_PUBLIC_PORT`, default `8443`): l'interfaccia dell'in-out usa percorsi assoluti (`/api`, `/static`, websocket su `location.host`), quindi deve rispondere dalla radice `/`. Con Caddy è in HTTPS con lo stesso certificato del pannello; in LAN è in HTTP.
- **Accesso**: serve il login del pannello (lo stesso cookie di sessione, verificato anche sulla 8443). Poi l'in-out chiede il suo login come sempre.
- **Un in-out alla volta per browser**: l'in-out scelto è salvato in un cookie; aprendone un altro, le schede già aperte passano al nuovo.
- I cookie del pannello non vengono mai inoltrati all'in-out. Ogni apertura è registrata negli eventi del dispositivo.
- Limiti: richieste e risposte fino a 32 MB (e comunque entro `MAX_MESSAGE_MB`), attesa massima `REMOTE_TIMEOUT` secondi. Le risposte in streaming (download progressivi, SSE) arrivano solo quando sono complete.
- Serve l'in-out aggiornato alla versione con l'accesso remoto (modulo `remote_tunnel`). Sull'in-out si può disattivare con `app_api.message_broker.remote_access = false` in `config.json`.

Il canale deve essere aperto: l'ACL permette all'in-out di leggere `<ID>/tunnel/req` e di rispondere su `<ID>/tunnel/res`; il pannello è l'unico che può scrivere sulle richieste. Questi messaggi non finiscono nello storico.

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
| `CLIENT_CERT_DAYS` | `365` | Validità dei certificati client (gli in-out li rinnovano da soli) |
| `ENROLL_ENABLED` | `1` | Registrazione automatica degli in-out sulla porta 8884 |
| `ENROLL_PUBLIC_PORT` | `8884` | Porta di registrazione pubblicata |
| `REMOTE_ENABLED` | `1` | Pulsante di accesso remoto agli in-out nel pannello |
| `REMOTE_PUBLIC_PORT` | `8443` | Porta dell'accesso remoto (HTTPS con Caddy, HTTP in LAN) |
| `REMOTE_TIMEOUT` | `60` | Secondi di attesa della risposta dell'in-out |
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
  pki.py       CA, certificati, CRL
  broker.py    mosquitto.conf, ACL, ricarica, scollegamento, lettura del log (stato online)
  ingest.py    subscriber MQTT interno che archivia i messaggi, pulizia dello storico
  enroll.py    servizio HTTPS di registrazione automatica degli in-out (porta 8884)
  remote.py    accesso remoto all'in-out: proxy HTTP/websocket sulla porta 8081 (8443 tramite Caddy)
  tunnel.py    protocollo del tunnel su MQTT (<ID>/tunnel/req e <ID>/tunnel/res)
  db.py        schema SQLite
docker/        entrypoint e supervisord (Mosquitto, pannello e registrazione nello stesso container)
```
