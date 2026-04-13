#!/bin/sh
set -eu

TEMPLATE_PATH="/workspace/gateway/default.http.conf.template"
TLS_CERT_PATH=""
TLS_KEY_PATH=""

if [ -n "${DOMAIN:-}" ]; then
  CANDIDATE_CERT="/etc/nginx/external-certs/live/${DOMAIN}/fullchain.pem"
  CANDIDATE_KEY="/etc/nginx/external-certs/live/${DOMAIN}/privkey.pem"
  if [ -f "${CANDIDATE_CERT}" ] && [ -f "${CANDIDATE_KEY}" ]; then
    TEMPLATE_PATH="/workspace/gateway/default.tls.conf.template"
    TLS_CERT_PATH="${CANDIDATE_CERT}"
    TLS_KEY_PATH="${CANDIDATE_KEY}"
  fi
fi

export NGINX_CLIENT_MAX_BODY_SIZE
export NGINX_HTTPS_PORT
export DOMAIN
export TLS_CERT_PATH
export TLS_KEY_PATH

envsubst '${NGINX_CLIENT_MAX_BODY_SIZE} ${NGINX_HTTPS_PORT} ${DOMAIN} ${TLS_CERT_PATH} ${TLS_KEY_PATH}' \
  < "${TEMPLATE_PATH}" \
  > /etc/nginx/conf.d/default.conf

exec nginx -g 'daemon off;'
