/**
 * On-chain watcher — subscribes to Polygon RPC WebSocket and listens for
 * Polymarket CTF Exchange OrderFilled events in real time (~2-4s latency
 * vs ~6-13s for REST polling).
 *
 * Contract: 0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E (Polygon mainnet)
 * Event:    OrderFilled(bytes32 orderHash, address maker, address taker,
 *                       uint256 makerAssetId, uint256 takerAssetId,
 *                       uint256 makerAmountFilled, uint256 takerAmountFilled,
 *                       uint256 fee)
 *
 * How pricing works:
 *   Polymarket uses USDC (6 decimals) as collateral.
 *   Outcome tokens also have 6 decimals on Polygon.
 *   When a maker BUYS an outcome token:
 *     makerAsset = collateral (USDC position)  → gives USDC
 *     takerAsset = outcome token               → receives shares
 *     price = makerAmountFilled / takerAmountFilled  (both in 1e6 units)
 *   When a maker SELLS an outcome token:
 *     makerAsset = outcome token  → gives shares
 *     takerAsset = collateral     → receives USDC
 *     price = takerAmountFilled / makerAmountFilled
 *
 *   We identify direction by comparing with the known collateral token ID.
 *   If neither asset matches the collateral ID, we fall back to enrichment
 *   via the Polymarket Data API (see watcher.ts).
 */

import { EventEmitter } from "events";
import { ethers } from "ethers";
import WebSocket from "ws";
import { OnChainTrade } from "./types";

// Polymarket CTF Exchange on Polygon mainnet
const CTF_EXCHANGE = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E".toLowerCase();

// OrderFilled event ABI
const ABI = [
  "event OrderFilled(bytes32 indexed orderHash, address indexed maker, address indexed taker, uint256 makerAssetId, uint256 takerAssetId, uint256 makerAmountFilled, uint256 takerAmountFilled, uint256 fee)",
];
const iface = new ethers.Interface(ABI);
const ORDER_FILLED_TOPIC: string = iface.getEvent("OrderFilled")!.topicHash;

// Collateral (USDC) asset ID in Polymarket's CTF Exchange.
// ID 0 is used for the native ERC20 collateral position.
const COLLATERAL_ASSET_ID = BigInt(0);

const RECONNECT_DELAYS = [1_000, 2_000, 4_000, 8_000, 16_000, 30_000];
const PING_INTERVAL_MS = 20_000;

export class OnChainWatcher extends EventEmitter {
  private ws: WebSocket | null = null;
  private subId: string | null = null;
  private pingTimer: NodeJS.Timeout | null = null;
  private reconnectAttempt = 0;
  private alive = true;
  private reqId = 1;
  private targetWallets = new Set<string>();

  constructor(private wsRpcUrl: string) {
    super();
  }

  get isConnected(): boolean {
    return this.ws?.readyState === WebSocket.OPEN;
  }

  updateTargets(wallets: string[]): void {
    this.targetWallets = new Set(wallets.map((w) => w.toLowerCase()));
  }

  connect(): void {
    console.log(`[OnChain] Connecting to ${this.wsRpcUrl}`);
    this.ws = new WebSocket(this.wsRpcUrl);

    this.ws.on("open", () => {
      console.log("[OnChain] Connected to Polygon RPC");
      this.reconnectAttempt = 0;
      this._startPing();
      this._subscribe();
    });

    this.ws.on("message", (raw: Buffer) => {
      try {
        this._handle(JSON.parse(raw.toString()));
      } catch {
        // ignore malformed frames
      }
    });

    this.ws.on("close", (code) => {
      console.warn(`[OnChain] Connection closed (code=${code})`);
      this._clearPing();
      this.subId = null;
      if (this.alive) this._scheduleReconnect();
    });

    this.ws.on("error", (err: Error) => {
      console.error("[OnChain] WebSocket error:", err.message);
    });
  }

  private _subscribe(): void {
    // Subscribe to all OrderFilled events from the CTF Exchange.
    // We filter by maker client-side (target wallets change dynamically).
    this._send({
      jsonrpc: "2.0",
      method: "eth_subscribe",
      params: [
        "logs",
        {
          address: CTF_EXCHANGE,
          topics: [ORDER_FILLED_TOPIC],
        },
      ],
      id: this.reqId++,
    });
  }

