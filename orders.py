"""Order placement, cancellation, and hedging logic.

All functions receive `mgr: TradingManager` as their first argument so they
can read/write shared state without circular imports.
"""
from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, List, Optional

from py_clob_client_v2.clob_types import OrderArgs, PostOrdersV2Args as PostOrdersArgs, OrderType, OrderPayload
from py_clob_client_v2.exceptions import PolyApiException
from utils import fetch_order_books

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
    with mgr.lock:
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
        except PolyApiException as exc:
            if "order_version_mismatch" in str(exc.error_msg):
                logger.warning("[orders] order_version_mismatch for %s — refreshing version and retrying", token_id)
                try:
                    client._ClobClient__cached_version = None  # force version cache clear
                    signed = client.create_order(order_args)
                    resp = client.post_order(signed, OrderType.GTD)
                except Exception as retry_exc:
                    logger.error("[orders] Retry failed for token %s: %s", token_id, retry_exc)
                    return None
            else:
                logger.error("[orders] Error placing order for token %s: %s", token_id, exc)
                return None
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
    with mgr.lock:
        order = mgr.active_orders.get(token_id)
        if not order:
            logger.debug("[orders] Token %s not in active_orders — already cancelled", token_id)
            return

        event_id = order.event_id
        try:
            resp = client.cancel_order(OrderPayload(orderID=order.order_id))
            logger.info("[orders] Cancel %s → %s", order.order_id, resp)
            order.status = OrderStatus.CANCELLED
        except Exception as exc:
            logger.error("[orders] Error cancelling %s: %s", order.order_id, exc)
            return

        mgr.order_id_mapping.pop(order.order_id, None)
        mgr.active_orders.pop(token_id, None)
        logger.info("[orders] Order %s removed from tracking", order.order_id)
        mgr.check_and_deactivate_event(event_id)


def cancel_orders(mgr: TradingManager, token_ids: List[str], client) -> None:
    """Bulk-cancel all active orders for the given token IDs."""
    with mgr.lock:
        order_ids: List[str] = []
        token_map: dict = {}
        event_ids: set = set()

        for token_id in token_ids:
            order = mgr.active_orders.get(token_id)
            if order:
                order_ids.append(order.order_id)
                token_map[order.order_id] = token_id
                event_ids.add(order.event_id)

        if not order_ids:
            logger.debug("[orders] No active orders to cancel")
            return

        logger.info("[orders] Bulk cancelling %d orders", len(order_ids))
        for attempt in range(2):
            try:
                resp = client.cancel_orders(order_ids)
                logger.info("[orders] Bulk cancel response: %s", resp)
                break
            except Exception as exc:
                logger.error("[orders] Bulk cancel attempt %d failed: %s", attempt + 1, exc)
                if attempt == 1:
                    # Leave the orders tracked — they may still be live; the next
                    # fill event or the expiry sweep will reconcile them.
                    return

        for order_id in order_ids:
            token_id = token_map[order_id]
            order = mgr.active_orders.get(token_id)
            if order:
                order.status = OrderStatus.CANCELLED
            mgr.order_id_mapping.pop(order_id, None)
            mgr.active_orders.pop(token_id, None)
            logger.info("[orders] Order %s (token %s) removed", order_id, token_id)

        for event_id in event_ids:
            mgr.check_and_deactivate_event(event_id)


# ── Hedge ──────────────────────────────────────────────────────────────────────

_HEDGE_BATCH = 15  # max orders per post_orders call


