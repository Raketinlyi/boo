from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import aiohttp

from utils.symbols import normalize_pair_symbol, extract_base_asset

KRAKEN_API = "https://api.kraken.com/0/public"
COINGECKO_API = "https://api.coingecko.com/api/v3"
KYBER_API = "https://aggregator-api.kyberswap.com"

CACHE_DIR = Path("data/kraken_kyber_cache")
COINGECKO_LIST_FILE = Path("data/coingecko_list.json")
CONTRACT_INDEX_FILE = CACHE_DIR / "kraken_contract_index.json"
CONTRACT_INDEX_STATUS_FILE = CACHE_DIR / "kraken_contract_index_status.json"

# CoinGecko platform -> Kyber path segment + main stable tokens.
# Only EVM networks are included because KyberSwap Aggregator EVM API is EVM-chain based.
CHAIN_MAP: Dict[str, Dict[str, Any]] = {
    "ethereum": {
        "kyber": "ethereum",
        "stable": {"symbol": "USDC", "address": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48", "decimals": 6},
    },
    "binance-smart-chain": {
        "kyber": "bsc",
        "stable": {"symbol": "USDT", "address": "0x55d398326f99059fF775485246999027B3197955", "decimals": 18},
    },
    "polygon-pos": {
        "kyber": "polygon",
        "stable": {"symbol": "USDC", "address": "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174", "decimals": 6},
    },
    "arbitrum-one": {
        "kyber": "arbitrum",
        "stable": {"symbol": "USDC", "address": "0xaf88d065e77c8cC2239327C5EDb3A432268e5831", "decimals": 6},
    },
    "base": {
        "kyber": "base",
        "stable": {"symbol": "USDC", "address": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", "decimals": 6},
    },
    "optimistic-ethereum": {
        "kyber": "optimism",
        "stable": {"symbol": "USDC", "address": "0x0b2C639c533813f4Aa9D7837CAf62653d097Ff85", "decimals": 6},
    },
    "avalanche": {
        "kyber": "avalanche",
        "stable": {"symbol": "USDC", "address": "0xB97EF9Ef8734C71904D8002F8b6Bc66Dd9c48a6E", "decimals": 6},
    },
    "linea": {
        "kyber": "linea",
        "stable": {"symbol": "USDC", "address": "0x176211869cA2b568f2A7D4EE941E073a821EE1ff", "decimals": 6},
    },
    "mantle": {
        "kyber": "mantle",
        "stable": {"symbol": "USDT", "address": "0x201EBa5CC46D216Ce6DC03F6a759e8E766e956aE", "decimals": 6},
    },
}


def _now() -> float:
    return time.time()


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        f = float(v)
        return f if math.isfinite(f) else default
    except Exception:
        return default


def _clean_kraken_asset(asset: str) -> str:
    a = str(asset or "").upper()
    aliases = {"XBT": "BTC", "XXBT": "BTC", "ZUSD": "USD", "ZEUR": "EUR", "XETH": "ETH"}
    return aliases.get(a, a.lstrip("XZ"))


def _read_json(path: Path, max_age: Optional[float] = None) -> Optional[Any]:
    try:
        if not path.exists():
            return None
        if max_age is not None and (_now() - path.stat().st_mtime) > max_age:
            return None
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _write_json(path: Path, data: Any) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        tmp.replace(path)
    except Exception as exc:
        logging.debug("cache write failed for %s: %s", path, exc)


def _index_status() -> Dict[str, Any]:
    status = _read_json(CONTRACT_INDEX_STATUS_FILE) or {}
    if not isinstance(status, dict):
        status = {}
    try:
        # Try full batch index first (528 assets)
        full_index_path = Path("data/kraken_contracts_full/index.json")
        if full_index_path.exists():
            full_data = _read_json(full_index_path) or {}
            if isinstance(full_data, dict) and full_data.get("assets"):
                assets = full_data.get("assets", {})
                # Always override entries/age from the authoritative index file
                status["entries"] = len(assets)
                status["updated_at"] = full_data.get("updated_at")
                status["age_sec"] = max(0, int(_now() - float(full_data.get("updated_at") or 0))) if full_data.get("updated_at") else None
                status["source"] = "data/kraken_contracts_full/index.json"
                status.setdefault("message", f"Индекс готов: {len(assets)} подтверждённых монет")
                status.setdefault("state", "ready")
                return status

        # Fallback to old index (14 assets)
        payload = _read_json(CONTRACT_INDEX_FILE) or {}
        if isinstance(payload, dict):
            status.setdefault("entries", len(payload.get("entries") or []))
            status.setdefault("updated_at", payload.get("updated_at"))
            status.setdefault("age_sec", max(0, int(_now() - float(payload.get("updated_at") or 0))) if payload.get("updated_at") else None)
    except Exception:
        pass
    status.setdefault("state", "missing")
    return status


def _set_index_status(**kwargs: Any) -> None:
    status = _index_status()
    status.update(kwargs)
    status["status_updated_at"] = _now()
    _write_json(CONTRACT_INDEX_STATUS_FILE, status)


def get_kraken_kyber_index_status() -> Dict[str, Any]:
    return _index_status()


@dataclass
class KrakenPair:
    norm: str
    base: str
    quote: str
    raw: str
    wsname: str
    bid: float = 0.0
    ask: float = 0.0

    @property
    def mid(self) -> float:
        if self.bid > 0 and self.ask > 0:
            return (self.bid + self.ask) / 2.0
        return self.bid or self.ask or 0.0


class KrakenKyberScanner:
    """Separate Kraken Pro ↔ KyberSwap scanner.

    It intentionally does NOT reuse the normal CEX↔CEX pair matcher.  This tab starts
    only when requested by the UI and resolves contracts by:
      Kraken pair -> CoinGecko exact symbol candidates -> CoinGecko Kraken ticker
      confirmation + price match -> platforms/contracts -> KyberSwap route quote.
    """

    def __init__(self, config: Any):
        self.config = config
        self.cache_ttl = int(self._cfg("kraken_kyber_cache_ttl_sec", 120) or 120)
        self.contract_cache_ttl = int(self._cfg("kraken_kyber_contract_cache_ttl_sec", 86400) or 86400)
        self.route_cache_ttl = int(self._cfg("kraken_kyber_route_cache_ttl_sec", 8) or 8)
        self.price_match_pct = float(self._cfg("kraken_kyber_price_match_pct", 20.0) or 20.0)
        self.max_candidates = int(self._cfg("kraken_kyber_max_candidates_per_symbol", 8) or 8)
        self.supported_chains = set(self._cfg("kraken_kyber_chains", [m["kyber"] for m in CHAIN_MAP.values()]) or [])
        self.use_contract_index = bool(self._cfg("kraken_kyber_use_contract_index", True))
        self.index_refresh_sec = int(self._cfg("kraken_kyber_index_refresh_sec", 5 * 3600) or (5 * 3600))
        self.index_asset_limit = int(self._cfg("kraken_kyber_index_asset_limit", 600) or 600)
        self.cg_delay_sec = float(self._cfg("kraken_kyber_coingecko_delay_sec", 2.2) or 2.2)
        self.kraken_ticker_ttl = int(self._cfg("kraken_kyber_kraken_ticker_ttl_sec", 5) or 5)
        self.kraken_orderbook_ttl = int(self._cfg("kraken_kyber_kraken_orderbook_ttl_sec", 5) or 5)
        self.kraken_orderbook_count = int(self._cfg("kraken_kyber_kraken_orderbook_count", 100) or 100)
        self.kraken_depth_min_fill_pct = float(self._cfg("kraken_kyber_kraken_depth_min_fill_pct", 95.0) or 95.0)
        self.reject_cg_stale_anomaly = bool(self._cfg("kraken_kyber_reject_cg_stale_anomaly", True))
        self.include_cg_depth = bool(self._cfg("kraken_kyber_include_coingecko_depth", False))
        self.headers = {"User-Agent": "ARBX Kraken-Kyber scanner/1.0", "x-client-id": "arbx-kraken-kyber"}
        # Optional per-service HTTP/HTTPS proxy/private egress IP.
        # Kraken public order books do not require auth/IP whitelist, but users may route
        # traffic via a private/static proxy to keep latency and source IP stable.
        self.proxy_all = str(self._cfg("kraken_kyber_proxy_url", "") or os.getenv("KRAKEN_KYBER_PROXY_URL", "") or "").strip()
        self.proxy_kraken = str(self._cfg("kraken_kyber_kraken_proxy_url", "") or os.getenv("KRAKEN_PROXY_URL", "") or self.proxy_all).strip()
        self.proxy_coingecko = str(self._cfg("kraken_kyber_coingecko_proxy_url", "") or os.getenv("COINGECKO_PROXY_URL", "") or self.proxy_all).strip()
        self.proxy_kyber = str(self._cfg("kraken_kyber_kyber_proxy_url", "") or os.getenv("KYBER_PROXY_URL", "") or self.proxy_all).strip()

    def _cfg(self, key: str, default: Any = None) -> Any:
        try:
            if hasattr(self.config, "get"):
                return self.config.get(key, default)
        except Exception:
            pass
        return default

    def _proxy_for_url(self, url: str) -> Optional[str]:
        u = str(url or "").lower()
        proxy = ""
        if "api.kraken.com" in u:
            proxy = self.proxy_kraken
        elif "coingecko.com" in u:
            proxy = self.proxy_coingecko
        elif "kyberswap.com" in u:
            proxy = self.proxy_kyber
        else:
            proxy = self.proxy_all
        return proxy or None

    async def _get_json(self, session: aiohttp.ClientSession, url: str, *, params: Optional[Dict[str, Any]] = None, ttl: Optional[int] = None, cache_key: Optional[str] = None) -> Optional[Any]:
        if ttl and cache_key:
            cached = _read_json(CACHE_DIR / cache_key, ttl)
            if cached is not None:
                return cached
        try:
            async with session.get(url, params=params, headers=self.headers, timeout=aiohttp.ClientTimeout(total=12), proxy=self._proxy_for_url(url)) as resp:
                if resp.status != 200:
                    txt = await resp.text()
                    logging.debug("GET %s status %s: %s", url, resp.status, txt[:160])
                    return None
                data = await resp.json(content_type=None)
                if ttl and cache_key:
                    _write_json(CACHE_DIR / cache_key, data)
                return data
        except Exception as exc:
            logging.debug("GET %s failed: %s", url, exc)
            return None

    async def get_kraken_pairs(self, session: aiohttp.ClientSession) -> List[KrakenPair]:
        data = await self._get_json(session, f"{KRAKEN_API}/AssetPairs", ttl=86400, cache_key="kraken_asset_pairs.json")
        result = (data or {}).get("result") if isinstance(data, dict) else None
        pairs: List[KrakenPair] = []
        if not isinstance(result, dict):
            return pairs
        seen = set()
        for raw, info in result.items():
            try:
                status = str(info.get("status") or "online").lower()
                base = _clean_kraken_asset(info.get("base") or "")
                quote = _clean_kraken_asset(info.get("quote") or "")
                if status != "online" or quote not in {"USD", "USDT"} or not base:
                    continue
                norm = normalize_pair_symbol(f"{base}{quote}")
                key = (base, quote)
                if key in seen:
                    continue
                seen.add(key)
                pairs.append(KrakenPair(norm=norm, base=base, quote=quote, raw=str(raw), wsname=str(info.get("wsname") or "")))
            except Exception:
                continue
        return pairs

    async def enrich_kraken_prices(self, session: aiohttp.ClientSession, pairs: List[KrakenPair]) -> List[KrakenPair]:
        by_raw = {p.raw: p for p in pairs}
        raws = list(by_raw)
        for i in range(0, len(raws), 60):
            batch = raws[i:i + 60]
            data = await self._get_json(session, f"{KRAKEN_API}/Ticker", params={"pair": ",".join(batch)}, ttl=self.kraken_ticker_ttl, cache_key=f"kraken_ticker_{i}.json")
            result = (data or {}).get("result") if isinstance(data, dict) else None
            if not isinstance(result, dict):
                continue
            for raw_ret, row in result.items():
                p = by_raw.get(raw_ret)
                if not p:
                    # Kraken sometimes returns alternate keys; match by raw pair prefix if possible.
                    p = next((x for x in pairs if x.raw == raw_ret), None)
                if not p:
                    continue
                p.bid = _safe_float((row.get("b") or [0])[0])
                p.ask = _safe_float((row.get("a") or [0])[0])
        return [p for p in pairs if p.bid > 0 and p.ask > 0]

    async def get_kraken_orderbook(self, session: aiohttp.ClientSession, pair: KrakenPair) -> Optional[Dict[str, Any]]:
        """Fetch live Kraken order book snapshot for the pair.

        This is the executable Kraken depth used for Kraken↔Kyber calculations.
        It is NOT CoinGecko depth. The cache TTL is intentionally only a few seconds.
        """
        data = await self._get_json(
            session,
            f"{KRAKEN_API}/Depth",
            params={"pair": pair.raw, "count": self.kraken_orderbook_count},
            ttl=self.kraken_orderbook_ttl,
            cache_key=f"kraken_depth_{pair.raw}_{self.kraken_orderbook_count}.json",
        )
        result = (data or {}).get("result") if isinstance(data, dict) else None
        if not isinstance(result, dict) or not result:
            return None
        row = result.get(pair.raw)
        if not isinstance(row, dict):
            # Kraken may return alternate key aliases. Use the first matching object.
            row = next((v for v in result.values() if isinstance(v, dict) and ("asks" in v or "bids" in v)), None)
        if not isinstance(row, dict):
            return None
        asks = row.get("asks") if isinstance(row.get("asks"), list) else []
        bids = row.get("bids") if isinstance(row.get("bids"), list) else []
        return {"asks": asks, "bids": bids, "pair": pair.raw, "fetched_at": _now(), "ttl_sec": self.kraken_orderbook_ttl}

    def _quote_buy_base_with_usd(self, levels: List[Any], notional_usd: float) -> Dict[str, Any]:
        """Spend quote/USD on asks and return base qty + VWAP."""
        remaining = max(0.0, float(notional_usd or 0.0))
        spent = 0.0
        base = 0.0
        depth_usd = 0.0
        levels_used = 0
        for lvl in levels or []:
            try:
                price = _safe_float(lvl[0])
                qty = _safe_float(lvl[1])
            except Exception:
                continue
            if price <= 0 or qty <= 0:
                continue
            level_usd = price * qty
            depth_usd += level_usd
            if remaining <= 0:
                continue
            take_usd = min(remaining, level_usd)
            if take_usd > 0:
                base += take_usd / price
                spent += take_usd
                remaining -= take_usd
                levels_used += 1
        fill_pct = (spent / notional_usd * 100.0) if notional_usd > 0 else 0.0
        return {
            "base_qty": base,
            "quote_spent": spent,
            "avg_price": (spent / base) if base > 0 else 0.0,
            "fill_pct": fill_pct,
            "depth_usd": depth_usd,
            "levels_used": levels_used,
        }

    def _quote_sell_base_for_usd(self, levels: List[Any], base_qty: float) -> Dict[str, Any]:
        """Sell base on bids and return quote/USD proceeds + VWAP."""
        remaining = max(0.0, float(base_qty or 0.0))
        sold = 0.0
        proceeds = 0.0
        depth_base = 0.0
        depth_usd = 0.0
        levels_used = 0
        for lvl in levels or []:
            try:
                price = _safe_float(lvl[0])
                qty = _safe_float(lvl[1])
            except Exception:
                continue
            if price <= 0 or qty <= 0:
                continue
            depth_base += qty
            depth_usd += price * qty
            if remaining <= 0:
                continue
            take_base = min(remaining, qty)
            if take_base > 0:
                sold += take_base
                proceeds += take_base * price
                remaining -= take_base
                levels_used += 1
        fill_pct = (sold / base_qty * 100.0) if base_qty > 0 else 0.0
        return {
            "base_sold": sold,
            "quote_received": proceeds,
            "avg_price": (proceeds / sold) if sold > 0 else 0.0,
            "fill_pct": fill_pct,
            "depth_base": depth_base,
            "depth_usd": depth_usd,
            "levels_used": levels_used,
        }

    async def load_coin_list(self, session: aiohttp.ClientSession) -> List[Dict[str, Any]]:
        local = _read_json(COINGECKO_LIST_FILE, 86400 * 7)
        if isinstance(local, list) and local:
            return local
        data = await self._get_json(session, f"{COINGECKO_API}/coins/list", params={"include_platform": "true"}, ttl=self.index_refresh_sec, cache_key="coingecko_list_platforms.json")
        if isinstance(data, list):
            try:
                COINGECKO_LIST_FILE.parent.mkdir(parents=True, exist_ok=True)
                _write_json(COINGECKO_LIST_FILE, data)
            except Exception:
                pass
            return data
        return []

    async def _coin_detail(self, session: aiohttp.ClientSession, coin_id: str) -> Optional[Dict[str, Any]]:
        return await self._get_json(
            session,
            f"{COINGECKO_API}/coins/{coin_id}",
            params={"localization": "false", "tickers": "false", "market_data": "true", "community_data": "false", "developer_data": "false", "sparkline": "false"},
            ttl=self.contract_cache_ttl,
            cache_key=f"cg_coin_{coin_id}.json",
        )

    async def _coin_kraken_tickers(self, session: aiohttp.ClientSession, coin_id: str) -> Optional[Dict[str, Any]]:
        return await self._get_json(
            session,
            f"{COINGECKO_API}/coins/{coin_id}/tickers",
            params={"exchange_ids": "kraken", "include_exchange_logo": "false", "page": 1, "order": "volume_desc", "depth": "true"},
            ttl=300,
            cache_key=f"cg_kraken_tickers_{coin_id}.json",
        )

    def _coin_price_usd(self, detail: Dict[str, Any]) -> float:
        try:
            return float((((detail.get("market_data") or {}).get("current_price") or {}).get("usd")) or 0)
        except Exception:
            return 0.0

    def _platform_contracts(self, detail: Dict[str, Any]) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        platforms = detail.get("platforms") if isinstance(detail, dict) else None
        details = detail.get("detail_platforms") if isinstance(detail, dict) else None
        if not isinstance(platforms, dict):
            return out
        for cg_platform, contract in platforms.items():
            contract = str(contract or "").strip()
            meta = CHAIN_MAP.get(str(cg_platform or ""))
            if not meta or not contract or contract == "0x0000000000000000000000000000000000000000":
                continue
            kyber_chain = meta["kyber"]
            if self.supported_chains and kyber_chain not in self.supported_chains:
                continue
            dec = None
            if isinstance(details, dict):
                try:
                    dec = ((details.get(cg_platform) or {}).get("decimal_place"))
                except Exception:
                    dec = None
            try:
                decimals = int(dec) if dec is not None else 18
            except Exception:
                decimals = 18
            if decimals <= 0 or decimals > 36:
                decimals = 18
            out.append({
                "cg_platform": cg_platform,
                "chain": kyber_chain,
                "contract": contract,
                "decimals": decimals,
                "stable": meta["stable"],
            })
        return out

    def _kraken_ticker_match(self, tickers_payload: Dict[str, Any], pair: KrakenPair) -> Tuple[bool, Optional[float], str, Dict[str, Any]]:
        """Confirm CoinGecko candidate against Kraken ticker and extract CG depth flags.

        CoinGecko does NOT give a full exchange order book here. With depth=true it
        returns ±2% depth fields (cost_to_move_up_usd / cost_to_move_down_usd),
        spread and stale/anomaly flags. Real executable price still comes from Kraken
        live ticker/orderbook + Kyber live route.
        """
        tickers = (tickers_payload or {}).get("tickers") if isinstance(tickers_payload, dict) else None
        if not isinstance(tickers, list):
            return False, None, "no coingecko kraken tickers", {}
        best_price = None
        best_meta: Dict[str, Any] = {}
        for t in tickers:
            try:
                base = str(t.get("base") or "").upper().replace("$", "")
                target = str(t.get("target") or "").upper()
                market = ((t.get("market") or {}).get("identifier") or (t.get("market") or {}).get("name") or "")
                if "kraken" not in str(market).lower():
                    continue
                if base != pair.base.upper():
                    continue
                if target not in {"USD", "USDT"}:
                    continue
                last = _safe_float(t.get("last") or ((t.get("converted_last") or {}).get("usd")))
                if last <= 0:
                    continue
                best_price = last
                best_meta = {
                    "source": "CoinGecko /coins/{id}/tickers?exchange_ids=kraken&depth=true",
                    "market_identifier": ((t.get("market") or {}).get("identifier") or "kraken"),
                    "base": base,
                    "target": target,
                    "last": last,
                    "converted_last_usd": _safe_float(((t.get("converted_last") or {}).get("usd"))),
                    "converted_volume_usd": _safe_float(((t.get("converted_volume") or {}).get("usd"))),
                    "trust_score": t.get("trust_score"),
                    "is_stale": bool(t.get("is_stale")),
                    "is_anomaly": bool(t.get("is_anomaly")),
                    "bid_ask_spread_pct": _safe_float(t.get("bid_ask_spread_percentage"), None),
                    "cost_to_move_up_usd": _safe_float(t.get("cost_to_move_up_usd"), None),
                    "cost_to_move_down_usd": _safe_float(t.get("cost_to_move_down_usd"), None),
                    "last_traded_at": t.get("last_traded_at"),
                    "last_fetch_at": t.get("last_fetch_at"),
                    "trade_url": t.get("trade_url"),
                }
                break
            except Exception:
                continue
        if not best_price:
            return False, None, "no exact Kraken ticker for this base/quote on CoinGecko", {}
        if self.reject_cg_stale_anomaly and (best_meta.get("is_stale") or best_meta.get("is_anomaly")):
            return False, best_price, "CoinGecko Kraken ticker is stale/anomaly; skipped", best_meta
        diff = abs(best_price - pair.mid) / pair.mid * 100.0 if pair.mid > 0 else 9999.0
        best_meta["price_match_diff_pct"] = diff
        if diff > self.price_match_pct:
            return False, best_price, f"CoinGecko Kraken ticker differs from Kraken live by {diff:.2f}% > {self.price_match_pct:.2f}%", best_meta
        return True, best_price, f"Kraken ticker match OK ({diff:.2f}%)", best_meta

    async def resolve_contracts_for_pair(self, session: aiohttp.ClientSession, coin_list: List[Dict[str, Any]], pair: KrakenPair) -> Tuple[Optional[Dict[str, Any]], List[Dict[str, Any]], List[str], Dict[str, Any]]:
        notes: List[str] = []
        candidates = [c for c in coin_list if str(c.get("symbol") or "").upper() == pair.base.upper()]
        candidates = candidates[: self.max_candidates]
        if not candidates:
            return None, [], [f"CoinGecko candidates not found for {pair.base}"], {}

        scored: List[Tuple[float, Dict[str, Any], List[Dict[str, Any]], List[str], Dict[str, Any]]] = []
        sem = asyncio.Semaphore(3)

        async def _one(c: Dict[str, Any]):
            async with sem:
                cid = str(c.get("id") or "")
                local_notes: List[str] = []
                if not cid:
                    return
                detail = await self._coin_detail(session, cid)
                if not isinstance(detail, dict):
                    return
                cg_price = self._coin_price_usd(detail)
                if cg_price <= 0:
                    local_notes.append("CoinGecko price missing")
                else:
                    diff = abs(cg_price - pair.mid) / pair.mid * 100.0 if pair.mid > 0 else 9999.0
                    if diff > self.price_match_pct:
                        local_notes.append(f"CoinGecko price mismatch {diff:.2f}%")
                        return
                    local_notes.append(f"CoinGecko price match {diff:.2f}%")
                tickers = await self._coin_kraken_tickers(session, cid)
                ok, kr_price, msg, cg_depth = self._kraken_ticker_match(tickers or {}, pair)
                local_notes.append(msg)
                if not ok:
                    return
                contracts = self._platform_contracts(detail)
                if not contracts:
                    local_notes.append("no supported EVM contracts")
                    return
                score = 100.0
                try:
                    score += min(50.0, math.log10(float(((detail.get("market_data") or {}).get("total_volume") or {}).get("usd") or 0) + 1) * 5.0)
                except Exception:
                    pass
                scored.append((score, detail, contracts, local_notes, cg_depth))

        await asyncio.gather(*[_one(c) for c in candidates], return_exceptions=True)
        if not scored:
            return None, [], [f"No verified CoinGecko candidate for {pair.base}: require Kraken ticker + ±{self.price_match_pct:.0f}% price match"], {}
        scored.sort(key=lambda x: x[0], reverse=True)
        best_score, best_detail, best_contracts, best_notes, best_cg_depth = scored[0]
        # If two candidates are very close, mark ambiguous and reject to avoid wrong-token routes.
        if len(scored) > 1 and (best_score - scored[1][0]) < 5:
            return None, [], [f"Ambiguous CoinGecko match for {pair.base}: multiple candidates verified; skipped"], {}
        return best_detail, best_contracts, best_notes, best_cg_depth

    def _to_int_amount(self, human: float, decimals: int) -> str:
        if human <= 0:
            human = 0
        return str(int(human * (10 ** decimals)))

    async def kyber_route(self, session: aiohttp.ClientSession, chain: str, token_in: str, token_out: str, amount_in: str) -> Optional[Dict[str, Any]]:
        cache_key = f"kyber_{chain}_{token_in.lower()}_{token_out.lower()}_{amount_in}.json".replace("/", "_")
        data = await self._get_json(
            session,
            f"{KYBER_API}/{chain}/api/v1/routes",
            params={"tokenIn": token_in, "tokenOut": token_out, "amountIn": amount_in},
            ttl=self.route_cache_ttl,
            cache_key=cache_key,
        )
        if not isinstance(data, dict):
            logging.warning(f"[Kyber] Route failed on {chain} for {token_in} -> {token_out}: API response is not a dict. Data: {data}")
            return None
        route = data.get("data") if isinstance(data.get("data"), dict) else data
        summary = route.get("routeSummary") if isinstance(route, dict) else None
        if not isinstance(summary, dict):
            logging.warning(f"[Kyber] Route failed on {chain} for {token_in} -> {token_out}: No routeSummary found. API Response: {data.get('message', data)}")
            return None
        amount_out = _safe_float(summary.get("amountOut") or summary.get("outputAmount") or 0)
        amount_in_v = _safe_float(summary.get("amountIn") or amount_in)
        if amount_out <= 0 or amount_in_v <= 0:
            logging.warning(f"[Kyber] Route failed on {chain} for {token_in} -> {token_out}: Invalid amounts (in: {amount_in_v}, out: {amount_out}).")
            return None
        return {"summary": summary, "amount_in": amount_in_v, "amount_out": amount_out}


    def load_contract_index(self) -> Dict[str, Any]:
        """Load contract index from full batch download or fallback to old index."""
        # Try full batch index first (528 assets vs 14 in old index)
        full_index_path = Path("data/kraken_contracts_full/index.json")
        if full_index_path.exists():
            try:
                with full_index_path.open("r", encoding="utf-8") as f:
                    full_data = json.load(f)
                if isinstance(full_data, dict) and full_data.get("assets"):
                    # Convert full index format to scanner format
                    converted = {
                        "version": 2,
                        "source": "batch_download",
                        "updated_at": full_data.get("updated_at"),
                        "entries": []
                    }
                    for symbol, asset_data in full_data["assets"].items():
                        if not isinstance(asset_data, dict):
                            continue
                        contracts = asset_data.get("contracts", [])
                        if not contracts:
                            continue

                        # Convert to scanner format
                        scanner_contracts = []
                        for c in contracts:
                            platform = c.get("platform", "")
                            # Map CoinGecko platform names to Kyber chain names
                            chain_map = {
                                "ethereum": "ethereum",
                                "binance-smart-chain": "bsc",
                                "polygon-pos": "polygon",
                                "arbitrum-one": "arbitrum",
                                "optimistic-ethereum": "optimism",
                                "base": "base",
                                "avalanche": "avalanche",
                                "linea": "linea",
                                "mantle": "mantle",
                            }
                            kyber_chain = chain_map.get(platform)
                            if not kyber_chain:
                                continue

                            scanner_contracts.append({
                                "cg_platform": platform,
                                "chain": kyber_chain,
                                "contract": c.get("contract", ""),
                                "decimals": c.get("decimals", 18),
                                "stable": CHAIN_MAP.get(platform, {}).get("stable", {})
                            })

                        if scanner_contracts:
                            converted["entries"].append({
                                "status": "verified",
                                "asset": symbol,
                                "symbol": f"{symbol}USD",
                                "coin_id": asset_data.get("coingecko_id", ""),
                                "coin_name": asset_data.get("name", symbol),
                                "contracts": scanner_contracts,
                                "notes": ["Loaded from batch download"],
                                "verified_at": asset_data.get("fetched_at", time.time())
                            })

                    logging.info(f"Loaded full contract index: {len(converted['entries'])} assets")
                    return converted
            except Exception as exc:
                logging.warning(f"Failed to load full contract index: {exc}")

        # Fallback to old index
        payload = _read_json(CONTRACT_INDEX_FILE) or {}
        return payload if isinstance(payload, dict) else {}

    def contract_index_needs_refresh(self) -> bool:
        payload = self.load_contract_index()
        updated = _safe_float(payload.get("updated_at") if isinstance(payload, dict) else 0)
        if not updated:
            return True
        return (_now() - updated) > self.index_refresh_sec

    def _index_entries_by_asset(self) -> Dict[str, Dict[str, Any]]:
        payload = self.load_contract_index()
        entries = payload.get("entries") if isinstance(payload, dict) else []
        out: Dict[str, Dict[str, Any]] = {}
        if isinstance(entries, list):
            for e in entries:
                if not isinstance(e, dict):
                    continue
                asset = str(e.get("asset") or e.get("base") or "").upper()
                if asset and e.get("status") == "verified":
                    out[asset] = e
        return out

    async def build_contract_index(self, *, force: bool = False, asset_limit: Optional[int] = None) -> Dict[str, Any]:
        """Build persistent Kraken -> CoinGecko -> contract index.

        This is intentionally slow and polite to CoinGecko.  It stores only identity data
        (Kraken pair, CoinGecko id, networks/contracts).  It does not store live arbitrage
        prices; Kraken order books and Kyber quotes are still refreshed at scan time.
        """
        if not force and not self.contract_index_needs_refresh():
            status = _index_status()
            status["state"] = "fresh"
            return status

        started = _now()
        limit = int(asset_limit or self.index_asset_limit or 600)
        _set_index_status(state="running", done=0, total=0, current="", message="Загружаю Kraken pairs и CoinGecko list...", started_at=started)
        entries: List[Dict[str, Any]] = []
        rejected: List[Dict[str, Any]] = []

        connector = aiohttp.TCPConnector(limit=6, ttl_dns_cache=300)
        async with aiohttp.ClientSession(connector=connector, trust_env=True) as session:
            pairs = await self.get_kraken_pairs(session)
            pairs = await self.enrich_kraken_prices(session, pairs)
            if not pairs:
                _set_index_status(state="error", done=0, total=0, current="", message="Не удалось получить свежие пары/цены Kraken; старый индекс не перезаписан", finished_at=_now())
                return _index_status()
            pairs.sort(key=lambda p: (0 if p.quote == "USDT" else 1, p.base))
            if limit > 0:
                pairs = pairs[:limit]
            coin_list = await self.load_coin_list(session)
            total = len(pairs)
            _set_index_status(state="running", done=0, total=total, current="", message=f"Строю индекс контрактов Kraken: 0/{total}")
            for idx, pair in enumerate(pairs, start=1):
                _set_index_status(state="running", done=idx-1, total=total, current=pair.base, message=f"Проверяю {pair.base} через CoinGecko/Kraken tickers")
                try:
                    detail, contracts, notes, cg_depth = await self.resolve_contracts_for_pair(session, coin_list, pair)
                    if detail and contracts:
                        entry = {
                            "status": "verified",
                            "asset": pair.base,
                            "symbol": pair.norm,
                            "quote": pair.quote,
                            "kraken_pair_raw": pair.raw,
                            "kraken_wsname": pair.wsname,
                            "kraken_mid_at_index": pair.mid,
                            "coin_id": str(detail.get("id") or ""),
                            "coin_name": str(detail.get("name") or pair.base),
                            "coin_symbol": str(detail.get("symbol") or pair.base).upper(),
                            "contracts": contracts,
                            "notes": notes[:6],
                            "coingecko_kraken_ticker": cg_depth if self.include_cg_depth else {},
                            "verified_at": _now(),
                            "match_rule": f"CoinGecko Kraken ticker + live Kraken price ±{self.price_match_pct:.0f}%",
                        }
                        entries.append(entry)
                    else:
                        rejected.append({"asset": pair.base, "symbol": pair.norm, "reason": "; ".join(notes[:3]) if notes else "not verified"})
                except Exception as exc:
                    rejected.append({"asset": pair.base, "symbol": pair.norm, "reason": str(exc)[:180]})
                # Save partial result so the tab can use already confirmed contracts.
                if idx % 5 == 0 or idx == total:
                    payload = {
                        "version": 1,
                        "updated_at": _now(),
                        "started_at": started,
                        "completed": False,
                        "entries": entries,
                        "rejected_sample": rejected[-50:],
                        "stats": {"done": idx, "total": total, "verified": len(entries), "rejected": len(rejected)},
                        "rule": "contracts are identity cache only; prices are refreshed live during scan",
                    }
                    _write_json(CONTRACT_INDEX_FILE, payload)
                    _set_index_status(state="running", done=idx, total=total, current=pair.base, entries=len(entries), rejected=len(rejected), message=f"Индекс Kraken↔Kyber: {idx}/{total}, найдено контрактов {len(entries)}")
                if self.cg_delay_sec > 0:
                    await asyncio.sleep(self.cg_delay_sec)

        payload = {
            "version": 1,
            "updated_at": _now(),
            "started_at": started,
            "completed": True,
            "entries": entries,
            "rejected_sample": rejected[-100:],
            "stats": {"done": len(entries) + len(rejected), "total": len(entries) + len(rejected), "verified": len(entries), "rejected": len(rejected)},
            "rule": "Kraken contracts are not from Kraken API; CoinGecko candidate must have Kraken ticker and price match",
        }
        _write_json(CONTRACT_INDEX_FILE, payload)
        _set_index_status(state="ready", done=payload["stats"]["done"], total=payload["stats"]["total"], entries=len(entries), rejected=len(rejected), current="", message=f"Индекс готов: {len(entries)} подтверждённых монет", finished_at=_now(), updated_at=payload["updated_at"], age_sec=0)
        return _index_status()

    async def scan(self, *, min_spread: float = 0.0, max_spread: Optional[float] = None, limit: int = 100, asset_limit: Optional[int] = None, notional_usd: Optional[float] = None) -> Dict[str, Any]:
        started = _now()
        if not bool(self._cfg("kraken_kyber_enabled", True)):
            return {"success": True, "data": [], "routes": [], "notes": ["Kraken↔Kyber disabled in config"], "scanner": "kraken_kyber"}
        asset_limit = int(asset_limit if asset_limit is not None else (self._cfg("kraken_kyber_asset_limit", 120) or 120))
        notional_usd = float(notional_usd or self._cfg("kraken_kyber_notional_usd", 250.0) or 250.0)
        asset_limit = max(1, min(asset_limit, 1000))
        limit = max(1, min(int(limit or 100), 300))
        min_spread = float(min_spread or 0.0)
        max_spread = float(max_spread if max_spread is not None else (self._cfg("max_spread", 100.0) or 100.0))

        connector = aiohttp.TCPConnector(limit=40, ttl_dns_cache=300)
        async with aiohttp.ClientSession(connector=connector, trust_env=True) as session:
            pairs = await self.get_kraken_pairs(session)
            pairs = await self.enrich_kraken_prices(session, pairs)
            index_status = _index_status()
            index_map = self._index_entries_by_asset() if self.use_contract_index else {}
            if index_map:
                pairs = [p for p in pairs if p.base.upper() in index_map]
            # Prefer USDT pairs, then USD. Keep deterministic by base name.
            pairs.sort(key=lambda p: (0 if p.quote == "USDT" else 1, p.base))
            if asset_limit > 0:
                pairs = pairs[:asset_limit]
            coin_list = [] if index_map else await self.load_coin_list(session)
            routes: List[Dict[str, Any]] = []
            rejected: List[Dict[str, Any]] = []
            stats: Dict[str, int] = {"pairs_with_contracts": len(pairs), "kyber_quotes": 0, "routes_raw": 0, "below_min_spread": 0, "above_max_spread": 0}
            sem = asyncio.Semaphore(int(self._cfg("kraken_kyber_max_assets_parallel", 12) or 12))
            kyber_sem = asyncio.Semaphore(int(self._cfg("kraken_kyber_max_kyber_parallel", 20) or 20))

            async def scan_pair(pair: KrakenPair):
                async with sem:
                    entry = index_map.get(pair.base.upper()) if index_map else None
                    if entry:
                        contracts = entry.get("contracts") if isinstance(entry.get("contracts"), list) else []
                        notes = list(entry.get("notes") or [])
                        coin_id = str(entry.get("coin_id") or "")
                        coin_name = str(entry.get("coin_name") or pair.base)
                        cg_depth = entry.get("coingecko_kraken_ticker") if isinstance(entry.get("coingecko_kraken_ticker"), dict) else {}
                    else:
                        detail, contracts, notes, cg_depth = await self.resolve_contracts_for_pair(session, coin_list, pair)
                        if not detail or not contracts:
                            reason = "; ".join(notes[:3]) if notes else "not verified in contract index"
                            rejected.append({"symbol": pair.norm, "asset": pair.base, "reason": reason})
                            return
                        coin_id = str(detail.get("id") or "")
                        coin_name = str(detail.get("name") or pair.base)
                    orderbook = await self.get_kraken_orderbook(session, pair)
                    if not orderbook:
                        rejected.append({"symbol": pair.norm, "asset": pair.base, "reason": "Kraken orderbook unavailable"})
                        return
                    kraken_buy = self._quote_buy_base_with_usd(orderbook.get("asks") or [], notional_usd)
                    kraken_sell = self._quote_sell_base_for_usd(orderbook.get("bids") or [], notional_usd / pair.mid if pair.mid > 0 else 0)

                    kraken_buy_valid = float(kraken_buy.get("fill_pct") or 0) >= self.kraken_depth_min_fill_pct
                    kraken_sell_valid = float(kraken_sell.get("fill_pct") or 0) >= self.kraken_depth_min_fill_pct

                    if not kraken_buy_valid and not kraken_sell_valid:
                        rejected.append({"symbol": pair.norm, "asset": pair.base, "reason": "Kraken depth too small in both directions"})
                        return

                    # Fire all Kyber quotes for all contracts in parallel
                    async def _kyber_for_contract(c):
                        async with kyber_sem:
                            chain = c["chain"]
                            stable = c["stable"]
                            token_dec = int(c.get("decimals") or 18)
                            stable_dec = int(stable.get("decimals") or 6)
                            contract = c["contract"]
                            stable_addr = stable["address"]
                            if pair.mid <= 0:
                                return
                            # Kraken -> Kyber: spend quote on Kraken asks, then sell the actual base qty on Kyber.
                            base_qty_from_kraken = float(kraken_buy.get("base_qty") or 0.0)
                            kraken_buy_price = float(kraken_buy.get("avg_price") or 0.0)
                            if kraken_buy_valid and base_qty_from_kraken > 0 and kraken_buy_price > 0:
                                sell_amount = self._to_int_amount(base_qty_from_kraken, token_dec)
                                sell_quote = await self.kyber_route(session, chain, contract, stable_addr, sell_amount)
                                if sell_quote:
                                    stats["kyber_quotes"] += 1
                                    stable_out = sell_quote["amount_out"] / (10 ** stable_dec)
                                    dex_sell_price = stable_out / base_qty_from_kraken if base_qty_from_kraken > 0 else 0
                                    spread = ((stable_out - kraken_buy["quote_spent"]) / kraken_buy["quote_spent"] * 100.0) if kraken_buy["quote_spent"] > 0 else -9999
                                    stats["routes_raw"] += 1
                                    if spread < min_spread:
                                        stats["below_min_spread"] += 1
                                    elif spread > max_spread:
                                        stats["above_max_spread"] += 1
                                    else:
                                        routes.append({
                                            "scanner": "kraken_kyber",
                                            "asset": pair.base,
                                            "symbol": pair.norm,
                                            "coin_id": coin_id,
                                            "coin_name": coin_name,
                                            "route_kind": "kraken_to_kyber",
                                            "buy_exchange": "Kraken Pro",
                                            "sell_exchange": f"KyberSwap/{chain}",
                                            "buy_price": kraken_buy_price,
                                            "sell_price": dex_sell_price,
                                            "spread": spread,
                                            "roi_pct": spread,
                                            "net_profit_usd": stable_out - kraken_buy["quote_spent"],
                                            "notional_usd": kraken_buy["quote_spent"],
                                            "base_qty": base_qty_from_kraken,
                                            "chain": chain,
                                            "contract": contract,
                                            "stable": stable.get("symbol"),
                                            "confidence": "HIGH",
                                            "contract_source": "CoinGecko candidate confirmed by Kraken ticker + price match",
                                            "kraken_depth": {"side": "ask", "fill_pct": kraken_buy.get("fill_pct"), "avg_price": kraken_buy_price, "depth_usd": kraken_buy.get("depth_usd"), "levels_used": kraken_buy.get("levels_used"), "ttl_sec": self.kraken_orderbook_ttl},
                                            "coingecko_depth": cg_depth if self.include_cg_depth else {},
                                            "transfer_status": "unknown",
                                            "transfer_note": "Deposit/withdraw status must come from exchange status endpoints/manual check, not CoinGecko",
                                            "notes": "; ".join(notes[:4]),
                                        })
                            # Kyber -> Kraken: buy token on Kyber with notional, then sell exact token_out into Kraken bids.
                            buy_amount = self._to_int_amount(notional_usd, stable_dec)
                            buy_quote = await self.kyber_route(session, chain, stable_addr, contract, buy_amount)
                            if buy_quote:
                                stats["kyber_quotes"] += 1
                                token_out = buy_quote["amount_out"] / (10 ** token_dec)
                                dex_buy_price = notional_usd / token_out if token_out > 0 else 0
                                kraken_sell = self._quote_sell_base_for_usd(orderbook.get("bids") or [], token_out)
                                if float(kraken_sell.get("fill_pct") or 0) < self.kraken_depth_min_fill_pct:
                                    return
                                kraken_sell_price = float(kraken_sell.get("avg_price") or 0.0)
                                proceeds = float(kraken_sell.get("quote_received") or 0.0)
                                spread = ((proceeds - notional_usd) / notional_usd * 100.0) if notional_usd > 0 else -9999
                                stats["routes_raw"] += 1
                                if spread < min_spread:
                                    stats["below_min_spread"] += 1
                                elif spread > max_spread:
                                    stats["above_max_spread"] += 1
                                else:
                                    routes.append({
                                        "scanner": "kraken_kyber",
                                        "asset": pair.base,
                                        "symbol": pair.norm,
                                        "coin_id": coin_id,
                                        "coin_name": coin_name,
                                        "route_kind": "kyber_to_kraken",
                                        "buy_exchange": f"KyberSwap/{chain}",
                                        "sell_exchange": "Kraken Pro",
                                        "buy_price": dex_buy_price,
                                        "sell_price": kraken_sell_price,
                                        "spread": spread,
                                        "roi_pct": spread,
                                        "net_profit_usd": proceeds - notional_usd,
                                        "notional_usd": notional_usd,
                                        "base_qty": token_out,
                                        "chain": chain,
                                        "contract": contract,
                                        "stable": stable.get("symbol"),
                                        "confidence": "HIGH",
                                        "contract_source": "CoinGecko candidate confirmed by Kraken ticker + price match",
                                        "kraken_depth": {"side": "bid", "fill_pct": kraken_sell.get("fill_pct"), "avg_price": kraken_sell_price, "depth_usd": kraken_sell.get("depth_usd"), "levels_used": kraken_sell.get("levels_used"), "ttl_sec": self.kraken_orderbook_ttl},
                                        "coingecko_depth": cg_depth if self.include_cg_depth else {},
                                        "transfer_status": "unknown",
                                        "transfer_note": "Deposit/withdraw status must come from exchange status endpoints/manual check, not CoinGecko",
                                        "notes": "; ".join(notes[:4]),
                                    })

                    await asyncio.gather(*[_kyber_for_contract(c) for c in contracts[:8]], return_exceptions=True)

            await asyncio.gather(*[scan_pair(p) for p in pairs], return_exceptions=True)
            routes.sort(key=lambda r: float(r.get("spread") or 0), reverse=True)
            routes = routes[:limit]
            return {
                "success": True,
                "scanner": "kraken_kyber",
                "data": routes,
                "routes": routes,
                "rejected_sample": rejected[:25],
                "meta": {
                    "asset_limit": asset_limit,
                    "kraken_pairs_scanned": len(pairs),
                    "notional_usd": notional_usd,
                    "min_spread": min_spread,
                    "max_spread": max_spread,
                    "price_match_pct": self.price_match_pct,
                    "elapsed_sec": round(_now() - started, 3),
                    "contract_rule": "CoinGecko ID accepted only if CoinGecko tickers include Kraken for the same base and price is within threshold",
                    "contract_index": _index_status(),
                    "contract_index_used": bool(index_map),
                    "contract_index_entries": len(index_map),
                    "scan_stats": stats,
                },
                "timestamp": int(_now()),
            }
