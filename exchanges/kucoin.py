import logging
import asyncio
from typing import Dict, List, Set, Any, Optional
from aiohttp_retry import RetryClient
from .base_exchange import Exchange
from config import Config
from utils.symbols import format_pair_symbol, normalize_pair_symbol

class KuCoin(Exchange):
    def __init__(self, config: Config, enabled: bool = True):
        super().__init__(
            name="KuCoin",
            pairs_url="https://api.kucoin.com/api/v1/symbols",
            ticker_url="https://api.kucoin.com/api/v1/market/allTickers",
            orderbook_url="https://api.kucoin.com/api/v1/market/orderbook/level2_20",
            config=config,
            enabled=enabled
        )
        # Map normalized symbol -> raw KuCoin symbol (e.g., BTCUSDT -> BTC-USDT)
        self._raw_symbol_by_norm: Dict[str, str] = {}

    async def get_all_pairs(self, session: RetryClient) -> Set[str]:
        try:
            data = await self._make_request(session, self.pairs_url)
            if not data or "data" not in data:
                return set()
            
            pairs = set()
            for entry in data["data"]:
                if entry.get("enableTrading"):
                    raw = entry.get("symbol")
                    sym = normalize_pair_symbol(raw)
                    if sym:
                        pairs.add(sym)
                        if raw:
                            self._raw_symbol_by_norm[sym] = str(raw).upper()
            
            self.available_pairs = pairs
            return pairs
        except Exception as e:
            logging.error(f"KuCoin: Error fetching pairs: {e}")
            return set()

    async def get_all_tickers(self, session: RetryClient) -> Dict[str, float]:
        try:
            data = await self._make_request(session, self.ticker_url)
            if not data or "data" not in data or "ticker" not in data["data"]:
                return {}
            
            tickers = {}
            for entry in data["data"]["ticker"]:
                raw_symbol = entry.get("symbol")
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
            logging.error(f"KuCoin: Error fetching tickers: {e}")
            return {}

    async def get_order_books(self, session: RetryClient, symbols: List[str]) -> Dict[str, Dict[str, float]]:
        results = {}
        semaphore = asyncio.Semaphore(10)

        async def _fetch_one(symbol: str) -> None:
            async with semaphore:
                try:
                    raw_symbol = self._raw_symbol_by_norm.get(symbol) or format_pair_symbol(symbol, sep="-")
                    url = f"https://api.kucoin.com/api/v1/market/orderbook/level2_100?symbol={raw_symbol}"
                    data = await self._make_request(session, url)
                    if data and isinstance(data.get("data"), dict) and data["data"].get("asks") and data["data"].get("bids"):
                        asks = data["data"]["asks"]
                        bids = data["data"]["bids"]
                        results[symbol] = {
                            "ask": float(asks[0][0]),
                            "bid": float(bids[0][0]),
                            "ask_volume": float(asks[0][1]),
                            "bid_volume": float(bids[0][1]),
                            "asks": asks,
                            "bids": bids,
                        }
                except Exception as e:
                    logging.error(f"KuCoin: Error fetching orderbook for {symbol}: {e}")

        await asyncio.gather(*[_fetch_one(s) for s in symbols])
        return results
