#!/usr/bin/env bash
# Uso:
#   ./gen-certs.sh init mqtt.tuodominio.it     # crea CA + certificato server + certificato backend
#   ./gen-certs.sh client inout-001            # crea certificato per un in-out
set -euo pipefail

OUT="$(dirname "$0")/pki"
mkdir -p "$OUT"
cd "$OUT"

case "${1:-}" in
  init)
    DOMAIN="${2:?dominio del broker mancante}"

    # CA (ca.key va tenuta al sicuro, NON sul server)
    openssl genrsa -out ca.key 4096
    openssl req -x509 -new -nodes -key ca.key -sha256 -days 3650 \
      -subj "/CN=Baron-MQTT-CA" -out ca.crt

    # Server
    openssl genrsa -out server.key 2048
    openssl req -new -key server.key -subj "/CN=${DOMAIN}" -out server.csr
    printf "subjectAltName=DNS:%s" "$DOMAIN" > san.ext
    openssl x509 -req -in server.csr -CA ca.crt -CAkey ca.key -CAcreateserial \
      -days 825 -sha256 -extfile san.ext -out server.crt

    # Backend (CN=backend, richiesto dall'ACL)
    openssl genrsa -out backend.key 2048
    openssl req -new -key backend.key -subj "/CN=backend" -out backend.csr
    openssl x509 -req -in backend.csr -CA ca.crt -CAkey ca.key -CAcreateserial \
      -days 825 -sha256 -out backend.crt

    rm -f *.csr san.ext
    echo "OK. Copia ca.crt, server.crt, server.key in mosquitto/certs/ sul server."
    ;;
  client)
    ID="${2:?id in-out mancante (es. inout-001)}"
    [ -f ca.key ] || { echo "Esegui prima: $0 init <dominio>"; exit 1; }
    openssl genrsa -out "${ID}.key" 2048
    openssl req -new -key "${ID}.key" -subj "/CN=${ID}" -out "${ID}.csr"
    openssl x509 -req -in "${ID}.csr" -CA ca.crt -CAkey ca.key -CAcreateserial \
      -days 825 -sha256 -out "${ID}.crt"
    rm -f "${ID}.csr"
    echo "OK. Porta sull'in-out: ca.crt, ${ID}.crt, ${ID}.key"
    ;;
  *)
    echo "Uso: $0 init <dominio> | client <id>"; exit 1;;
esac
