import logging
import asyncio
from typing import Dict, List, Set, Any, Optional
from aiohttp_retry import RetryClient
from .base_exchange import Exchange
from config import Config

class CoinEx(Exchange):
    def __init__(self, config: Config, enabled: bool = True):
        super().__init__(
            name="CoinEx",
            pairs_url="https://api.coinex.com/v1/market/list",
            ticker_url="https://api.coinex.com/v1/market/ticker/all",
            orderbook_url="https://api.coinex.com/v1/market/depth",
            config=config,
            enabled=enabled
        )

    async def get_all_pairs(self, session: RetryClient) -> Set[str]:
        try:
            data = await self._make_request(session, self.pairs_url)
            if not data or "data" not in data:
                return set()
            
            pairs = set(data["data"])
            self.available_pairs = pairs
            return pairs
        except Exception as e:
            logging.error(f"CoinEx: Error fetching pairs: {e}")
            return set()

    async def get_all_tickers(self, session: RetryClient) -> Dict[str, float]:
        try:
            data = await self._make_request(session, self.ticker_url)
            if not data or "data" not in data or "ticker" not in data["data"]:
                return {}
            
            tickers = {}
            for symbol, entry in data["data"]["ticker"].items():
                last = entry.get("last")
                if last:
                    try:
                        tickers[symbol] = float(last)
                    except (ValueError, TypeError):
                        continue
            
            return tickers
        except Exception as e:
            logging.error(f"CoinEx: Error fetching tickers: {e}")
            return {}

    async def get_order_books(self, session: RetryClient, symbols: List[str]) -> Dict[str, Dict[str, float]]:
        results = {}
        semaphore = asyncio.Semaphore(10)

        async def _fetch_one(symbol: str) -> None:
            async with semaphore:
                try:
                    url = f"{self.orderbook_url}?market={symbol}&limit=50&merge=0"
                    data = await self._make_request(session, url)
                    if data and "data" in data and data["data"].get("asks") and data["data"].get("bids"):
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
                    logging.error(f"CoinEx: Error fetching orderbook for {symbol}: {e}")

        await asyncio.gather(*[_fetch_one(s) for s in symbols])
        return results
