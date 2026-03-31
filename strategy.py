"""High-level strategy: maker placement and the main trading loop."""
from __future__ import annotations

import logging
import math
import time
from typing import Optional

from py_clob_client.clob_types import BalanceAllowanceParams, AssetType, BookParams

from models import MarketToken, OrderSide
from manager import TradingManager

logger = logging.getLogger(__name__)


# ── Maker placement ────────────────────────────────────────────────────────────

def try_place_new_makers(
    manager: TradingManager,
    event,
    client,
    cash_usdc: float = 0.0,
) -> None:
    """Evaluate a neg-risk event and post limit BUY orders on NO tokens."""
    no_token_ids = event.get_all_no_tokens()
    if not no_token_ids:
        return

    book_params = [BookParams(token_id=tid) for tid in no_token_ids]
    try:
        books = client.get_order_books(params=book_params)
    except Exception as exc:
        logger.warning("[strategy] Order books fetch failed for '%s': %s", event.title, exc)
        return

    if len(books) != len(no_token_ids):
        return

    # ── Pass 1: compute aggregate stats and refresh token snapshots ────────────
    price_sum = 0.0
    min_ask_size = float("inf")
    min_ask_price = 1.0
    valid_books = []

    for book in books:
        if not book.asks:
            return  # any missing book aborts the whole event
        ask = book.asks[-1]
        p = float(ask.price)
        s = float(ask.size)
        best_bid = float(book.bids[-1].price) if book.bids else 0.0

        price_sum += p
        min_ask_size = min(min_ask_size, s)
        min_ask_price = min(min_ask_price, p)

        # Refresh token snapshot
        if book.asset_id not in manager.tokens:
            market = manager.get_market_by_token(book.asset_id)
            cid = market.market_id if market else ""
            manager.tokens[book.asset_id] = MarketToken(
                condition_id=cid, token_id=book.asset_id, token_type="NO",
                best_bid=best_bid, best_ask=p,
            )
        else:
            token = manager.tokens[book.asset_id]
            token.best_ask = p
            token.best_bid = best_bid

        valid_books.append(book)

    # Guard: skip events with illiquid or tiny markets
    if min_ask_size < 5 or min_ask_price * min_ask_size < 1:
        return

    # ── Pass 2: place a maker order per token ─────────────────────────────────
    for book in valid_books:
        market = manager.get_market_by_token(book.asset_id)
        if not market:
            continue

        min_tick = market.min_tick
        initial_ask = float(book.asks[-1].price)
        initial_bid = float(book.bids[-1].price) if book.bids else 0.0

        # Theoretical fair-value bid for this leg
        theoretical = len(no_token_ids) - 1 - (price_sum - initial_ask) - min_tick - 0.001

        spread = initial_ask - initial_bid
        blended_bid = initial_bid + 0.4 * spread
        our_bid = min(theoretical, blended_bid)
        our_bid = math.floor(our_bid / min_tick) * min_tick

        edge = theoretical - our_bid
        target_size = min(min_ask_size, cash_usdc / len(no_token_ids), 60)

        if target_size < 5:
            logger.debug("[strategy] Skipping %s — target_size too small (%.2f)", book.asset_id, target_size)
            continue

        if our_bid >= initial_bid - 1e-6:
            placed = manager.place_order(
                token_id=book.asset_id,
                price=our_bid,
                size=target_size,
                side=OrderSide.BUY,
                initial_market_bid=initial_bid,
                initial_market_ask=initial_ask,
                client=client,
            )
            if placed:
                manager.mark_event_interesting(event.event_id)
                logger.info(
                    "[strategy] Placed maker in '%s' @ %.4f | size=%.2f | edge=%.4f | spread=%.4f",
                    event.title, our_bid, target_size, edge, spread,
                )


# ── Main trading loop ──────────────────────────────────────────────────────────

def market_making() -> None:
    from config import client, api_creds
    from api import get_YES_NO_And_Condition

    manager = TradingManager(
        client=client,
        api_key=api_creds.api_key,
        api_secret=api_creds.api_secret,
        api_passphrase=api_creds.api_passphrase,
    )
    manager.register_signal_handler()

    raw_data = get_YES_NO_And_Condition()
    manager.process_api_response(raw_data)
    manager.last_market_refresh = time.time()

    cash_usdc: float = 0.0
    iteration = 0

    while manager.ws_running:
        iteration += 1
        now_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        logger.info("Iteration %d — %s — Active events: %d", iteration, now_str, len(manager.active_events))

        # WS heartbeat check
        if time.time() - manager.last_ws_message_time > 120:
            logger.warning("[loop] No WS messages for 2 min → reconnecting")
            from ws_manager import stop_user_websocket, start_user_websocket
            stop_user_websocket(manager)
            time.sleep(1)
            start_user_websocket(manager)

        # Periodic market data refresh
        if manager.should_refresh_markets():
            manager.refresh_markets()

        # ── A. Periodic scan for NEW opportunities ─────────────────────────────
        if manager.should_do_full_scan():
            logger.info(
                "[loop] Scanning for new makers (excluding %d active events)",
                len(manager.active_events),
            )
            active_snapshot = manager.active_events.copy()
            candidates = [e for eid, e in manager.events.items() if eid not in active_snapshot]

            try:
                collateral = client.get_balance_allowance(
                    params=BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
                )
                cash_usdc = float(collateral["balance"]) / 4_000_000
            except Exception as exc:
                logger.warning("[loop] Balance fetch failed: %s", exc)
                cash_usdc = 0.0

            for event in candidates:
                try_place_new_makers(manager, event, client, cash_usdc=cash_usdc)
                time.sleep(0.1)

        # ── B. Maintain live positions ─────────────────────────────────────────
        if not manager.active_events:
            logger.info("[loop] No active orders → sleeping 10 s")
            time.sleep(10)
            continue

        logger.info("[loop] Checking %d events with live positions…", len(manager.active_events))

        for event_id in list(manager.active_events):
            event = manager.events.get(event_id)
            if not event:
                manager.active_events.discard(event_id)
                continue

            if not manager.should_check_event_now(event_id, min_seconds_between=10):
                logger.debug("[loop] Skipping %s — checked too recently", event_id)
                continue

            try_place_new_makers(manager, event, client, cash_usdc=cash_usdc)

        manager.cleanup_expired_orders()

        sleep_sec = 5 if manager.active_events else 10
        logger.info("[loop] Sleeping %d s…", sleep_sec)
        time.sleep(sleep_sec)