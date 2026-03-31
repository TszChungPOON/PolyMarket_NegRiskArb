"""Order placement, cancellation, and hedging logic.

All functions receive `mgr: TradingManager` as their first argument so they
can read/write shared state without circular imports.
"""
from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, List, Optional

from py_clob_client.clob_types import OrderArgs, PostOrdersArgs, OrderType, BookParams

from models import MarketToken, OrderRecord, OrderSide, OrderStatus

if TYPE_CHECKING:
    from manager import TradingManager
    from models import Event

logger = logging.getLogger(__name__)


# ── Place ──────────────────────────────────────────────────────────────────────

def place_order(
    mgr: TradingManager,
    token_id: str,
    price: float,
    size: float,
    side: OrderSide,
    initial_market_bid: float,
    initial_market_ask: float,
    client,
) -> Optional[OrderRecord]:
    """Sign and post a single limit order; register it in the manager."""
    token_info = mgr.get_token_info(token_id)
    if not token_info:
        logger.error("[orders] Token %s not found in manager", token_id)
        return None

    token, market, event = token_info

    # Cancel any existing order for this token
    if token_id in mgr.active_orders:
        logger.info("[orders] Cancelling existing order for token %s", token_id)
        cancel_order(mgr, token_id, client)

    order_args = OrderArgs(
        price=price,
        size=size,
        side=side.value,
        token_id=token_id,
        expiration=int(time.time()) + 300,
    )

    try:
        signed = client.create_order(order_args)
        resp = client.post_order(signed, OrderType.GTD)
    except Exception as exc:
        logger.error("[orders] Error placing order for token %s: %s", token_id, exc)
        return None

    if not resp.get("success", False):
        logger.error("[orders] Order failed: %s", resp.get("errorMsg", "unknown"))
        return None

    record = OrderRecord(
        order_id=resp["orderID"],
        asset_id=token_id,
        market_id=market.market_id,
        event_id=event.event_id,
        price=price,
        size=size,
        initial_market_bid=initial_market_bid,
        initial_market_ask=initial_market_ask,
        side=side,
        token_type=token.token_type,
        created_at=time.time(),
        expiration=order_args.expiration,
        status=OrderStatus.MATCHED if resp.get("status") == "matched" else OrderStatus.PENDING,
        matched_amount=float(resp["takingAmount"]) if resp.get("takingAmount") else None,
        tx_hash=(resp.get("transactionsHashes") or [None])[0],
        raw_response=resp,
    )

    mgr.active_orders[token_id] = record
    mgr.order_id_mapping[resp["orderID"]] = token_id
    mgr.mark_event_interesting(event.event_id)

    logger.info(
        "[orders] Placed: %s | %s token in '%s' @ $%.4f size=%.2f bid=%.4f ask=%.4f",
        record.order_id, token.token_type, market.title, price, size,
        initial_market_bid, initial_market_ask,
    )

    from ws_manager import subscribe_tokens
    subscribe_tokens(mgr, event.get_all_no_tokens())
    return record


# ── Cancel ─────────────────────────────────────────────────────────────────────

def cancel_order(mgr: TradingManager, token_id: str, client) -> None:
    """Cancel a single active order and remove it from tracking."""
    if token_id not in mgr.active_orders:
        logger.debug("[orders] Token %s not in active_orders — already cancelled", token_id)
        return

    order = mgr.active_orders[token_id]
    event_id = order.event_id

    try:
        resp = client.cancel(order_id=order.order_id)
        logger.info("[orders] Cancel %s → %s", order.order_id, resp)
        order.status = OrderStatus.CANCELLED
    except Exception as exc:
        logger.error("[orders] Error cancelling %s: %s", order.order_id, exc)
        return

    mgr.order_id_mapping.pop(order.order_id, None)
    del mgr.active_orders[token_id]
    logger.info("[orders] Order %s removed from tracking", order.order_id)
    mgr.check_and_deactivate_event(event_id)


def cancel_orders(mgr: TradingManager, token_ids: List[str], client) -> None:
    """Bulk-cancel all active orders for the given token IDs."""
    order_ids: List[str] = []
    token_map: dict = {}
    event_ids: set = set()

    for token_id in token_ids:
        if token_id in mgr.active_orders:
            order = mgr.active_orders[token_id]
            order_ids.append(order.order_id)
            token_map[order.order_id] = token_id
            event_ids.add(order.event_id)

    if not order_ids:
        logger.debug("[orders] No active orders to cancel")
        return

    try:
        logger.info("[orders] Bulk cancelling %d orders: %s", len(order_ids), order_ids)
        resp = client.cancel_orders(order_ids)
        logger.info("[orders] Bulk cancel response: %s", resp)

        for order_id in order_ids:
            token_id = token_map[order_id]
            order = mgr.active_orders[token_id]
            order.status = OrderStatus.CANCELLED
            mgr.order_id_mapping.pop(order_id, None)
            del mgr.active_orders[token_id]
            logger.info("[orders] Order %s (token %s) removed", order_id, token_id)

        for event_id in event_ids:
            mgr.check_and_deactivate_event(event_id)

    except Exception as exc:
        logger.error("[orders] Bulk cancellation error: %s", exc)


# ── Hedge ──────────────────────────────────────────────────────────────────────

