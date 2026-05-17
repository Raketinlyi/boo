from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import aiohttp

from utils.bridge_providers import get_bridge_candidates
from utils.dex_swap_links import build_dex_swap_url
from utils.exchange_info import exchange_info_fetcher
from utils.interchain_live_quotes import (
    JUPITER_SOL_MINT,
    JUPITER_USDC_MINT,
    JUPITER_USDT_MINT,
    build_jupiter_dex_quote,
    canon_chain_name,
    fetch_debridge_quote,
    fetch_geckoterminal_dex_quotes,
    fetch_layerzero_quote,
    fetch_mayan_quote,
    fetch_relay_quote,
    get_mayan_supported_token,
)
from utils.symbols import extract_base_asset, split_pair_symbol


STABLE_ASSETS = {"USDC", "USDT", "DAI", "FDUSD", "TUSD", "USDE", "USD1"}
MAJOR_CHAINS = {
    "ethereum",
    "binance-smart-chain",
    "polygon-pos",
    "arbitrum-one",
    "optimistic-ethereum",
    "base",
    "solana",
    "avalanche",
    "tron",
    "sui",
    "mantle",
    "linea",
    "scroll",
}
NATIVE_LABELS = {"", "-", "native", "native coin", "native token", "unknown"}
QUOTE_PRIORITY = {"USDT": 0, "USDC": 1, "USD": 2}
CONTRACT_FIRST_PRIORITY_ASSETS = [
    "BTC",
    "ETH",
    "SOL",
    "LINK",
    "AAVE",
    "UNI",
    "ARB",
    "OP",
    "AVAX",
    "TRX",
    "TON",
    "DOGE",
    "USDC",
    "USDT",
]
DEXSCREENER_CHAIN_MAP = {
    "ethereum": "ethereum",
    "binance-smart-chain": "bsc",
    "polygon-pos": "polygon",
    "arbitrum-one": "arbitrum",
    "optimistic-ethereum": "optimism",
    "base": "base",
    "avalanche": "avalanche",
    "solana": "solana",
    "sonic": "sonic",
    "tron": "tron",
    "sui": "sui",
    "aptos": "aptos",
    "linea": "linea",
    "scroll": "scroll",
    "mantle": "mantle",
    "celo": "celo",
    "near-protocol": "near",
    "the-open-network": "ton",
}
NATIVE_WRAPPER_MAP: Dict[Tuple[str, str], Dict[str, Any]] = {
    ("SOL", "solana"): {"contract": JUPITER_SOL_MINT, "decimals": 9},
    ("ETH", "ethereum"): {"contract": "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2", "decimals": 18},
    ("ETH", "arbitrum-one"): {"contract": "0x82af49447d8a07e3bd95bd0d56f35241523fbab1", "decimals": 18},
    ("ETH", "optimistic-ethereum"): {"contract": "0x4200000000000000000000000000000000000006", "decimals": 18},
    ("ETH", "base"): {"contract": "0x4200000000000000000000000000000000000006", "decimals": 18},
    ("ETH", "polygon-pos"): {"contract": "0x7ceB23fD6bC0adD59E62ac25578270cFf1b9f619", "decimals": 18},
    ("BNB", "binance-smart-chain"): {"contract": "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c", "decimals": 18},
}


def _to_float(value: Any) -> Optional[float]:
    try:
        if value in (None, "", "null"):
            return None
        return float(value)
    except Exception:
        return None


def _clean_chain_name(raw_name: Any) -> str:
    raw = str(raw_name or "").strip()
    if not raw:
        return "unknown"

    lowered = raw.lower()
    exact_aliases = {
        "arb": "arbitrum-one",
        "arbevm": "arbitrum-one",
        "arbitrum one": "arbitrum-one",
        "avaxc": "avalanche",
        "avax-c": "avalanche",
        "avax c-chain": "avalanche",
        "bep20": "binance-smart-chain",
        "erc20": "ethereum",
        "op": "optimistic-ethereum",
        "opeth": "optimistic-ethereum",
        "optimism": "optimistic-ethereum",
        "sol": "solana",
        "s": "sonic",
        "trc20": "tron",
        "trx": "tron",
        "xlayer": "x-layer",
        "x layer": "x-layer",
        "zksera": "zksync-era",
        "zksync": "zksync-era",
        "zksync era": "zksync-era",
    }
    exact = exact_aliases.get(lowered)
    if exact:
        return exact

    direct = canon_chain_name(raw)
    if direct != raw.lower() or direct in DEXSCREENER_CHAIN_MAP or direct in MAJOR_CHAINS:
        return direct

    simplified = re.sub(r"\([^)]*\)", " ", lowered)
    simplified = simplified.replace("_", " ").replace("/", " ")
    simplified = re.sub(r"\s+", " ", simplified).strip()
    exact = exact_aliases.get(simplified)
    if exact:
        return exact
    direct = canon_chain_name(simplified)
    if direct != simplified or direct in DEXSCREENER_CHAIN_MAP or direct in MAJOR_CHAINS:
        return direct

    substring_map = [
        ("arbitrum", "arbitrum-one"),
        ("optimism", "optimistic-ethereum"),
        ("bnb smart chain", "binance-smart-chain"),
        ("bsc", "binance-smart-chain"),
        ("polygon", "polygon-pos"),
        ("matic", "polygon-pos"),
        ("ethereum", "ethereum"),
        ("erc20", "ethereum"),
        ("solana", "solana"),
        ("tron", "tron"),
        ("trc20", "tron"),
        ("avalanche", "avalanche"),
        ("avax", "avalanche"),
        ("base", "base"),
        ("aptos", "aptos"),
        ("toncoin", "the-open-network"),
        ("ton", "the-open-network"),
        ("near", "near-protocol"),
        ("linea", "linea"),
        ("scroll", "scroll"),
        ("mantle", "mantle"),
        ("sui", "sui"),
        ("celo", "celo"),
        ("berachain", "berachain"),
        ("unichain", "unichain"),
    ]
    for needle, chain in substring_map:
        if needle in simplified:
            return chain
    return canon_chain_name(simplified)


def _stable_prices_ok(asset: str, *prices: float) -> bool:
    asset_u = str(asset or "").strip().upper()
    if asset_u not in STABLE_ASSETS:
        return True
    for price in prices:
        value = _to_float(price)
        if value is None or value <= 0:
            continue
        if value < 0.85 or value > 1.15:
            return False
    return True


_DEFAULT_GAS_BUFFER_USD: Dict[str, float] = {
    # Realistic DEX-swap gas costs (Apr 2026). Values are conservative — a
    # little over typical steady-state cost so that marginal opportunities
    # that only pay off with cheap gas are filtered out. Override any of
    # these in config.json via `dex_gas_buffer_usd`.
    "ethereum": 15.0,            # was 7.50 — too optimistic, real Uniswap swaps 10-25 USD
    "arbitrum-one": 0.60,
    "optimistic-ethereum": 0.60,
    "base": 0.40,
    "polygon-pos": 0.20,
    "binance-smart-chain": 0.40,
    "solana": 0.10,              # was 0.35 — Solana fees are very low
    "avalanche": 0.40,
    "tron": 0.50,
    "fantom": 0.40,
    "sui": 0.15,
    "aptos": 0.15,
    "the-open-network": 0.60,
    "near-protocol": 0.40,
    "linea": 0.30,
    "scroll": 0.30,
    "mantle": 0.30,
    "celo": 0.20,
}


def _gas_buffer_usd(chain: str, overrides: Optional[Mapping[str, float]] = None) -> float:
    """Estimated gas/fee buffer for a single swap on the given chain.

    `overrides` may come from config.json ("dex_gas_buffer_usd") and wins
    over the default table when present. Unknown chains fall back to
    1.50 USD which is conservative for most long-tail EVM networks.
    """
    if overrides:
        try:
            override_value = overrides.get(chain)
        except Exception:
            override_value = None
        if override_value is not None:
            try:
                return max(0.0, float(override_value))
            except (TypeError, ValueError):
                pass
    return _DEFAULT_GAS_BUFFER_USD.get(chain, 1.50)


def _request_bool(args: Mapping[str, Any], key: str, default: bool = False) -> bool:
    value = str(args.get(key, "1" if default else "0") or "").strip().lower()
    return value in {"1", "true", "yes", "on", "y"}


