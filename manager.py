"""TradingManager — holds all shared state and exposes high-level helpers.

Heavy logic (order placement, cancellation, hedging, websockets) lives in
orders.py and websocket.py to keep this file focused on state management.
"""
from __future__ import annotations

import logging
import signal
import sys
import time
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from models import (
    Event, Market, MarketRewards, MarketToken,
    OrderRecord, OrderSide, OrderStatus,
)

logger = logging.getLogger(__name__)


class TradingManager:
    # ── Construction ───────────────────────────────────────────────────────────

    def __init__(self, client=None, api_key="", api_secret="", api_passphrase=""):
        # State registries
        self.events: Dict[str, Event] = {}
        self.markets: Dict[str, Market] = {}
        self.tokens: Dict[str, MarketToken] = {}
        self.active_orders: Dict[str, OrderRecord] = {}    # token_id -> OrderRecord
        self.order_id_mapping: Dict[str, str] = {}         # order_id -> token_id
        self.positions: Dict[str, Dict] = {}               # token_id -> position info

        # Active-event tracking
        self.active_events: Set[str] = set()
        self.recently_checked_events: Dict[str, float] = {}

        # Timing
        self.last_full_scan: float = 0.0
        self.FULL_SCAN_INTERVAL_SECONDS: float = 60.0
        self.last_market_refresh: float = 0.0
        self.MARKET_REFRESH_INTERVAL_SECONDS: float = 3600.0

        # Client + credentials
        self.client = client
        self.api_key = api_key
        self.api_secret = api_secret
        self.api_passphrase = api_passphrase

        # WebSocket handles (populated by websocket.py)
        self.ws = None
        self.ws_thread = None
        self.ws_running: bool = False
        self.market_ws = None
        self.market_ws_thread = None
        self.market_ws_running: bool = False
        self.last_ws_message_time: float = time.time()

        # Start WebSockets if credentials are available
        if self.api_key and self.api_secret and self.api_passphrase:
            import websocket as ws_module  # noqa: F401 — ensure library present
            from ws_manager import start_user_websocket, start_market_websocket
            start_user_websocket(self)
            start_market_websocket(self)

    # ── Convenience shim used in websocket.py ──────────────────────────────────
    def _OrderStatus_CANCELLED(self):
        return OrderStatus.CANCELLED

    # ── Market data loading ────────────────────────────────────────────────────

    def process_api_response(self, api_data: list) -> None:
        logger.info("[manager] Processing API response (%d events)…", len(api_data))

        for event_data in api_data:
            title = event_data[0]
            yes_ids, no_ids, condition_ids = event_data[1], event_data[2], event_data[3]
            daily_rates, min_sizes, max_spreads, min_ticks = (
                event_data[4], event_data[5], event_data[6], event_data[7]
            )

            if not condition_ids:
                continue

            event_id = title
            markets: Dict[str, Market] = {}

            for i, (cid, yid, nid, rate, minsz, maxsp, mintick) in enumerate(
                zip(condition_ids, yes_ids, no_ids, daily_rates, min_sizes, max_spreads, min_ticks)
            ):
                yes_token = MarketToken(condition_id=cid, token_id=yid, token_type="YES")
                no_token = MarketToken(condition_id=cid, token_id=nid, token_type="NO")
                rewards = MarketRewards(rate, minsz, maxsp)
                mtitle = f"{title} - Market {i+1}" if len(condition_ids) > 1 else title
                market = Market(
                    market_id=cid, title=mtitle, condition_id=cid,
                    yes_token=yes_token, no_token=no_token,
                    rewards=rewards, min_tick=mintick,
                )
                markets[cid] = market
                self.markets[cid] = market
                self.tokens[yid] = yes_token
                self.tokens[nid] = no_token

            self.events[event_id] = Event(event_id=event_id, title=title, markets=markets)

        logger.info(
            "[manager] Loaded %d events, %d markets, %d tokens",
            len(self.events), len(self.markets), len(self.tokens),
        )

    def should_refresh_markets(self) -> bool:
        now = time.time()
        if now - self.last_market_refresh >= self.MARKET_REFRESH_INTERVAL_SECONDS:
            self.last_market_refresh = now
            return True
        return False

    def refresh_markets(self, fetch_count: int = 500) -> None:
        logger.info("[manager] Refreshing market data…")
        from api import get_YES_NO_And_Condition
        from orders import cancel_orders

        try:
            raw_data = get_YES_NO_And_Condition(fetch_count=fetch_count)
        except Exception as exc:
            logger.error("[manager] Fetch failed: %s", exc)
            return

        incoming_ids = {d[0] for d in raw_data if d[3]}
        closed_ids = set(self.events.keys()) - incoming_ids

        for event_id in closed_ids:
            event = self.events.get(event_id)
            if not event:
                continue
            active_tokens = [
                tid for tid, o in self.active_orders.items() if o.event_id == event_id
            ]
            if active_tokens:
                logger.info("[manager] Closing event '%s' — cancelling %d orders", event_id, len(active_tokens))
                cancel_orders(self, active_tokens, self.client)
            del self.events[event_id]
            self.active_events.discard(event_id)
            logger.info("[manager] Removed closed event '%s'", event_id)

        self.process_api_response(raw_data)
        logger.info(
            "[manager] Refresh done — %d events, %d markets, %d tokens",
            len(self.events), len(self.markets), len(self.tokens),
        )

    # ── Look-ups ───────────────────────────────────────────────────────────────

    def get_market_by_token(self, token_id: str) -> Optional[Market]:
        token = self.tokens.get(token_id)
        return self.markets.get(token.condition_id) if token else None

    def get_event_by_token(self, token_id: str) -> Optional[Event]:
        market = self.get_market_by_token(token_id)
        if market:
            for event in self.events.values():
                if market.market_id in event.markets:
                    return event
        return None

    def get_event_name_by_token(self, token_id: str) -> Optional[str]:
        event = self.get_event_by_token(token_id)
        return event.title if event else None

    def get_token_info(self, token_id: str) -> Optional[Tuple[MarketToken, Market, Event]]:
        token = self.tokens.get(token_id)
        if not token:
            return None
        market = self.get_market_by_token(token_id)
        if not market:
            return None
        event = self.get_event_by_token(token_id)
        return (token, market, event) if event else None

    # ── Active-event helpers ───────────────────────────────────────────────────

    def mark_event_interesting(self, event_id: str) -> None:
        self.active_events.add(event_id)

    def check_and_deactivate_event(self, event_id: str) -> None:
        has_orders = any(o.event_id == event_id for o in self.active_orders.values())
        if not has_orders:
            self.active_events.discard(event_id)
            self.recently_checked_events.pop(event_id, None)
            logger.info("[manager] Event '%s' deactivated — no remaining orders", event_id)
            event = self.events.get(event_id)
            if event and self.market_ws and self.market_ws_running:
                from ws_manager import unsubscribe_tokens
                unsubscribe_tokens(self, event.get_all_no_tokens())

    def should_do_full_scan(self) -> bool:
        now = time.time()
        if now - self.last_full_scan >= self.FULL_SCAN_INTERVAL_SECONDS:
            self.last_full_scan = now
            return True
        return False

    def should_check_event_now(self, event_id: str, min_seconds_between: float = 30) -> bool:
        now = time.time()
        last = self.recently_checked_events.get(event_id)
        if last is None or now - last >= min_seconds_between:
            self.recently_checked_events[event_id] = now
            return True
        return False

    # ── Convenience delegators (thin wrappers for callers that hold a manager) ─

    def place_order(self, token_id, price, size, side, initial_market_bid, initial_market_ask, client):
        from orders import place_order
        return place_order(self, token_id, price, size, side, initial_market_bid, initial_market_ask, client)

    def cancel_order(self, token_id, client):
        from orders import cancel_order
        cancel_order(self, token_id, client)

    def cancel_orders(self, token_ids, client):
        from orders import cancel_orders
        cancel_orders(self, token_ids, client)

    def hedging(self, event, client):
        from orders import hedge_event
        return hedge_event(self, event, client)

    # ── Position tracking ──────────────────────────────────────────────────────

    def update_position(self, token_id: str, shares: float, cost: float) -> None:
        pos = self.positions.setdefault(token_id, {"shares": 0.0, "avg_cost": 0.0, "total_cost": 0.0})
        pos["shares"] += shares
        pos["total_cost"] += cost
        pos["avg_cost"] = pos["total_cost"] / pos["shares"] if pos["shares"] else 0.0
        logger.info(
            "[position] %s: %.2f shares @ avg %.4f | total cost %.2f",
            token_id, pos["shares"], pos["avg_cost"], pos["total_cost"],
        )

    def get_position(self, token_id: str) -> float:
        return self.positions.get(token_id, {}).get("shares", 0.0)

    def clear_position(self, token_id: str) -> None:
        if token_id in self.positions:
            del self.positions[token_id]
            logger.info("[position] Cleared position for %s", token_id)

    def get_event_pnl(self, event: Event) -> Dict:
        total_cost = 0.0
        total_shares = 0.0
        legs: Dict = {}
        for market in event.markets.values():
            tid = market.no_token.token_id
            pos = self.positions.get(tid, {})
            shares = pos.get("shares", 0.0)
            cost = pos.get("total_cost", 0.0)
            total_cost += cost
            total_shares = max(total_shares, shares)
            legs[tid] = {"shares": shares, "cost": cost}
        n = len(event.markets)
        profit = total_shares * (n - 1) - total_cost
        return {
            "event": event.title,
            "total_cost": round(total_cost, 4),
            "max_shares": round(total_shares, 4),
            "theoretical_profit": round(profit, 4),
            "legs": legs,
        }

    def get_price(self, token_id: str) -> float:
        from py_clob_client.clob_types import BookParams
        book_param = BookParams(token_id=token_id)
        books = self.client.get_order_books(params=[book_param])
        return float(books[0].asks[-1].price) if books and books[0].asks else 0.999

    # ── Order clean-up ─────────────────────────────────────────────────────────

    def cleanup_expired_orders(self) -> None:
        expired = [tid for tid, o in self.active_orders.items() if o.is_expired()]
        event_ids: Set[str] = set()
        for tid in expired:
            order = self.active_orders[tid]
            event_ids.add(order.event_id)
            self.order_id_mapping.pop(order.order_id, None)
            del self.active_orders[tid]
            logger.info("[manager] Cleaned up expired order %s", order.order_id)
        for eid in event_ids:
            self.check_and_deactivate_event(eid)

    def cleanup_expired_orders_of_event(self, event: Event) -> None:
        expired = [
            tid for tid, o in self.active_orders.items()
            if o.is_expired() and o.event_id == event.event_id
        ]
        event_ids: Set[str] = set()
        for tid in expired:
            order = self.active_orders[tid]
            event_ids.add(order.event_id)
            self.order_id_mapping.pop(order.order_id, None)
            del self.active_orders[tid]
            logger.info("[manager] Cleaned up expired order %s", order.order_id)
        for eid in event_ids:
            self.check_and_deactivate_event(eid)

    # ── Shutdown ───────────────────────────────────────────────────────────────

    def shutdown(self) -> None:
        logger.info("[manager] Graceful shutdown starting…")
        all_tokens = list(self.active_orders.keys())
        if all_tokens:
            logger.info("[manager] Cancelling %d active orders…", len(all_tokens))
            from orders import cancel_orders
            cancel_orders(self, all_tokens, self.client)

        from ws_manager import stop_user_websocket
        stop_user_websocket(self)

        self.market_ws_running = False
        if self.market_ws:
            self.market_ws.close()

        logger.info("[manager] Goodbye!")
        sys.exit(0)

    def register_signal_handler(self) -> None:
        def handler(sig, frame):
            logger.info("[manager] SIGINT received")
            self.shutdown()
        signal.signal(signal.SIGINT, handler)
        logger.info("[manager] Signal handler registered — Ctrl+C to stop gracefully")

    # ── Query helpers ──────────────────────────────────────────────────────────

    def get_orders_by_event(self, event_id: str) -> List[OrderRecord]:
        return [o for o in self.active_orders.values() if o.event_id == event_id]

    def get_orders_by_market(self, market_id: str) -> List[OrderRecord]:
        return [o for o in self.active_orders.values() if o.market_id == market_id]

    def get_orders_by_token(self, token_id: str) -> List[OrderRecord]:
        order = self.active_orders.get(token_id)
        return [order] if order else []

    def get_all_no_tokens(self) -> List[MarketToken]:
        return [t for t in self.tokens.values() if t.token_type == "NO"]

    def get_all_yes_tokens(self) -> List[MarketToken]:
        return [t for t in self.tokens.values() if t.token_type == "YES"]

    def get_opposite_token(self, token_id: str) -> Optional[MarketToken]:
        market = self.get_market_by_token(token_id)
        return market.get_other_token(token_id) if market else None

    def filter_events(self, filter_func: Callable[[Event], bool]) -> List[Event]:
        return [e for e in self.events.values() if filter_func(e)]