#!/usr/bin/env bash
# Deploys the committed code (git HEAD) to the Linode and restarts the app.
#
#   scripts/deploy.sh root@LINODE_IP            # first run also installs Docker
#
# Ships `git archive HEAD` (so uncommitted changes and .env are never included), copies .env
# separately over SSH, sets BASE_URL to the server's address, then rebuilds and restarts.
set -euo pipefail
HOST="${1:?usage: scripts/deploy.sh root@LINODE_IP}"
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
IP="${HOST#*@}"
ssh "$HOST" "cd $DIR && chmod 600 .env && sed -i '/^BASE_URL=/d;/^MONITOR_MINUTES=/d;/^MONITOR_DAY=/d' .env && \
  printf '\nBASE_URL=http://%s\nMONITOR_MINUTES=%s\nMONITOR_DAY=%s\n' '$IP' '${MONITOR_MINUTES:-15}' '${MONITOR_DAY:-tomorrow}' >> .env"

echo "== Building and starting"
ssh "$HOST" "cd $DIR && docker compose up -d --build && docker image prune -f >/dev/null"

echo "== Waiting for health check"
for i in $(seq 1 20); do
  if curl -fsS "http://$IP/healthz" 2>/dev/null; then echo; echo "Live at http://$IP/"; exit 0; fi
  sleep 3
done
echo "Not healthy yet. Logs:"; ssh "$HOST" "cd $DIR && docker compose logs --tail 40"; exit 1
