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