def hedge_event(mgr: TradingManager, event: "Event", client) -> bool:
    """Hedge an event whose maker orders have (partially) filled.

    Order of operations matters — cancel first, *then* measure, *then* hedge:
      1. Cancel every live maker order in the event so no further fills land.
      2. Re-poll each leg's final fill amount (post-cancel = the real truth).
      3. Record the maker fills into positions (complete, idempotent accounting).
      4. Target a balanced basket sized to the largest single-leg fill.
      5. FAK-buy every leg up to that target, retrying shortfalls.
      6. Report PnL from positions (a single, consistent cost basis).
    """
    from api import send_telegram
    from config import (
        TG_TOKEN, CHAT_ID, HEDGE_SLIPPAGE_TICKS,
        HEDGE_RETRY_SLIPPAGE_TICKS, HEDGE_MAX_RETRIES,
    )

    with mgr.lock:
        all_asset_ids = event.get_all_no_tokens()

        # Snapshot this event's live maker orders before they are cancelled.
        event_orders = {
            tid: o for tid, o in mgr.active_orders.items()
            if o.event_id == event.event_id
        }
        if not event_orders:
            logger.debug("[hedge] No live orders for '%s' — nothing to hedge", event.title)
            return True

        logger.info("[hedge] Hedging '%s' — %d live maker order(s)",
                    event.title, len(event_orders))

        # STEP 1: cancel all makers FIRST so no further fills land mid-hedge.
        cancel_orders(mgr, list(event_orders.keys()), client)

        # STEP 2: re-poll FINAL fills. The exchange's size_matched is
        # authoritative; fall back to the WS hint only if the poll fails.
        maker_fills: dict = {}
        for tid, order in event_orders.items():
            matched = order.matched_amount or 0.0
            try:
                resp = client.get_order(order.order_id)
                matched = float(resp.get("size_matched", 0) or 0)
            except Exception as exc:
                logger.warning("[hedge] Fill poll failed for %s: %s — using WS hint %.2f",
                               tid, exc, matched)
            if matched > 0:
                maker_fills[tid] = matched

        if not maker_fills:
            logger.info("[hedge] No fills detected for '%s' — nothing to hedge", event.title)
            return True

        # STEP 3: record maker fills into positions (idempotent — only the delta).
        for tid, matched in maker_fills.items():
            order = event_orders[tid]
            already = mgr.get_position(tid)
            if matched > already:
                add = matched - already
                mgr.update_position(tid, add, add * order.price)

        # STEP 4: balanced-basket target = the largest single-leg fill.
        size_to_hedge = max(maker_fills.values())
        logger.info("[hedge] Target basket: %.2f shares across %d leg(s)",
                    size_to_hedge, len(all_asset_ids))

        # STEP 5: FAK-hedge every leg up to the target, retrying shortfalls.
        _hedge_to_target(
            mgr, client, event, all_asset_ids, size_to_hedge,
            HEDGE_SLIPPAGE_TICKS, HEDGE_RETRY_SLIPPAGE_TICKS, HEDGE_MAX_RETRIES,
        )

        # STEP 6: report — every number comes from positions (one cost basis).
        pnl = mgr.get_event_pnl(event)
        balanced = abs(pnl["max_shares"] - pnl["min_shares"]) < 1e-6
        warn = "" if balanced else (
            f"\n⚠️ UNBALANCED — legs hold {pnl['min_shares']:.2f}–{pnl['max_shares']:.2f} shares"
        )
        send_telegram(
            TG_TOKEN, CHAT_ID,
            f"Trade Notification\n"
            f"Event: {event.title}\n"
            f"Hedge executed | Target/leg: {size_to_hedge:.2f}\n"
            f"Total cost: {pnl['total_cost']:.2f}\n"
            f"Shares/leg: {pnl['min_shares']:.2f}–{pnl['max_shares']:.2f}\n"
            f"Guaranteed profit: {pnl['guaranteed_profit']:.2f} | "
            f"Best case: {pnl['best_case_profit']:.2f}{warn}"
        )
        return True


def _hedge_to_target(
    mgr: TradingManager, client, event: "Event", all_asset_ids: List[str],
    target: float, slippage_ticks: int, retry_slippage_ticks: int, max_retries: int,
) -> None:
    """FAK-buy every leg up to `target` shares, retrying shortfalls at wider slippage."""
    from config import HEDGE_MIN_SHARES

    for attempt in range(max_retries + 1):
        deficits = {tid: target - mgr.get_position(tid) for tid in all_asset_ids}
        deficits = {tid: d for tid, d in deficits.items() if d > HEDGE_MIN_SHARES}
        if not deficits:
            logger.info("[hedge] Basket fully hedged")
            return

        ticks = slippage_ticks if attempt == 0 else retry_slippage_ticks
        logger.info("[hedge] FAK pass %d/%d — %d leg(s) short",
                    attempt + 1, max_retries + 1, len(deficits))
        _hedge_legs_once(mgr, client, list(deficits.items()), ticks)

    # Final shortfall check — alert if any leg could not be filled.
    short = {
        tid: target - mgr.get_position(tid)
        for tid in all_asset_ids
        if target - mgr.get_position(tid) > HEDGE_MIN_SHARES
    }
    if short:
        from api import send_telegram
        from config import TG_TOKEN, CHAT_ID
        detail = ", ".join(f"{tid[:12]}…: short {d:.1f}" for tid, d in short.items())
        logger.error("[hedge] INCOMPLETE for '%s' after %d retr(ies) — %s",
                      event.title, max_retries, detail)
        send_telegram(
            TG_TOKEN, CHAT_ID,
            f"⚠️ Hedge INCOMPLETE — '{event.title}'\n"
            f"Unhedged legs: {detail}\nManual review needed."
        )


