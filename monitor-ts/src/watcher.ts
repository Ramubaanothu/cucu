import axios, { AxiosInstance } from "axios";
import { CopySignal, Trade, WalletInfo } from "./types";

const DATA_API_BASE = "https://data-api.polymarket.com";

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
        // First poll — set cursor, return nothing (avoid copying stale positions)
        if (trades.length > 0) {
          this.lastSeen.set(address, trades[0].id);
        }
        return [];
      }

      const newTrades: Trade[] = [];
      for (const t of trades) {
        if (t.id === lastId) break;
        newTrades.push(t);
      }

      if (newTrades.length > 0) {
        this.lastSeen.set(address, trades[0].id);
      }
      return newTrades;
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : String(err);
      console.warn(`[WalletPoller] Error polling ${address.slice(0, 10)}: ${msg}`);
      return [];
    }
  }
}

export class WalletWatcher {
  private targetWallets = new Set<string>();
  private agentHttp: AxiosInstance;
  private agentUrl: string;

  constructor(agentUrl: string) {
    this.agentUrl = agentUrl;
    this.agentHttp = axios.create({
      baseURL: agentUrl,
      timeout: 5_000,
    });
  }

  async loadTargets(): Promise<string[]> {
    try {
      const resp = await this.agentHttp.get<WalletInfo[]>("/wallets");
      const active = resp.data.filter((w) => w.is_active).map((w) => w.address.toLowerCase());
      this.targetWallets = new Set(active);
      return active;
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : String(err);
      console.warn(`[WalletWatcher] Could not load targets: ${msg}`);
      return [];
    }
  }

  get targets(): string[] {
    return Array.from(this.targetWallets);
  }

  isTarget(address: string): boolean {
    return this.targetWallets.has(address.toLowerCase());
  }

  async forwardSignal(trade: Trade, detectionMethod: "poll" | "ws"): Promise<void> {
    const walletAddr = (trade.user || trade.proxyWallet || "").toLowerCase();
    if (!this.isTarget(walletAddr)) return;

    const signal: CopySignal = {
      source_wallet: walletAddr,
      market_id: trade.conditionId,
      token_id: trade.asset,
      side: (trade.outcome?.toUpperCase() === "NO" ? "NO" : "YES") as "YES" | "NO",
      order_size_usdc: trade.size,
      price: trade.price,
      detected_at: trade.timestamp || new Date().toISOString(),
      detection_method: detectionMethod,
    };

    for (let attempt = 0; attempt < 3; attempt++) {
      try {
        await this.agentHttp.post("/copy-signal", signal);
        console.log(
          `[WalletWatcher] Signal forwarded: ${walletAddr.slice(0, 10)} ${signal.side} $${signal.order_size_usdc.toFixed(2)} @ ${signal.price.toFixed(3)}`
        );
        return;
      } catch {
        await new Promise((r) => setTimeout(r, 500 * (attempt + 1)));
      }
    }
    console.error(`[WalletWatcher] Failed to forward signal after 3 attempts`);
  }
}
