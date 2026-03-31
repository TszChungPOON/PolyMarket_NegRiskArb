"""Entry point — run with:  python main.py"""
import logging

from strategy import market_making

logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)


if __name__ == "__main__":
    logger.info("Starting Polymarket market-making bot…")
    market_making()