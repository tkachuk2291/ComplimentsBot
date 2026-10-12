#!/usr/bin/env bash
# Собирает образ под сервер (по умолчанию linux/amd64) и упаковывает его в архив
# для импорта в Portainer (Images → Import) или `docker load` на сервере.
#   ./build-image.sh                 # для обычного x86-сервера
#   PLATFORM=linux/arm64 ./build-image.sh   # для ARM-сервера (Raspberry Pi, Oracle Ampere и т.п.)
set -euo pipefail
cd "$(dirname "$0")"

PLATFORM="${PLATFORM:-linux/amd64}"
IMAGE="${IMAGE:-compliment-bot:latest}"
OUT="dist/compliment-bot-${PLATFORM//\//-}.tar.gz"

mkdir -p dist
echo "→ Собираю $IMAGE для $PLATFORM"
docker buildx build --platform "$PLATFORM" -t "$IMAGE" --load .
echo "→ Упаковываю в $OUT"
docker save "$IMAGE" | gzip > "$OUT"
echo "✓ Готово: $OUT ($(du -h "$OUT" | cut -f1))"
