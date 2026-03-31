from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


# ── Enums ──────────────────────────────────────────────────────────────────────

class OrderSide(Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderStatus(Enum):
    PENDING = "pending"
    MATCHED = "matched"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    FAILED = "failed"


# ── Token / Market / Event ─────────────────────────────────────────────────────

@dataclass
class MarketToken:
    """Represents a single outcome token (YES or NO)."""
    condition_id: str
    token_id: str        # YES_ID or NO_ID
    token_type: str      # "YES" or "NO"
    best_bid: Optional[float] = None
    best_ask: Optional[float] = None

    def to_dict(self) -> Dict:
        return {
            "condition_id": self.condition_id,
            "token_id": self.token_id,
            "token_type": self.token_type,
            "best_bid": self.best_bid,
            "best_ask": self.best_ask,
        }


@dataclass
class MarketRewards:
    """Market rewards and trading limits."""
    rates: float = 0.0
    min_size: float = 0.0
    max_spread: float = 0.0

    def to_dict(self) -> Dict:
        return {
            "rates": self.rates,
            "min_size": self.min_size,
            "max_spread": self.max_spread,
        }


@dataclass
class Market:
    """Represents a single market within an event."""
    market_id: str          # condition_id used as market_id
    title: str
    condition_id: str
    yes_token: MarketToken
    no_token: MarketToken
    min_tick: float
    rewards: MarketRewards = field(default_factory=MarketRewards)
    last_price: Optional[float] = None

    def get_token(self, token_type: str) -> MarketToken:
        if token_type.upper() == "YES":
            return self.yes_token
        elif token_type.upper() == "NO":
            return self.no_token
        raise ValueError(f"Invalid token type: {token_type}")

    def get_other_token(self, token_id: str) -> Optional[MarketToken]:
        if token_id == self.yes_token.token_id:
            return self.no_token
        elif token_id == self.no_token.token_id:
            return self.yes_token
        return None

    def to_dict(self) -> Dict:
        return {
            "market_id": self.market_id,
            "title": self.title,
            "condition_id": self.condition_id,
            "yes_token": self.yes_token.to_dict(),
            "no_token": self.no_token.to_dict(),
            "rewards": self.rewards.to_dict(),
            "last_price": self.last_price,
        }


@dataclass
class Event:
    """Represents a complete event with multiple markets."""
    event_id: str
    title: str
    markets: Dict[str, Market]   # market_id -> Market

    def get_all_tokens(self) -> List[MarketToken]:
        tokens: List[MarketToken] = []
        for market in self.markets.values():
            tokens.extend([market.yes_token, market.no_token])
        return tokens

    def get_all_no_tokens(self) -> List[str]:
        return [market.no_token.token_id for market in self.markets.values()]

    def get_market_by_token(self, token_id: str) -> Optional[Market]:
        for market in self.markets.values():
            if market.yes_token.token_id == token_id or market.no_token.token_id == token_id:
                return market
        return None

    def to_dict(self) -> Dict:
        return {
            "event_id": self.event_id,
            "title": self.title,
            "markets": {k: v.to_dict() for k, v in self.markets.items()},
        }


# ── Order record ───────────────────────────────────────────────────────────────

@dataclass
class OrderRecord:
    """Track an active maker order."""
    order_id: str
    asset_id: str           # token_id
    market_id: str
    event_id: str
    price: float
    size: float
    initial_market_bid: float
    initial_market_ask: float
    side: OrderSide
    token_type: str         # "YES" or "NO"
    created_at: float
    expiration: float
    status: OrderStatus = OrderStatus.PENDING
    matched_amount: Optional[float] = None
    tx_hash: Optional[str] = None
    last_checked: Optional[float] = None
    raw_response: Optional[Dict] = None

    def is_expired(self) -> bool:
        return time.time() > self.expiration

    def to_dict(self) -> Dict:
        return {
            "order_id": self.order_id,
            "asset_id": self.asset_id,
            "market_id": self.market_id,
            "event_id": self.event_id,
            "price": self.price,
            "size": self.size,
            "initial_market_bid": self.initial_market_bid,
            "initial_market_ask": self.initial_market_ask,
            "side": self.side.value,
            "token_type": self.token_type,
            "created_at": self.created_at,
            "expiration": self.expiration,
            "status": self.status.value,
            "matched_amount": self.matched_amount,
            "tx_hash": self.tx_hash,
            "last_checked": self.last_checked,
        }