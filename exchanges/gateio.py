import logging
import asyncio
from typing import Dict, List, Set, Any, Optional
from aiohttp_retry import RetryClient
from .base_exchange import Exchange
from config import Config
from utils.symbols import format_pair_symbol, normalize_pair_symbol

class GateIO(Exchange):
    def __init__(self, config: Config, enabled: bool = True):
        super().__init__(
            name="Gate.io",
            pairs_url="https://api.gateio.ws/api/v4/spot/currency_pairs",
            ticker_url="https://api.gateio.ws/api/v4/spot/tickers",
            orderbook_url="https://api.gateio.ws/api/v4/spot/order_book",
            config=config,
            enabled=enabled
        )
        # Map normalized symbol -> raw Gate.io currency_pair (e.g., BTCUSDT -> BTC_USDT)
        self._raw_symbol_by_norm: Dict[str, str] = {}

    async def get_all_pairs(self, session: RetryClient) -> Set[str]:
        try:
            data = await self._make_request(session, self.pairs_url)
            if not data:
                return set()
            
            pairs = set()
            for entry in data:
                if entry.get("trade_status") == "tradable":
                    raw = entry.get("id")
                    sym = normalize_pair_symbol(raw)
                    if sym:
                        pairs.add(sym)
                        if raw:
                            self._raw_symbol_by_norm[sym] = str(raw).upper()
            
            self.available_pairs = pairs
            return pairs
        except Exception as e:
            logging.error(f"GateIO: Error fetching pairs: {e}")
            return set()

    async def get_all_tickers(self, session: RetryClient) -> Dict[str, float]:
        try:
            data = await self._make_request(session, self.ticker_url)
            if not data:
                return {}
            
            tickers = {}
            for entry in data:
                raw_symbol = entry.get("currency_pair")
                symbol = normalize_pair_symbol(raw_symbol)
                last = entry.get("last")
                if symbol and last:
                    try:
                        tickers[symbol] = float(last)
                        if raw_symbol:
                            self._raw_symbol_by_norm[symbol] = str(raw_symbol).upper()
                    except (ValueError, TypeError):
                        continue
            
            return tickers
        except Exception as e:
            logging.error(f"GateIO: Error fetching tickers: {e}")
            return {}

    async def get_order_books(self, session: RetryClient, symbols: List[str]) -> Dict[str, Dict[str, float]]:
        results = {}
        semaphore = asyncio.Semaphore(10)

        async def _fetch_one(symbol: str) -> None:
            async with semaphore:
                try:
                    raw_symbol = self._raw_symbol_by_norm.get(symbol) or format_pair_symbol(symbol, sep="_")
                    url = f"{self.orderbook_url}?currency_pair={raw_symbol}&limit=100"
                    data = await self._make_request(session, url)
                    if data and data.get("asks") and data.get("bids"):
                        asks = data["asks"]
                        bids = data["bids"]
                        results[symbol] = {
                            "ask": float(asks[0][0]),
                            "bid": float(bids[0][0]),
                            "ask_volume": float(asks[0][1]),
                            "bid_volume": float(bids[0][1]),
                            "asks": asks,
                            "bids": bids,
                        }
                except Exception as e:
                    logging.error(f"GateIO: Error fetching orderbook for {symbol}: {e}")

        await asyncio.gather(*[_fetch_one(s) for s in symbols])
        return results
