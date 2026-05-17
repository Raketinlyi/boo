import asyncio
import logging
from typing import Dict, List, Set

from aiohttp_retry import RetryClient

from exchanges.base_exchange import Exchange
from config import Config
from utils.symbols import normalize_pair_symbol


class PionexUS(Exchange):
    """Public Pionex/Pionex.US spot market data adapter.

    Uses Pionex public market-data API. Trade links point to pionex.us, because
    the user opens the US web interface manually.
    """

    def __init__(self, config: Config, enabled: bool = True):
        self.BASE_URL = str(config.get("pionexus_base_url", "https://api.pionex.com") or "https://api.pionex.com").rstrip("/")
        self._raw_symbol_by_norm: Dict[str, str] = {}
        super().__init__(
            name="Pionex.US",
            pairs_url=f"{self.BASE_URL}/api/v1/common/symbols",
            ticker_url=f"{self.BASE_URL}/api/v1/market/bookTickers",
            orderbook_url=f"{self.BASE_URL}/api/v1/market/depth",
            config=config,
            enabled=enabled,
        )

    async def check_connection(self, session: RetryClient) -> bool:
        data = await self._make_request(session, self.pairs_url, params={"type": "SPOT"})
        return isinstance(data, dict) and bool(data.get("result", True)) and isinstance(data.get("data"), (list, dict))

    def _extract_symbols_list(self, data):
        if not isinstance(data, dict):
            return []
        payload = data.get("data")
        if isinstance(payload, dict):
            for key in ("symbols", "list", "items"):
                if isinstance(payload.get(key), list):
                    return payload.get(key)
        if isinstance(payload, list):
            return payload
        return []

    async def get_all_pairs(self, session: RetryClient) -> Set[str]:
        data = await self._make_request(session, self.pairs_url, params={"type": "SPOT"})
        pairs: Set[str] = set()
        for item in self._extract_symbols_list(data):
            try:
                raw = str(item.get("symbol") or item.get("pair") or "").upper().replace("-", "_").replace("/", "_")
                base = str(item.get("baseCurrency") or item.get("base") or item.get("baseCoin") or "").upper()
                quote = str(item.get("quoteCurrency") or item.get("quote") or item.get("quoteCoin") or "").upper()
                status = str(item.get("status") or item.get("enable") or item.get("state") or "TRADING").upper()
                if raw and "_" in raw and (not base or not quote):
                    b, q = raw.split("_", 1)
                    base, quote = base or b, quote or q
                if raw and not base and not quote and raw.endswith("USDT"):
                    base, quote = raw[:-4], "USDT"
                if quote in {"USDT", "USD"} and base and status not in {"OFFLINE", "BREAK", "CLOSED", "DISABLED"}:
                    norm = normalize_pair_symbol(f"{base}{quote}")
                    pairs.add(norm)
                    self._raw_symbol_by_norm[norm] = raw if raw else f"{base}_{quote}"
            except Exception:
                continue
        self.available_pairs = pairs
        return pairs

    async def get_all_tickers(self, session: RetryClient) -> Dict[str, float]:
        data = await self._make_request(session, self.ticker_url)
        out: Dict[str, float] = {}
        rows = []
        if isinstance(data, dict):
            payload = data.get("data")
            if isinstance(payload, list):
                rows = payload
            elif isinstance(payload, dict):
                rows = payload.get("tickers") or payload.get("list") or payload.get("items") or []
        if isinstance(data, list):
            rows = data
        for row in rows or []:
            try:
                raw = str(row.get("symbol") or "").upper().replace("-", "_").replace("/", "_")
                sym = normalize_pair_symbol(raw)
                bid = float(row.get("bidPrice") or row.get("bid") or row.get("buy") or 0)
                ask = float(row.get("askPrice") or row.get("ask") or row.get("sell") or 0)
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
                if "_" not in raw and raw.endswith("USDT"):
                    raw = f"{raw[:-4]}_USDT"
                data = await self._make_request(session, self.orderbook_url, params={"symbol": raw, "limit": 20})
                try:
                    payload = data.get("data") if isinstance(data, dict) else data
                    bids = (payload or {}).get("bids") or []
                    asks = (payload or {}).get("asks") or []
                    if not bids or not asks:
                        return
                    b0, a0 = bids[0], asks[0]
                    bid = float(b0[0] if isinstance(b0, (list, tuple)) else b0.get("price"))
                    bid_vol = float(b0[1] if isinstance(b0, (list, tuple)) else b0.get("size") or b0.get("quantity") or 0)
                    ask = float(a0[0] if isinstance(a0, (list, tuple)) else a0.get("price"))
                    ask_vol = float(a0[1] if isinstance(a0, (list, tuple)) else a0.get("size") or a0.get("quantity") or 0)
                    if bid > 0 and ask > 0:
                        results[symbol] = {"bid": bid, "ask": ask, "bid_volume": bid_vol, "ask_volume": ask_vol, "bids": bids, "asks": asks}
                except Exception as exc:
                    logging.debug("Pionex.US parse orderbook failed for %s: %s", symbol, exc)

        await asyncio.gather(*[_one(s) for s in symbols])
        return results
