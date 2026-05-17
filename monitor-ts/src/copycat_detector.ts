/**
 * CopycatDetector — finds the SOURCE wallet in copy-trading clusters.
 *
 * Problem: if bot B always copies bot A within 60s, we might accidentally
 * watch B (the echo) instead of A (the alpha). A gets the edge; B just
 * delays it by 30–60 seconds and adds slippage.
 *
 * Logic:
 *   For each tokenId, maintain a rolling 90-second window of (maker, timestamp).
 *   When maker B trades tokenId T that maker A already traded in the window,
 *   A gets a "lead point" — A moved first. When A accumulates MIN_LEADS lead
 *   points across different tokens (not the same trade twice), emit "source-wallet".
 *
 * This surfaces wallets that OTHERS copy, not wallets that copy others.
 * A lead point counts only once per unique (A, B, tokenId) triple.
 */

import { EventEmitter } from "events";

const WINDOW_MS       = 90  * 1000;   // 90-second co-trade window
const MIN_LEADS       = 4;            // lead points before we call someone a source
const COOLDOWN_MS     = 4  * 60 * 60 * 1000;  // 4h between alerts for same wallet
const CLEANUP_INTERVAL = 2 * 60 * 1000;

interface TradeEntry {
  maker:     string;
  timestamp: number;
}

export class CopycatDetector extends EventEmitter {
  // tokenId → recent trades in rolling window
  private recentByToken = new Map<string, TradeEntry[]>();
  // (leader_address) → lead-point count
  private leadCounts    = new Map<string, number>();
  // seen (A, B, tokenId) triples — each unique triple counts only once
  private seenTriples   = new Set<string>();
  // per-wallet alert cooldown
  private lastAlerted   = new Map<string, number>();
  private cleanupTimer: NodeJS.Timeout;

  constructor() {
    super();
    this.cleanupTimer = setInterval(() => this._cleanup(), CLEANUP_INTERVAL);
  }

  /**
   * Record a trade event. Call this for every "activity" event from OnChainWatcher.
   */
  observe(maker: string, tokenId: string): void {
    const now = Date.now();
    const cutoff = now - WINDOW_MS;

    // Fetch existing entries for this tokenId within the window
    const entries = (this.recentByToken.get(tokenId) ?? []).filter(
      (e) => e.timestamp > cutoff
    );

    // Award lead points to everyone who traded this token BEFORE this maker
    for (const prior of entries) {
      if (prior.maker === maker) continue;

      const triple = `${prior.maker}:${maker}:${tokenId}`;
      if (this.seenTriples.has(triple)) continue;
      this.seenTriples.add(triple);

      const prev = this.leadCounts.get(prior.maker) ?? 0;
      const next = prev + 1;
      this.leadCounts.set(prior.maker, next);

      if (next >= MIN_LEADS) {
        this._maybeAlert(prior.maker, next, now);
      }
    }

    // Record this trade in the window
    entries.push({ maker, timestamp: now });
    this.recentByToken.set(tokenId, entries);
  }

  private _maybeAlert(source: string, leadCount: number, now: number): void {
    const last = this.lastAlerted.get(source) ?? 0;
    if (now - last < COOLDOWN_MS) return;

    this.lastAlerted.set(source, now);
    // Reset lead count so the alert doesn't fire every new trade
    this.leadCounts.set(source, 0);

    console.log(
      `[Copycat] 🎯 Source wallet detected: ${source.slice(0, 10)} ` +
      `(${leadCount} others copied their trades)`
    );
    this.emit("source-wallet", {
      address:    source,
      lead_count: leadCount,
      reason:     `Copied by ${leadCount} other wallets within 90s window`,
    });
  }

  private _cleanup(): void {
    const cutoff = Date.now() - WINDOW_MS;

    for (const [tokenId, entries] of this.recentByToken.entries()) {
      const fresh = entries.filter((e) => e.timestamp > cutoff);
      if (fresh.length === 0) {
        this.recentByToken.delete(tokenId);
      } else {
        this.recentByToken.set(tokenId, fresh);
      }
    }

    // Prune old triple entries (cap set size to avoid unbounded growth)
    if (this.seenTriples.size > 50_000) {
      // Keep only the most recently added (Set maintains insertion order)
      const arr = Array.from(this.seenTriples);
      this.seenTriples = new Set(arr.slice(-25_000));
    }
  }

  destroy(): void {
    clearInterval(this.cleanupTimer);
  }
}