  private _handle(msg: Record<string, unknown>): void {
    // Subscription acknowledgement
    if (
      typeof msg.result === "string" &&
      msg.result.startsWith("0x") &&
      !msg.method
    ) {
      this.subId = msg.result as string;
      console.log(`[OnChain] Subscribed to CTF Exchange (subId=${this.subId})`);
      return;
    }

    // Incoming log event
    if (
      msg.method === "eth_subscription" &&
      msg.params &&
      typeof msg.params === "object"
    ) {
      const result = (msg.params as Record<string, unknown>).result;
      if (result && typeof result === "object") {
        this._processLog(result as Record<string, unknown>);
      }
    }
  }

  private _processLog(log: Record<string, unknown>): void {
    try {
      const topics = log.topics as string[];
      const data = log.data as string;
      const txHash = (log.transactionHash as string) ?? "";
      const blockNumber = parseInt((log.blockNumber as string) ?? "0", 16);

      const decoded = iface.parseLog({ topics, data });
      if (!decoded) return;

      const maker: string = (decoded.args.maker as string).toLowerCase();

      // Only process events from our target wallets
      if (!this.targetWallets.has(maker)) return;

      const makerAssetId: bigint = decoded.args.makerAssetId as bigint;
      const takerAssetId: bigint = decoded.args.takerAssetId as bigint;
      const makerAmount: bigint = decoded.args.makerAmountFilled as bigint;
      const takerAmount: bigint = decoded.args.takerAmountFilled as bigint;

      // Determine direction and price
      let tokenId: string;
      let sizeUsdc: number;
      let price: number;
      let side: "YES" | "NO";

      if (makerAssetId === COLLATERAL_ASSET_ID) {
        // Maker gives USDC → BUYING an outcome token
        tokenId = takerAssetId.toString();
        sizeUsdc = Number(makerAmount) / 1e6;
        price = takerAmount > 0n ? Number(makerAmount) / Number(takerAmount) : 0;
        // Outcome token side determined during enrichment; default YES
        side = "YES";
      } else if (takerAssetId === COLLATERAL_ASSET_ID) {
        // Maker gives outcome token → SELLING
        tokenId = makerAssetId.toString();
        sizeUsdc = Number(takerAmount) / 1e6;
        price = makerAmount > 0n ? Number(takerAmount) / Number(makerAmount) : 0;
        side = "NO"; // selling YES = effectively NO signal; enrichment corrects this
      } else {
        // Neither is collateral — token-to-token swap, skip
        return;
      }

      // Sanity check: price must be 0–1
      if (price <= 0 || price >= 1 || sizeUsdc < 0.5) return;

      const trade: OnChainTrade = {
        maker,
        tokenId,
        sizeUsdc,
        price,
        side,
        txHash,
        blockNumber,
        detectedAt: new Date().toISOString(),
      };

      console.log(
        `[OnChain] Detected trade: ${maker.slice(0, 10)} token=${tokenId.slice(0, 12)} $${sizeUsdc.toFixed(2)} @ ${price.toFixed(3)} tx=${txHash.slice(0, 12)}`
      );

      this.emit("trade", trade);
    } catch {
      // ignore decode errors (different contract events, etc.)
    }
  }

  private _startPing(): void {
    this.pingTimer = setInterval(() => {
      if (this.ws?.readyState === WebSocket.OPEN) {
        this.ws.ping();
      }
    }, PING_INTERVAL_MS);
  }

  private _clearPing(): void {
    if (this.pingTimer) {
      clearInterval(this.pingTimer);
      this.pingTimer = null;
    }
  }

  private _scheduleReconnect(): void {
    const delay =
      RECONNECT_DELAYS[Math.min(this.reconnectAttempt, RECONNECT_DELAYS.length - 1)];
    this.reconnectAttempt++;
    console.log(`[OnChain] Reconnecting in ${delay}ms…`);
    setTimeout(() => this.connect(), delay);
  }

  private _send(msg: object): void {
    if (this.ws?.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(msg));
    }
  }

  destroy(): void {
    this.alive = false;
    this._clearPing();
    this.ws?.terminate();
  }
}
