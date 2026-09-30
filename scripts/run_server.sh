#!/usr/bin/env bash
# Start the copilot service detached, with simulated model latency so the
# concurrency controls are observable. Used for the benchmark; not part of the
# teaching material.
set -euo pipefail
cd "$(dirname "$0")/.."
PORT="${1:-8097}"
LATENCY="${2:-400}"
export PYTHONPATH=src
rm -f "/tmp/ailab-serve-$PORT.log"
setsid nohup python3 -m ailab_ops.cli serve --port "$PORT" --llm-latency-ms "$LATENCY" \
  > "/tmp/ailab-serve-$PORT.log" 2>&1 < /dev/null &
disown || true
for i in $(seq 1 40); do
  if curl -s --noproxy '*' "http://127.0.0.1:$PORT/v1/health" >/dev/null 2>&1; then
    echo "server up on $PORT"
    exit 0
  fi
  sleep 0.5
done
echo "server failed to start; log:" >&2
tail -20 "/tmp/ailab-serve-$PORT.log" >&2
exit 1
