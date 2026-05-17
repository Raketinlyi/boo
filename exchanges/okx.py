import logging
import asyncio
from typing import Dict, List, Set, Any, Optional
from aiohttp_retry import RetryClient
from .base_exchange import Exchange
from config import Config
from utils.symbols import format_pair_symbol, normalize_pair_symbol

class OKX(Exchange):
    def __init__(self, config: Config, enabled: bool = True):
        super().__init__(
            name="OKX",
            pairs_url="https://www.okx.com/api/v5/public/instruments?instType=SPOT",
            ticker_url="https://www.okx.com/api/v5/market/tickers?instType=SPOT",
            orderbook_url="https://www.okx.com/api/v5/market/books",
            config=config,
            enabled=enabled
        )
        # Map normalized symbol -> raw OKX symbol (e.g., BTCUSDT -> BTC-USDT)
        self._raw_symbol_by_norm: Dict[str, str] = {}

    async def get_all_pairs(self, session: RetryClient) -> Set[str]:
        try:
            data = await self._make_request(session, self.pairs_url)
            if not data or data.get("code") != "0" or "data" not in data:
                return set()
            
            pairs = set()
            for entry in data["data"]:
                if entry.get("state") == "live":
                    raw = entry.get("instId")
                    sym = normalize_pair_symbol(raw)
                    if sym:
                        pairs.add(sym)
                        if raw:
                            self._raw_symbol_by_norm[sym] = str(raw).upper()
            
            self.available_pairs = pairs
            return pairs
        except Exception as e:
            logging.error(f"OKX: Error fetching pairs: {e}")
            return set()

    async def get_all_tickers(self, session: RetryClient) -> Dict[str, float]:
        try:
            data = await self._make_request(session, self.ticker_url)
            if not data or data.get("code") != "0" or "data" not in data:
                return {}
            
            tickers = {}
            for entry in data["data"]:
                raw_symbol = entry.get("instId")
                symbol = normalize_pair_symbol(raw_symbol)
                last = entry.get("last")
                ask = entry.get("askPx")
                bid = entry.get("bidPx")
                if symbol and (ask or bid or last):
                    try:
                        ask_f = float(ask) if ask not in (None, "") else None
                        bid_f = float(bid) if bid not in (None, "") else None
                        last_f = float(last) if last not in (None, "") else None

                        # OKX can keep `last` stale on illiquid pairs while bid/ask already moved.
                        # The scanner uses a single ticker price, so use the current BBO midpoint when possible.
                        if ask_f and bid_f and ask_f > 0 and bid_f > 0:
                            tickers[symbol] = (ask_f + bid_f) / 2.0
                        elif ask_f and ask_f > 0:
                            tickers[symbol] = ask_f
                        elif bid_f and bid_f > 0:
                            tickers[symbol] = bid_f
                        elif last_f and last_f > 0:
                            tickers[symbol] = last_f
                        else:
                            continue
                        if raw_symbol:
                            self._raw_symbol_by_norm[symbol] = str(raw_symbol).upper()
                    except (ValueError, TypeError):
                        continue
            
            return tickers
        except Exception as e:
            logging.error(f"OKX: Error fetching tickers: {e}")
            return {}

    async def get_order_books(self, session: RetryClient, symbols: List[str]) -> Dict[str, Dict[str, float]]:
        results = {}
        semaphore = asyncio.Semaphore(10)

        async def _fetch_one(symbol: str) -> None:
            async with semaphore:
                try:
                    raw_symbol = self._raw_symbol_by_norm.get(symbol) or format_pair_symbol(symbol, sep="-")
                    url = f"{self.orderbook_url}?instId={raw_symbol}&sz=100"
                    data = await self._make_request(session, url)
                    if data and data.get("code") == "0" and "data" in data and data["data"]:
                        book = data["data"][0]
                        if book.get("asks") and book.get("bids"):
                            asks = book["asks"]
                            bids = book["bids"]
                            results[symbol] = {
                                "ask": float(asks[0][0]),
                                "bid": float(bids[0][0]),
                                "ask_volume": float(asks[0][1]),
                                "bid_volume": float(bids[0][1]),
                                "asks": asks,
                                "bids": bids,
                            }
                except Exception as e:
                    logging.error(f"OKX: Error fetching orderbook for {symbol}: {e}")

        await asyncio.gather(*[_fetch_one(s) for s in symbols])
        return results

    async def get_volumes(self, session: RetryClient, symbols: List[str]) -> Dict[str, float]:
        """Fetch 24h volumes for specific symbols"""
        try:
            data = await self._make_request(session, self.ticker_url)
            if not data or data.get("code") != "0" or "data" not in data:
                return {}
            
            volumes = {}
            for item in data["data"]:
                raw_symbol = item.get("instId")
                symbol = normalize_pair_symbol(raw_symbol)
                if symbol in symbols:
                    # volCcy24h is volume in quote currency (usually USDT)
                    vol = item.get("volCcy24h")
                    if vol:
                        volumes[symbol] = float(vol)
            
            return volumes
        except Exception as e:
            logging.error(f"OKX: Error fetching volumes: {e}")
            return {}
