# cucu — Polymarket Copy-Trading AI Agent

Scans Polymarket for high-performing trading bots, scores them, and copies their trades automatically. Claude AI gates each copy decision. Runs in **paper mode** (no credentials required) or **live mode** with a Polygon wallet.

---

## Architecture

```
Python agent (FastAPI + asyncio)
  ├── Scanner: leaderboard → wallet scoring → bot detection
  ├── AI Analyst: Claude tool-use for wallet ranking + pre-trade assessment
  ├── Executor: paper (log only) | live (Polymarket CLOB orders)
  └── HTTP API on :8000

TypeScript monitor (Node.js)
  └── Polls target wallet activity every 3s
      → POSTs copy signals to /copy-signal
```

---

## Quick Start

```bash
# 1. Setup
./scripts/setup.sh

# 2. (Optional) Edit .env with your credentials
#    Leave TRADING_MODE=paper to run without any credentials

# 3. Run — starts both Python agent + TypeScript monitor
./scripts/run.sh

# 4. Trigger first scan (finds top bots to copy)
curl -X POST localhost:8000/rescan

# 5. Watch the logs and check state
curl localhost:8000/status
curl localhost:8000/wallets
curl localhost:8000/trades
```

---

## Configuration (`.env`)

| Variable | Default | Description |
|---|---|---|
| `TRADING_MODE` | `paper` | `paper` or `live` |
| `ANTHROPIC_API_KEY` | — | Claude AI (optional but recommended) |
| `POLYMARKET_API_KEY` | — | Required for live mode |
| `POLYGON_PRIVATE_KEY` | — | Required for live mode |
| `COPY_SCALE` | `0.10` | Fraction of target's trade size to copy (10%) |
| `MAX_EXPOSURE_PER_MARKET` | `50` | USDC cap per market |
| `MAX_TOTAL_EXPOSURE` | `500` | Total portfolio USDC cap |
| `MIN_BOT_SCORE` | `70` | Wallet score threshold (0–100) |
| `SCAN_INTERVAL_SECONDS` | `3600` | How often to re-run leaderboard scan |
| `TARGET_WALLETS` | — | Comma-separated addresses (bypass auto-scan) |

---

## API Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/status` | Agent state, exposure summary |
| `GET` | `/wallets` | Tracked wallets with scores |
| `POST` | `/wallets/{addr}/toggle` | Enable/disable a wallet |
| `GET` | `/positions?mode=paper` | Open positions |
| `GET` | `/trades?limit=50` | Trade log |
| `POST` | `/copy-signal` | Inject a copy signal (used by TS monitor) |
| `POST` | `/rescan` | Trigger leaderboard re-scan |

Interactive docs: `http://localhost:8000/docs`

---

## How Bot Detection Works

Each candidate wallet is scored 0-100:
- **Win rate** (30%) — trades closed profitably
- **ROI 30d** (25%) — return on capital deployed
- **Sharpe** (20%) — daily PnL volatility-adjusted
- **Consistency** (15%) — % of weeks with positive PnL
- **Trade frequency** (10%) — trades per day

Bot-like signals (high frequency, consistent sizing, 24h activity) are flagged but don't disqualify — many excellent market-makers look bot-like.

Claude then re-ranks the top candidates and selects the best `MAX_TARGET_WALLETS`.

---

## Live Mode Setup

1. Create a Polymarket account and fund a Polygon wallet with USDC
2. Register for CLOB API access at polymarket.com
3. Set `TRADING_MODE=live` and fill in all credential fields in `.env`
4. Start with small limits (`MAX_TOTAL_EXPOSURE=50`) for the first run

**Note**: Live orders use FOK (fill-or-kill) to avoid stale resting orders.
