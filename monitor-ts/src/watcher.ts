import axios, { AxiosInstance } from "axios";
import { CopySignal, OnChainTrade, Trade, WalletInfo } from "./types";

const DATA_API_BASE = "https://data-api.polymarket.com";

// ── Polling-based watcher ────────────────────────────────────────────────────

export class WalletPoller {
  private http: AxiosInstance;
  private lastSeen = new Map<string, string>(); // address -> last trade id

  constructor() {
    this.http = axios.create({
      baseURL: DATA_API_BASE,
      timeout: 10_000,
      headers: { Accept: "application/json" },
    });
  }

  async pollWallet(address: string): Promise<Trade[]> {
    try {
      const resp = await this.http.get<Trade[]>("/activity", {
        params: { user: address, limit: 20 },
      });
      const trades: Trade[] = Array.isArray(resp.data)
        ? resp.data
        : (resp.data as { data?: Trade[] })?.data ?? [];

      const lastId = this.lastSeen.get(address);
      if (!lastId) {
        if (trades.length > 0) this.lastSeen.set(address, trades[0].id);
        return []; // skip stale trades on first poll
      }

      const newTrades: Trade[] = [];
      for (const t of trades) {
        if (t.id === lastId) break;
        newTrades.push(t);
      }
      if (newTrades.length > 0) this.lastSeen.set(address, trades[0].id);
      return newTrades;
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : String(err);
      console.warn(`[Poller] Error polling ${address.slice(0, 10)}: ${msg}`);
      return [];
    }
  }
}

// ── Signal forwarder ─────────────────────────────────────────────────────────

export class WalletWatcher {
  private targetWallets = new Set<string>();
  private agentHttp: AxiosInstance;
  private dataHttp: AxiosInstance;

  constructor(agentUrl: string) {
    this.agentHttp = axios.create({ baseURL: agentUrl, timeout: 5_000 });
    this.dataHttp = axios.create({
      baseURL: DATA_API_BASE,
      timeout: 8_000,
      headers: { Accept: "application/json" },
    });
  }

  async loadTargets(): Promise<string[]> {
    try {
      const resp = await this.agentHttp.get<WalletInfo[]>("/wallets");
      const active = resp.data
        .filter((w) => w.is_active)
        .map((w) => w.address.toLowerCase());
      this.targetWallets = new Set(active);
      return active;
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : String(err);
      console.warn(`[Watcher] Could not load targets: ${msg}`);
      return [];
    }
  }

  get targets(): string[] {
    return Array.from(this.targetWallets);
  }

  isTarget(address: string): boolean {
    return this.targetWallets.has(address.toLowerCase());
  }

  // ── From polling ────────────────────────────────────────────────────────

  async forwardPollTrade(trade: Trade): Promise<void> {
    const wallet = (trade.user || trade.proxyWallet || "").toLowerCase();
    if (!this.isTarget(wallet)) return;

    const signal: CopySignal = {
      source_wallet: wallet,
      market_id: trade.conditionId,
      token_id: trade.asset,
      side: trade.outcome?.toUpperCase() === "NO" ? "NO" : "YES",
      order_size_usdc: trade.size,
      price: trade.price,
      detected_at: trade.timestamp || new Date().toISOString(),
      detection_method: "poll",
    };
    await this._forward(signal);
  }

  // ── From on-chain event ─────────────────────────────────────────────────

  async forwardOnChainTrade(onchain: OnChainTrade): Promise<void> {
    if (!this.isTarget(onchain.maker)) return;

    // Enrich with API data to get conditionId and confirmed side
    const enriched = await this._enrich(onchain);
    if (!enriched) {
      // Forward with partial data — Python agent will validate
      const signal: CopySignal = {
        source_wallet: onchain.maker,
        market_id: onchain.tokenId, // best guess until enriched
        token_id: onchain.tokenId,
        side: onchain.side,
        order_size_usdc: onchain.sizeUsdc,
        price: onchain.price,
        detected_at: onchain.detectedAt,
        detection_method: "onchain",
      };
      await this._forward(signal);
      return;
    }
    await this._forward(enriched);
  }

  private async _enrich(onchain: OnChainTrade): Promise<CopySignal | null> {
    try {
      // Fetch the most recent trades for this wallet and match by token ID
      // within a 30-second window of detection
      const resp = await this.dataHttp.get<Trade[]>("/activity", {
        params: { user: onchain.maker, limit: 10 },
      });
      const trades: Trade[] = Array.isArray(resp.data)
        ? resp.data
        : (resp.data as { data?: Trade[] })?.data ?? [];

      const detectedMs = new Date(onchain.detectedAt).getTime();

      for (const t of trades) {
        const tradeMs = new Date(t.timestamp).getTime();
        const ageSeconds = Math.abs(detectedMs - tradeMs) / 1000;

        // Match by token ID and recency (within 45s)
        if (t.asset === onchain.tokenId && ageSeconds < 45) {
          return {
            source_wallet: onchain.maker,
            market_id: t.conditionId,
            token_id: t.asset,
            side: t.outcome?.toUpperCase() === "NO" ? "NO" : "YES",
            order_size_usdc: t.size || onchain.sizeUsdc,
            price: t.price || onchain.price,
            detected_at: onchain.detectedAt,
            detection_method: "onchain",
          };
        }
      }
      return null;
    } catch {
      return null;
    }
  }

  private async _forward(signal: CopySignal): Promise<void> {
    for (let attempt = 0; attempt < 3; attempt++) {
      try {
        await this.agentHttp.post("/copy-signal", signal);
        console.log(
          `[Watcher] ✓ ${signal.detection_method.toUpperCase()} ${signal.source_wallet.slice(0, 10)} ${signal.side} $${signal.order_size_usdc.toFixed(2)} @ ${signal.price.toFixed(3)}`
        );
        return;
      } catch {
        await new Promise((r) => setTimeout(r, 500 * (attempt + 1)));
      }
    }
    console.error("[Watcher] Failed to forward signal after 3 attempts");
  }
}
