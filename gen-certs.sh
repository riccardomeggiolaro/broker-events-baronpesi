#!/usr/bin/env bash
# Gestione CA, certificati e revoche (CRL) per il broker MQTT.
#
#   ./gen-certs.sh init mqtt.tuodominio.it   # CA + server + backend + CRL vuota
#   ./gen-certs.sh client inout-001          # nuovo certificato per un in-out
#   ./gen-certs.sh revoke inout-001          # revoca e rigenera la CRL
#   ./gen-certs.sh crl                       # rigenera solo la CRL
#   ./gen-certs.sh list                      # certificati emessi (V=valido, R=revocato)
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
PKI="$ROOT/pki"
BROKER_CERTS="$ROOT/mosquitto/certs"
CNF="openssl.cnf"

mkdir -p "$PKI"
cd "$PKI"

write_cnf() {
  cat > "$CNF" <<'EOF'
[ ca ]
default_ca = CA_default

[ CA_default ]
dir              = .
database         = $dir/index.txt
new_certs_dir    = $dir/newcerts
serial           = $dir/serial
crlnumber        = $dir/crlnumber
certificate      = $dir/ca.crt
private_key      = $dir/ca.key
default_md       = sha256
default_days     = 825
default_crl_days = 1825
unique_subject   = no
policy           = policy_cn

[ policy_cn ]
commonName = supplied
EOF
}

# issue <nome-file> <CN> [file-estensioni]
issue() {
  local NAME="$1" CN="$2" EXT="${3:-}"
  openssl genrsa -out "$NAME.key" 2048 2>/dev/null
  openssl req -new -key "$NAME.key" -subj "/CN=$CN" -out "$NAME.csr"
  local ARGS=(-batch -config "$CNF" -in "$NAME.csr" -out "$NAME.crt" -notext -days 825)
  if [ -n "$EXT" ]; then ARGS+=(-extfile "$EXT"); fi
  openssl ca "${ARGS[@]}"
  rm -f "$NAME.csr"
  chmod 600 "$NAME.key"
}

gen_crl() {
  openssl ca -config "$CNF" -gencrl -out crl.pem
  if [ -d "$BROKER_CERTS" ]; then
    cp crl.pem "$BROKER_CERTS/crl.pem"
    echo "CRL copiata in mosquitto/certs/crl.pem"
  fi
}

case "${1:-}" in
  init)
    DOMAIN="${2:?dominio del broker mancante}"
    [ ! -f ca.key ] || { echo "CA gia' esistente in pki/: non la sovrascrivo."; exit 1; }

    write_cnf
    mkdir -p newcerts
    : > index.txt
    echo "unique_subject = no" > index.txt.attr
    echo 1000 > serial
    echo 1000 > crlnumber

    # CA (ca.key va tenuta al sicuro, NON sul server)
    openssl genrsa -out ca.key 4096 2>/dev/null
    openssl req -x509 -new -nodes -key ca.key -sha256 -days 3650 \
      -subj "/CN=Baron-MQTT-CA" \
      -addext "basicConstraints=critical,CA:TRUE" \
      -addext "keyUsage=critical,keyCertSign,cRLSign" \
      -out ca.crt
    chmod 600 ca.key

    # Server (il SAN deve coincidere con l'host a cui si collegano i client)
    printf "subjectAltName=DNS:%s" "$DOMAIN" > san.ext
    issue server "$DOMAIN" san.ext
    rm -f san.ext

    # Backend (CN=backend, richiesto dall'ACL)
    issue backend backend

    gen_crl
    echo
    echo "OK. Sul server copia in mosquitto/certs/: ca.crt, server.crt, server.key, crl.pem"
    ;;

  client)
    ID="${2:?id in-out mancante (es. inout-001)}"
    [ -f ca.key ] || { echo "Esegui prima: $0 init <dominio>"; exit 1; }
    [ ! -f "$ID.crt" ] || { echo "$ID.crt esiste gia'. Revocalo o usa un altro id."; exit 1; }
    issue "$ID" "$ID"
    echo "OK. Porta sull'in-out: ca.crt, $ID.crt, $ID.key"
    ;;

  revoke)
    ID="${2:?id da revocare mancante}"
    [ -f "$ID.crt" ] || { echo "$ID.crt non trovato in pki/"; exit 1; }
    openssl ca -config "$CNF" -revoke "$ID.crt"
    gen_crl
    echo
    echo "Revocato $ID. Copia crl.pem sul server e riavvia il broker:"
    echo "  docker compose restart mosquitto"
    ;;

  crl)
    gen_crl
    ;;

  list)
    awk -F'\t' '{ printf "%s  serie %s  %s\n", $1, $4, $6 }' index.txt
    ;;

  *)
    echo "Uso: $0 init <dominio> | client <id> | revoke <id> | crl | list"
    exit 1
    ;;
esac
