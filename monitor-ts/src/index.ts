import * as dotenv from "dotenv";
import path from "path";

dotenv.config({ path: path.resolve(__dirname, "../../.env") });

import axios from "axios";
import { HotWalletAlert, OnChainActivity, OnChainTrade } from "./types";
import { OnChainWatcher } from "./onchain";
import { HotWalletTracker } from "./hot_tracker";
import { CopycatDetector } from "./copycat_detector";
import { WalletPoller, WalletWatcher } from "./watcher";

const AGENT_URL = `http://localhost:${process.env.AGENT_HTTP_PORT ?? "8000"}`;
const POLL_INTERVAL_MS = parseInt(process.env.POLL_INTERVAL_SECONDS ?? "3") * 1000;
const SCAN_INTERVAL_MS = parseInt(process.env.SCAN_INTERVAL_SECONDS ?? "3600") * 1000;

// Default to Polygon public WS endpoint; replace with Alchemy/Infura for reliability
const POLYGON_WS_RPC =
  process.env.POLYGON_WS_RPC_URL ?? "wss://polygon-bor-rpc.publicnode.com";

const poller     = new WalletPoller();
const watcher    = new WalletWatcher(AGENT_URL);
const onchain    = new OnChainWatcher(POLYGON_WS_RPC);
const hotTracker = new HotWalletTracker();
const copycat    = new CopycatDetector();

const pollingTimers = new Map<string, NodeJS.Timeout>();

// ── Polling management ───────────────────────────────────────────────────────

function startPolling(address: string): void {
  if (pollingTimers.has(address)) return;
  const timer = setInterval(async () => {
    const trades = await poller.pollWallet(address);
    for (const t of trades) await watcher.forwardPollTrade(t);
  }, POLL_INTERVAL_MS);
  pollingTimers.set(address, timer);
}

function stopPolling(address: string): void {
  const t = pollingTimers.get(address);
  if (t) { clearInterval(t); pollingTimers.delete(address); }
}

// ── Target sync ──────────────────────────────────────────────────────────────

async function syncTargets(): Promise<void> {
  const prev = new Set(pollingTimers.keys());
  const next = await watcher.loadTargets();

  if (next.length === 0) {
    console.warn("[Monitor] No active wallets. POST /rescan to the Python agent.");
    return;
  }

  const nextSet = new Set(next);

  for (const addr of prev) {
    if (!nextSet.has(addr)) { stopPolling(addr); console.log(`[Monitor] Dropped ${addr.slice(0, 10)}`); }
  }
  for (const addr of next) {
    if (!prev.has(addr)) { startPolling(addr); console.log(`[Monitor] Watching ${addr.slice(0, 10)}`); }
  }

  // Update on-chain filter with current target list
  onchain.updateTargets(next);
  console.log(`[Monitor] Synced — ${next.length} wallets (on-chain + polling)`);
}

// ── On-chain event handlers ──────────────────────────────────────────────────

// Copy-signal path: known target wallets only
onchain.on("trade", async (trade: OnChainTrade) => {
  await watcher.forwardOnChainTrade(trade);
});

// Hot-wallet discovery: track ALL makers in real time
onchain.on("activity", (activity: OnChainActivity) => {
  hotTracker.track(activity.maker, activity.sizeUsdc, activity.isBuy, activity.tokenId);
  copycat.observe(activity.maker, activity.tokenId);
});

// Copycat source-wallet alerts: forward as hot-wallet signals (same scoring path)
copycat.on("source-wallet", async (alert: { address: string; lead_count: number; reason: string }) => {
  try {
    await axios.post(`${AGENT_URL}/hot-wallet`, {
      address:        alert.address,
      trade_count:    alert.lead_count,
      window_hours:   1.5,
      buy_usdc:       0,
      sell_usdc:      0,
      profit_ratio:   0,
      unique_markets: 0,
      reason:         `copycat-source: ${alert.reason}`,
    }, { timeout: 10_000 });
    console.log(`[Copycat] Forwarded source-wallet ${alert.address.slice(0, 10)} to agent`);
  } catch (err: unknown) {
    const msg = err instanceof Error ? err.message : String(err);
    console.warn(`[Copycat] Failed to forward: ${msg}`);
  }
});

// Forward hot-wallet alerts to Python for immediate scoring and activation
hotTracker.on("hot-wallet", async (alert: HotWalletAlert) => {
  try {
    const resp = await axios.post(`${AGENT_URL}/hot-wallet`, alert, { timeout: 10_000 });
    const { status, score } = resp.data as { status: string; score?: number };
    console.log(
      `[HotTracker] Agent response for ${alert.address.slice(0, 10)}: ${status}` +
      (score !== undefined ? ` (score=${score.toFixed(1)})` : "")
    );
  } catch (err: unknown) {
    const msg = err instanceof Error ? err.message : String(err);
    console.warn(`[HotTracker] Failed to forward alert: ${msg}`);
  }
});

// ── Main ─────────────────────────────────────────────────────────────────────

async function waitForAgent(): Promise<void> {
  for (let i = 1; i <= 20; i++) {
    try {
      await axios.get(`${AGENT_URL}/status`, { timeout: 2_000 });
      return;
    } catch {
      console.log(`[Monitor] Waiting for Python agent (${i}/20)…`);
      await new Promise((r) => setTimeout(r, 2_000));
    }
  }
  throw new Error("Python agent not reachable after 40s");
}

async function main(): Promise<void> {
  console.log("[Monitor] Starting cucu monitor");
  console.log(`[Monitor] Agent:    ${AGENT_URL}`);
  console.log(`[Monitor] RPC:      ${POLYGON_WS_RPC}`);
  console.log(`[Monitor] Poll:     ${POLL_INTERVAL_MS}ms (fallback)`);

  await waitForAgent();
  await syncTargets();

  // Primary: on-chain subscription
  onchain.connect();

  // Refresh target list periodically (after each scanner run)
  setInterval(syncTargets, SCAN_INTERVAL_MS);

  console.log(
    `[Monitor] Running — ${pollingTimers.size} wallets.\n` +
    `          On-chain (primary): Polygon WS → ~2-4s latency\n` +
    `          Polling (fallback):  REST API  → ~${POLL_INTERVAL_MS / 1000}s latency`
  );
}

process.on("SIGINT", () => {
  console.log("\n[Monitor] Shutting down…");
  for (const a of pollingTimers.keys()) stopPolling(a);
  onchain.destroy();
  hotTracker.destroy();
  copycat.destroy();
  process.exit(0);
});

main().catch((err) => {
  console.error("[Monitor] Fatal:", err);
  process.exit(1);
});
