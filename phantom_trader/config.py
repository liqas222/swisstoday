import os
from dotenv import load_dotenv

load_dotenv()

HL_API_URL          = "https://api.hyperliquid.xyz"
HL_WS_URL           = "wss://api.hyperliquid.xyz/ws"
HL_PRIVATE_KEY      = os.getenv("HL_PRIVATE_KEY", "")   # EVM 0x..., never logged

PAPER_TRADING       = os.getenv("PAPER_TRADING", "true").lower() == "true"
PAPER_BALANCE       = float(os.getenv("PAPER_BALANCE", "1000"))

# Copy-trade decision thresholds
MIN_WIN_RATE        = float(os.getenv("MIN_WIN_RATE", "0.60"))
MIN_ROI_RATE        = float(os.getenv("MIN_ROI_RATE", "0.90"))
MIN_ROI_THRESHOLD   = float(os.getenv("MIN_ROI_THRESHOLD", "30"))
MAX_LOSS_DURATION_MIN = int(os.getenv("MAX_LOSS_DURATION_MIN", "180"))

# Risk limits
MAX_POSITION_USD    = float(os.getenv("MAX_POSITION_USD", "100"))
MAX_LEVERAGE        = float(os.getenv("MAX_LEVERAGE", "5"))
MAX_OPEN_POSITIONS  = int(os.getenv("MAX_OPEN_POSITIONS", "3"))
COPY_COOLDOWN_SEC   = int(os.getenv("COPY_COOLDOWN_SEC", "300"))

DASHBOARD_PORT      = int(os.getenv("DASHBOARD_PORT", "5001"))
TOP_WALLET_COUNT    = 20
