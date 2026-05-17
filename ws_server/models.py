from __future__ import annotations

from dataclasses import asdict, dataclass
import time
from typing import Optional


@dataclass
class Quote:
    exchange: str
    symbol: str
    bid: Optional[float] = None
    ask: Optional[float] = None
    bid_size: Optional[float] = None
    ask_size: Optional[float] = None
    last: Optional[float] = None
    ts: float = 0.0
    source: str = "unknown"

    def fresh(self, ttl_sec: float) -> bool:
        return self.ts > 0 and (time.time() - self.ts) <= ttl_sec

    def buy_price(self) -> Optional[float]:
        if self.ask and self.ask > 0:
            return self.ask
        return None

    def sell_price(self) -> Optional[float]:
        if self.bid and self.bid > 0:
            return self.bid
        return None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Opportunity:
    symbol: str
    buy_exchange: str
    sell_exchange: str
    buy_price: float
    sell_price: float
    spread: float
    buy_source: str
    sell_source: str
    timestamp: float
    buy_top_liquidity_usd: Optional[float] = None
    sell_top_liquidity_usd: Optional[float] = None
    min_top_liquidity_usd: Optional[float] = None
    executable_notional_usd: Optional[float] = None
    top_liquidity_executable: bool = False
    manual_only: bool = False
    execution_mode: str = "market_data"

    def to_dict(self) -> dict:
        return asdict(self)
