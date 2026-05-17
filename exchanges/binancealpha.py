import asyncio
import logging
from typing import Any, Dict, List, Set, Tuple

from aiohttp_retry import RetryClient

from exchanges.base_exchange import Exchange
from config import Config
from utils.symbols import normalize_pair_symbol


class BinanceAlphaManual(Exchange):
    """Read-only Binance Alpha market-data adapter.

    Binance Alpha public API exposes market data and depth, but normal public
    API trading is not available for regular users. In this bot it is a
    MANUAL-ONLY venue: it can show Kraken/Binance Alpha or CEX/Binance Alpha
    opportunities, but it must not be treated as an auto-trading exchange.
    """

    def __init__(self, config: Config, enabled: bool = True):
        self.BASE_URL = "https://www.binance.com"
        self.token_list_url = f"{self.BASE_URL}/bapi/defi/v1/public/wallet-direct/buw/wallet/cex/alpha/all/token/list"
        self.depth_url = f"{self.BASE_URL}/bapi/defi/v1/public/alpha-trade/fullDepth"
        self._raw_alpha_symbol_by_norm: Dict[str, str] = {}
        self._token_by_norm: Dict[str, Dict[str, Any]] = {}
        self._duplicate_count_by_norm: Dict[str, int] = {}
        self._last_token_list: List[Dict[str, Any]] = []
        super().__init__(
            name="Binance Alpha (manual)",
            pairs_url=self.token_list_url,
            ticker_url=self.token_list_url,
            orderbook_url=self.depth_url,
            config=config,
            enabled=enabled,
        )
        # This exchange is intentionally read-only/manual-only.
        self.manual_only = True
        self.market_data_only = True

    def _extract_tokens(self, payload: Any) -> List[Dict[str, Any]]:
        if not isinstance(payload, dict):
            return []
        data = payload.get("data")
        if isinstance(data, list):
            return [x for x in data if isinstance(x, dict)]
        # Be tolerant if Binance wraps data differently in future.
        if isinstance(data, dict):
            for key in ("list", "tokens", "rows"):
                val = data.get(key)
                if isinstance(val, list):
                    return [x for x in val if isinstance(x, dict)]
        return []

    def _score_token(self, token: Dict[str, Any]) -> float:
        def f(key: str) -> float:
            try:
                return float(token.get(key) or 0)
            except Exception:
                return 0.0
        # Prefer liquid tokens if Alpha has several tokens with the same ticker.
        return f("liquidity") * 10.0 + f("volume24h") + f("marketCap") * 0.001

    def _alpha_symbol(self, token: Dict[str, Any]) -> str:
        alpha_id = str(token.get("alphaId") or token.get("alphaID") or "").strip().upper()
        if not alpha_id:
            return ""
        if not alpha_id.startswith("ALPHA_"):
            # Docs show ALPHA_175, but be defensive if API ever returns just a number.
            if alpha_id.isdigit():
                alpha_id = f"ALPHA_{alpha_id}"
        return f"{alpha_id}USDT"

    async def _load_tokens(self, session: RetryClient) -> List[Dict[str, Any]]:
        payload = await self._make_request(session, self.token_list_url)
        tokens = self._extract_tokens(payload)
        self._last_token_list = tokens
        return tokens

    async def check_connection(self, session: RetryClient) -> bool:
        tokens = await self._load_tokens(session)
        return bool(tokens)

    async def get_all_pairs(self, session: RetryClient) -> Set[str]:
        tokens = await self._load_tokens(session)
        by_symbol: Dict[str, Tuple[float, Dict[str, Any]]] = {}
        counts: Dict[str, int] = {}
        for token in tokens:
            try:
                symbol = str(token.get("symbol") or "").strip().upper()
                price = float(token.get("price") or 0)
                alpha_symbol = self._alpha_symbol(token)
                if not symbol or symbol in {"USDT", "USDC", "USD"} or price <= 0 or not alpha_symbol:
                    continue
                norm = normalize_pair_symbol(f"{symbol}USDT")
                if not norm:
                    continue
                counts[norm] = counts.get(norm, 0) + 1
                score = self._score_token(token)
                old = by_symbol.get(norm)
                if old is None or score > old[0]:
                    by_symbol[norm] = (score, token)
            except Exception:
                continue

        pairs: Set[str] = set()
        self._raw_alpha_symbol_by_norm.clear()
        self._token_by_norm.clear()
        self._duplicate_count_by_norm.clear()
        for norm, (_score, token) in by_symbol.items():
            alpha_symbol = self._alpha_symbol(token)
            if alpha_symbol:
                pairs.add(norm)
                self._raw_alpha_symbol_by_norm[norm] = alpha_symbol
                self._token_by_norm[norm] = token
                self._duplicate_count_by_norm[norm] = counts.get(norm, 1)
        self.available_pairs = pairs
        return pairs

    async def get_all_tickers(self, session: RetryClient) -> Dict[str, float]:
        tokens = await self._load_tokens(session)
        out: Dict[str, float] = {}
        # Same duplicate handling as get_all_pairs: highest liquidity wins,
        # but keep duplicate count so the arbitrage calculator can block ambiguous tickers.
        best: Dict[str, Tuple[float, float, Dict[str, Any]]] = {}
        counts: Dict[str, int] = {}
        for token in tokens:
            try:
                symbol = str(token.get("symbol") or "").strip().upper()
                price = float(token.get("price") or 0)
                if not symbol or symbol in {"USDT", "USDC", "USD"} or price <= 0:
                    continue
                alpha_symbol = self._alpha_symbol(token)
                if not alpha_symbol:
                    continue
                norm = normalize_pair_symbol(f"{symbol}USDT")
                counts[norm] = counts.get(norm, 0) + 1
                score = self._score_token(token)
                old = best.get(norm)
                if old is None or score > old[0]:
                    best[norm] = (score, price, token)
            except Exception:
                continue
        for norm, (_score, price, token) in best.items():
            out[norm] = float(price)
            self._raw_alpha_symbol_by_norm[norm] = self._alpha_symbol(token)
            self._token_by_norm[norm] = token
            self._duplicate_count_by_norm[norm] = counts.get(norm, 1)
        return out

    async def get_order_books(self, session: RetryClient, symbols: List[str]) -> Dict[str, Dict[str, float]]:
        results: Dict[str, Dict[str, float]] = {}
        sem = asyncio.Semaphore(6)

        async def _one(symbol: str):
            alpha_symbol = self._raw_alpha_symbol_by_norm.get(symbol)
            if not alpha_symbol:
                # Try to refresh mappings once if orderbook is requested before get_all_pairs.
                try:
                    await self.get_all_pairs(session)
                    alpha_symbol = self._raw_alpha_symbol_by_norm.get(symbol)
                except Exception:
                    alpha_symbol = None
            if not alpha_symbol:
                return
            async with sem:
                data = await self._make_request(
                    session,
                    self.depth_url,
                    params={"symbol": alpha_symbol, "limit": 20},
                )
                try:
                    book = data.get("data") if isinstance(data, dict) else None
                    bids = book.get("bids") if isinstance(book, dict) else []
                    asks = book.get("asks") if isinstance(book, dict) else []
                    if not bids or not asks:
                        return
                    bid = float(bids[0][0]); bid_vol = float(bids[0][1])
                    ask = float(asks[0][0]); ask_vol = float(asks[0][1])
                    if bid > 0 and ask > 0:
                        results[symbol] = {
                            "bid": bid,
                            "ask": ask,
                            "bid_volume": bid_vol,
                            "ask_volume": ask_vol,
                            "bids": bids,
                            "asks": asks,
                            "manual_only": True,
                            "alpha_symbol": alpha_symbol,
                            "alpha_token_name": str((self._token_by_norm.get(symbol) or {}).get("name") or ""),
                            "alpha_contract_address": str((self._token_by_norm.get(symbol) or {}).get("contractAddress") or (self._token_by_norm.get(symbol) or {}).get("contract_address") or ""),
                            "alpha_duplicate_count": int(self._duplicate_count_by_norm.get(symbol, 1) or 1),
                        }
                except Exception as exc:
                    logging.debug("Binance Alpha parse orderbook failed for %s/%s: %s", symbol, alpha_symbol, exc)

        await asyncio.gather(*[_one(s) for s in symbols])
        return results
