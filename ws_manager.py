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
        try:
            data = json.loads(message)
            _process_user_message(mgr, data)
        except json.JSONDecodeError as exc:
            logger.warning("[UserWS] JSON decode failed: %s", exc)
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
        try:
            data = json.loads(message)
            _process_market_message(mgr, data)
        except Exception as exc:
            logger.error("[MarketWS] Error processing: %s", exc)

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
    from orders import hedge_event  # local import to avoid circular dependency

    event_type = data.get("event_type")

    if event_type == "order":
        order_id = data.get("id")
        token_id = mgr.order_id_mapping.get(order_id)
        if not token_id or token_id not in mgr.active_orders:
            return

        order = mgr.active_orders[token_id]
        order.matched_amount = float(data.get("size_matched", 0))
        order.last_checked = time.time()
        status_type = data.get("type")

        if status_type == "CANCELLATION":
            order.status = mgr._OrderStatus_CANCELLED()
            logger.info("[UserWS] Order %s CANCELLED for token %s", order_id, token_id)
            mgr.order_id_mapping.pop(order_id, None)
        elif status_type == "UPDATE" and order.matched_amount > 0:
            logger.info("[UserWS] Partial fill %s / %s for %s", order.matched_amount, order.size, token_id)
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
            matched = float(mo.get("matched_amount", 0))
            order.matched_amount = (order.matched_amount or 0) + matched
            order.last_checked = time.time()

            if status in ("MATCHED", "MINED", "CONFIRMED"):
                logger.info("[UserWS] Trade %s for order %s — matched %s", status, order_id, matched)
                event = mgr.events.get(order.event_id)
                if event:
                    hedge_event(mgr, event, mgr.client)
            elif status in ("FAILED", "RETRYING"):
                logger.warning("[UserWS] Trade issue (%s) for token %s", status, token_id)
                event = mgr.events.get(order.event_id)
                from api import send_telegram
                from config import TG_TOKEN, CHAT_ID
                send_telegram(
                    TG_TOKEN, CHAT_ID,
                    f"Trade Failed for order {order_id} in event {event.title if event else 'unknown'}"
                )


def _process_market_message(mgr: TradingManager, data) -> None:
    from strategy import try_place_new_makers

    if not isinstance(data, (dict, list)):
        return  # heartbeat / unexpected

    messages = data if isinstance(data, list) else [data]

    for msg in messages:
        event_type = msg.get("event_type")
        if not event_type:
            continue

        if event_type == "price_change":
            for price_update in msg.get("price_changes", []):
                token_id = price_update.get("asset_id")
                if not token_id:
                    continue

                token_obj = mgr.tokens.get(token_id)
                if not token_obj or token_obj.token_type != "NO":
                    continue

                event = mgr.get_event_by_token(token_id)
                if not event:
                    continue

                has_active = any(o.event_id == event.event_id for o in mgr.active_orders.values())
                if not has_active:
                    continue

                new_best_ask = float(price_update["best_ask"])

                if token_id in mgr.active_orders:
                    initial_ask = mgr.active_orders[token_id].initial_market_ask
                else:
                    initial_ask = token_obj.best_ask

                if initial_ask is not None and new_best_ask > initial_ask + 0.001:
                    logger.info(
                        "[MarketWS] ASK WORSENED on %s: %.4f → %.4f",
                        token_id, initial_ask, new_best_ask,
                    )
                    from orders import cancel_orders
                    cancel_orders(mgr, event.get_all_no_tokens(), mgr.client)
                    try_place_new_makers(mgr, event, mgr.client, cash_usdc=1600)
                    if token_id in mgr.tokens:
                        mgr.tokens[token_id].best_ask = new_best_ask

        elif event_type == "book":
            token_id = msg.get("asset_id")
            if token_id in mgr.active_orders:
                size = msg.get("asks", [{}])[0].get("size", "N/A")
                logger.debug("[MarketWS] Book update for %s — best ask size: %s", token_id, size)

        elif event_type in ("subscribed", "unsubscribed"):
            logger.info("[MarketWS] Subscription ack: %s", msg)

        elif event_type == "last_trade_price":
            pass  # intentionally ignored

        else:
            logger.debug("[MarketWS] Unknown event_type '%s'", event_type)