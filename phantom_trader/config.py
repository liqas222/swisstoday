import os
from dotenv import load_dotenv

load_dotenv()

HELIUS_API_KEY      = os.getenv("HELIUS_API_KEY", "")
WALLET_PRIVATE_KEY  = os.getenv("WALLET_PRIVATE_KEY", "")   # base58, never logged

PAPER_TRADING       = os.getenv("PAPER_TRADING", "true").lower() == "true"
PAPER_BALANCE       = float(os.getenv("PAPER_BALANCE", "1000"))

# Copy-trade decision thresholds
MIN_WIN_RATE        = float(os.getenv("MIN_WIN_RATE", "0.60"))      # 60%
MIN_ROI_RATE        = float(os.getenv("MIN_ROI_RATE", "0.90"))      # 90% of trades
MIN_ROI_THRESHOLD   = float(os.getenv("MIN_ROI_THRESHOLD", "30"))   # must exceed 30% ROI
MAX_LOSS_DURATION_MIN = int(os.getenv("MAX_LOSS_DURATION_MIN", "180"))  # 3 hours

# Risk limits
MAX_POSITION_USD    = float(os.getenv("MAX_POSITION_USD", "100"))
MAX_LEVERAGE        = float(os.getenv("MAX_LEVERAGE", "5"))
MAX_OPEN_POSITIONS  = int(os.getenv("MAX_OPEN_POSITIONS", "3"))
COPY_COOLDOWN_SEC   = int(os.getenv("COPY_COOLDOWN_SEC", "300"))

WEBHOOK_SECRET      = os.getenv("WEBHOOK_SECRET", "")
WEBHOOK_URL         = os.getenv("WEBHOOK_URL", "")   # public URL for Helius to POST to
DASHBOARD_PORT      = int(os.getenv("DASHBOARD_PORT", "5001"))

HELIUS_RPC_URL      = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}" if HELIUS_API_KEY else "https://api.mainnet-beta.solana.com"
HELIUS_API_BASE     = "https://api.helius.xyz/v0"

JUPITER_PERPS_PROGRAM = "PERPHjGBqRHArX4DySjwM6UJHiR3sWAatqfdBS2qQJu"
TOP_WALLET_COUNT    = 20
LEADERBOARD_PERIOD  = "7d"
