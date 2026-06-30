import os
from dotenv import load_dotenv

# Load .env file from the same directory as this config
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

# Alpaca Paper Trading Credentials (from .env or environment variables)
ALPACA_API_KEY = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
ALPACA_BASE_URL = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")

# Target settings
TARGET_SYMBOL = "QQQM"
CRASH_THRESHOLD = 3.0

# Discord Webhook URL (from .env or environment variable)
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")

