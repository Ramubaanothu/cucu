#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

echo "=== Polymarket Copy-Trading Agent Setup ==="

# Copy .env if missing
if [ ! -f .env ]; then
  cp .env.example .env
  echo "[+] Created .env from .env.example — edit it before running live mode"
else
  echo "[*] .env already exists"
fi

# Create data directory
mkdir -p data

# Python dependencies
echo "[+] Installing Python dependencies…"
if command -v uv &>/dev/null; then
  uv sync
else
  pip install -e ".[dev]" 2>/dev/null || pip install -e .
fi

# TypeScript dependencies
echo "[+] Installing TypeScript dependencies…"
cd monitor-ts
npm install
cd ..

echo ""
echo "=== Setup complete ==="
echo "Next steps:"
echo "  1. Edit .env with your credentials (or leave TRADING_MODE=paper to run without them)"
echo "  2. Run: ./scripts/run.sh"
echo "  3. To run a scan immediately: cucu run --scan-now"
echo "  4. API docs: http://localhost:8000/docs"
