import WebSocket from "ws";
import { EventEmitter } from "events";

const WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market";
const PING_INTERVAL_MS = 20_000;
const RECONNECT_DELAY_MS = [1000, 2000, 4000, 8000, 16000, 30000];

export interface CLOBEvent {
  event_type: string;
  market?: string;
  asset_id?: string;
  price?: number;
  side?: string;
  size?: number;
  timestamp?: string;
  [key: string]: unknown;
}

export class OrderBookStream extends EventEmitter {
  private ws: WebSocket | null = null;
  private pingTimer: NodeJS.Timeout | null = null;
  private reconnectAttempt = 0;
  private subscriptions = new Set<string>();
  private alive = true;

  connect(): void {
    this.ws = new WebSocket(WS_URL);

    this.ws.on("open", () => {
      console.log("[OrderBookStream] Connected to CLOB WebSocket");
      this.reconnectAttempt = 0;
      this._startPing();

      // Re-subscribe all token IDs after reconnect
      for (const tokenId of this.subscriptions) {
        this._subscribe(tokenId);
      }
    });

    this.ws.on("message", (raw: Buffer) => {
      try {
        const events: CLOBEvent | CLOBEvent[] = JSON.parse(raw.toString());
        const list = Array.isArray(events) ? events : [events];
        for (const event of list) {
          this.emit("event", event);
        }
      } catch {
        // ignore malformed frames
      }
    });

    this.ws.on("close", () => {
      console.warn("[OrderBookStream] Connection closed");
      this._clearPing();
      if (this.alive) this._scheduleReconnect();
    });

    this.ws.on("error", (err: Error) => {
      console.error("[OrderBookStream] Error:", err.message);
    });
  }

  subscribe(tokenId: string): void {
    this.subscriptions.add(tokenId);
    if (this.ws?.readyState === WebSocket.OPEN) {
      this._subscribe(tokenId);
    }
  }

  private _subscribe(tokenId: string): void {
    this.ws?.send(
      JSON.stringify({
        type: "subscribe",
        channel: "market",
        markets: [tokenId],
      })
    );
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
      RECONNECT_DELAY_MS[
        Math.min(this.reconnectAttempt, RECONNECT_DELAY_MS.length - 1)
      ];
    this.reconnectAttempt++;
    console.log(`[OrderBookStream] Reconnecting in ${delay}ms…`);
    setTimeout(() => this.connect(), delay);
  }

  destroy(): void {
    this.alive = false;
    this._clearPing();
    this.ws?.terminate();
  }
}
