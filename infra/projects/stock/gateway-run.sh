#!/bin/sh
set -eu

RUNTIME=${STOCK_RUNTIME_DIR:-/opt/hermes-stock/runtime}
NAME=${STOCK_GATEWAY_CONTAINER:-hermes-stock-gateway}
IMAGE=${STOCK_GATEWAY_IMAGE:-hermes-stock-agent:stock-v1}
ENV_FILE="$RUNTIME/hermes/email.env"
CONFIG_FILE="$RUNTIME/hermes-home/config.yaml"
CONFIG_TEMPLATE="$RUNTIME/code/hermes/config.yaml.example"

[ -f "$ENV_FILE" ] || { echo "missing $ENV_FILE" >&2; exit 1; }
mkdir -p "$RUNTIME/hermes-home" "$RUNTIME/incident-archive"
if [ ! -f "$CONFIG_FILE" ]; then
  [ -f "$CONFIG_TEMPLATE" ] || { echo "missing $CONFIG_TEMPLATE" >&2; exit 1; }
  umask 022
  cp "$CONFIG_TEMPLATE" "$CONFIG_FILE"
  chmod 0644 "$CONFIG_FILE"
fi

case "${1:-restart}" in
  start|restart)
    if [ "$1" = start ] && [ "$(docker inspect -f '{{.State.Running}}' "$NAME" 2>/dev/null || true)" = true ]; then
      exit 0
    fi
    docker rm -f "$NAME" >/dev/null 2>&1 || true
    docker run -d --name "$NAME" \
      --init \
      --restart unless-stopped \
      --user 10001:10001 \
      --read-only \
      --security-opt no-new-privileges:true \
      --cap-drop ALL \
      --tmpfs /tmp:rw,noexec,nosuid,size=256m \
      --tmpfs /run:rw,noexec,nosuid,size=4m \
      --env-file "$ENV_FILE" \
      --workdir /opt/hermes-stock/app \
      -e HOME=/opt/hermes-data \
      -e HERMES_HOME=/opt/hermes-data \
      -e STOCK_HERMES_HOME=/opt/hermes-data \
      -e STOCK_ROOT=/opt/hermes-stock/app \
      -e STOCK_NAS_ROOT=/nas/stock \
      -e STOCK_QUEUE_DIR=/nas/stock/.hermes/pending-upload \
      -e TZ=Asia/Shanghai \
      -v "$RUNTIME/hermes-home:/opt/hermes-data:rw" \
      -v "$RUNTIME/incident-archive:/opt/hermes-data/incident-archive:rw" \
      -v "$RUNTIME/code:/opt/hermes-stock/app:rw" \
      -v "$RUNTIME/key:/opt/hermes-stock/key:ro" \
      -v "$RUNTIME/config:/opt/hermes-stock/app/config:ro" \
      -v "$RUNTIME/logs:/opt/hermes-stock/app/logs:ro" \
      -v /mnt/nasdisk:/mnt/nasdisk:ro \
      -v /mnt/nasdisk/stock:/nas/stock:ro \
      --health-cmd 'pgrep -af "hermes_cli.main gateway run" >/dev/null' \
      --health-interval 30s \
      --health-timeout 10s \
      --health-start-period 30s \
      --health-retries 3 \
      "$IMAGE" gateway run
    ;;
  stop)
    docker stop "$NAME" >/dev/null 2>&1 || true
    ;;
  status)
    docker ps -a --filter "name=^/${NAME}$" --format '{{.Names}}|{{.Image}}|{{.Status}}'
    docker inspect -f 'health={{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}} restart={{.RestartCount}}' "$NAME" 2>/dev/null || true
    ;;
  *)
    echo "usage: $0 {start|restart|stop|status}" >&2
    exit 2
    ;;
esac
