"""WebSocket helpers for the Polymarket user and market channels.

Both managers are *mixed into* TradingManager via composition — they receive
a back-reference to the manager so they can update shared state without
creating circular imports.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from typing import TYPE_CHECKING

import websocket

if TYPE_CHECKING:
    from manager import TradingManager

logger = logging.getLogger(__name__)

WSS_USER_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/user"
WSS_MARKET_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


# ── User WebSocket ─────────────────────────────────────────────────────────────

def start_user_websocket(mgr: TradingManager) -> None:
    """Start the authenticated user WebSocket in a daemon thread."""

    def on_message(ws, message: str):
        mgr.last_ws_message_time = time.time()
        if not message.strip():
            return
        if message == "PONG":
            return
        try:
            data = json.loads(message)
            _process_user_message(mgr, data)
        except json.JSONDecodeError as exc:
            logger.warning("[UserWS] JSON decode failed: %s | message: %s", exc, message)
        except Exception as exc:
            logger.error("[UserWS] Processing error: %s", exc)

    def on_error(ws, error):
        logger.error("[UserWS] Error: %s", error)

    def on_close(ws, code, msg):
        logger.info("[UserWS] Closed: %s - %s", code, msg)
        mgr.ws_running = False

    def on_open(ws):
        logger.info("[UserWS] Connected → subscribing to user channel")
        auth = {
            "apiKey": mgr.api_key,
            "secret": mgr.api_secret,
            "passphrase": mgr.api_passphrase,
        }
        ws.send(json.dumps({"markets": [], "type": "user", "auth": auth}))

        def ping_loop():
            while mgr.ws_running:
                try:
                    ws.send("PING")
                except Exception as exc:
                    logger.warning("[UserWS] Ping failed: %s", exc)
                    break
                time.sleep(10)

        threading.Thread(target=ping_loop, daemon=True).start()
        logger.info("[UserWS] Ping thread started")

    def run():
        while mgr.ws_running:
            try:
                mgr.ws = websocket.WebSocketApp(
                    WSS_USER_URL,
                    on_open=on_open,
                    on_message=on_message,
                    on_error=on_error,
                    on_close=on_close,
                )
                mgr.ws.run_forever(ping_interval=30, ping_timeout=10)
            except Exception as exc:
                logger.warning("[UserWS] Reconnecting in 5 s: %s", exc)
                time.sleep(5)

    mgr.ws_running = True
    mgr.ws_thread = threading.Thread(target=run, daemon=True)
    mgr.ws_thread.start()
    logger.info("[UserWS] Thread started")


def stop_user_websocket(mgr: TradingManager) -> None:
    mgr.ws_running = False
    if mgr.ws:
        mgr.ws.close()


# ── Market WebSocket ───────────────────────────────────────────────────────────

def start_market_websocket(mgr: TradingManager) -> None:
    """Subscribe to the market channel for price_change updates."""

    def on_message(ws, message: str):
        mgr.last_ws_message_time = time.time()
        if message == "PONG":
            return
        try:
            data = json.loads(message)
            _process_market_message(mgr, data)
        except json.JSONDecodeError as exc:
            logger.warning("[MarketWS] JSON decode failed: %s | message: %s", exc, message)
        except Exception as exc:
            logger.error("[MarketWS] Processing error: %s", exc)
            

    def on_error(ws, error):
        logger.error("[MarketWS] Error: %s", error)

    def on_close(ws, code, msg):
        logger.info("[MarketWS] Closed: %s - %s", code, msg)
        mgr.market_ws_running = False

    def on_open(ws):
        logger.info("[MarketWS] Connected → re-subscribing to active tokens")
        token_ids = []
        for event_id in mgr.active_events:
            event = mgr.events.get(event_id)
            if event:
                token_ids.extend(event.get_all_no_tokens())
        if token_ids:
            ws.send(json.dumps({"assets_ids": token_ids, "operation": "subscribe"}))
            logger.info("[MarketWS] Re-subscribed to %d tokens", len(token_ids))
        else:
            logger.info("[MarketWS] No active tokens on connect")

    def run():
        while mgr.market_ws_running:
            try:
                mgr.market_ws = websocket.WebSocketApp(
                    WSS_MARKET_URL,
                    on_open=on_open,
                    on_message=on_message,
                    on_error=on_error,
                    on_close=on_close,
                )
                mgr.market_ws.run_forever(ping_interval=30, ping_timeout=10)
            except Exception as exc:
                logger.warning("[MarketWS] Reconnecting in 5 s: %s", exc)
                time.sleep(5)

    mgr.market_ws_running = True
    mgr.market_ws_thread = threading.Thread(target=run, daemon=True)
    mgr.market_ws_thread.start()
    logger.info("[MarketWS] Thread started")


def subscribe_tokens(mgr: TradingManager, token_ids: list) -> None:
    _send_subscription(mgr, token_ids, "subscribe")


def unsubscribe_tokens(mgr: TradingManager, token_ids: list) -> None:
    _send_subscription(mgr, token_ids, "unsubscribe")


def _send_subscription(mgr: TradingManager, token_ids: list, operation: str) -> None:
    if not mgr.market_ws:
        return
    try:
        mgr.market_ws.send(json.dumps({"assets_ids": token_ids, "operation": operation}))
        logger.info("[MarketWS] %s %d tokens", operation, len(token_ids))
    except Exception as exc:
        logger.warning("[MarketWS] %s failed: %s", operation, exc)


# ── Message processors ─────────────────────────────────────────────────────────

def _process_user_message(mgr: TradingManager, data: dict) -> None:
    """Handle a user-channel message: order updates and trade fills."""
    from orders import hedge_event  # local import to avoid circular dependency

    event_type = data.get("event_type")

    with mgr.lock:
        if event_type == "order":
            order_id = data.get("id")
            token_id = mgr.order_id_mapping.get(order_id)
            if not token_id or token_id not in mgr.active_orders:
                return

            order = mgr.active_orders[token_id]
            order.matched_amount = float(data.get("size_matched", 0) or 0)
            order.last_checked = time.time()
            status_type = data.get("type")

            if status_type == "CANCELLATION":
                order.status = mgr._OrderStatus_CANCELLED()
                logger.info("[UserWS] Order %s CANCELLED for token %s", order_id, token_id)
                mgr.order_id_mapping.pop(order_id, None)
            elif status_type == "UPDATE" and order.matched_amount > 0:
                logger.info("[UserWS] Partial fill %s / %s for %s",
                            order.matched_amount, order.size, token_id)
                event = mgr.events.get(order.event_id)
                if event:
                    hedge_event(mgr, event, mgr.client)
            order.raw_response = data

        elif event_type == "trade":
            status = data.get("status")
            for mo in data.get("maker_orders", []):
                order_id = mo.get("order_id")
                token_id = mgr.order_id_mapping.get(order_id)
                if not token_id or token_id not in mgr.active_orders:
                    continue

                order = mgr.active_orders[token_id]
                matched = float(mo.get("matched_amount", 0) or 0)
                order.matched_amount = (order.matched_amount or 0) + matched
                order.last_checked = time.time()

                if status in ("MATCHED", "MINED", "CONFIRMED"):
                    logger.warning("[UserWS] Trade %s for order %s — matched %s",
                                   status, order_id, matched)
                    event = mgr.events.get(order.event_id)
                    if event:
                        hedge_event(mgr, event, mgr.client)
                    else:
                        logger.warning("[UserWS] Trade triggered but no active event associated")
                elif status in ("FAILED", "RETRYING"):
                    logger.warning("[UserWS] Trade issue (%s) for token %s", status, token_id)
                    event = mgr.events.get(order.event_id)
                    from api import send_telegram
                    from config import TG_TOKEN, CHAT_ID
                    send_telegram(
                        TG_TOKEN, CHAT_ID,
                        f"Trade {status} for order {order_id} in event "
                        f"{event.title if event else 'unknown'}"
                    )
                    # On terminal failure, reconcile: cancel makers, re-poll, re-hedge.
                    if status == "FAILED" and event:
                        hedge_event(mgr, event, mgr.client)


def _process_market_message(mgr: TradingManager, data) -> None:
    """Handle market-channel messages: refresh price snapshots, re-quote on moves."""
    from strategy import try_place_new_makers
    from orders import cancel_orders
    from config import REQUOTE_MIN_SECONDS

    if not isinstance(data, (dict, list)):
        return  # heartbeat / unexpected

    messages = data if isinstance(data, list) else [data]

    with mgr.lock:
        for msg in messages:
            event_type = msg.get("event_type")
            if not event_type:
                continue

            if event_type == "price_change":
                # Refresh every touched NO token's snapshot so quotes never go
                # stale, then decide re-quotes once per event (deduped + debounced).
                events_to_check: set = set()
                for price_update in msg.get("price_changes", []):
                    token_id = price_update.get("asset_id")
                    if not token_id:
                        continue
                    token_obj = mgr.tokens.get(token_id)
                    if not token_obj:
                        continue
                    _apply_price_update(token_obj, price_update)
                    if token_obj.token_type != "NO":
                        continue
                    event = mgr.get_event_by_token(token_id)
                    if not event:
                        continue
                    if any(o.event_id == event.event_id for o in mgr.active_orders.values()):
                        events_to_check.add(event.event_id)

                for event_id in events_to_check:
                    event = mgr.events.get(event_id)
                    if not event:
                        continue
                    if _event_needs_requote(mgr, event) and \
                            mgr.should_check_event_now(event_id, REQUOTE_MIN_SECONDS):
                        logger.info("[MarketWS] Re-quoting '%s' — cross-market price moved",
                                    event.title)
                        cancel_orders(mgr, event.get_all_no_tokens(), mgr.client)
                        try_place_new_makers(mgr, event, mgr.client, cash_usdc=mgr.cash_usdc)

            elif event_type == "book":
                pass  # price_change is the live feed; book snapshots are redundant

            elif event_type in ("subscribed", "unsubscribed"):
                logger.info("[MarketWS] Subscription ack: %s", msg)

            elif event_type == "last_trade_price":
                pass  # intentionally ignored

            else:
                logger.debug("[MarketWS] Unknown event_type '%s'", event_type)


def _apply_price_update(token_obj, price_update: dict) -> None:
    """Refresh a token's best bid/ask from a price_change update."""
    ba = price_update.get("best_ask")
    bb = price_update.get("best_bid")
    if ba is not None:
        try:
            token_obj.best_ask = float(ba)
        except (TypeError, ValueError):
            pass
    if bb is not None:
        try:
            token_obj.best_bid = float(bb)
        except (TypeError, ValueError):
            pass


