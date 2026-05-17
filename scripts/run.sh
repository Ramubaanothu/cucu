#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

MODE="${TRADING_MODE:-paper}"
SCAN_NOW="${SCAN_NOW:-}"
SCAN_FLAG=""
[ -n "$SCAN_NOW" ] && SCAN_FLAG="--scan-now"

echo "=== Starting Polymarket Copy-Trading Agent [mode=$MODE] ==="

cleanup() {
  echo ""
  echo "[run.sh] Shutting down…"
  kill "$AGENT_PID" 2>/dev/null || true
  kill "$MONITOR_PID" 2>/dev/null || true
  wait "$AGENT_PID" 2>/dev/null || true
  wait "$MONITOR_PID" 2>/dev/null || true
  echo "[run.sh] Done"
}
trap cleanup SIGINT SIGTERM

# Start Python agent
echo "[run.sh] Starting Python agent…"
if command -v uv &>/dev/null; then
  uv run cucu run --mode "$MODE" $SCAN_FLAG &
else
  python -m agent.main run --mode "$MODE" $SCAN_FLAG &
fi
AGENT_PID=$!

# Wait for agent to be ready
echo "[run.sh] Waiting for agent HTTP server…"
for i in $(seq 1 30); do
  if curl -sf "http://localhost:${AGENT_HTTP_PORT:-8000}/status" > /dev/null 2>&1; then
    echo "[run.sh] Agent ready"
    break
  fi
  sleep 1
done

# Start TypeScript monitor
echo "[run.sh] Starting TypeScript monitor…"
cd monitor-ts
npm start &
MONITOR_PID=$!
cd ..

echo "[run.sh] Both processes running. Press Ctrl+C to stop."
wait
