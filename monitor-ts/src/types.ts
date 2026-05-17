export interface CopySignal {
  source_wallet: string;
  market_id: string;
  token_id: string;
  side: "YES" | "NO";
  order_size_usdc: number;
  price: number;
  detected_at: string;
  detection_method: "poll" | "ws";
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
  dataApiBase: string;
  maxRetries: number;
}