def _hedge_legs_once(mgr: TradingManager, client, deficit_items: List, ticks: int) -> None:
    """One FAK pass: fetch books, sign, batch-post, record actual fills."""
    token_ids = [tid for tid, _ in deficit_items]
    try:
        books = fetch_order_books(client, token_ids)
    except Exception as exc:
        logger.error("[hedge] Order-book fetch failed: %s", exc)
        return
    book_by_id = {b.asset_id: b for b in books}

    orders_info: List[dict] = []
    for tid, deficit in deficit_items:
        book = book_by_id.get(tid)
        if not book or not getattr(book, "asks", None):
            logger.warning("[hedge] No asks for %s — cannot hedge this leg", tid)
            continue
        best_ask = float(book.asks[-1].price)
        market = mgr.get_market_by_token(tid)
        min_tick = market.min_tick if market else 0.001
        # Cap at the highest valid tick (1 - min_tick); 0.999 would be off-grid
        # for coarser tick sizes and rejected by create_order's price check.
        limit_price = round(min(1.0 - min_tick, best_ask + ticks * min_tick), 4)
        if deficit * limit_price < 1.0:
            logger.warning("[hedge] Skip %s — deficit %.2f worth only USD %.2f",
                           tid, deficit, deficit * limit_price)
            continue
        orders_info.append({
            "token_id": tid,
            "price": limit_price,
            "size": round(deficit, 2),
        })

    if not orders_info:
        return

    for i in range(0, len(orders_info), _HEDGE_BATCH):
        chunk = orders_info[i:i + _HEDGE_BATCH]
        try:
            resps = _sign_and_post_batch(client, chunk, OrderType.FAK)
        except Exception as exc:
            logger.error("[hedge] Batch post failed: %s", exc)
            continue
        if len(resps) != len(chunk):
            logger.warning("[hedge] Response count %d != order count %d", len(resps), len(chunk))
        for info, resp in zip(chunk, resps):
            tid = info["token_id"]
            if not isinstance(resp, dict) or not resp.get("success"):
                err = resp.get("errorMsg") if isinstance(resp, dict) else resp
                logger.error("[hedge] FAK failed for %s: %s", tid, err)
                continue
            taking = float(resp.get("takingAmount", 0) or 0)   # shares received
            making = float(resp.get("makingAmount", 0) or 0)   # USDC spent
            if taking > 0:
                cost = making if making > 0 else taking * info["price"]
                mgr.update_position(tid, taking, cost)
                logger.info("[hedge] FAK filled %.2f shares of %s (USD %.2f)",
                            taking, tid, cost)
            else:
                logger.warning("[hedge] FAK no fill for %s @ %.4f", tid, info["price"])


def _sign_and_post_batch(client, orders_info: List[dict], order_type) -> list:
    """Sign and batch-post BUY orders, retrying once on order_version_mismatch."""
    def build_and_post():
        post_args = []
        for info in orders_info:
            oa = OrderArgs(
                price=info["price"], size=info["size"],
                side="BUY", token_id=info["token_id"],
            )
            post_args.append(PostOrdersArgs(order=client.create_order(oa), orderType=order_type))
        return client.post_orders(post_args)

    try:
        return build_and_post()
    except PolyApiException as exc:
        if "order_version_mismatch" in str(exc.error_msg):
            logger.warning("[hedge] order_version_mismatch — refreshing version, retrying batch")
            client._ClobClient__cached_version = None
            return build_and_post()
        raise