import logging
import asyncio
from typing import Dict, List, Set, Any, Optional
from aiohttp_retry import RetryClient
from .base_exchange import Exchange
from config import Config

class Mexc(Exchange):
    def __init__(self, config: Config, enabled: bool = True):
        super().__init__(
            name="MEXC",
            pairs_url="https://api.mexc.com/api/v3/exchangeInfo",
            ticker_url="https://api.mexc.com/api/v3/ticker/price",
            orderbook_url="https://api.mexc.com/api/v3/depth",
            config=config,
            enabled=enabled
        )
        self.headers.update({
            "Referer": "https://www.mexc.com/",
            "Origin": "https://www.mexc.com",
        })

    async def get_all_pairs(self, session: RetryClient) -> Set[str]:
        try:
            data = await self._make_request(session, self.pairs_url)
            if not data or "symbols" not in data:
                return set()
            
            pairs = set()
            for entry in data["symbols"]:
                if entry.get("status") == "ENABLED" or entry.get("status") == "1":
                    pairs.add(entry.get("symbol"))
            
            self.available_pairs = pairs
            return pairs
        except Exception as e:
            logging.error(f"MEXC: Error fetching pairs: {e}")
            return set()

    async def get_all_tickers(self, session: RetryClient) -> Dict[str, float]:
        endpoints = [
            self.ticker_url,
            "https://api.mexc.com/api/v3/ticker/24hr",
        ]
        for url in endpoints:
            try:
                response = await session.get(url, headers=self.headers, timeout=self.timeout)
                if response.status != 200:
                    logging.warning(f"MEXC: ticker HTTP {response.status} for {url}")
                    continue
                data = await response.json(content_type=None)
                if not data:
                    continue

                tickers = {}
                for entry in data:
                    symbol = entry.get("symbol")
                    price = entry.get("price") or entry.get("lastPrice")
                    if symbol and price:
                        try:
                            tickers[symbol] = float(price)
                        except (ValueError, TypeError):
                            continue
                if tickers:
                    self.error_count = 0
                    self.last_error_time = None
                    return tickers
            except Exception as e:
                logging.warning(f"MEXC: ticker endpoint skipped {url}: {e}")
        return {}

    async def get_order_books(self, session: RetryClient, symbols: List[str]) -> Dict[str, Dict[str, float]]:
        results = {}
        # MEXC depth is used as a fast validation layer, not as a full market dump.
        # Keep it bounded so one slow MEXC batch does not make the whole scanner look stuck.
        try:
            max_symbols = int(self.config.get("mexc_orderbook_max_symbols", 12) or 12)
        except Exception:
            max_symbols = 12
        max_symbols = max(1, max_symbols)
        symbols = list(symbols)[:max_symbols]

        try:
            per_symbol_timeout = float(self.config.get("mexc_orderbook_per_symbol_timeout_sec", 1.8) or 1.8)
        except Exception:
            per_symbol_timeout = 1.8
        per_symbol_timeout = max(0.5, per_symbol_timeout)

        try:
            concurrency = int(self.config.get("mexc_orderbook_concurrency", 5) or 5)
        except Exception:
            concurrency = 5
        semaphore = asyncio.Semaphore(max(1, min(10, concurrency)))

        async def _fetch_one(symbol: str) -> None:
            async with semaphore:
                try:
                    url = f"{self.orderbook_url}?symbol={symbol}&limit=5"
                    response = await asyncio.wait_for(
                        session.get(url, headers=self.headers, timeout=per_symbol_timeout),
                        timeout=per_symbol_timeout + 0.5,
                    )
                    if response.status != 200:
                        # MEXC can WAF/throttle depth requests while tickers still work.
                        # Do not put the whole exchange into cooldown because of depth 403/400.
                        if response.status not in (400, 403):
                            logging.warning(f"MEXC: depth HTTP {response.status} for {symbol}")
                        return
                    data = await response.json(content_type=None)
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
                except asyncio.TimeoutError:
                    logging.debug(f"MEXC: depth timeout for {symbol}")
                except Exception as e:
                    logging.warning(f"MEXC: depth skipped for {symbol}: {e}")

        await asyncio.gather(*[_fetch_one(s) for s in symbols])
        return results
