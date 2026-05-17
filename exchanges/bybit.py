import logging
import asyncio
from typing import Dict, List, Set, Any, Optional
from aiohttp_retry import RetryClient
from .base_exchange import Exchange
from config import Config

class Bybit(Exchange):
    def __init__(self, config: Config, enabled: bool = True):
        super().__init__(
            name="Bybit",
            pairs_url="https://api.bybit.com/v5/market/instruments-info?category=spot",
            ticker_url="https://api.bybit.com/v5/market/tickers?category=spot",
            orderbook_url="https://api.bybit.com/v5/market/orderbook",
            config=config,
            enabled=enabled
        )

    async def get_all_pairs(self, session: RetryClient) -> Set[str]:
        try:
            data = await self._make_request(session, self.pairs_url)
            if not data or "result" not in data or "list" not in data["result"]:
                return set()
            
            pairs = set()
            for entry in data["result"]["list"]:
                if entry.get("status") == "Trading":
                    pairs.add(entry.get("symbol"))
            
            self.available_pairs = pairs
            return pairs
        except Exception as e:
            logging.error(f"Bybit: Error fetching pairs: {e}")
            return set()

    async def get_all_tickers(self, session: RetryClient) -> Dict[str, float]:
        try:
            data = await self._make_request(session, self.ticker_url)
            if not data or "result" not in data or "list" not in data["result"]:
                return {}
            
            tickers = {}
            for entry in data["result"]["list"]:
                symbol = entry.get("symbol")
                last = entry.get("lastPrice")
                if symbol and last:
                    try:
                        tickers[symbol] = float(last)
                    except (ValueError, TypeError):
                        continue
            
            return tickers
        except Exception as e:
            logging.error(f"Bybit: Error fetching tickers: {e}")
            return {}

    async def get_order_books(self, session: RetryClient, symbols: List[str]) -> Dict[str, Dict[str, float]]:
        results = {}
        semaphore = asyncio.Semaphore(10)

        async def _fetch_one(symbol: str) -> None:
            async with semaphore:
                try:
                    url = f"{self.orderbook_url}?category=spot&symbol={symbol}&limit=100"
                    data = await self._make_request(session, url)
                    if data and "result" in data and data["result"].get("a") and data["result"].get("b"):
                        asks = data["result"]["a"]
                        bids = data["result"]["b"]
                        results[symbol] = {
                            "ask": float(asks[0][0]),
                            "bid": float(bids[0][0]),
                            "ask_volume": float(asks[0][1]),
                            "bid_volume": float(bids[0][1]),
                            "asks": asks,
                            "bids": bids,
                        }
                except Exception as e:
                    logging.error(f"Bybit: Error fetching orderbook for {symbol}: {e}")

        await asyncio.gather(*[_fetch_one(s) for s in symbols])
        return results
