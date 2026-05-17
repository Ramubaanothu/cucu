import * as dotenv from "dotenv";
import path from "path";

dotenv.config({ path: path.resolve(__dirname, "../../.env") });

import { Config, Trade } from "./types";
import { OrderBookStream, CLOBEvent } from "./orderbook";
import { WalletPoller, WalletWatcher } from "./watcher";

const config: Config = {
  agentUrl: `http://localhost:${process.env.AGENT_HTTP_PORT ?? "8000"}`,
  pollIntervalMs: parseInt(process.env.POLL_INTERVAL_SECONDS ?? "3") * 1000,
  scanIntervalMs: parseInt(process.env.SCAN_INTERVAL_SECONDS ?? "3600") * 1000,
  dataApiBase: "https://data-api.polymarket.com",
  maxRetries: 3,
};

const poller = new WalletPoller();
const watcher = new WalletWatcher(config.agentUrl);
const stream = new OrderBookStream();

const pollingIntervals = new Map<string, NodeJS.Timeout>();

function startPollingWallet(address: string): void {
  if (pollingIntervals.has(address)) return;
  const timer = setInterval(async () => {
    const newTrades = await poller.pollWallet(address);
    for (const trade of newTrades) {
      await watcher.forwardSignal(trade, "poll");
    }
  }, config.pollIntervalMs);
  pollingIntervals.set(address, timer);
}

function stopPollingWallet(address: string): void {
  const timer = pollingIntervals.get(address);
  if (timer) {
    clearInterval(timer);
    pollingIntervals.delete(address);
  }
}

async function syncTargets(): Promise<void> {
  const prevTargets = new Set(pollingIntervals.keys());
  const newTargets = await watcher.loadTargets();

  if (newTargets.length === 0) {
    console.warn(
      "[Monitor] No active target wallets. Run a scan via: curl -X POST localhost:8000/rescan"
    );
  }

  const newSet = new Set(newTargets);

  // Stop polling wallets no longer in target list
  for (const addr of prevTargets) {
    if (!newSet.has(addr)) {
      stopPollingWallet(addr);
      console.log(`[Monitor] Stopped watching ${addr.slice(0, 10)}`);
    }
  }

  // Start polling new targets
  for (const addr of newTargets) {
    if (!prevTargets.has(addr)) {
      startPollingWallet(addr);
      console.log(`[Monitor] Now watching ${addr.slice(0, 10)}`);
    }
  }
}

// WS stream events — supplementary signal for order book depth changes
stream.on("event", (event: CLOBEvent) => {
  // The CLOB WS stream doesn't expose maker addresses directly,
  // but we log significant price movements for awareness
  if (event.event_type === "last_trade_price" && event.price) {
    // No action needed — polling handles copy signals
  }
});

async function main(): Promise<void> {
  console.log(`[Monitor] Starting Polymarket wallet watcher`);
  console.log(`[Monitor] Agent URL: ${config.agentUrl}`);
  console.log(`[Monitor] Poll interval: ${config.pollIntervalMs}ms`);

  // Wait for the Python agent to be ready
  let agentReady = false;
  for (let i = 0; i < 20; i++) {
    try {
      const axios = (await import("axios")).default;
      await axios.get(`${config.agentUrl}/status`, { timeout: 2000 });
      agentReady = true;
      break;
    } catch {
      console.log(`[Monitor] Waiting for agent (attempt ${i + 1}/20)…`);
      await new Promise((r) => setTimeout(r, 2000));
    }
  }

  if (!agentReady) {
    console.error("[Monitor] Agent not reachable. Start the Python agent first.");
    process.exit(1);
  }

  // Initial target load
  await syncTargets();

  // Connect to CLOB WebSocket (supplementary)
  stream.connect();

  // Refresh target list on scan interval
  setInterval(() => syncTargets(), config.scanIntervalMs);

  console.log(
    `[Monitor] Watching ${pollingIntervals.size} wallets. Press Ctrl+C to stop.`
  );
}

process.on("SIGINT", () => {
  console.log("\n[Monitor] Shutting down…");
  for (const addr of pollingIntervals.keys()) stopPollingWallet(addr);
  stream.destroy();
  process.exit(0);
});

main().catch((err) => {
  console.error("[Monitor] Fatal error:", err);
  process.exit(1);
});
