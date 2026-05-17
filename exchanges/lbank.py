import asyncio
import logging
from typing import Dict, List, Set

from aiohttp_retry import RetryClient

from exchanges.base_exchange import Exchange
from config import Config
from utils.symbols import normalize_pair_symbol, split_pair_symbol


class LBank(Exchange):
    """LBank public spot market-data adapter.

    Uses only public endpoints:
      - GET /v2/currencyPairs.do
      - GET /v2/ticker/24hr.do?symbol=all
      - GET /v2/depth.do?symbol=base_quote&size=20

    No trading, no withdrawals, no private permissions are used here.
    """

    def __init__(self, config: Config, enabled: bool = True):
        self.BASE_URL = str(config.get("lbank_base_url", "https://api.lbkex.com") or "https://api.lbkex.com").rstrip("/")
        self._raw_symbol_by_norm: Dict[str, str] = {}
        super().__init__(
            name="LBank",
            pairs_url=f"{self.BASE_URL}/v2/currencyPairs.do",
            ticker_url=f"{self.BASE_URL}/v2/ticker/24hr.do",
            orderbook_url=f"{self.BASE_URL}/v2/depth.do",
            config=config,
            enabled=enabled,
        )
        # LBank allows broad public limits, but keep concurrency conservative so home IP does not get noisy.
        self._orderbook_concurrency = int(config.get("lbank_orderbook_concurrency", 5) or 5)

    async def check_connection(self, session: RetryClient) -> bool:
        data = await self._make_request(session, self.pairs_url)
        payload = (data or {}).get("data") if isinstance(data, dict) else data
        return isinstance(payload, list) and len(payload) > 0

    def _raw_from_norm(self, symbol: str) -> str:
        raw = self._raw_symbol_by_norm.get(symbol)
        if raw:
            return raw
        base, quote = split_pair_symbol(symbol)
        if base and quote:
            return f"{base.lower()}_{quote.lower()}"
        return str(symbol or "").lower()

    async def get_all_pairs(self, session: RetryClient) -> Set[str]:
        data = await self._make_request(session, self.pairs_url)
        payload = (data or {}).get("data") if isinstance(data, dict) else data
        pairs: Set[str] = set()
        if not isinstance(payload, list):
            self.available_pairs = pairs
            return pairs
        for item in payload:
            try:
                raw = str(item or "").strip().lower().replace("-", "_").replace("/", "_")
                if not raw or "_" not in raw:
                    continue
                base, quote = raw.split("_", 1)
                if quote.upper() not in {"USDT", "USD"} or not base:
                    continue
                norm = normalize_pair_symbol(f"{base.upper()}{quote.upper()}")
                if norm:
                    pairs.add(norm)
                    self._raw_symbol_by_norm[norm] = raw
            except Exception:
                continue
        self.available_pairs = pairs
        return pairs

    async def get_all_tickers(self, session: RetryClient) -> Dict[str, float]:
        data = await self._make_request(session, self.ticker_url, params={"symbol": "all"})
        payload = (data or {}).get("data") if isinstance(data, dict) else data
        out: Dict[str, float] = {}
        if not isinstance(payload, list):
            return out
        for row in payload:
            try:
                raw = str(row.get("symbol") or "").strip().lower().replace("-", "_").replace("/", "_")
                ticker = row.get("ticker") or {}
                latest = float(ticker.get("latest") or row.get("price") or 0)
                if not raw or latest <= 0:
                    continue
                norm = normalize_pair_symbol(raw)
                if norm:
                    out[norm] = latest
                    self._raw_symbol_by_norm[norm] = raw
            except Exception:
                continue
        return out

    async def get_order_books(self, session: RetryClient, symbols: List[str]) -> Dict[str, Dict[str, float]]:
        results: Dict[str, Dict[str, float]] = {}
        sem = asyncio.Semaphore(max(1, min(12, self._orderbook_concurrency)))

        async def _one(symbol: str):
            async with sem:
                raw = self._raw_from_norm(symbol)
                data = await self._make_request(session, self.orderbook_url, params={"symbol": raw, "size": 20})
                try:
                    payload = data.get("data") if isinstance(data, dict) else data
                    bids = (payload or {}).get("bids") or []
                    asks = (payload or {}).get("asks") or []
                    if not bids or not asks:
                        return
                    bid0, ask0 = bids[0], asks[0]
                    bid = float(bid0[0] if isinstance(bid0, (list, tuple)) else bid0.get("price"))
                    bid_vol = float(bid0[1] if isinstance(bid0, (list, tuple)) else bid0.get("qty") or bid0.get("quantity") or 0)
                    ask = float(ask0[0] if isinstance(ask0, (list, tuple)) else ask0.get("price"))
                    ask_vol = float(ask0[1] if isinstance(ask0, (list, tuple)) else ask0.get("qty") or ask0.get("quantity") or 0)
                    if bid > 0 and ask > 0:
                        results[symbol] = {"bid": bid, "ask": ask, "bid_volume": bid_vol, "ask_volume": ask_vol, "bids": bids, "asks": asks}
                except Exception as exc:
                    logging.debug("LBank parse orderbook failed for %s: %s", symbol, exc)

        await asyncio.gather(*[_one(s) for s in symbols])
        return results
