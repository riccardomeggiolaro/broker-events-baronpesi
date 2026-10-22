"""Servizio sul server: riceve i dati dagli in-out e li salva a DB.

Sessione persistente + QoS 1: se il servizio e' giu', il broker accoda i
messaggi e li consegna alla riconnessione. I duplicati (QoS 1) vanno gestiti
con un insert idempotente.
"""
import json
import logging

import paho.mqtt.client as mqtt

BROKER_HOST = "mqtt.tuodominio.it"
BROKER_PORT = 8883
CLIENT_ID = "backend-1"  # FISSO: indispensabile per la sessione persistente

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("consumer")


def save_weighing(in_out_id: str, data: dict) -> None:
    # TODO: insert idempotente, es. PostgreSQL:
    # INSERT ... ON CONFLICT (in_out_id, msg_id) DO NOTHING
    log.info("pesata da %s: %s", in_out_id, data)


def on_connect(client, userdata, flags, reason_code, properties):
    log.info("connesso: %s", reason_code)
    # Va rifatta ad ogni connect; con sessione persistente il broker la ricorda comunque
    client.subscribe("baron/+/data", qos=1)
    client.subscribe("baron/+/status", qos=1)


def on_message(client, userdata, msg):
    parts = msg.topic.split("/")
    in_out_id, kind = parts[1], parts[2]
    try:
        if kind == "status":
            log.info("stato %s: %s", in_out_id, msg.payload.decode())
        else:
            save_weighing(in_out_id, json.loads(msg.payload))
    except Exception:
        # Se solleviamo qui il messaggio e' comunque ACKato da paho:
        # logga/accoda gli errori invece di perderli in silenzio
        log.exception("errore elaborando %s", msg.topic)


client = mqtt.Client(
    mqtt.CallbackAPIVersion.VERSION2,
    client_id=CLIENT_ID,
    clean_session=False,
)
client.tls_set(ca_certs="ca.crt", certfile="backend.crt", keyfile="backend.key")
client.on_connect = on_connect
client.on_message = on_message
client.reconnect_delay_set(min_delay=1, max_delay=60)
client.connect(BROKER_HOST, BROKER_PORT, keepalive=60)
client.loop_forever()
