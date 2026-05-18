export interface CopySignal {
  source_wallet: string;
  market_id: string;
  token_id: string;
  side: "YES" | "NO";
  order_size_usdc: number;
  price: number;
  detected_at: string;
  detection_method: "poll" | "onchain";
}

export interface Trade {
  id: string;
  user: string;
  proxyWallet?: string;
  conditionId: string;
  asset: string;
  side: string;
  size: number;
  price: number;
  outcome: string;
  timestamp: string;
  profit?: number;
}

export interface WalletInfo {
  address: string;
  score: number;
  is_active: boolean;
}

export interface Config {
  agentUrl: string;
  pollIntervalMs: number;
  scanIntervalMs: number;
  polygonWsRpcUrl: string;
  maxRetries: number;
}

export interface OnChainTrade {
  maker: string;
  tokenId: string;       // ERC1155 outcome token ID
  sizeUsdc: number;      // USDC amount (human units)
  price: number;         // 0.0 – 1.0
  side: "YES" | "NO";
  txHash: string;
  blockNumber: number;
  detectedAt: string;
}

// Emitted for every maker (not just known targets) — used by HotWalletTracker
export interface OnChainActivity {
  maker: string;
  tokenId: string;
  sizeUsdc: number;
  price: number;
  isBuy: boolean;        // true = maker gave USDC, false = maker gave outcome token
  txHash: string;
  blockNumber: number;
  detectedAt: string;
}

// Forwarded to Python /hot-wallet when a wallet crosses alert thresholds
export interface HotWalletAlert {
  address: string;
  trade_count: number;
  window_hours: number;
  buy_usdc: number;
  sell_usdc: number;
  profit_ratio: number;
  unique_markets: number;
  reason: string;
}