def _request_float(args: Mapping[str, Any], key: str, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(args.get(key, default) or default)
    except Exception:
        value = default
    return max(minimum, min(maximum, value))


def _request_int(args: Mapping[str, Any], key: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(float(args.get(key, default) or default))
    except Exception:
        value = default
    return max(minimum, min(maximum, value))


class ContractFirstInterchainScanner:
    def __init__(
        self,
        bot: Any,
        request_args: Mapping[str, Any],
        asset_status_cache: Dict[str, Any],
        dex_quote_cache: Dict[str, Any],
        *,
        asset_status_ttl: int,
        dex_quote_ttl: int,
    ) -> None:
        self.bot = bot
        self.request_args = request_args
        self.asset_status_cache = asset_status_cache
        self.dex_quote_cache = dex_quote_cache
        self.asset_status_ttl = asset_status_ttl
        self.dex_quote_ttl = dex_quote_ttl
        self.snapshot = getattr(bot, "_last_all_prices", None) or {}
        self.snapshot_ts = float(getattr(bot, "_last_all_prices_ts", 0.0) or 0.0)
        self.enabled_exchanges = self._enabled_exchange_names()
        self.min_spread = _request_float(request_args, "min_spread", 0.0, 0.0, 1000.0)
        self.limit = _request_int(request_args, "limit", 80, 1, 300)
        self.asset_limit = _request_int(request_args, "asset_limit", 40, 1, 150)
        default_notional = float(bot.config.get("arb_min_notional_usd", 100.0) or 100.0)
        self.notional_usd = _request_float(request_args, "notional_usd", default_notional, 10.0, 5000.0)
        self.quick_mode = _request_bool(request_args, "quick_mode", False)
        self.route_group = str(request_args.get("route_group", "all") or "all").strip().lower()
        if self.route_group not in {"all", "cex_dex", "cross_chain"}:
            self.route_group = "all"
        self.chain_scope = str(request_args.get("chain_scope", "all") or "all").strip().lower()
        if self.chain_scope not in {"all", "major", "small"}:
            self.chain_scope = "all"
        self.asset_profile = str(request_args.get("asset_profile", "balanced") or "balanced").strip().lower()
        if self.asset_profile not in {"balanced", "long_tail", "majors"}:
            self.asset_profile = "balanced"
        self.per_asset_timeout_sec = _request_float(
            request_args,
            "per_asset_timeout",
            5.0 if self.quick_mode else 7.0,
            0.5,
            30.0,
        )
        self.parallel_assets = _request_int(
            request_args,
            "parallel_assets",
            int(bot.config.get("interchain_parallel_assets_quick" if self.quick_mode else "interchain_parallel_assets", 3 if self.quick_mode else 4) or 0),
            1,
            8,
        )
        self.execution_quality_filter = str(request_args.get("execution_quality", "") or "").strip().lower()
        if self.execution_quality_filter not in {"", "estimated", "hybrid", "live", "actionable"}:
            self.execution_quality_filter = ""
        self.requested_assets = self._requested_assets()

        # Configurable economic constants (previously hard-coded).
        # "dex_fee_rate" is the per-swap fee applied on each DEX leg.
        # "dex_gas_buffer_usd" is an optional dict overriding defaults per chain.
        try:
            self.dex_fee_rate = max(0.0, float(bot.config.get("dex_fee_rate", 0.003) or 0.003))
        except (TypeError, ValueError):
            self.dex_fee_rate = 0.003
        gas_overrides_raw = bot.config.get("dex_gas_buffer_usd", {}) if hasattr(bot, "config") else {}
        self.gas_overrides: Dict[str, float] = {}
        if isinstance(gas_overrides_raw, dict):
            for key, val in gas_overrides_raw.items():
                try:
                    self.gas_overrides[str(key)] = max(0.0, float(val))
                except (TypeError, ValueError):
                    continue

        # Sanity limits for route building. All are config-overridable so
        # the user can loosen or tighten them without editing the code.
        # See config.json for defaults; see _build_cex_to_dex /
        # _build_dex_bridge_dex for the reasoning behind each cap.
        def _cfg_float(key: str, default: float) -> float:
            try:
                return max(0.0, float(bot.config.get(key, default) or default))
            except (TypeError, ValueError):
                return default
        self.cex_dex_max_spread_pct = _cfg_float("cex_dex_max_spread_pct", 15.0)
        self.cex_dex_min_pool_liq_ratio = _cfg_float("cex_dex_min_pool_liquidity_ratio", 3.0)
        self.cex_dex_min_pool_liq_floor_usd = _cfg_float("cex_dex_min_pool_liquidity_floor_usd", 1500.0)
        self.cross_chain_max_roi_pct = _cfg_float("cross_chain_max_roi_pct", 25.0)
        self.cross_chain_bridge_ratio_min = _cfg_float("cross_chain_bridge_ratio_min", 0.80)
        self.cross_chain_bridge_ratio_max = _cfg_float("cross_chain_bridge_ratio_max", 1.02)

    def _enabled_exchange_names(self) -> List[str]:
        calc_obj = getattr(self.bot, "calc", None)
        try:
            if calc_obj and hasattr(calc_obj, "get_enabled_exchanges"):
                return [ex.name for ex in calc_obj.get_enabled_exchanges()]
            if calc_obj and hasattr(calc_obj, "exchanges"):
                return [ex.name for ex in calc_obj.exchanges if getattr(ex, "enabled", False)]
        except Exception:
            pass
        return [name for name, prices in self.snapshot.items() if isinstance(prices, dict)]

    def _requested_assets(self) -> List[str]:
        raw = str(self.request_args.get("assets", "") or "").strip()
        if not raw:
            return []
        result: List[str] = []
        seen = set()
        for part in raw.split(","):
            asset = extract_base_asset(part.strip().upper(), assume_pair=True) or str(part or "").strip().upper()
            if asset and asset not in seen:
                seen.add(asset)
                result.append(asset)
        return result[:30]

    def _asset_priority_bucket(self, asset: str) -> int:
        asset_u = str(asset or "").strip().upper()
        if asset_u in STABLE_ASSETS:
            return 3
        if asset_u in CONTRACT_FIRST_PRIORITY_ASSETS:
            return 2
        return 1

    def _profile_sort_key(
        self,
        asset: str,
        *,
        exchange_count: int,
        market_cap: float,
        volume_24h: float,
        spread: float,
        has_market_data: bool,
    ) -> Tuple[float, float, float, float, float]:
        priority_bucket = float(self._asset_priority_bucket(asset))
        exchange_count_f = float(exchange_count)
        market_cap_f = float(market_cap)
        volume_24h_f = float(volume_24h)
        spread_f = float(spread)
        market_data_f = 1.0 if has_market_data else 0.0

        if self.asset_profile == "majors":
            return (
                priority_bucket,
                market_data_f,
                market_cap_f,
                exchange_count_f,
                volume_24h_f,
            )
        if self.asset_profile == "long_tail":
            return (
                0.0 if priority_bucket > 1.0 else 1.0,
                exchange_count_f,
                spread_f,
                volume_24h_f,
                market_cap_f,
            )
        return (
            priority_bucket,
            market_data_f,
            exchange_count_f,
            market_cap_f,
            volume_24h_f + spread_f,
        )

    def _candidate_assets(self) -> List[str]:
        if self.requested_assets:
            return list(self.requested_assets)

        cached_opps = getattr(self.bot, "cached_opportunities", None) or []
        ranked_assets: List[str] = []
        seen = set()
        snapshot_meta: Dict[str, Dict[str, Any]] = {}
        for exchange_name in self.enabled_exchanges:
            exchange_prices = self.snapshot.get(exchange_name) or {}
            if not isinstance(exchange_prices, dict):
                continue
            for symbol in exchange_prices.keys():
                base, quote = split_pair_symbol(symbol)
                if not base or quote not in QUOTE_PRIORITY:
                    continue
                meta = snapshot_meta.setdefault(base, {
                    "exchange_count": 0,
                    "quotes": set(),
                })
                meta["quotes"].add(quote)
                meta["exchange_count"] += 1

        opp_meta: Dict[str, Dict[str, Any]] = {}
        for opp in cached_opps:
            asset = extract_base_asset(opp.get("symbol"), assume_pair=True)
            if not asset:
                continue
            meta = opp_meta.setdefault(asset, {
                "exchange_count": 0,
                "market_cap": 0.0,
                "volume_24h": 0.0,
                "spread": 0.0,
                "has_market_data": False,
            })
            try:
                meta["exchange_count"] = max(meta["exchange_count"], int(float(opp.get("exchange_count", 0) or 0)))
            except Exception:
                pass
            market_cap = _to_float(opp.get("cg_market_cap_usd")) or 0.0
            volume_24h = _to_float(opp.get("cg_volume_24h_usd")) or 0.0
            spread = _to_float(opp.get("spread")) or 0.0
            meta["market_cap"] = max(float(meta["market_cap"]), market_cap)
            meta["volume_24h"] = max(float(meta["volume_24h"]), volume_24h)
            meta["spread"] = max(float(meta["spread"]), spread)
            if market_cap > 0 or volume_24h > 0:
                meta["has_market_data"] = True

        all_assets = set(snapshot_meta.keys()) | set(opp_meta.keys())
        sorted_assets = sorted(
            all_assets,
            key=lambda asset: self._profile_sort_key(
                asset,
                exchange_count=max(
                    int((snapshot_meta.get(asset) or {}).get("exchange_count", 0) or 0),
                    int((opp_meta.get(asset) or {}).get("exchange_count", 0) or 0),
                ),
                market_cap=float((opp_meta.get(asset) or {}).get("market_cap", 0.0) or 0.0),
                volume_24h=float((opp_meta.get(asset) or {}).get("volume_24h", 0.0) or 0.0),
                spread=float((opp_meta.get(asset) or {}).get("spread", 0.0) or 0.0),
                has_market_data=bool((opp_meta.get(asset) or {}).get("has_market_data")),
            ),
            reverse=True,
        )
        for asset in sorted_assets:
            if asset in seen:
                continue
            seen.add(asset)
            ranked_assets.append(asset)
            if len(ranked_assets) >= self.asset_limit:
                return ranked_assets
        return ranked_assets[: self.asset_limit]

    def _auto_asset_budget_limit(self) -> int:
        if self.requested_assets:
            return self.asset_limit
        # Budget scales with parallelism: with 6 workers at 7s timeout each,
        # ~50s buys us ~42 asset slots, enough to cover the full 40-asset
        # long-tail candidate pool in a single UI request.
        budget_sec = 22.0 if self.quick_mode else 50.0
        estimated = int((budget_sec * max(1, self.parallel_assets)) / max(self.per_asset_timeout_sec, 0.5))
        return max(self.parallel_assets, min(self.asset_limit, estimated))

    def _select_auto_scan_assets(self, candidate_assets_all: List[str]) -> Tuple[List[str], int]:
        if self.requested_assets:
            return list(candidate_assets_all), 0

        budget_limit = self._auto_asset_budget_limit()
        if len(candidate_assets_all) <= budget_limit:
            return list(candidate_assets_all), 0

        if self.asset_profile == "majors":
            return list(candidate_assets_all[:budget_limit]), 0

        core_size = min(max(4, self.parallel_assets + 1), budget_limit)
        core_assets = list(candidate_assets_all[:core_size])
        tail_assets = list(candidate_assets_all[core_size:])
        tail_budget = max(0, budget_limit - len(core_assets))
        if tail_budget <= 0 or not tail_assets:
            return core_assets[:budget_limit], 0

        rotation_period_sec = 20 if self.quick_mode else 45
        seed_ts = float(self.snapshot_ts or time.time())
        rotation_index = int(seed_ts // rotation_period_sec)
        offset = rotation_index % len(tail_assets)

        rotated_tail = tail_assets[offset:] + tail_assets[:offset]
        selected_tail = rotated_tail[:tail_budget]

        selected_assets: List[str] = []
        seen = set()
        for asset in core_assets + selected_tail:
            if asset in seen:
                continue
            seen.add(asset)
            selected_assets.append(asset)
            if len(selected_assets) >= budget_limit:
                break
        return selected_assets, offset

    def _exchange_fee_rate(self, exchange_name: str) -> float:
        try:
            return max(0.0, float(self.bot.config.get_exchange_fee(exchange_name, 0.1))) / 100.0
        except Exception:
            return 0.001

    def _chain_matches_scope(self, chain: str) -> bool:
        if self.chain_scope == "all":
            return True
        is_major = chain in MAJOR_CHAINS
        if self.chain_scope == "major":
            return is_major
        return not is_major

    async def get_asset_rows(self, asset: str) -> List[Dict[str, Any]]:
        now = time.time()
        cached = self.asset_status_cache.get(asset)
        if cached and (now - float(cached.get("ts", 0.0) or 0.0) < self.asset_status_ttl):
            data = cached.get("data") or []
            return [row for row in data if str(row.get("exchange") or "") in self.enabled_exchanges]

        info = await exchange_info_fetcher.get_all_exchange_info(asset)
        rows = info.get("exchanges", []) if isinstance(info, dict) else []
        normalized: List[Dict[str, Any]] = []
        enabled = {str(name or "").strip().lower() for name in self.enabled_exchanges}
        for row in rows:
            if not isinstance(row, dict):
                continue
            exchange_name = str(row.get("exchange") or "").strip()
            if exchange_name.lower() not in enabled:
                continue
            normalized.append({
                "exchange": exchange_name,
                "asset": str(row.get("asset") or asset).strip().upper(),
                "chain": str(row.get("chain") or "-").strip() or "-",
                "contract": str(row.get("contract") or row.get("contract_address") or "").strip(),
                "deposit_enabled": row.get("deposit_enabled") is True,
                "withdraw_enabled": row.get("withdraw_enabled") is True,
                "withdraw_fee": row.get("withdraw_fee"),
                "min_withdraw": row.get("min_withdraw"),
            })
        self.asset_status_cache[asset] = {"ts": time.time(), "data": normalized}
        return normalized

    def build_contract_rows(self, asset: str, rows: Sequence[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        contract_rows: List[Dict[str, Any]] = []
        skipped: List[Dict[str, Any]] = []
        seen = set()
        asset_u = str(asset or "").strip().upper()
        for row in rows:
            exchange_name = str(row.get("exchange") or "").strip()
            chain = _clean_chain_name(row.get("chain"))
            if not exchange_name or chain in {"unknown", "-", ""}:
                skipped.append({"exchange": exchange_name, "chain": row.get("chain"), "reason": "unknown_chain"})
                continue
            if not self._chain_matches_scope(chain):
                continue
            raw_contract = str(row.get("contract") or "").strip()
            contract = None
            contract_source = None
            decimals = None
            if raw_contract and raw_contract.lower() not in NATIVE_LABELS:
                contract = raw_contract
                contract_source = "exchange_contract"
            else:
                wrapper = NATIVE_WRAPPER_MAP.get((asset_u, chain))
                if wrapper:
                    contract = str(wrapper.get("contract") or "").strip()
                    decimals = wrapper.get("decimals")
                    contract_source = "native_wrapper"
            if not contract:
                skipped.append({"exchange": exchange_name, "chain": row.get("chain"), "reason": "missing_contract"})
                continue
            dedupe_key = (exchange_name.lower(), chain, contract.lower())
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            contract_rows.append({
                "exchange": exchange_name,
                "asset": asset_u,
                "chain": chain,
                "raw_chain": row.get("chain"),
                "contract": contract,
                "contract_source": contract_source,
                "decimals": decimals,
                "deposit_enabled": row.get("deposit_enabled") is True,
                "withdraw_enabled": row.get("withdraw_enabled") is True,
                "withdraw_fee": row.get("withdraw_fee"),
                "min_withdraw": row.get("min_withdraw"),
            })
        return contract_rows, skipped

    def pick_cex_quotes(self, asset: str) -> List[Dict[str, Any]]:
        quotes: List[Dict[str, Any]] = []
        for exchange_name in self.enabled_exchanges:
            exchange_prices = self.snapshot.get(exchange_name) or {}
            if not isinstance(exchange_prices, dict):
                continue
            best = None
            for symbol, raw_price in exchange_prices.items():
                price = _to_float(raw_price)
                if price is None or price <= 0:
                    continue
                base, quote = split_pair_symbol(symbol)
                if base != asset or quote not in QUOTE_PRIORITY:
                    continue
                candidate = {
                    "exchange": exchange_name,
                    "symbol": symbol,
                    "price": float(price),
                    "quote": quote,
                    "priority": QUOTE_PRIORITY[quote],
                    "fee_rate": self._exchange_fee_rate(exchange_name),
                }
                if best is None or candidate["priority"] < best["priority"]:
                    best = candidate
            if best:
                quotes.append(best)
        return quotes

    async def _fetch_dexscreener_quote(
        self,
        session: aiohttp.ClientSession,
        *,
        asset: str,
        chain: str,
        contract: str,
        contract_source: str,
    ) -> Optional[Dict[str, Any]]:
        ds_chain = DEXSCREENER_CHAIN_MAP.get(chain)
        if not ds_chain:
            return None
        try:
            async with session.get(
                f"https://api.dexscreener.com/tokens/v1/{ds_chain}/{contract}",
                timeout=aiohttp.ClientTimeout(total=10),
            ) as response:
                if response.status != 200:
                    return None
                payload = await response.json(content_type=None)
        except Exception as exc:
            logging.debug("DexScreener quote fetch failed for %s %s on %s: %s", asset, contract, chain, exc)
            return None

        pairs = payload if isinstance(payload, list) else []
        contract_lc = contract.lower()
        best_quote = None
        for pair in pairs:
            if not isinstance(pair, dict):
                continue
            base_token = pair.get("baseToken") or {}
            if str(base_token.get("address") or "").strip().lower() != contract_lc:
                continue
            price = _to_float(pair.get("priceUsd"))
            liquidity_usd = _to_float((pair.get("liquidity") or {}).get("usd")) or 0.0
            volume_24h = _to_float((pair.get("volume") or {}).get("h24")) or 0.0
            if price is None or price <= 0 or liquidity_usd <= 0:
                continue
            score = (liquidity_usd * 10.0) + volume_24h
            quote = {
                "asset": asset,
                "chain": chain,
                "contract": contract,
                "contract_source": contract_source,
                "price": float(price),
                "liquidity_usd": float(liquidity_usd),
                "volume_24h": float(volume_24h),
                "dex_id": str(pair.get("dexId") or "dex").strip() or "dex",
                "pair_address": str(pair.get("pairAddress") or "").strip(),
                "url": str(pair.get("url") or "").strip() or f"https://dexscreener.com/{ds_chain}/{pair.get('pairAddress')}",
                "swap_url": build_dex_swap_url(chain=chain, contract=contract),
                "label": f"{str(pair.get('dexId') or 'DEX').strip()} ({chain})",
                "score": float(score),
                "quote_source": "dexscreener",
                "quote_mode": "estimated",
                "route_labels": [],
            }
            if best_quote is None or quote["score"] > best_quote["score"]:
                best_quote = quote
        return best_quote

    async def fetch_dex_quote(
        self,
        session: aiohttp.ClientSession,
        *,
        asset: str,
        chain: str,
        contract: str,
        contract_source: str,
        decimals: Optional[int],
    ) -> Optional[Dict[str, Any]]:
        cache_key = f"contract-first:{chain}:{contract.lower()}"
        cached = self.dex_quote_cache.get(cache_key)
        if cached and (time.time() - float(cached.get("ts", 0.0) or 0.0) < self.dex_quote_ttl):
            return cached.get("data")

        quote = await self._fetch_dexscreener_quote(
            session,
            asset=asset,
            chain=chain,
            contract=contract,
            contract_source=contract_source,
        )
        if quote is None:
            try:
                gecko_quotes = await fetch_geckoterminal_dex_quotes(
                    session,
                    asset=asset,
                    chain=chain,
                    contract=contract,
                    contract_source=contract_source,
                )
            except Exception as exc:
                logging.debug("GeckoTerminal quote fetch failed for %s %s on %s: %s", asset, contract, chain, exc)
                gecko_quotes = []
            quote = gecko_quotes[0] if gecko_quotes else None

        if quote is None and chain == "solana":
            known_decimals = decimals
            if known_decimals is None:
                if contract == JUPITER_SOL_MINT:
                    known_decimals = 9
                elif contract in {JUPITER_USDC_MINT, JUPITER_USDT_MINT}:
                    known_decimals = 6
            if known_decimals is not None:
                quote = await build_jupiter_dex_quote(
                    session,
                    symbol=asset,
                    mint=contract,
                    decimals=known_decimals,
                    notional_usd=self.notional_usd,
                    usd_hint=None,
                    metadata={"verified": contract_source in {"exchange_contract", "native_wrapper"}},
                )
                if quote:
                    quote["contract_source"] = contract_source

        self.dex_quote_cache[cache_key] = {"ts": time.time(), "data": quote}
        return quote

    def _build_cex_to_dex(self, asset: str, cex_quote: Dict[str, Any], dex_quote: Dict[str, Any], row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        cex_price = float(cex_quote["price"])
        dex_price = float(dex_quote["price"])
        if cex_price <= 0 or dex_price <= 0 or not _stable_prices_ok(asset, cex_price, dex_price):
            return None

        # Do NOT drop the route when the withdraw status is unknown (None
        # usually means the exchange status API failed or is throttled).
        # Only a hard-False ("withdraw disabled") should kill the route.
        withdraw_flag = row.get("withdraw_enabled")
        if withdraw_flag is False:
            return None
        transfer_status = "ok" if withdraw_flag is True else "unknown"

        withdraw_fee_asset = max(0.0, _to_float(row.get("withdraw_fee")) or 0.0)
        dex_fee_rate = self.dex_fee_rate
        gas_usd = _gas_buffer_usd(str(dex_quote.get("chain") or ""), self.gas_overrides)
        gross_tokens = self.notional_usd / cex_price
        tokens_after_trade = gross_tokens * (1.0 - float(cex_quote.get("fee_rate") or 0.0))
        tokens_after_transfer = tokens_after_trade - withdraw_fee_asset
        if tokens_after_transfer <= 0:
            return None
        proceeds_usd = tokens_after_transfer * dex_price * (1.0 - dex_fee_rate)
        spread = ((dex_price / cex_price) - 1.0) * 100.0
        net_profit_usd = proceeds_usd - self.notional_usd - gas_usd
        # Show the route even when it is unprofitable on the current
        # notional: the user sees the opportunity and decides whether to
        # scale it or skip it. Sanity gates below still block bogus data.
        if spread < self.min_spread:
            return None

        # Sanity gate: reject bogus routes from broken / thin DEX pools.
        # A real CEX<->DEX spread above ~15% is almost always one of:
        #   1) thinly-traded pool on a small chain (KCC, OKC, ...) whose
        #      last price is days old, producing a fantasy 40-120% spread;
        #   2) a wrong-token match where the DEX pool is actually another
        #      asset with the same ticker.
        # A legitimate CEX->DEX opportunity rarely exceeds 5%. We still
        # allow up to 15% to be lenient, and we also require the pool's
        # USD liquidity to be at least ~3x the notional so the quote
        # survives even 1/3 of the size we intend to trade.
        liquidity_usd = float(dex_quote.get("liquidity_usd") or 0.0)
        if spread > self.cex_dex_max_spread_pct:
            logging.debug(
                "[%s] cex->dex: implausible spread %.2f%% on %s/%s (liq=$%.0f); dropping (cap=%.1f%%)",
                asset, spread, cex_quote.get("exchange"), dex_quote.get("label"),
                liquidity_usd, self.cex_dex_max_spread_pct,
            )
            return None
        min_liq_required = max(self.cex_dex_min_pool_liq_floor_usd, self.notional_usd * self.cex_dex_min_pool_liq_ratio)
        if liquidity_usd > 0 and liquidity_usd < min_liq_required:
            logging.debug(
                "[%s] cex->dex: pool too thin (liq=$%.0f, required=$%.0f); dropping",
                asset, liquidity_usd, min_liq_required,
            )
            return None

        return {
            "symbol": cex_quote["symbol"],
            "asset": asset,
            "route_kind": "cex_to_dex",
            "buy_type": "cex",
            "sell_type": "dex",
            "buy_exchange": cex_quote["exchange"],
            "sell_exchange": dex_quote["label"],
            "buy_price": cex_price,
            "sell_price": dex_price,
            "spread": spread,
            "net_profit_usd": net_profit_usd,
            "roi_pct": (net_profit_usd / self.notional_usd) * 100.0,
            "notional_usd": self.notional_usd,
            "buy_chain": row["chain"],
            "sell_chain": row["chain"],
            "chain": row["chain"],
            "contract": row["contract"],
            "buy_contract": row["contract"],
            "sell_contract": row["contract"],
            "contract_source": row.get("contract_source"),
            "liquidity_usd": dex_quote["liquidity_usd"],
            "volume_24h": dex_quote["volume_24h"],
            "transfer_status": transfer_status,
            "withdraw_fee_asset": withdraw_fee_asset,
            "gas_estimate_usd": gas_usd,
            "bridge_required": False,
            "execution_quality": str(dex_quote.get("quote_mode") or "estimated"),
            "quote_sources": [str(dex_quote.get("quote_source") or "dex")],
            "buy_url": None,
            "sell_url": dex_quote.get("swap_url") or dex_quote.get("url"),
            "notes": "Contract-first CEX->DEX route built from exchange chain metadata.",
        }

    def _build_dex_to_cex(self, asset: str, dex_quote: Dict[str, Any], cex_quote: Dict[str, Any], row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        dex_price = float(dex_quote["price"])
        cex_price = float(cex_quote["price"])
        if cex_price <= 0 or dex_price <= 0 or not _stable_prices_ok(asset, cex_price, dex_price):
            return None

        # Symmetric to _build_cex_to_dex: keep the route when deposit status
        # is unknown (likely an API glitch), only drop on explicit False.
        deposit_flag = row.get("deposit_enabled")
        if deposit_flag is False:
            return None
        transfer_status = "ok" if deposit_flag is True else "unknown"

        dex_fee_rate = self.dex_fee_rate
        gas_usd = _gas_buffer_usd(str(dex_quote.get("chain") or ""), self.gas_overrides)
        tokens_after_swap = (self.notional_usd / dex_price) * (1.0 - dex_fee_rate)
        if tokens_after_swap <= 0:
            return None
        proceeds_usd = tokens_after_swap * cex_price * (1.0 - float(cex_quote.get("fee_rate") or 0.0))
        spread = ((cex_price / dex_price) - 1.0) * 100.0
        net_profit_usd = proceeds_usd - self.notional_usd - gas_usd
        # Show the route even when it is unprofitable on the current
        # notional (gas + fees may eat profit on small size); the sanity
        # gates below still reject bogus pools / fantasy spreads.
        if spread < self.min_spread:
            return None

        # Same sanity gate as _build_cex_to_dex: 15% spread cap and pool
        # liquidity >= 3x notional. See the comment there for rationale.
        liquidity_usd = float(dex_quote.get("liquidity_usd") or 0.0)
        if spread > self.cex_dex_max_spread_pct:
            logging.debug(
                "[%s] dex->cex: implausible spread %.2f%% on %s/%s (liq=$%.0f); dropping (cap=%.1f%%)",
                asset, spread, dex_quote.get("label"), cex_quote.get("exchange"),
                liquidity_usd, self.cex_dex_max_spread_pct,
            )
            return None
        min_liq_required = max(self.cex_dex_min_pool_liq_floor_usd, self.notional_usd * self.cex_dex_min_pool_liq_ratio)
        if liquidity_usd > 0 and liquidity_usd < min_liq_required:
            logging.debug(
                "[%s] dex->cex: pool too thin (liq=$%.0f, required=$%.0f); dropping",
                asset, liquidity_usd, min_liq_required,
            )
            return None

        return {
            "symbol": cex_quote["symbol"],
            "asset": asset,
            "route_kind": "dex_to_cex",
            "buy_type": "dex",
            "sell_type": "cex",
            "buy_exchange": dex_quote["label"],
            "sell_exchange": cex_quote["exchange"],
            "buy_price": dex_price,
            "sell_price": cex_price,
            "spread": spread,
            "net_profit_usd": net_profit_usd,
            "roi_pct": (net_profit_usd / self.notional_usd) * 100.0,
            "notional_usd": self.notional_usd,
            "buy_chain": row["chain"],
            "sell_chain": row["chain"],
            "chain": row["chain"],
            "contract": row["contract"],
            "buy_contract": row["contract"],
            "sell_contract": row["contract"],
            "contract_source": row.get("contract_source"),
            "liquidity_usd": dex_quote["liquidity_usd"],
            "volume_24h": dex_quote["volume_24h"],
            "transfer_status": transfer_status,
            "withdraw_fee_asset": 0.0,
            "gas_estimate_usd": gas_usd,
            "bridge_required": False,
            "execution_quality": str(dex_quote.get("quote_mode") or "estimated"),
            "quote_sources": [str(dex_quote.get("quote_source") or "dex")],
            "buy_url": dex_quote.get("swap_url") or dex_quote.get("url"),
            "sell_url": None,
            "notes": "Contract-first DEX->CEX route built from exchange chain metadata.",
        }

    async def _build_cross_chain_routes(
        self,
        session: aiohttp.ClientSession,
        asset: str,
        contract_rows: Sequence[Dict[str, Any]],
        dex_quotes: Mapping[Tuple[str, str], Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Builds DEX->bridge->DEX routes for a single asset.

        Strategy:
          1. Group DEX quotes by chain (keep the best one per chain).
          2. For every (chain_src, chain_dst) pair where the price spread is
             at least min_spread_cfg, ask the Mayan any-asset bridge for a
             live quote. Mayan is chosen because it natively supports
             non-stable assets across Solana/EVM without requiring the same
             canonical token address on both sides.
          3. If the bridge can honor the route, compute a net-profit figure
             that accounts for DEX swap fees on both sides, the bridge
             relayer fee and destination-chain gas.

        The function is intentionally conservative: when anything looks
        suspicious (missing decimals, zero liquidity, unsupported chain)
        the candidate is skipped rather than faked. The caller is free to
        log per-asset failures at debug level.
        """
        routes: List[Dict[str, Any]] = []
        if len(dex_quotes) < 2:
            return routes

        # Collect best DEX quote per chain plus matching contract row.
        per_chain: Dict[str, Dict[str, Any]] = {}
        for row in contract_rows:
            chain = str(row.get("chain") or "").strip().lower()
            contract_lc = str(row.get("contract") or "").strip().lower()
            if not chain or not contract_lc:
                continue
            quote = dex_quotes.get((chain, contract_lc))
            if not quote:
                continue
            price = _to_float(quote.get("price"))
            liquidity = _to_float(quote.get("liquidity_usd")) or 0.0
            if price is None or price <= 0:
                continue
            if liquidity < max(1000.0, self.notional_usd * 3.0):
                # Require at least enough liquidity to cover our trade size
                # with a reasonable buffer; otherwise slippage dominates.
                continue
            current = per_chain.get(chain)
            current_liq = float((current or {}).get("liquidity", -1.0))
            if current is None or liquidity > current_liq:
                per_chain[chain] = {
                    "chain": chain,
                    "row": dict(row),
                    "quote": dict(quote),
                    "price": float(price),
                    "liquidity": float(liquidity),
                }

        if len(per_chain) < 2:
            return routes

        asset_u = str(asset or "").strip().upper()
        min_spread_cfg = float(self.min_spread or 0.0)

        # Build candidate (buy_chain, sell_chain) pairs sorted by spread.
        chain_names = list(per_chain.keys())
        candidates: List[Tuple[float, str, str]] = []
        for i, chain_a in enumerate(chain_names):
            for chain_b in chain_names[i + 1:]:
                price_a = per_chain[chain_a]["price"]
                price_b = per_chain[chain_b]["price"]
                if price_a <= 0 or price_b <= 0:
                    continue
                if price_a < price_b:
                    buy, sell = chain_a, chain_b
                else:
                    buy, sell = chain_b, chain_a
                buy_price = per_chain[buy]["price"]
                sell_price = per_chain[sell]["price"]
                if buy_price <= 0:
                    continue
                spread_pct = ((sell_price - buy_price) / buy_price) * 100.0
                if spread_pct < min_spread_cfg:
                    continue
                if not _stable_prices_ok(asset_u, buy_price, sell_price):
                    continue
                candidates.append((spread_pct, buy, sell))

        if not candidates:
            return routes

        # Evaluate top-3 spreads per asset to bound bridge API calls.
        candidates.sort(reverse=True)
        bridge_budget = 3
        preferred_providers = list(self.bot.config.get("interchain_live_bridge_provider_priority", []) or [])
        blacklist_providers = list(self.bot.config.get("interchain_live_bridge_provider_blacklist", []) or [])

        for spread_pct, buy_chain, sell_chain in candidates[:bridge_budget]:
            buy_side = per_chain[buy_chain]
            sell_side = per_chain[sell_chain]

            # Ensure at least one supported any-asset bridge is a candidate
            # for this lane and asset. Wormhole is excluded because its live
            # quote layer only covers USDC and cannot price arbitrary tokens.
            bridge_candidates = get_bridge_candidates(
                asset=asset_u,
                source_chain=buy_chain,
                dest_chain=sell_chain,
                preferred=preferred_providers,
                blacklist=blacklist_providers,
            )
            supported_ids = {"mayan", "relay", "debridge", "layerzero"}
            lane_has_supported_bridge = any(
                str(item.get("id") or "").lower() in supported_ids
                for item in bridge_candidates
            )
            if not lane_has_supported_bridge:
                continue

            try:
                bridge_quote = await self._fetch_bridge_quote(
                    session,
                    asset=asset_u,
                    source_chain=buy_chain,
                    dest_chain=sell_chain,
                    source_contract=str(buy_side["row"].get("contract") or ""),
                    dest_contract=str(sell_side["row"].get("contract") or ""),
                    source_row_decimals=buy_side["row"].get("decimals"),
                    dest_row_decimals=sell_side["row"].get("decimals"),
                    buy_price_usd=float(buy_side["price"]),
                    provider_candidates=bridge_candidates,
                )
            except Exception as exc:
                logging.debug(
                    "cross-chain quote failed for %s %s->%s: %s",
                    asset_u, buy_chain, sell_chain, exc,
                )
                continue
            if not bridge_quote:
                continue

            route = self._build_dex_bridge_dex(
                asset=asset_u,
                buy_side=buy_side,
                sell_side=sell_side,
                spread_pct=spread_pct,
                bridge_quote=bridge_quote,
            )
            if route:
                routes.append(route)

        return routes

    @staticmethod
    def _default_decimals_for(chain: str, asset: str) -> int:
        """Conservative fallback when a bridge SDK does not know the token.

        Solana tokens typically use 9 decimals (6 for stables), EVM chains
        default to 18 (6 for stables). These are *only* used as a last
        resort — Mayan's token registry is preferred when available.
        """
        chain_canon = canon_chain_name(chain)
        asset_u = str(asset or "").strip().upper()
        if asset_u in {"USDC", "USDT", "DAI", "FDUSD", "TUSD", "USDE", "USD1"}:
            return 6
        if chain_canon == "solana":
            return 9
        return 18

    def _generic_token_payload(
        self,
        chain: str,
        asset: str,
        contract: str,
        decimals: Optional[int],
    ) -> Dict[str, Any]:
        """Token payload accepted by Relay/deBridge/LayerZero quote APIs.

        These APIs do not expose a symbol registry that we can reliably
        search, so we synthesise a minimal dict from what we already know
        about the DEX row plus sensible chain-specific decimals defaults.
        """
        effective_decimals = decimals if isinstance(decimals, int) and decimals > 0 else self._default_decimals_for(chain, asset)
        return {
            "address": contract,
            "contract": contract,
            "decimals": int(effective_decimals),
            "symbol": str(asset or "").strip().upper(),
        }

    async def _fetch_bridge_quote(
        self,
        session: aiohttp.ClientSession,
        *,
        asset: str,
        source_chain: str,
        dest_chain: str,
        source_contract: str,
        dest_contract: str,
        source_row_decimals: Optional[int],
        dest_row_decimals: Optional[int],
        buy_price_usd: float,
        provider_candidates: Sequence[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        """Get a live cross-chain quote, trying bridges in priority order.

        The order is determined by `get_bridge_candidates()` which already
        honours the user's `interchain_live_bridge_provider_priority` list
        from config. We restrict ourselves to providers that can price
        arbitrary (non-USDC) tokens: Mayan, Relay, deBridge, LayerZero.

        Returns the first successful quote dict (provider-neutral schema
        produced by utils.interchain_live_quotes) or None if every
        supported provider rejects the lane.
        """
        if buy_price_usd <= 0:
            return None
        tokens_needed = float(self.notional_usd) / float(buy_price_usd)
        if tokens_needed <= 0:
            return None

        supported_ids = {"mayan", "relay", "debridge", "layerzero"}
        for candidate in provider_candidates:
            provider_id = str(candidate.get("id") or "").lower()
            if provider_id not in supported_ids:
                continue
            try:
                if provider_id == "mayan":
                    quote = await self._try_mayan_quote(
                        session,
                        asset=asset,
                        source_chain=source_chain,
                        dest_chain=dest_chain,
                        source_contract=source_contract,
                        dest_contract=dest_contract,
                        tokens_needed=tokens_needed,
                    )
                elif provider_id == "relay":
                    quote = await self._try_generic_quote(
                        session,
                        fetch_fn=fetch_relay_quote,
                        asset=asset,
                        source_chain=source_chain,
                        dest_chain=dest_chain,
                        source_contract=source_contract,
                        dest_contract=dest_contract,
                        source_decimals_hint=source_row_decimals,
                        dest_decimals_hint=dest_row_decimals,
                        tokens_needed=tokens_needed,
                    )
                elif provider_id == "debridge":
                    quote = await self._try_generic_quote(
                        session,
                        fetch_fn=fetch_debridge_quote,
                        asset=asset,
                        source_chain=source_chain,
                        dest_chain=dest_chain,
                        source_contract=source_contract,
                        dest_contract=dest_contract,
                        source_decimals_hint=source_row_decimals,
                        dest_decimals_hint=dest_row_decimals,
                        tokens_needed=tokens_needed,
                    )
                elif provider_id == "layerzero":
                    quote = await self._try_generic_quote(
                        session,
                        fetch_fn=fetch_layerzero_quote,
                        asset=asset,
                        source_chain=source_chain,
                        dest_chain=dest_chain,
                        source_contract=source_contract,
                        dest_contract=dest_contract,
                        source_decimals_hint=source_row_decimals,
                        dest_decimals_hint=dest_row_decimals,
                        tokens_needed=tokens_needed,
                    )
                else:
                    continue
            except Exception as exc:
                logging.debug(
                    "%s quote raised for %s %s->%s: %s",
                    provider_id, asset, source_chain, dest_chain, exc,
                )
                quote = None
            if quote:
                return quote
        return None

    async def _try_mayan_quote(
        self,
        session: aiohttp.ClientSession,
        *,
        asset: str,
        source_chain: str,
        dest_chain: str,
        source_contract: str,
        dest_contract: str,
        tokens_needed: float,
    ) -> Optional[Dict[str, Any]]:
        source_token = await get_mayan_supported_token(
            session, source_chain, asset, contract=source_contract or None
        )
        dest_token = await get_mayan_supported_token(
            session, dest_chain, asset, contract=dest_contract or None
        )
        if not source_token or not dest_token:
            return None
        try:
            src_decimals = int(source_token.get("decimals") or 0)
        except (TypeError, ValueError):
            src_decimals = 0
        if src_decimals <= 0:
            return None
        amount_atomic = max(1, int(round(tokens_needed * (10 ** src_decimals))))
        return await fetch_mayan_quote(
            session,
            source_chain=source_chain,
            dest_chain=dest_chain,
            source_token=source_token,
            dest_token=dest_token,
            amount_atomic=amount_atomic,
        )

    async def _try_generic_quote(
        self,
        session: aiohttp.ClientSession,
        *,
        fetch_fn: Any,
        asset: str,
        source_chain: str,
        dest_chain: str,
        source_contract: str,
        dest_contract: str,
        source_decimals_hint: Optional[int],
        dest_decimals_hint: Optional[int],
        tokens_needed: float,
    ) -> Optional[Dict[str, Any]]:
        """Relay / deBridge / LayerZero call helper.

        These providers accept raw contract addresses, so we skip the
        registry lookup step and build a minimal token payload directly.
        """
        if not source_contract or not dest_contract:
            return None
        source_token = self._generic_token_payload(source_chain, asset, source_contract, source_decimals_hint)
        dest_token = self._generic_token_payload(dest_chain, asset, dest_contract, dest_decimals_hint)
        src_decimals = int(source_token["decimals"])
        if src_decimals <= 0:
            return None
        amount_atomic = max(1, int(round(tokens_needed * (10 ** src_decimals))))
        return await fetch_fn(
            session,
            source_chain=source_chain,
            dest_chain=dest_chain,
            source_token=source_token,
            dest_token=dest_token,
            amount_atomic=amount_atomic,
        )

    def _build_dex_bridge_dex(
        self,
        *,
        asset: str,
        buy_side: Dict[str, Any],
        sell_side: Dict[str, Any],
        spread_pct: float,
        bridge_quote: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        """Assembles a dex_bridge_dex route dict from the raw bridge quote
        plus the two DEX legs.

        Profit model (token-based, NOT bridge-USD based):

          1. User spends ``notional_usd`` on ``buy_side`` DEX and receives
             ``tokens_after_buy = (notional / buy_price) * (1 - dex_fee)``
             tokens of ``asset`` on the source chain.
          2. The bridge forwards those tokens; ``bridge_quote`` tells us how
             many destination tokens come out (``amount_out_atomic``). Any
             relayer / wallet-gas fees are already baked into that number.
          3. The user sells ``tokens_received`` on ``sell_side`` DEX at
             ``sell_price``, paying ``dex_fee`` again, to obtain
             ``proceeds_usd``.
          4. ``net_profit_usd = proceeds_usd - notional_usd - chain_gas``.

        Prior revisions multiplied ``amount_out_usd`` from the bridge by a
        DEX fee - that value is the bridge's own USD estimate (often based
        on a shallow off-chain price feed) and can be wildly wrong for
        thinly-traded tokens (USD1 on Solana returned a >$7k profit for a
        $300 notional). Using ``tokens * sell_price`` keeps the result
        anchored to the same DEX prices we compute the spread from, and a
        final ROI sanity cap rejects remaining outliers.
        """
        buy_price = float(buy_side["price"])
        sell_price = float(sell_side["price"])
        if buy_price <= 0 or sell_price <= 0:
            return None

        dex_fee_rate = self.dex_fee_rate
        gas_buy_usd = _gas_buffer_usd(buy_side["chain"], self.gas_overrides)
        gas_sell_usd = _gas_buffer_usd(sell_side["chain"], self.gas_overrides)

        relayer_fee_usd = max(0.0, _to_float(bridge_quote.get("relayer_fee_usd")) or 0.0)
        wallet_gas_usd = max(0.0, _to_float(bridge_quote.get("wallet_gas_usd")) or 0.0)

        src_decimals = int(
            ((bridge_quote.get("source_token") or {}).get("decimals"))
            or buy_side["row"].get("decimals")
            or 0
        )
        dst_decimals = int(
            ((bridge_quote.get("dest_token") or {}).get("decimals"))
            or sell_side["row"].get("decimals")
            or 0
        )
        amount_in_atomic = int(bridge_quote.get("amount_in_atomic") or 0)
        amount_out_atomic = int(bridge_quote.get("amount_out_atomic") or 0)
        if src_decimals <= 0 or dst_decimals <= 0 or amount_in_atomic <= 0 or amount_out_atomic <= 0:
            return None

        tokens_in = amount_in_atomic / float(10 ** src_decimals)
        tokens_received = amount_out_atomic / float(10 ** dst_decimals)
        if tokens_in <= 0 or tokens_received <= 0:
            return None

        # Sanity check #1: the bridge's effective token-to-token rate
        # should be close to 1:1 (same asset on both sides). A rate wildly
        # different from the DEX spread means either decimals are wrong or
        # the bridge is priced against a wrapped asset with a different
        # supply / price on the destination chain. Reject outliers so we
        # don't display fantasy profits.
        bridge_token_ratio = tokens_received / tokens_in
        # Tolerate up to ~5% haircut (Mayan swift fees etc.) and 1% premium
        # for rounding/slippage. Anything outside that is treated as junk.
        if bridge_token_ratio < self.cross_chain_bridge_ratio_min or bridge_token_ratio > self.cross_chain_bridge_ratio_max:
            logging.debug(
                "[%s] %s->%s: bridge ratio out of range: %.4f (tokens_in=%.6f tokens_out=%.6f)",
                asset, buy_side["chain"], sell_side["chain"],
                bridge_token_ratio, tokens_in, tokens_received,
            )
            return None

        # Proceeds are priced via the same live DEX sell price we used to
        # compute the spread, NOT the bridge's internal USD estimate.
        proceeds_after_sell = tokens_received * sell_price * (1.0 - dex_fee_rate)

        # Cost: what the user spent on the buy-side DEX (notional already
        # includes DEX fee because we derived tokens_needed from notional/
        # buy_price at fetch time) plus gas on both chains and any explicit
        # wallet-gas charge reported by the bridge.
        notional_usd = float(self.notional_usd)
        total_cost = notional_usd + gas_buy_usd + gas_sell_usd + wallet_gas_usd
        net_profit_usd = proceeds_after_sell - total_cost
        # Do NOT drop routes with negative net profit here. A cross-chain
        # opportunity can be real but currently eaten by gas on small
        # notional; the user sees it and can decide to scale up, use a
        # cheaper bridge, or wait for lower gas. Sanity checks below still
        # block data errors (bridge_ratio / ROI / implied-vs-DEX spread).

        roi_pct = (net_profit_usd / notional_usd) * 100.0 if notional_usd > 0 else 0.0

        # Sanity check #2: cross-chain DEX->bridge->DEX arbitrage rarely
        # returns more than a few percent. Anything above the configured cap
        # (default 25%) is almost always a pricing glitch on one leg.
        if roi_pct > self.cross_chain_max_roi_pct:
            logging.debug(
                "[%s] %s->%s: implausible ROI %.2f%% (notional=$%.2f proceeds=$%.2f); dropping route",
                asset, buy_side["chain"], sell_side["chain"],
                roi_pct, notional_usd, proceeds_after_sell,
            )
            return None

        # Sanity check #3: the profit-driven spread must agree (within ~1pp)
        # with the DEX price spread. Large disagreements mean the route's
        # profit is coming from the bridge quote, not from a real arbitrage.
        implied_spread_pct = ((proceeds_after_sell / notional_usd) - 1.0) * 100.0
        if implied_spread_pct - float(spread_pct) > 5.0:
            logging.debug(
                "[%s] %s->%s: implied spread %.2f%% exceeds DEX spread %.2f%% by >5pp; dropping route",
                asset, buy_side["chain"], sell_side["chain"],
                implied_spread_pct, float(spread_pct),
            )
            return None

        amount_in_usd = tokens_in * buy_price
        amount_out_usd = tokens_received * sell_price

        provider_name = str(bridge_quote.get("provider_name") or bridge_quote.get("provider_id") or "bridge")
        router = str(bridge_quote.get("router") or provider_name)
        buy_dex_label = str(buy_side["quote"].get("label") or f"DEX ({buy_side['chain']})")
        sell_dex_label = str(sell_side["quote"].get("label") or f"DEX ({sell_side['chain']})")

        return {
            "symbol": f"{asset}/USD",
            "asset": asset,
            "route_kind": "dex_bridge_dex",
            "buy_type": "dex",
            "sell_type": "dex",
            "buy_exchange": buy_dex_label,
            "sell_exchange": sell_dex_label,
            "buy_price": buy_price,
            "sell_price": sell_price,
            "spread": float(spread_pct),
            "net_profit_usd": float(net_profit_usd),
            "roi_pct": float(roi_pct),
            "notional_usd": notional_usd,
            "buy_chain": buy_side["chain"],
            "sell_chain": sell_side["chain"],
            "chain": buy_side["chain"],
            "contract": str(buy_side["row"].get("contract") or ""),
            "buy_contract": str(buy_side["row"].get("contract") or ""),
            "sell_contract": str(sell_side["row"].get("contract") or ""),
            "contract_source": buy_side["row"].get("contract_source"),
            "liquidity_usd": min(
                float(buy_side["liquidity"]),
                float(sell_side["liquidity"]),
            ),
            "volume_24h": min(
                float(buy_side["quote"].get("volume_24h") or 0.0),
                float(sell_side["quote"].get("volume_24h") or 0.0),
            ),
            "transfer_status": "ok",
            "withdraw_fee_asset": 0.0,
            "gas_estimate_usd": float(gas_buy_usd + gas_sell_usd + wallet_gas_usd),
            "bridge_required": True,
            "bridge_provider": provider_name,
            "bridge_provider_id": str(bridge_quote.get("provider_id") or "").lower(),
            "bridge_router": router,
            "bridge_relayer_fee_usd": float(relayer_fee_usd),
            "bridge_time_sec": int(bridge_quote.get("time_estimate_sec") or 0),
            "execution_quality": "live",
            "quote_sources": [
                str(buy_side["quote"].get("quote_source") or "dex"),
                str(sell_side["quote"].get("quote_source") or "dex"),
                "mayan",
            ],
            "buy_url": buy_side["quote"].get("swap_url") or buy_side["quote"].get("url"),
            "sell_url": sell_side["quote"].get("swap_url") or sell_side["quote"].get("url"),
            "notes": (
                f"DEX->bridge->DEX route via {provider_name}; "
                f"buy on {buy_side['chain']}, sell on {sell_side['chain']}."
            ),
        }

    async def process_asset(self, session: aiohttp.ClientSession, asset: str) -> Dict[str, Any]:
        rows = await self.get_asset_rows(asset)
        contract_rows, skipped_rows = self.build_contract_rows(asset, rows)
        if not contract_rows:
            return {
                "asset": asset,
                "routes": [],
                "dex_quotes_found": 0,
                "contract_rows": contract_rows,
                "skipped_rows": skipped_rows,
                "cex_quotes": self.pick_cex_quotes(asset),
                "dex_quotes": [],
            }

        quote_tasks = {}
        for row in contract_rows:
            key = (row["chain"], row["contract"].lower())
            if key not in quote_tasks:
                quote_tasks[key] = asyncio.create_task(
                    self.fetch_dex_quote(
                        session,
                        asset=asset,
                        chain=row["chain"],
                        contract=row["contract"],
                        contract_source=str(row.get("contract_source") or "exchange_contract"),
                        decimals=row.get("decimals"),
                    )
                )
        dex_quotes: Dict[Tuple[str, str], Dict[str, Any]] = {}
        for key, task in quote_tasks.items():
            try:
                quote = await asyncio.wait_for(task, timeout=self.per_asset_timeout_sec)
            except Exception:
                quote = None
            if quote:
                dex_quotes[key] = quote

        cex_quotes = self.pick_cex_quotes(asset)
        cex_by_exchange = {item["exchange"]: item for item in cex_quotes}
        routes: List[Dict[str, Any]] = []

        # CEX <-> DEX routes (cex_to_dex / dex_to_cex).
        # Skipped when the user explicitly selected "cross_chain" only.
        if self.route_group in ("all", "cex_dex"):
            for row in contract_rows:
                dex_quote = dex_quotes.get((row["chain"], row["contract"].lower()))
                cex_quote = cex_by_exchange.get(row["exchange"])
                if not dex_quote or not cex_quote:
                    continue
                route = self._build_cex_to_dex(asset, cex_quote, dex_quote, row)
                if route:
                    routes.append(route)
                route = self._build_dex_to_cex(asset, dex_quote, cex_quote, row)
                if route:
                    routes.append(route)

        # Cross-chain DEX<->DEX routes via an any-asset bridge (Mayan).
        # Skipped when the user explicitly selected "cex_dex" only.
        if self.route_group in ("all", "cross_chain"):
            try:
                cross_routes = await self._build_cross_chain_routes(
                    session, asset, contract_rows, dex_quotes
                )
                routes.extend(cross_routes)
            except Exception as exc:
                logging.debug("cross-chain route build failed for %s: %s", asset, exc)

        deduped: Dict[Tuple[str, str, str, str], Dict[str, Any]] = {}
        for route in routes:
            key = (
                str(route.get("route_kind") or ""),
                str(route.get("buy_exchange") or ""),
                str(route.get("sell_exchange") or ""),
                str(route.get("contract") or ""),
            )
            current = deduped.get(key)
            if current is None or float(route.get("net_profit_usd", 0.0) or 0.0) > float(current.get("net_profit_usd", 0.0) or 0.0):
                deduped[key] = route

        return {
            "asset": asset,
            "routes": list(deduped.values()),
            "dex_quotes_found": len(dex_quotes),
            "contract_rows": contract_rows,
            "skipped_rows": skipped_rows,
            "cex_quotes": cex_quotes,
            "dex_quotes": list(dex_quotes.values()),
        }

    async def scan(self) -> Dict[str, Any]:
        candidate_assets_all = self._candidate_assets()
        candidate_assets, candidate_rotation_offset = self._select_auto_scan_assets(candidate_assets_all)
        # Cross-chain mode is now ENABLED (was previously hard-disabled with
        # "contract_first_phase1" marker). Routes of kind "dex_bridge_dex" are
        # produced in process_asset() via _build_cross_chain_routes() using the
        # Mayan any-asset bridge. When route_group="cex_dex" we skip bridge
        # quote requests to keep the fast path fast.

        routes: List[Dict[str, Any]] = []
        assets_scanned = 0
        dex_quotes_found = 0
        skipped_rows_without_contract = 0
        contract_rows_used = 0
        timeout_counter = 0
        failed_assets = 0

        semaphore = asyncio.Semaphore(self.parallel_assets)

        async with aiohttp.ClientSession(headers={"User-Agent": "arbx/1.0 (+contract-first-cex-dex)"}) as session:
            async def worker(asset: str) -> Dict[str, Any]:
                async with semaphore:
                    return await asyncio.wait_for(self.process_asset(session, asset), timeout=self.per_asset_timeout_sec)

            tasks = []
            for asset in candidate_assets:
                tasks.append(asyncio.create_task(worker(asset)))

            for asset, task in zip(candidate_assets, tasks):
                try:
                    result = await task
                except TimeoutError:
                    timeout_counter += 1
                    logging.info("Contract-first scan timed out for %s after %.1fs", asset, self.per_asset_timeout_sec)
                    continue
                except Exception as exc:
                    failed_assets += 1
                    logging.debug("Contract-first scan failed for %s: %s", asset, exc)
                    continue
                assets_scanned += 1
                routes.extend(result.get("routes") or [])
                dex_quotes_found += int(result.get("dex_quotes_found") or 0)
                skipped_rows_without_contract += len(result.get("skipped_rows") or [])
                contract_rows_used += len(result.get("contract_rows") or [])

        routes_before_net_filter = len(routes)
        try:
            require_positive_net = bool(self.bot.config.get("cex_dex_require_positive_net_profit", True))
        except Exception:
            require_positive_net = True
        if require_positive_net and self.route_group in ("all", "cex_dex"):
            routes = [
                item for item in routes
                if str(item.get("route_kind") or "") != "cex_to_dex"
                and str(item.get("route_kind") or "") != "dex_to_cex"
                or float(item.get("net_profit_usd", 0.0) or 0.0) > 0.0
            ]

        if self.execution_quality_filter:
            if self.execution_quality_filter == "actionable":
                allowed = {"live", "hybrid"}
            else:
                allowed = {self.execution_quality_filter}
            routes = [item for item in routes if str(item.get("execution_quality") or "").strip().lower() in allowed]

        routes.sort(
            key=lambda item: (
                float(item.get("net_profit_usd", 0.0) or 0.0),
                float(item.get("roi_pct", 0.0) or 0.0),
                float(item.get("spread", 0.0) or 0.0),
            ),
            reverse=True,
        )
        return {
            "data": routes[: self.limit],
            "statistics": {
                "assets_considered": len(candidate_assets),
                "candidate_assets_total": len(candidate_assets_all),
                "candidate_rotation_offset": candidate_rotation_offset,
                "assets_scanned": assets_scanned,
                "requested_assets": list(self.requested_assets),
                "candidate_assets_sample": list(candidate_assets[:20]),
                "routes_found": len(routes),
                "routes_before_net_filter": routes_before_net_filter,
                "require_positive_net_profit": require_positive_net,
                "dex_quotes_found": dex_quotes_found,
                "contract_rows_used": contract_rows_used,
                "skipped_rows_without_contract": skipped_rows_without_contract,
                "timed_out_assets": timeout_counter,
                "failed_assets": failed_assets,
                "notional_usd": self.notional_usd,
                "quick_mode": self.quick_mode,
                "parallel_assets": self.parallel_assets,
                "route_group": self.route_group,
                "chain_scope": self.chain_scope,
                "asset_profile": self.asset_profile,
                "execution_quality_filter": self.execution_quality_filter or None,
                "scanner_mode": "contract_first_phase2",
                "cross_chain_enabled": self.route_group in ("all", "cross_chain"),
            },
        }

    async def debug_asset(self, asset: str) -> Dict[str, Any]:
        async with aiohttp.ClientSession(headers={"User-Agent": "arbx/1.0 (+contract-first-debug)"}) as session:
            result = await self.process_asset(session, asset)
        return {
            "asset": asset,
            "asset_status_rows": await self.get_asset_rows(asset),
            "contract_rows": result.get("contract_rows") or [],
            "skipped_rows": result.get("skipped_rows") or [],
            "cex_quotes": result.get("cex_quotes") or [],
            "dex_quotes": result.get("dex_quotes") or [],
            "cex_dex_routes": sorted(
                result.get("routes") or [],
                key=lambda item: float(item.get("net_profit_usd", 0.0) or 0.0),
                reverse=True,
            )[:20],
            "cross_chain_mode": "enabled_mayan_v1",
        }


def handle_contract_first_interchain_opportunities(
    bot: Any,
    request_args: Mapping[str, Any],
    asset_status_cache: Dict[str, Any],
    dex_quote_cache: Dict[str, Any],
    interchain_scan_cache: Dict[str, Any],
    *,
    asset_status_ttl: int,
    dex_quote_ttl: int,
    interchain_scan_ttl: int,
) -> Dict[str, Any]:
    if not isinstance(getattr(bot, "_last_all_prices", None), dict) or not getattr(bot, "_last_all_prices", None):
        return {
            "success": False,
            "error": "No ticker snapshot available yet. Wait for the next monitor iteration.",
        }

    scanner = ContractFirstInterchainScanner(
        bot,
        request_args,
        asset_status_cache,
        dex_quote_cache,
        asset_status_ttl=asset_status_ttl,
        dex_quote_ttl=dex_quote_ttl,
    )
    cache_key = (
        f"cf:{scanner.snapshot_ts:.3f}:{scanner.min_spread:.4f}:{scanner.limit}:{scanner.asset_limit}:"
        f"{scanner.notional_usd:.2f}:{int(scanner.quick_mode)}:{scanner.route_group}:{scanner.chain_scope}:"
        f"{scanner.asset_profile}:{scanner.execution_quality_filter or '-'}:{','.join(scanner.requested_assets)}"
    )
    cached = interchain_scan_cache.get(cache_key)
    if cached and (time.time() - float(cached.get("ts", 0.0) or 0.0) < interchain_scan_ttl):
        return cached.get("payload") or {"success": True, "data": []}

    try:
        scan_result = asyncio.run(asyncio.wait_for(scanner.scan(), timeout=45))
    except TimeoutError:
        logging.warning("Contract-first interchain scan timed out after 45s")
        return {
            "success": False,
            "error": "Contract-first interchain scan timed out",
            "data": [],
            "statistics": {
                "notional_usd": scanner.notional_usd,
                "route_group": scanner.route_group,
                "chain_scope": scanner.chain_scope,
                "asset_profile": scanner.asset_profile,
                "scanner_mode": "contract_first_phase1",
            },
            "snapshot_ts": scanner.snapshot_ts if scanner.snapshot_ts > 0 else None,
        }

    snapshot_age_sec = int(max(0.0, time.time() - scanner.snapshot_ts)) if scanner.snapshot_ts > 0 else None
    try:
        stale_after = float(bot.config.get("monitor_interval", 60) or 60) * 2.0
    except Exception:
        stale_after = 120.0
    payload = {
        "success": True,
        "data": scan_result.get("data") or [],
        "statistics": scan_result.get("statistics") or {},
        "snapshot_ts": scanner.snapshot_ts if scanner.snapshot_ts > 0 else None,
        "snapshot_age_sec": snapshot_age_sec,
        "snapshot_stale": bool(snapshot_age_sec is not None and snapshot_age_sec >= stale_after),
    }
    interchain_scan_cache[cache_key] = {"ts": time.time(), "payload": payload}
    try:
        interchain_scan_cache.move_to_end(cache_key)
        if len(interchain_scan_cache) > 12:
            interchain_scan_cache.popitem(last=False)
    except Exception:
        pass
    return payload


def handle_contract_first_interchain_debug(
    bot: Any,
    request_args: Mapping[str, Any],
    asset_status_cache: Dict[str, Any],
    dex_quote_cache: Dict[str, Any],
    *,
    asset_status_ttl: int,
    dex_quote_ttl: int,
) -> Dict[str, Any]:
    asset_raw = str(request_args.get("asset", "") or "").strip().upper()
    asset = extract_base_asset(asset_raw, assume_pair=True) or asset_raw
    if not asset:
        return {"success": False, "error": "Asset is required"}
    if not isinstance(getattr(bot, "_last_all_prices", None), dict) or not getattr(bot, "_last_all_prices", None):
        return {
            "success": False,
            "error": "No ticker snapshot available yet. Wait for the next monitor iteration.",
        }

    scanner = ContractFirstInterchainScanner(
        bot,
        request_args,
        asset_status_cache,
        dex_quote_cache,
        asset_status_ttl=asset_status_ttl,
        dex_quote_ttl=dex_quote_ttl,
    )
    try:
        debug_data = asyncio.run(asyncio.wait_for(scanner.debug_asset(asset), timeout=30))
    except TimeoutError:
        return {"success": False, "error": "Contract-first debug timed out", "data": {"asset": asset}}
    snapshot_age_sec = int(max(0.0, time.time() - scanner.snapshot_ts)) if scanner.snapshot_ts > 0 else None
    try:
        stale_after = float(bot.config.get("monitor_interval", 60) or 60) * 2.0
    except Exception:
        stale_after = 120.0
    return {
        "success": True,
        "data": debug_data,
        "snapshot_ts": scanner.snapshot_ts if scanner.snapshot_ts > 0 else None,
        "snapshot_age_sec": snapshot_age_sec,
        "snapshot_stale": bool(snapshot_age_sec is not None and snapshot_age_sec >= stale_after),
    }
