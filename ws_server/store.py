from __future__ import annotations

import asyncio
import time
from typing import Dict, Iterable, List, Optional

from .models import Opportunity, Quote


class QuoteStore:
    def __init__(self) -> None:
        self._quotes: Dict[str, Dict[str, Quote]] = {}
        self._lock = asyncio.Lock()
        self.started_at = time.time()

    async def upsert(self, quote: Quote) -> None:
        if quote.ts <= 0:
            quote.ts = time.time()
        async with self._lock:
            self._quotes.setdefault(quote.exchange, {})[quote.symbol] = quote

    async def upsert_many(self, quotes: Iterable[Quote]) -> None:
        now = time.time()
        async with self._lock:
            for quote in quotes:
                if quote.ts <= 0:
                    quote.ts = now
                self._quotes.setdefault(quote.exchange, {})[quote.symbol] = quote

    async def snapshot(self) -> Dict[str, Dict[str, Quote]]:
        async with self._lock:
            return {ex: dict(symbols) for ex, symbols in self._quotes.items()}

    async def symbol_quotes(self, symbol: str) -> Dict[str, Quote]:
        symbol = str(symbol or "").strip().upper()
        async with self._lock:
            return {
                ex: quote
                for ex, symbols in self._quotes.items()
                for sym, quote in symbols.items()
                if sym == symbol
            }

    async def build_opportunities(
        self,
        *,
        min_spread: float,
        max_spread: float,
        ttl_sec: float,
        limit: int,
        notional_usd: float = 0.0,
        require_top_liquidity: bool = False,
    ) -> List[Opportunity]:
        snap = await self.snapshot()
        symbols = sorted({sym for by_symbol in snap.values() for sym in by_symbol.keys()})
        now = time.time()
        out: List[Opportunity] = []

        for symbol in symbols:
            rows = []
            for exchange, by_symbol in snap.items():
                quote = by_symbol.get(symbol)
                if not quote or not quote.fresh(ttl_sec):
                    continue
                buy = quote.buy_price()
                sell = quote.sell_price()
                if buy and sell:
                    rows.append((exchange, quote, buy, sell))

            for buy_exchange, buy_quote, buy_price, _ in rows:
                for sell_exchange, sell_quote, _, sell_price in rows:
                    if buy_exchange == sell_exchange:
                        continue
                    if buy_price <= 0 or sell_price <= 0:
                        continue
                    spread = ((sell_price / buy_price) - 1.0) * 100.0
                    if min_spread <= spread <= max_spread:
                        buy_liq = self._top_liquidity_usd(buy_quote.ask_size, buy_price)
                        sell_liq = self._top_liquidity_usd(sell_quote.bid_size, sell_price)
                        liq_values = [v for v in (buy_liq, sell_liq) if isinstance(v, (int, float))]
                        min_liq = min(liq_values) if len(liq_values) == 2 else None
                        executable = bool(
                            notional_usd > 0
                            and isinstance(min_liq, (int, float))
                            and min_liq >= notional_usd
                        )
                        if require_top_liquidity and not executable:
                            continue
                        manual_only = any("binance alpha" in str(x).lower() for x in (buy_exchange, sell_exchange))
                        out.append(
                            Opportunity(
                                symbol=symbol,
                                buy_exchange=buy_exchange,
                                sell_exchange=sell_exchange,
                                buy_price=float(buy_price),
                                sell_price=float(sell_price),
                                spread=float(spread),
                                buy_source=buy_quote.source,
                                sell_source=sell_quote.source,
                                timestamp=now,
                                buy_top_liquidity_usd=buy_liq,
                                sell_top_liquidity_usd=sell_liq,
                                min_top_liquidity_usd=min_liq,
                                executable_notional_usd=notional_usd if notional_usd > 0 else None,
                                top_liquidity_executable=executable,
                                manual_only=manual_only,
                                execution_mode="manual_signal" if manual_only else "market_data",
                            )
                        )

        out.sort(key=lambda item: item.spread, reverse=True)
        return out[: max(1, limit)]

    @staticmethod
    def _top_liquidity_usd(size: Optional[float], price: float) -> Optional[float]:
        try:
            if size is None or price <= 0:
                return None
            out = float(size) * float(price)
            return out if out > 0 else None
        except (TypeError, ValueError):
            return None
