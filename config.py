from __future__ import annotations

import os
from dotenv import load_dotenv

load_dotenv()

# ── Wallet / Keys ────────────────────────────────────────────────────────────
WALLET_PRIVATE_KEY: str = os.getenv("WALLET_PRIVATE_KEY", "")
HELIUS_API_KEY: str     = os.getenv("HELIUS_API_KEY", "")
TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID: str   = os.getenv("TELEGRAM_CHAT_ID", "")
PAPER_TRADE: bool       = os.getenv("PAPER_TRADE", "true").lower() in ("true", "1", "yes")

# ── Solana constants ─────────────────────────────────────────────────────────
SOL_MINT           = "So11111111111111111111111111111111111111112"
LAMPORTS_PER_SOL   = 1_000_000_000

# ── RPC / API endpoints ──────────────────────────────────────────────────────
HELIUS_RPC_URL     = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
SOLANA_RPC_URL     = "https://api.mainnet-beta.solana.com"
HELIUS_TX_URL      = "https://api-mainnet.helius-rpc.com/v0/addresses/{address}/transactions"
JUPITER_QUOTE_URL  = "https://api.jup.ag/swap/v1/quote"
JUPITER_SWAP_URL   = "https://api.jup.ag/swap/v1/swap"
JUPITER_PRICE_URL  = "https://api.jup.ag/price/v2"
RUGCHECK_URL       = "https://api.rugcheck.xyz/v1/tokens/{mint}/report"
TELEGRAM_SEND_URL  = "https://api.telegram.org/bot{token}/sendMessage"

# ── GMGN endpoints ───────────────────────────────────────────────────────────
GMGN_BASE          = "https://gmgn.ai/defi/quotation/v1"
GMGN_TRENDING_URL  = f"{GMGN_BASE}/rank/sol/swaps/1h"
GMGN_SIGNALS_URL   = f"{GMGN_BASE}/signals/sol"
GMGN_NEW_TOKEN_URL = f"{GMGN_BASE}/new_token/sol"
GMGN_TOP_WALLETS_URL = f"{GMGN_BASE}/smartmoney/sol/wallets"
GMGN_WALLET_ACTIVITY_URL = f"{GMGN_BASE}/wallet_activity/sol"

# ── Trading sizes ─────────────────────────────────────────────────────────────
# Trending + new tokens: small size (higher risk)
TRENDING_BUY_SOL   = 0.05
# Smart money signals: medium (already validated)
SIGNAL_BUY_SOL     = 0.08
# Wallet copy: same as the tracked wallet's relative size
COPY_BUY_SOL       = 0.07

# ── Filters ───────────────────────────────────────────────────────────────────
MIN_LIQUIDITY_USD  = 5_000
MAX_MARKET_CAP_USD = 2_000_000
MIN_SWAPS_1H       = 20          # trending tokens need at least 20 swaps in last 1h
COPY_WALLET_COUNT  = 5           # how many top GMGN wallets to copy

# ── Scan timing ───────────────────────────────────────────────────────────────
SCAN_INTERVAL_SECONDS        = 30
WALLET_POLL_INTERVAL_SECONDS = 8
RISK_POLL_INTERVAL_SECONDS   = 5

# ── Risk / TP / SL ────────────────────────────────────────────────────────────
STOP_LOSS_PCT                 = -25.0
TRAILING_ACTIVATION_MULTIPLIER = 3.0
TRAILING_STOP_PCT             = -18.0
TP1_MULTIPLIER                = 2.0
TP1_SELL_PCT                  = 34.0
TP2_MULTIPLIER                = 5.0
TP2_SELL_PCT                  = 33.0
TP3_MULTIPLIER                = 10.0
TP3_SELL_PCT                  = 33.0
TIME_STOP_MINUTES             = 45
TIME_STOP_MIN_MULTIPLIER      = 1.5
DEFAULT_SLIPPAGE_BPS          = 300
MAX_RETRIES                   = 3
RETRY_DELAY_SECONDS           = 1.5
