#!/bin/sh
set -eu

# Generate a self-signed certificate for local HTTPS in LAN. Input: domain or IP, optional cert root dir. Output: fullchain.pem and privkey.pem.

if [ "$#" -lt 1 ] || [ "$#" -gt 2 ]; then
  echo "Usage: $0 <domain-or-ip> [cert-root]" >&2
  echo "Example: $0 192.168.1.50 ./certs" >&2
  exit 1
fi

DOMAIN_OR_IP="$1"
CERT_ROOT="${2:-./certs}"
TARGET_DIR="${CERT_ROOT}/live/${DOMAIN_OR_IP}"

mkdir -p "${TARGET_DIR}"

if printf '%s' "${DOMAIN_OR_IP}" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$'; then
  SAN="IP:${DOMAIN_OR_IP}"
else
  SAN="DNS:${DOMAIN_OR_IP}"
fi

openssl req \
  -x509 \
  -nodes \
  -newkey rsa:2048 \
  -sha256 \
  -days 825 \
  -keyout "${TARGET_DIR}/privkey.pem" \
  -out "${TARGET_DIR}/fullchain.pem" \
  -subj "/CN=${DOMAIN_OR_IP}" \
  -addext "subjectAltName=${SAN}"

echo "Generated: ${TARGET_DIR}/fullchain.pem"
echo "Generated: ${TARGET_DIR}/privkey.pem"
