import logging
from typing import Dict, List, Set

from aiohttp_retry import RetryClient

from config import Config
from utils.symbols import normalize_pair_symbol
from .base_exchange import Exchange


class Bitget(Exchange):
    def __init__(self, config: Config, enabled: bool = True):
        super().__init__(
            name="Bitget",
            pairs_url="https://api.bitget.com/api/v2/spot/public/symbols",
            ticker_url="https://api.bitget.com/api/v2/spot/market/tickers",
            orderbook_url="https://api.bitget.com/api/v2/spot/market/orderbook",
            config=config,
            enabled=enabled,
        )
        self._raw_symbol_by_norm: Dict[str, str] = {}

    async def get_all_pairs(self, session: RetryClient) -> Set[str]:
        try:
            data = await self._make_request(session, self.pairs_url)
            if not data or data.get("code") != "00000" or "data" not in data:
                return set()

            pairs: Set[str] = set()
            for entry in data["data"]:
                status = str(entry.get("status") or "").lower()
                if status not in ("online", "listed", "normal"):
                    continue
                raw = str(entry.get("symbol") or "").upper().strip()
                symbol = normalize_pair_symbol(raw)
                if symbol:
                    pairs.add(symbol)
                    self._raw_symbol_by_norm[symbol] = raw

            self.available_pairs = pairs
            return pairs
        except Exception as e:
            logging.error(f"Bitget: Error fetching pairs: {e}")
            return set()

    async def get_all_tickers(self, session: RetryClient) -> Dict[str, float]:
        try:
            data = await self._make_request(session, self.ticker_url)
            if not data or data.get("code") != "00000" or "data" not in data:
                return {}

            tickers: Dict[str, float] = {}
            for entry in data["data"]:
                raw = str(entry.get("symbol") or "").upper().strip()
                symbol = normalize_pair_symbol(raw)
                last = entry.get("lastPr") or entry.get("lastPrice")
                if not symbol or last in (None, ""):
                    continue
                try:
                    tickers[symbol] = float(last)
                    self._raw_symbol_by_norm[symbol] = raw
                except (TypeError, ValueError):
                    continue
            return tickers
        except Exception as e:
            logging.error(f"Bitget: Error fetching tickers: {e}")
            return {}

    async def get_order_books(self, session: RetryClient, symbols: List[str]) -> Dict[str, Dict[str, float]]:
        import asyncio
        results: Dict[str, Dict[str, float]] = {}
        semaphore = asyncio.Semaphore(10)

        async def _fetch_one(symbol: str) -> None:
            async with semaphore:
                try:
                    raw_symbol = self._raw_symbol_by_norm.get(symbol) or str(symbol or "").upper().strip()
                    if not raw_symbol:
                        return
                    url = f"{self.orderbook_url}?symbol={raw_symbol}&type=step0&limit=100"
                    data = await self._make_request(session, url)
                    if not data or data.get("code") != "00000" or "data" not in data:
                        return
                    book = data["data"] or {}
                    asks = book.get("asks") or []
                    bids = book.get("bids") or []
                    if not asks or not bids:
                        return
                    results[symbol] = {
                        "ask": float(asks[0][0]),
                        "bid": float(bids[0][0]),
                        "ask_volume": float(asks[0][1]),
                        "bid_volume": float(bids[0][1]),
                        "asks": asks,
                        "bids": bids,
                    }
                except Exception as e:
                    logging.error(f"Bitget: Error fetching orderbook for {symbol}: {e}")

        await asyncio.gather(*[_fetch_one(s) for s in symbols])
        return results

    async def get_volumes(self, session: RetryClient, symbols: List[str]) -> Dict[str, float]:
        try:
            data = await self._make_request(session, self.ticker_url)
            if not data or data.get("code") != "00000" or "data" not in data:
                return {}

            wanted = set(symbols)
            volumes: Dict[str, float] = {}
            for entry in data["data"]:
                raw = str(entry.get("symbol") or "").upper().strip()
                symbol = normalize_pair_symbol(raw)
                if symbol not in wanted:
                    continue
                vol = entry.get("usdtVolume") or entry.get("quoteVolume")
                if vol in (None, ""):
                    continue
                try:
                    volumes[symbol] = float(vol)
                except (TypeError, ValueError):
                    continue
            return volumes
        except Exception as e:
            logging.error(f"Bitget: Error fetching volumes: {e}")
            return {}
