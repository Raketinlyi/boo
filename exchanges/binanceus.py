import asyncio
import logging
from typing import Dict, List, Set

from aiohttp_retry import RetryClient

from exchanges.base_exchange import Exchange
from config import Config
from utils.symbols import normalize_pair_symbol


class BinanceUS(Exchange):
    """Public Binance.US spot market data adapter."""

    def __init__(self, config: Config, enabled: bool = True):
        self.BASE_URL = "https://api.binance.us"
        self._raw_symbol_by_norm: Dict[str, str] = {}
        super().__init__(
            name="Binance.US",
            pairs_url=f"{self.BASE_URL}/api/v3/exchangeInfo",
            ticker_url=f"{self.BASE_URL}/api/v3/ticker/bookTicker",
            orderbook_url=f"{self.BASE_URL}/api/v3/depth",
            config=config,
            enabled=enabled,
        )

    async def get_all_pairs(self, session: RetryClient) -> Set[str]:
        data = await self._make_request(session, self.pairs_url)
        pairs: Set[str] = set()
        for item in (data or {}).get("symbols", []) if isinstance(data, dict) else []:
            try:
                status = str(item.get("status") or "").upper()
                quote = str(item.get("quoteAsset") or "").upper()
                raw = str(item.get("symbol") or "").upper()
                if status == "TRADING" and quote in {"USDT", "USD"} and raw:
                    sym = normalize_pair_symbol(raw)
                    if sym:
                        pairs.add(sym)
                        self._raw_symbol_by_norm[sym] = raw
            except Exception:
                continue
        self.available_pairs = pairs
        return pairs

    async def get_all_tickers(self, session: RetryClient) -> Dict[str, float]:
        data = await self._make_request(session, self.ticker_url)
        out: Dict[str, float] = {}
        if isinstance(data, dict):
            data = [data]
        for row in data or []:
            try:
                raw = str(row.get("symbol") or "").upper()
                sym = normalize_pair_symbol(raw)
                bid = float(row.get("bidPrice") or 0)
                ask = float(row.get("askPrice") or 0)
                if sym and bid > 0 and ask > 0:
                    out[sym] = (bid + ask) / 2.0
                    self._raw_symbol_by_norm[sym] = raw
            except Exception:
                continue
        return out

    async def get_order_books(self, session: RetryClient, symbols: List[str]) -> Dict[str, Dict[str, float]]:
        results: Dict[str, Dict[str, float]] = {}
        sem = asyncio.Semaphore(8)

        async def _one(symbol: str):
            async with sem:
                raw = self._raw_symbol_by_norm.get(symbol) or symbol
                url = f"{self.orderbook_url}?symbol={raw}&limit=20"
                data = await self._make_request(session, url)
                try:
                    bids = data.get("bids") or []
                    asks = data.get("asks") or []
                    if not bids or not asks:
                        return
                    bid = float(bids[0][0]); bid_vol = float(bids[0][1])
                    ask = float(asks[0][0]); ask_vol = float(asks[0][1])
                    if bid > 0 and ask > 0:
                        results[symbol] = {"bid": bid, "ask": ask, "bid_volume": bid_vol, "ask_volume": ask_vol, "bids": bids, "asks": asks}
                except Exception as exc:
                    logging.debug("Binance.US parse orderbook failed for %s: %s", symbol, exc)

        await asyncio.gather(*[_one(s) for s in symbols])
        return results
