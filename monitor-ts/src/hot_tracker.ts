/**
 * HotWalletTracker — real-time detector for new profitable wallets.
 *
 * Watches ALL makers on the CTF Exchange (not just known targets) and emits
 * a "hot-wallet" event the moment any of these conditions are met:
 *
 *   1. MIN_TRADES in WINDOW_MS   → active new bot, possible 80%+ accuracy
 *   2. Large sell (≥ LARGE_SELL_USDC in one trade) → big cashout / market win
 *   3. High profit ratio (≥ MIN_RETURN_RATIO AND sell_usdc ≥ MIN_PROFIT_USDC)
 *      → e.g. put in $100, got back $500+ = 5x return
 *
 * Detection latency: < 1 block (~2 seconds on Polygon).
 * Compare: leaderboard = 2-4 weeks, on-chain batch scan = hours.
 */

import { EventEmitter } from "events";
import { HotWalletAlert } from "./types";

const WINDOW_MS        = 4  * 60 * 60 * 1000;  // 4-hour rolling window
const MIN_TRADES       = 5;                      // trades in window → active bot
const LARGE_SELL_USDC  = 1_000;                  // single sell ≥ $1 000 → big cashout
const MIN_RETURN_RATIO = 3.0;                    // 3× return in window
const MIN_PROFIT_USDC  = 200;                    // minimum sell volume for ratio trigger
const COOLDOWN_MS      = 2  * 60 * 60 * 1000;   // max one alert per wallet per 2h
const CLEANUP_INTERVAL = 5  * 60 * 1000;         // prune stale stats every 5 min

interface MakerStats {
  tradeTimes: number[];   // epoch-ms of each trade in the rolling window
  buyUsdc: number;        // cumulative USDC in (bought outcome tokens)
  sellUsdc: number;       // cumulative USDC out (sold outcome tokens)
  uniqueTokens: Set<string>;
  lastAlertedAt: number;  // epoch-ms of last alert (for cooldown)
}

export class HotWalletTracker extends EventEmitter {
  private stats = new Map<string, MakerStats>();
  private cleanupTimer: NodeJS.Timeout;

  constructor() {
    super();
    this.cleanupTimer = setInterval(() => this._cleanup(), CLEANUP_INTERVAL);
  }

  /**
   * Call this for every on-chain activity event, regardless of whether the
   * maker is a known target.
   */
  track(
    maker: string,
    sizeUsdc: number,
    isBuy: boolean,
    tokenId: string,
  ): void {
    const now = Date.now();

    if (!this.stats.has(maker)) {
      this.stats.set(maker, {
        tradeTimes: [],
        buyUsdc: 0,
        sellUsdc: 0,
        uniqueTokens: new Set(),
        lastAlertedAt: 0,
      });
    }

    const s = this.stats.get(maker)!;

    // Record this trade
    s.tradeTimes.push(now);
    s.uniqueTokens.add(tokenId);
    if (isBuy) {
      s.buyUsdc += sizeUsdc;
    } else {
      s.sellUsdc += sizeUsdc;
    }

    // Trim trades older than the rolling window
    const cutoff = now - WINDOW_MS;
    s.tradeTimes = s.tradeTimes.filter((t) => t > cutoff);

    // Respect per-wallet cooldown to avoid alert spam
    if (now - s.lastAlertedAt < COOLDOWN_MS) return;

    const recentTrades = s.tradeTimes.length;
    const profitRatio   = s.buyUsdc > 0 ? s.sellUsdc / s.buyUsdc : 0;

    let reason: string | null = null;

    if (recentTrades >= MIN_TRADES) {
      reason = `${recentTrades} trades in 4h window`;
    } else if (!isBuy && sizeUsdc >= LARGE_SELL_USDC) {
      reason = `Large cashout: $${sizeUsdc.toFixed(0)} received in single sell`;
    } else if (profitRatio >= MIN_RETURN_RATIO && s.sellUsdc >= MIN_PROFIT_USDC) {
      reason = `${profitRatio.toFixed(1)}× return ($${s.sellUsdc.toFixed(0)} out / $${s.buyUsdc.toFixed(0)} in)`;
    }

    if (!reason) return;

    s.lastAlertedAt = now;

    const alert: HotWalletAlert = {
      address:        maker,
      trade_count:    recentTrades,
      window_hours:   4,
      buy_usdc:       s.buyUsdc,
      sell_usdc:      s.sellUsdc,
      profit_ratio:   profitRatio,
      unique_markets: s.uniqueTokens.size,
      reason,
    };

    console.log(
      `[HotTracker] 🔥 ${maker.slice(0, 10)} — ${reason} (markets=${alert.unique_markets})`
    );
    this.emit("hot-wallet", alert);
  }

  private _cleanup(): void {
    const cutoff = Date.now() - WINDOW_MS;
    for (const [addr, s] of this.stats.entries()) {
      s.tradeTimes = s.tradeTimes.filter((t) => t > cutoff);
      // Drop wallets with no recent trades and expired cooldown
      if (
        s.tradeTimes.length === 0 &&
        Date.now() - s.lastAlertedAt > COOLDOWN_MS
      ) {
        this.stats.delete(addr);
      }
    }
  }

  destroy(): void {
    clearInterval(this.cleanupTimer);
  }
}
