#!/usr/bin/env bash
# Build, upload and restart Torrent Search on the Synology NAS in one go.
#
#   ./deploy/synology/redeploy.sh
#
# Needs SSH key login to the NAS (an admin account) and NAS_SSH=user@nas-ip,
# either exported or set in deploy/synology/.env. The NAS asks for your
# password once, for sudo, to load the image and restart the container.
#   SKIP_TESTS=1   skip the test suite
#   PLATFORM=linux/arm64   for an ARM NAS
set -euo pipefail
cd "$(dirname "$0")/../.."

ENV_FILE=deploy/synology/.env
env_value() { grep -E "^$1=" "$ENV_FILE" 2>/dev/null | tail -1 | cut -d= -f2-; }

NAS_SSH="${NAS_SSH:-$(env_value NAS_SSH)}"
NAS_DIR="${NAS_DIR:-/volume1/docker/torrent-search}"
WEB_PORT="$(env_value WEB_PORT)"
PLATFORM="${PLATFORM:-linux/amd64}"
TAR="dist/torrent-search-mcp-${PLATFORM#linux/}.tar"

[ -f "$ENV_FILE" ] || { echo "Missing $ENV_FILE (copy .env.example and fill it in)."; exit 1; }
[ -n "$NAS_SSH" ] || { echo "Set NAS_SSH=user@nas-ip (exported, or in $ENV_FILE)."; exit 1; }

if [ -z "${SKIP_TESTS:-}" ]; then
  echo "==> Running tests"
  uv run pytest -q
fi

PLATFORM="$PLATFORM" ./deploy/synology/build-image.sh

echo "==> Uploading to $NAS_SSH:$NAS_DIR"
scp -O -q "$TAR" deploy/synology/compose.yaml "$ENV_FILE" "$NAS_SSH:$NAS_DIR/"

echo "==> Loading the image and restarting (the NAS asks for your password for sudo)"
ssh -t "$NAS_SSH" "cd '$NAS_DIR' && chmod 600 .env \
  && sudo docker load -i '$(basename "$TAR")' \
  && sudo docker compose -p torrent-search up -d --force-recreate \
  && sudo docker image prune -f >/dev/null"

URL="http://${NAS_SSH#*@}:${WEB_PORT:-8000}"
echo "==> Waiting for $URL"
for _ in $(seq 1 45); do
  if curl -sf -o /dev/null "$URL/"; then
    echo "==> Up. NAS status: $(curl -s "$URL/nas/status")"
    exit 0
  fi
  sleep 2
done
echo "The app didn't answer at $URL within 90 s. Check Container Manager → Project → torrent-search → Log."
exit 1
