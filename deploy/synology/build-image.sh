#!/usr/bin/env bash
# Build the Docker image on this computer for the NAS's CPU and save it as a
# tarball to import in Container Manager (Image → Add → Add from file).
#   ./deploy/synology/build-image.sh            # Intel/AMD NAS (most "+" models)
#   PLATFORM=linux/arm64 ./deploy/synology/build-image.sh   # ARM NAS
set -euo pipefail
cd "$(dirname "$0")/../.."

PLATFORM="${PLATFORM:-linux/amd64}"
OUT="dist/torrent-search-mcp-${PLATFORM#linux/}.tar"
mkdir -p dist

echo "==> Building torrent-search-mcp:latest for $PLATFORM"
docker buildx build \
  --platform "$PLATFORM" \
  --tag torrent-search-mcp:latest \
  --output "type=docker,dest=$OUT" \
  .

echo "==> Saved $OUT ($(du -h "$OUT" | cut -f1))"
echo "    Upload it to the NAS and import it in Container Manager → Image → Add → Add from file."
