import os
import logging
from dotenv import load_dotenv
from py_clob_client_v2.constants import POLYGON
from py_clob_client_v2.client import ClobClient

load_dotenv()

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# ── Secrets ────────────────────────────────────────────────────────────────────
PRIVATE_KEY: str = os.getenv("PRIVATE_KEY", "")
FUNDER: str = os.getenv("funder", "")
TG_TOKEN: str = os.getenv("tg_token", "")

if not PRIVATE_KEY:
    raise EnvironmentError("PRIVATE_KEY is not set in .env")

# ── Polymarket ─────────────────────────────────────────────────────────────────
HOST: str = "https://clob.polymarket.com"
CHAIN_ID = POLYGON

# ── Telegram ───────────────────────────────────────────────────────────────────
CHAT_ID: str = "-1002321264842"

# ── Strategy hyperparameters ───────────────────────────────────────────────────
FETCH_COUNT: int = 500
MAX_MARKETS: int = 3000

# ── Hedging / re-quote tunables ────────────────────────────────────────────────
HEDGE_SLIPPAGE_TICKS: int = 2             # FAK price buffer above best ask (in ticks)
HEDGE_RETRY_SLIPPAGE_TICKS: int = 6       # wider buffer on retry passes
HEDGE_MAX_RETRIES: int = 2                # extra FAK passes after the first
HEDGE_MIN_SHARES: float = 5.0             # smallest hedge deficit worth an order
ORDER_EXPIRY_GRACE_SECONDS: float = 15.0  # delay before dropping an expired order
REQUOTE_MIN_SECONDS: float = 3.0          # debounce window for WS-triggered re-quotes
REQUOTE_EDGE_THRESHOLD: float = 0.02      # re-quote if a resting bid is this far below fair value

# ── Build the shared client + API creds ───────────────────────────────────────
client = ClobClient(HOST, key=PRIVATE_KEY, chain_id=CHAIN_ID, signature_type=1, funder=FUNDER)
api_creds = client.create_or_derive_api_key()
client.set_api_creds(api_creds)

logger.info("Polymarket client initialised")