def hedge_event(mgr: TradingManager, event: "Event", client) -> bool:
    """Buy the necessary NO tokens to hedge filled maker orders in an event."""
    logger.info("[hedge] Starting hedge for event '%s'", event.title)

    all_asset_ids = event.get_all_no_tokens()
    if not any(o.event_id == event.event_id for o in mgr.active_orders.values()):
        return True

    orders_filled: List[List] = []
    filled_asset_ids: List[str] = []
    total_cost = 0.0

    for token_id in all_asset_ids:
        if token_id not in mgr.active_orders:
            continue
        order = mgr.active_orders[token_id]
        matched = order.matched_amount or 0.0

        # Light fallback poll if WS data is stale
        if matched == 0 and (order.last_checked is None or time.time() - order.last_checked > 60):
            try:
                resp = client.get_order(order.order_id)
                matched = float(resp.get("size_matched", 0))
                order.matched_amount = matched
                order.last_checked = time.time()
            except Exception as exc:
                logger.warning("[hedge] Poll failed for %s: %s", token_id, exc)
                continue

        already_hedged = mgr.get_position(token_id)
        net_unhedged = max(0.0, matched - already_hedged)
        if net_unhedged > 0:
            orders_filled.append([token_id, net_unhedged])
            total_cost += net_unhedged * order.price
            filled_asset_ids.append(token_id)

    if not orders_filled:
        mgr.cleanup_expired_orders()
        return True

    # Cancel all makers in this event before hedging
    cancel_orders(mgr, all_asset_ids, client)
    size_to_hedge = max(m[1] for m in orders_filled)

    # Hedge partial fills
    for token_id, filled in orders_filled:
        remaining = size_to_hedge - filled
        if abs(remaining) < 1e-7:
            continue
        price = _get_best_ask(mgr, token_id)
        usd = remaining * price
        if usd < 1 or remaining < 5:
            logger.warning("[hedge] Cannot hedge remaining %.2f on %s (USD %.2f)", remaining, token_id, usd)
            total_cost += usd
            continue
        _place_hedge_order(mgr, client, token_id, price, remaining)
        total_cost += remaining * price

    # Hedge unfilled positions
    not_filled = [t for t in all_asset_ids if t not in filled_asset_ids]
    if not_filled:
        total_cost += _hedge_unfilled(mgr, client, not_filled, size_to_hedge)

    # Telegram notification
    from api import send_telegram
    from config import TG_TOKEN, CHAT_ID
    pnl = mgr.get_event_pnl(event)
    send_telegram(
        TG_TOKEN, CHAT_ID,
        f"Trade Notification:\nEvent: {event.title}\n"
        f"Hedge executed | Cost: {total_cost:.2f} | Size/token: {size_to_hedge} | "
        f"Theoretical Profit: {pnl['theoretical_profit']:.2f}"
    )
    return True


def _get_best_ask(mgr: TradingManager, token_id: str) -> float:
    book_param = BookParams(token_id=token_id)
    books = mgr.client.get_order_books(params=[book_param])
    return float(books[0].asks[-1].price) if books and books[0].asks else 0.999


def _place_hedge_order(mgr: TradingManager, client, token_id: str, price: float, size: float) -> None:
    order_args = OrderArgs(
        price=price, size=size, side="BUY",
        token_id=token_id, expiration=int(time.time()) + 700,
    )
    signed = client.create_order(order_args)
    resp = client.post_order(signed, OrderType.GTD)
    making = float(resp.get("makingAmount", size * price))
    mgr.update_position(token_id, size, making)
    logger.info("[hedge] Hedged %.2f of token %s at %.4f (USD %.2f)", size, token_id, price, making)


def _hedge_unfilled(mgr: TradingManager, client, token_ids: List[str], size: float) -> float:
    """Batch-post hedge orders for tokens not yet filled; return total USD cost."""
    BATCH = 15
    total = 0.0

    book_params = [BookParams(token_id=t) for t in token_ids]
    books = client.get_order_books(params=book_params)

    post_args: List[PostOrdersArgs] = []
    orders_info: List[dict] = []

    for book in books:
        token_id = book.asset_id
        if not book.asks:
            continue
        price = float(book.asks[-1].price)
        usd = size * price
        if usd < 1:
            logger.warning("[hedge] Skip %s — USD %.2f too small", token_id, usd)
            total += usd
            continue
        order_args = OrderArgs(
            price=price, size=size, side="BUY",
            token_id=token_id, expiration=int(time.time()) + 70,
        )
        signed = client.create_order(order_args)
        post_args.append(PostOrdersArgs(order=signed, orderType=OrderType.GTD, postOnly=False))
        orders_info.append({"token_id": token_id, "price": price, "size": size})

    logger.info("[hedge] Posting %d unfilled hedge orders in batches of %d", len(post_args), BATCH)

    for i in range(0, len(post_args), BATCH):
        batch_resps = client.post_orders(post_args[i:i + BATCH])
        for j, resp in enumerate(batch_resps):
            info = orders_info[i + j]
            if not resp.get("success"):
                logger.error("[hedge] Hedge failed for %s: %s", info["token_id"], resp.get("errorMsg"))
                continue
            making = float(resp.get("makingAmount", info["size"] * info["price"]))
            total += making
            mgr.update_position(info["token_id"], info["size"], making)
            logger.info("[hedge] Placed hedge for %s @ %.4f size=%.2f (USD %.2f)",
                        info["token_id"], info["price"], info["size"], making)

            # Handle partial fills
            taking = float(resp.get("takingAmount", 0))
            remaining = info["size"] - taking
            rem_usd = remaining * info["price"]
            if remaining > 0 and rem_usd >= 1 and remaining >= 5:
                logger.info("[hedge] Re-hedging partial remainder %.2f (USD %.2f)", remaining, rem_usd)
                order_args2 = OrderArgs(
                    price=info["price"] + 0.001,
                    size=remaining, side="BUY",
                    token_id=info["token_id"], expiration=int(time.time()) + 70,
                )
                signed2 = client.create_order(order_args2)
                resp2 = client.post_order(signed2, OrderType.GTD)
                making2 = float(resp2.get("makingAmount", remaining * info["price"]))
                total += making2
                mgr.update_position(info["token_id"], remaining, making2)

    return total