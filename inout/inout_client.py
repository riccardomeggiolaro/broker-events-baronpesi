"""Esempio lato in-out: pubblica pesate in QoS 1 + stato online/offline retained."""
import json
import time
import uuid

import paho.mqtt.client as mqtt

BROKER_HOST = "mqtt.tuodominio.it"
BROKER_PORT = 8883
IN_OUT_ID = "inout-001"  # deve coincidere con il CN del certificato

TOPIC_DATA = f"baron/{IN_OUT_ID}/data"
TOPIC_STATUS = f"baron/{IN_OUT_ID}/status"


def on_connect(client, userdata, flags, reason_code, properties):
    # Stato online retained (il Last Will imposta offline se cade la connessione)
    client.publish(TOPIC_STATUS, "online", qos=1, retain=True)


client = mqtt.Client(
    mqtt.CallbackAPIVersion.VERSION2,
    client_id=IN_OUT_ID,
    clean_session=False,
)
client.tls_set(ca_certs="ca.crt", certfile=f"{IN_OUT_ID}.crt", keyfile=f"{IN_OUT_ID}.key")
client.will_set(TOPIC_STATUS, "offline", qos=1, retain=True)
client.on_connect = on_connect
client.reconnect_delay_set(min_delay=1, max_delay=60)
client.max_queued_messages_set(0)  # 0 = nessun limite per i messaggi in attesa (in RAM)
client.connect(BROKER_HOST, BROKER_PORT, keepalive=30)
client.loop_start()  # gestisce rete, ritrasmissioni QoS 1 e riconnessioni


def send_weighing(weight_kg: float) -> None:
    payload = {
        "msg_id": str(uuid.uuid4()),  # chiave per deduplicare lato DB
        "ts": time.time(),
        "weight_kg": weight_kg,
    }
    # publish() accoda il messaggio e lo ritrasmette finche' non arriva l'ACK,
    # anche attraverso brevi cadute di rete (buffer in RAM: se il processo
    # muore i dati in coda si perdono, per averli al riavvio serve un buffer su disco).
    client.publish(TOPIC_DATA, json.dumps(payload), qos=1)


if __name__ == "__main__":
    while True:
        send_weighing(1234.5)
        time.sleep(5)
