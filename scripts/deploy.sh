#!/usr/bin/env bash
# Deploys the committed code (git HEAD) to the Linode behind HTTPS and restarts the app.
#
#   scripts/deploy.sh root@LINODE_IP                      # https://<ip-with-dashes>.sslip.io
#   scripts/deploy.sh root@LINODE_IP closure.example.org  # your own domain (A record -> LINODE_IP)
#
# Ships `git archive HEAD` (so uncommitted changes and .env are never included), copies .env
# separately over SSH, sets DOMAIN and BASE_URL, then rebuilds and restarts. Caddy obtains the
# Let's Encrypt certificate on first start. The firewall must allow TCP 22, 80 and 443.
set -euo pipefail
HOST="${1:?usage: scripts/deploy.sh root@LINODE_IP [domain]}"
IP="${HOST#*@}"
DOMAIN="${2:-${IP//./-}.sslip.io}"
DIR=/opt/closure-call
cd "$(dirname "$0")/.."

if [ -n "$(git status --porcelain)" ]; then
  echo "Note: uncommitted changes are NOT deployed (deploying $(git rev-parse --short HEAD))."
fi

echo "== Installing Docker if needed"
ssh "$HOST" 'command -v docker >/dev/null && docker compose version >/dev/null 2>&1 || {
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -q && apt-get install -yq docker.io docker-compose-v2 && systemctl enable --now docker
}'

echo "== Copying code ($(git rev-parse --short HEAD)) and .env"
ssh "$HOST" "mkdir -p $DIR && find $DIR -mindepth 1 -maxdepth 1 ! -name .env -exec rm -rf {} +"
git archive --format=tar HEAD | ssh "$HOST" "tar -x -C $DIR"
scp -q .env "$HOST:$DIR/.env"
ssh "$HOST" "cd $DIR && chmod 600 .env && sed -i '/^DOMAIN=/d;/^BASE_URL=/d;/^MONITOR_MINUTES=/d;/^MONITOR_DAY=/d' .env && \
  printf '\nDOMAIN=%s\nBASE_URL=https://%s\nMONITOR_MINUTES=%s\nMONITOR_DAY=%s\n' '$DOMAIN' '$DOMAIN' \
    '${MONITOR_MINUTES:-15}' '${MONITOR_DAY:-tomorrow}' >> .env"

echo "== Building and starting (app + Caddy for HTTPS)"
ssh "$HOST" "cd $DIR && docker compose up -d --build && docker image prune -f >/dev/null"

echo "== Waiting for https://$DOMAIN (the first certificate can take up to a minute)"
for i in $(seq 1 30); do
  if curl -fsS "https://$DOMAIN/healthz" 2>/dev/null; then echo; echo "Live at https://$DOMAIN/"; exit 0; fi
  sleep 4
done
echo "Not reachable over HTTPS yet. Recent logs:"
ssh "$HOST" "cd $DIR && docker compose logs --tail 30"
exit 1
