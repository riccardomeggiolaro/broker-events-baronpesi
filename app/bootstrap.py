"""Preparazione all'avvio del container: DB, CA, certificati, configurazione di Mosquitto."""
from . import broker, db


def main() -> None:
    db.init()
    broker.bootstrap()
    print("[bootstrap] configurazione pronta", flush=True)


if __name__ == "__main__":
    main()
