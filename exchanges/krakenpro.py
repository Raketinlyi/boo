import asyncio
import logging
from typing import Dict, List, Set

from aiohttp_retry import RetryClient

from exchanges.base_exchange import Exchange
from config import Config
from utils.symbols import normalize_pair_symbol


class KrakenPro(Exchange):
    """Public Kraken/Kraken Pro spot market data adapter."""

    def __init__(self, config: Config, enabled: bool = True):
        self.BASE_URL = "https://api.kraken.com/0/public"
        self._raw_pair_by_norm: Dict[str, str] = {}
        self._ws_symbol_by_norm: Dict[str, str] = {}
        super().__init__(
            name="Kraken Pro",
            pairs_url=f"{self.BASE_URL}/AssetPairs",
            ticker_url=f"{self.BASE_URL}/Ticker",
            orderbook_url=f"{self.BASE_URL}/Depth",
            config=config,
            enabled=enabled,
        )

    @staticmethod
    def _clean_asset(asset: str) -> str:
        a = str(asset or "").upper()
        aliases = {"XBT": "BTC", "XXBT": "BTC", "ZUSD": "USD", "ZEUR": "EUR", "XETH": "ETH"}
        return aliases.get(a, a.lstrip("XZ"))

    async def get_all_pairs(self, session: RetryClient) -> Set[str]:
        data = await self._make_request(session, self.pairs_url)
        result = (data or {}).get("result") if isinstance(data, dict) else None
        pairs: Set[str] = set()
        if not isinstance(result, dict):
            self.available_pairs = pairs
            return pairs
        for raw_name, info in result.items():
            try:
                status = str(info.get("status") or "online").lower()
                quote = self._clean_asset(info.get("quote") or "")
                base = self._clean_asset(info.get("base") or "")
                wsname = str(info.get("wsname") or "")
                if status == "online" and quote in {"USDT", "USD"} and base:
                    sym = normalize_pair_symbol(f"{base}{quote}")
                    pairs.add(sym)
                    self._raw_pair_by_norm[sym] = str(raw_name)
                    if wsname:
                        self._ws_symbol_by_norm[sym] = wsname
            except Exception:
                continue
        self.available_pairs = pairs
        return pairs

    async def get_all_tickers(self, session: RetryClient) -> Dict[str, float]:
        if not self.available_pairs:
            await self.get_all_pairs(session)
        out: Dict[str, float] = {}
        raw_items = list(self._raw_pair_by_norm.items())
        for i in range(0, len(raw_items), 60):
            batch = raw_items[i:i+60]
            raw_pairs = ",".join(raw for _, raw in batch)
            data = await self._make_request(session, self.ticker_url, params={"pair": raw_pairs})
            result = (data or {}).get("result") if isinstance(data, dict) else None
            if not isinstance(result, dict):
                continue
            reverse = {raw: sym for sym, raw in self._raw_pair_by_norm.items()}
            for raw, row in result.items():
                try:
                    sym = reverse.get(raw) or next((s for s, r in batch if r == raw), None)
                    ask = float((row.get("a") or [0])[0])
                    bid = float((row.get("b") or [0])[0])
                    if sym and bid > 0 and ask > 0:
                        out[sym] = (bid + ask) / 2.0
                except Exception:
                    continue
        return out

    async def get_order_books(self, session: RetryClient, symbols: List[str]) -> Dict[str, Dict[str, float]]:
        results: Dict[str, Dict[str, float]] = {}
        sem = asyncio.Semaphore(6)

        async def _one(symbol: str):
            raw = self._raw_pair_by_norm.get(symbol)
            if not raw:
                return
            async with sem:
                data = await self._make_request(session, self.orderbook_url, params={"pair": raw, "count": 20})
                result = (data or {}).get("result") if isinstance(data, dict) else None
                if not isinstance(result, dict):
                    return
                book = next(iter(result.values()), None)
                try:
                    bids = book.get("bids") or []
                    asks = book.get("asks") or []
                    if not bids or not asks:
                        return
                    bid = float(bids[0][0]); bid_vol = float(bids[0][1])
                    ask = float(asks[0][0]); ask_vol = float(asks[0][1])
                    if bid > 0 and ask > 0:
                        results[symbol] = {"bid": bid, "ask": ask, "bid_volume": bid_vol, "ask_volume": ask_vol, "bids": bids, "asks": asks}
                except Exception as exc:
                    logging.debug("Kraken Pro parse orderbook failed for %s: %s", symbol, exc)

        await asyncio.gather(*[_one(s) for s in symbols])
        return results