def _event_needs_requote(mgr: TradingManager, event) -> bool:
    """True if any resting maker quote is now mispriced against live sibling asks.

    For neg-risk leg A the fair maker bid is
        theoretical_A = (N-1) - Σ(best_ask_j, j≠A) - min_tick - 0.001
    so when any *other* leg's ask moves, A's fair value moves too. Re-quote if a
    resting bid is now above fair value (would fill at a loss — risk) or well
    below it (leaving edge on the table — opportunity).
    """
    from config import REQUOTE_EDGE_THRESHOLD

    no_ids = event.get_all_no_tokens()
    n = len(no_ids)
    if n < 2:
        return False

    asks: dict = {}
    for tid in no_ids:
        tok = mgr.tokens.get(tid)
        if not tok or tok.best_ask is None:
            return False  # incomplete data — don't act on a partial picture
        asks[tid] = tok.best_ask
    ask_sum = sum(asks.values())

    for tid in no_ids:
        order = mgr.active_orders.get(tid)
        if not order:
            continue
        market = mgr.get_market_by_token(tid)
        min_tick = market.min_tick if market else 0.001
        theoretical = (n - 1) - (ask_sum - asks[tid]) - min_tick - 0.001
        if order.price > theoretical + 1e-6:
            logger.info("[MarketWS] '%s' leg now unprofitable: bid %.4f > fair %.4f",
                        event.title, order.price, theoretical)
            return True
        if order.price < theoretical - REQUOTE_EDGE_THRESHOLD:
            logger.info("[MarketWS] '%s' leg too conservative: bid %.4f << fair %.4f",
                        event.title, order.price, theoretical)
            return True
    return False