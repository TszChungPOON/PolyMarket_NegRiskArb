"""Shared utilities for PolyMarket neg-risk arb bot."""
from __future__ import annotations

import types
from typing import List, Any


def _to_ns(obj: Any) -> Any:
    """Recursively convert dicts/lists to SimpleNamespace objects for attribute access."""
    if isinstance(obj, dict):
        return types.SimpleNamespace(**{k: _to_ns(v) for k, v in obj.items()})
    if isinstance(obj, list):
        return [_to_ns(x) for x in obj]
    return obj


def fetch_order_books(client, token_ids: List[str]) -> List[types.SimpleNamespace]:
    """Fetch order books for the given token IDs.

    Works around py_clob_client_v2's get_order_books not accepting BookParams
    objects directly in JSON serialization, and converts the raw dict response
    into objects with attribute access (book.asks, ask.price, book.asset_id…).
    """
    body = [{"token_id": tid} for tid in token_ids]
    raw = client.get_order_books(params=body)
    return _to_ns(raw)
