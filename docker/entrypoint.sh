#!/bin/sh
set -e
# Il volume dati deve appartenere all'utente del servizio (anche se montato come cartella)
mkdir -p /data
chown -R broker:broker /data
# Prepara CA, certificati e configurazione come utente non privilegiato
setpriv --reuid=broker --regid=broker --init-groups python -m app.bootstrap
# supervisord resta root solo per gestire i log; Mosquitto e pannello girano come "broker"
exec supervisord -c /etc/supervisor/broker.conf
