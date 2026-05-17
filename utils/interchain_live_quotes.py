from __future__ import annotations

import asyncio
import logging
import math
import os
import re
import time
from collections import OrderedDict
from typing import Any, Dict, List, Optional, Sequence

import aiohttp

from utils.dex_swap_links import build_dex_swap_url
from constants import (
    DEBRIDGE_DLN_API_BASE,
    GECKOTERMINAL_API_BASE,
    JUPITER_LITE_API_BASE,
    LAYERZERO_TRANSFER_API_BASE,
    MAYAN_PRICE_API_BASE,
    MAYAN_SDK_VERSION,
    MAYAN_SIA_API_BASE,
    RELAY_API_BASE,
    WORMHOLE_CIRCLE_V2_API_BASE,
    WORMHOLE_EXECUTOR_API_BASE,
)


JUPITER_USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
JUPITER_USDT_MINT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
JUPITER_SOL_MINT = "So11111111111111111111111111111111111111112"
SOLANA_PLACEHOLDER_ADDRESS = "11111111111111111111111111111111"
EVM_PLACEHOLDER_ADDRESS = "0x000000000000000000000000000000000000dEaD"
SAFE_SYMBOL_ONLY_SOLANA_ASSETS = {
    "SOL",
    "USDC",
    "USDT",
    "ETH",
    "BONK",
    "JUP",
    "PYTH",
    "POPCAT",
    "WIF",
    "JTO",
    "RAY",
    "KMNO",
    "DRIFT",
    "FARTCOIN",
    "MEW",
    "PONKE",
    "TNSR",
    "CLOUD",
    "HNT",
    "MOBILE",
    "RENDER",
}

_JUPITER_SEARCH_TTL = 300
_JUPITER_PRICE_TTL = 45
_JUPITER_QUOTE_TTL = 20
_MAYAN_INIT_TTL = 1800
_MAYAN_TOKENS_TTL = 1800
_MAYAN_QUOTE_TTL = 30
_WORMHOLE_QUOTE_TTL = 30
_LAYERZERO_QUOTE_TTL = 30
_RELAY_CHAINS_TTL = 1800
_RELAY_QUOTE_TTL = 30
_DEBRIDGE_QUOTE_TTL = 30
_GECKOTERMINAL_NETWORKS_TTL = 6 * 3600
_GECKOTERMINAL_POOLS_TTL = 45
_GECKOTERMINAL_SEARCH_TTL = 120
_DEXSCREENER_SEARCH_TTL = 120

_jupiter_search_cache: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
_jupiter_price_cache: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
_jupiter_quote_cache: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
_mayan_init_cache: Dict[str, Any] = {"ts": 0.0, "data": None}
_mayan_tokens_cache: Dict[str, Any] = {"ts": 0.0, "data": None}
_mayan_quote_cache: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
_wormhole_quote_cache: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
_layerzero_quote_cache: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
_relay_chains_cache: Dict[str, Any] = {"ts": 0.0, "data": None}
_relay_quote_cache: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
_debridge_quote_cache: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
_geckoterminal_networks_cache: Dict[str, Any] = {"ts": 0.0, "data": None}
_geckoterminal_pools_cache: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
_geckoterminal_search_cache: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
_dexscreener_search_cache: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
_coingecko_platform_list_cache: Dict[str, Any] = {"ts": 0.0, "data": None}

_DEBRIDGE_CHAIN_IDS = {
    "ethereum": 1,
    "binance-smart-chain": 56,
    "polygon-pos": 137,
    "optimistic-ethereum": 10,
    "arbitrum-one": 42161,
    "avalanche": 43114,
    "base": 8453,
    "linea": 59144,
    "mantle": 5000,
    "solana": 7565164,
}

_GECKOTERMINAL_NETWORK_HINTS: Dict[str, Sequence[str]] = {
    "ethereum": ("eth",),
    "binance-smart-chain": ("bsc",),
    "polygon-pos": ("polygon_pos",),
    "arbitrum-one": ("arbitrum",),
    "optimistic-ethereum": ("optimism",),
    "avalanche": ("avax",),
    "solana": ("solana",),
    "base": ("base",),
    "linea": ("linea",),
    "scroll": ("scroll",),
    "mantle": ("mantle",),
    "tron": ("tron",),
    "fantom": ("fantom",),
    "sui": ("sui",),
    "the-open-network": ("ton",),
    "zksync": ("zksync",),
    "celo": ("celo",),
    "cronos": ("cronos",),
    "kcc": ("kcc",),
    "moonbeam": ("moonbeam",),
    "moonriver": ("movr", "moonriver"),
    "blast": ("blast",),
    "mode": ("mode",),
    "opbnb": ("opbnb",),
    "gnosis": ("xdai", "gnosis"),
    "polygon-zkevm": ("polygon_zkevm",),
    "zora-network": ("zora-network", "zora"),
    "sonic": ("sonic",),
    "berachain": ("berachain",),
}

_WORMHOLE_CHAIN_NAMES: Dict[str, str] = {
    "solana": "Solana",
    "ethereum": "Ethereum",
    "binance-smart-chain": "Bsc",
    "polygon-pos": "Polygon",
    "avalanche": "Avalanche",
    "sui": "Sui",
    "aptos": "Aptos",
    "arbitrum-one": "Arbitrum",
    "optimistic-ethereum": "Optimism",
    "base": "Base",
    "linea": "Linea",
    "sei-evm": "Seievm",
    "unichain": "Unichain",
    "worldchain": "Worldchain",
    "ink": "Ink",
    "hyperevm": "HyperEVM",
    "monad": "Monad",
    "sonic": "Sonic",
    "plume": "Plume",
}

_WORMHOLE_CHAIN_IDS: Dict[str, int] = {
    "Solana": 1,
    "Ethereum": 2,
    "Bsc": 4,
    "Polygon": 5,
    "Avalanche": 6,
    "Sui": 21,
    "Aptos": 22,
    "Arbitrum": 23,
    "Optimism": 24,
    "Base": 30,
    "Linea": 38,
    "Seievm": 40,
    "Unichain": 44,
    "Worldchain": 45,
    "Ink": 46,
    "HyperEVM": 47,
    "Monad": 48,
    "Sonic": 52,
    "Plume": 55,
}

_WORMHOLE_USDC_CONTRACTS: Dict[str, str] = {
    "ethereum": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
    "avalanche": "0xB97EF9Ef8734C71904D8002F8b6Bc66Dd9c48a6E",
    "optimistic-ethereum": "0x0b2C639c533813f4Aa9D7837CAf62653d097Ff85",
    "arbitrum-one": "0xaf88d065e77c8cC2239327C5EDb3A432268e5831",
    "solana": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
    "base": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
    "polygon-pos": "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359",
    "sui": "0xdba34672e30cb065b1f93e3ab55318768fd6fef66c15942c9f7cb846e2f900e7::usdc::USDC",
    "aptos": "0xbae207659db88bea0cbead6da0ed00aac12edcdda169e591cd41c94180b46f3b",
    "unichain": "0x078D782b760474a361dDA0AF3839290b0EF57AD6",
    "sonic": "0x29219dd400f2Bf60E5a23d13Be72B486D4038894",
    "linea": "0x176211869cA2b568f2A7D4EE941E073a821EE1ff",
    "worldchain": "0x79A02482A880bCE3F13e09Da970dC34db4CD24d1",
    "sei-evm": "0xe15fC38F6D8c56aF07bbCBe3BAf5708A2Bf42392",
    "hyperevm": "0xb88339CB7199b77E23DB6E890353E22632Ba630f",
    "plume": "0x222365EF19F7947e5484218551B56bb3965Aa7aF",
    "ink": "0x2D270e6886d130D724215A266106e6832161EAEd",
    "monad": "0x754704Bc059F8C67012fEd69BC8A327a5aafb603",
}

_WORMHOLE_CIRCLE_V2_DOMAINS: Dict[str, int] = {
    "ethereum": 0,
    "avalanche": 1,
    "optimistic-ethereum": 2,
    "arbitrum-one": 3,
    "solana": 5,
    "base": 6,
    "polygon-pos": 7,
    "unichain": 10,
    "linea": 11,
    "sonic": 13,
    "worldchain": 14,
    "monad": 15,
    "sei-evm": 16,
    "hyperevm": 19,
    "ink": 21,
    "plume": 22,
}

_WORMHOLE_CCTP_GAS_LIMITS: Dict[str, int] = {
    "Arbitrum": 800000,
    "Avalanche": 250000,
    "Base": 250000,
    "Ethereum": 250000,
    "HyperEVM": 250000,
    "Linea": 250000,
    "Optimism": 250000,
    "Polygon": 250000,
    "Unichain": 250000,
    "Seievm": 250000,
    "Solana": 250000,
    "Sonic": 250000,
    "Worldchain": 250000,
    "Plume": 250000,
    "Ink": 250000,
    "Monad": 500000,
}

_WORMHOLE_FAST_ETA_SEC: Dict[str, int] = {
    "Arbitrum": 8,
    "Base": 8,
    "Ethereum": 20,
    "Linea": 8,
    "Optimism": 8,
    "Polygon": 8,
    "Unichain": 8,
    "Seievm": 8,
    "Worldchain": 8,
    "Plume": 8,
    "Ink": 8,
}

_WORMHOLE_STANDARD_ETA_SEC: Dict[str, int] = {
    "Solana": 13,
    "Ethereum": 1081,
    "Avalanche": 1,
    "Arbitrum": 1065,
    "Optimism": 1025,
    "Base": 1025,
    "Polygon": 400,
    "Unichain": 1025,
    "Worldchain": 1025,
    "Seievm": 1,
    "HyperEVM": 2,
    "Ink": 513,
    "Monad": 2,
    "Sonic": 1,
    "Plume": 1025,
}


def _to_float(value: Any) -> Optional[float]:
    try:
        if value in (None, "", "null"):
            return None
        return float(value)
    except Exception:
        return None


def canon_chain_name(name: str) -> str:
    raw = str(name or "").strip().lower()
    mapping = {
        "eth": "ethereum",
        "erc20": "ethereum",
        "ethereum": "ethereum",
        "sol": "solana",
        "solana": "solana",
        "tron": "tron",
        "trc20": "tron",
        "trc-20": "tron",
        "bsc": "binance-smart-chain",
        "bnb smart chain": "binance-smart-chain",
        "binance smart chain": "binance-smart-chain",
        "binance-smart-chain": "binance-smart-chain",
        "polygon": "polygon-pos",
        "matic": "polygon-pos",
        "polygon-pos": "polygon-pos",
        "arb": "arbitrum-one",
        "arbitrum": "arbitrum-one",
        "arbitrum one": "arbitrum-one",
        "arbitrum-one": "arbitrum-one",
        "bnbchain": "binance-smart-chain",
        "bnb chain": "binance-smart-chain",
        "op": "optimistic-ethereum",
        "optimism": "optimistic-ethereum",
        "optimistic-ethereum": "optimistic-ethereum",
        "base": "base",
        "unichain": "unichain",
        "monad": "monad",
        "hyperevm": "hyperevm",
        "hypercore": "hypercore",
        "fogo": "fogo",
        "mantle": "mantle",
        "mnt": "mantle",
        "linea": "linea",
        "scroll": "scroll",
        "sonic": "sonic",
        "avax": "avalanche",
        "avalanche": "avalanche",
        "fantom": "fantom",
        "sui": "sui",
        "zksync": "zksync",
        "zksync era": "zksync",
        "zksync-era": "zksync",
        "blast": "blast",
        "mode": "mode",
        "celo": "celo",
        "cronos": "cronos",
        "kcc": "kcc",
        "kucoin-community-chain": "kcc",
        "moonbeam": "moonbeam",
        "moonriver": "moonriver",
        "movr": "moonriver",
        "opbnb": "opbnb",
        "polygon zkevm": "polygon-zkevm",
        "polygon-zkevm": "polygon-zkevm",
        "polygon_zkevm": "polygon-zkevm",
        "zora": "zora-network",
        "zora-network": "zora-network",
        "gnosis": "gnosis",
        "xdai": "gnosis",
        "canto": "canto",
        "sei": "sei-evm",
        "sei-evm": "sei-evm",
        "sonic": "sonic",
        "berachain": "berachain",
        "bera": "berachain",
        "apt": "aptos",
        "aptos": "aptos",
        "ton": "the-open-network",
        "the-open-network": "the-open-network",
        "near": "near-protocol",
        "near-protocol": "near-protocol",
    }
    return mapping.get(raw, raw or "unknown")


def _now() -> float:
    return time.time()


def _address_key(value: Any) -> str:
    return str(value or "").strip().lower()


def _text_key(value: Any) -> str:
    text = str(value or "").strip().lower()
    return re.sub(r"[^a-z0-9]+", "", text)


def _is_zero_address(value: Any) -> bool:
    normalized = _address_key(value)
    return normalized in {
        "",
        "0x0",
        "0x0000000000000000000000000000000000000000",
    }


def _stable_asset_symbol(value: Any) -> bool:
    return str(value or "").strip().upper() in {"USDC", "USDT", "DAI", "FDUSD", "TUSD", "USD1", "USDE"}


def wormhole_chain_name(chain: str) -> Optional[str]:
    return _WORMHOLE_CHAIN_NAMES.get(canon_chain_name(chain))


def _wormhole_native_decimals(chain: str) -> int:
    return 9 if canon_chain_name(chain) == "solana" else 18


def _wormhole_usdc_contract(chain: str) -> Optional[str]:
    return _WORMHOLE_USDC_CONTRACTS.get(canon_chain_name(chain))


def _wormhole_source_token_matches(chain: str, token: Dict[str, Any]) -> bool:
    contract = _wormhole_usdc_contract(chain)
    if not contract:
        return False
    token_address = str(token.get("address") or token.get("contract") or "").strip()
    if not token_address:
        return False
    return _address_key(token_address) == _address_key(contract)


def _wormhole_relay_instructions_hex(dest_chain: str) -> Optional[str]:
    wormhole_chain = wormhole_chain_name(dest_chain)
    if not wormhole_chain:
        return None
    gas_limit = int(_WORMHOLE_CCTP_GAS_LIMITS.get(wormhole_chain) or 0)
    if gas_limit <= 0:
        return None
    msg_value = 5001 if wormhole_chain == "Solana" else 0
    payload = bytes([1]) + int(gas_limit).to_bytes(16, "big") + int(msg_value).to_bytes(16, "big")
    return "0x" + payload.hex()


def _decode_wormhole_signed_quote(value: Any) -> Optional[Dict[str, int]]:
    raw = str(value or "").strip()
    if raw.startswith("0x"):
        raw = raw[2:]
    if not raw or len(raw) < (4 + 20 + 32 + 2 + 2 + 8 + 8 + 8 + 8 + 8) * 2:
        return None
    try:
        data = bytes.fromhex(raw)
    except ValueError:
        return None
    if len(data) < 105:
        return None
    if data[:4] != b"EQ01":
        return None
    offset = 4 + 20 + 32
    src_chain_id = int.from_bytes(data[offset:offset + 2], "big")
    offset += 2
    dst_chain_id = int.from_bytes(data[offset:offset + 2], "big")
    offset += 2
    expiry_time = int.from_bytes(data[offset:offset + 8], "big")
    offset += 8
    base_fee = int.from_bytes(data[offset:offset + 8], "big")
    offset += 8
    dst_gas_price = int.from_bytes(data[offset:offset + 8], "big")
    offset += 8
    src_price = int.from_bytes(data[offset:offset + 8], "big")
    offset += 8
    dst_price = int.from_bytes(data[offset:offset + 8], "big")
    return {
        "src_chain_id": src_chain_id,
        "dst_chain_id": dst_chain_id,
        "expiry_time": expiry_time,
        "base_fee": base_fee,
        "dst_gas_price": dst_gas_price,
        "src_price": src_price,
        "dst_price": dst_price,
    }


def _wormhole_native_fee_usd(source_chain: str, estimated_cost_atomic: int, src_price_scaled: int) -> float:
    if estimated_cost_atomic <= 0 or src_price_scaled <= 0:
        return 0.0
    native_decimals = _wormhole_native_decimals(source_chain)
    fee_native = float(estimated_cost_atomic) / float(10 ** native_decimals)
    native_price_usd = float(src_price_scaled) / 1e10
    return fee_native * native_price_usd


def _wormhole_fast_fee_atomic(amount_atomic: int, fee_bps: float) -> int:
    scaled_bps = int(round(float(fee_bps) * 100.0))
    return int((int(amount_atomic) * scaled_bps + 1_000_000 - 1) // 1_000_000)


def _cache_get(cache: Dict[str, Dict[str, Any]], key: str, ttl: float) -> Any:
    item = cache.get(key)
    if not item:
        return None
    if _now() - float(item.get("ts", 0.0) or 0.0) >= ttl:
        cache.pop(key, None)
        return None
    move_to_end = getattr(cache, "move_to_end", None)
    if callable(move_to_end):
        move_to_end(key)
    return item.get("data")


def _cache_set(cache: Dict[str, Dict[str, Any]], key: str, value: Any, max_size: int = 256) -> Any:
    cache.pop(key, None)
    cache[key] = {"ts": _now(), "data": value}
    move_to_end = getattr(cache, "move_to_end", None)
    if callable(move_to_end):
        move_to_end(key)
    if len(cache) > max_size:
        popitem = getattr(cache, "popitem", None)
        if callable(popitem):
            try:
                popitem(last=False)
            except TypeError:
                oldest_key = next(iter(cache))
                cache.pop(oldest_key, None)
        else:
            oldest_key = next(iter(cache))
            cache.pop(oldest_key, None)
    return value


async def _fetch_json(
    session: aiohttp.ClientSession,
    method: str,
    url: str,
    *,
    params: Optional[Dict[str, Any]] = None,
    json_payload: Optional[Dict[str, Any]] = None,
    headers: Optional[Dict[str, str]] = None,
    timeout_sec: float = 12.0,
) -> Optional[Any]:
    retryable_statuses = {408, 425, 429, 500, 502, 503, 504}
    backoff_schedule = (0.0, 0.35, 0.9)
    attempts = len(backoff_schedule)

    for attempt in range(attempts):
        if attempt > 0:
            await asyncio.sleep(backoff_schedule[attempt])
        try:
            async with session.request(
                method.upper(),
                url,
                params=params,
                json=json_payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=timeout_sec),
            ) as response:
                if response.status == 200:
                    return await response.json(content_type=None)
                if response.status in retryable_statuses and attempt < attempts - 1:
                    continue
                return None
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            if attempt >= attempts - 1:
                logging.debug("interchain live quote request failed for %s: %s", url, exc)
                return None
        except Exception as exc:
            logging.debug("interchain live quote request failed for %s: %s", url, exc)
            return None
    return None


def _jupiter_volume_24h(item: Dict[str, Any]) -> float:
    stats = item.get("stats24h") or {}
    if not isinstance(stats, dict):
        return 0.0
    buy_volume = _to_float(stats.get("buyVolume")) or 0.0
    sell_volume = _to_float(stats.get("sellVolume")) or 0.0
    return max(0.0, buy_volume + sell_volume)


def _score_jupiter_token(item: Dict[str, Any], symbol: str, preferred_mint: Optional[str]) -> float:
    symbol_u = str(symbol or "").strip().upper()
    item_symbol = str(item.get("symbol") or "").strip().upper()
    item_id = str(item.get("id") or "").strip()
    tags = {str(tag).strip().lower() for tag in (item.get("tags") or []) if tag}
    liquidity = max(0.0, _to_float(item.get("liquidity")) or 0.0)
    mcap = max(0.0, _to_float(item.get("mcap")) or _to_float(item.get("fdv")) or 0.0)
    organic = max(0.0, _to_float(item.get("organicScore")) or 0.0)
    verified = bool(item.get("isVerified"))

    score = 0.0
    if preferred_mint and item_id == preferred_mint:
        score += 5000.0
    if item_symbol == symbol_u:
        score += 3000.0
    elif item_symbol.startswith(symbol_u):
        score += 600.0
    if verified:
        score += 1000.0
    if "strict" in tags:
        score += 700.0
    if "verified" in tags:
        score += 500.0
    if "major" in tags:
        score += 400.0
    score += organic * 8.0
    score += math.log10(liquidity + 1.0) * 60.0
    score += math.log10(mcap + 1.0) * 35.0
    score += math.log10(_jupiter_volume_24h(item) + 1.0) * 15.0
    return score


def _is_canonical_jupiter_candidate(item: Dict[str, Any]) -> bool:
    tags = {str(tag).strip().lower() for tag in (item.get("tags") or []) if tag}
    verified = bool(item.get("isVerified")) or "verified" in tags
    if not verified:
        return False
    if {"strict", "major", "stable"} & tags:
        return True
    liquidity = max(0.0, _to_float(item.get("liquidity")) or 0.0)
    mcap = max(0.0, _to_float(item.get("mcap")) or _to_float(item.get("fdv")) or 0.0)
    organic = max(0.0, _to_float(item.get("organicScore")) or 0.0)
    return liquidity >= 1_000_000.0 and mcap >= 10_000_000.0 and organic >= 85.0


async def search_jupiter_tokens(
    session: aiohttp.ClientSession,
    query: str,
    *,
    limit: int = 12,
) -> List[Dict[str, Any]]:
    query_clean = str(query or "").strip()
    if not query_clean:
        return []
    cache_key = query_clean.upper()
    cached = _cache_get(_jupiter_search_cache, cache_key, _JUPITER_SEARCH_TTL)
    if cached is not None:
        return list(cached)
    url = f"{JUPITER_LITE_API_BASE}/tokens/v2/search"
    payload = await _fetch_json(session, "GET", url, params={"query": query_clean}, timeout_sec=12.0)
    if not isinstance(payload, list):
        return []
    return list(_cache_set(_jupiter_search_cache, cache_key, payload[: max(1, int(limit) * 2)], max_size=128))


async def fetch_jupiter_price(session: aiohttp.ClientSession, mint: str) -> Optional[Dict[str, Any]]:
    mint_clean = str(mint or "").strip()
    if not mint_clean:
        return None
    cached = _cache_get(_jupiter_price_cache, mint_clean, _JUPITER_PRICE_TTL)
    if cached is not None:
        return cached if isinstance(cached, dict) else None
    url = f"{JUPITER_LITE_API_BASE}/price/v3"
    payload = await _fetch_json(session, "GET", url, params={"ids": mint_clean}, timeout_sec=10.0)
    if not isinstance(payload, dict):
        return None
    data = payload.get(mint_clean)
    if not isinstance(data, dict):
        return None
    return _cache_set(_jupiter_price_cache, mint_clean, data, max_size=256)


async def resolve_solana_token(
    session: aiohttp.ClientSession,
    symbol: str,
    *,
    preferred_mint: Optional[str] = None,
    allow_symbol_only: bool = False,
) -> Optional[Dict[str, Any]]:
    symbol_u = str(symbol or "").strip().upper()
    if not preferred_mint and not allow_symbol_only and symbol_u not in SAFE_SYMBOL_ONLY_SOLANA_ASSETS:
        return None

    results = await search_jupiter_tokens(session, symbol)
    if not results and preferred_mint:
        price_data = await fetch_jupiter_price(session, preferred_mint)
        if not price_data:
            return None
        return {
            "chain": "solana",
            "contract": preferred_mint,
            "decimals": 9 if preferred_mint == JUPITER_SOL_MINT else 6 if preferred_mint in {JUPITER_USDC_MINT, JUPITER_USDT_MINT} else None,
            "price": _to_float(price_data.get("usdPrice")),
            "liquidity_usd": _to_float(price_data.get("liquidity")) or 0.0,
            "volume_24h": 0.0,
            "verified": False,
            "symbol": symbol_u,
            "name": symbol_u,
            "score": 0.0,
            "source": "jupiter_price",
        }

    exact_symbol = []
    fallback = []
    for item in results:
        if not isinstance(item, dict):
            continue
        if str(item.get("symbol") or "").strip().upper() == symbol_u:
            exact_symbol.append(item)
        else:
            fallback.append(item)
    pool = exact_symbol or fallback
    if not preferred_mint:
        pool = [item for item in pool if _is_canonical_jupiter_candidate(item)]
    if not pool:
        return None

    best = max(pool, key=lambda item: _score_jupiter_token(item, symbol, preferred_mint))
    return {
        "chain": "solana",
        "contract": str(best.get("id") or "").strip(),
        "decimals": int(best.get("decimals") or 0) if best.get("decimals") is not None else None,
        "price": _to_float(best.get("usdPrice")),
        "liquidity_usd": _to_float(best.get("liquidity")) or 0.0,
        "volume_24h": _jupiter_volume_24h(best),
        "verified": bool(best.get("isVerified")),
        "symbol": str(best.get("symbol") or symbol_u).strip().upper(),
        "name": str(best.get("name") or symbol_u).strip(),
        "score": _score_jupiter_token(best, symbol, preferred_mint),
        "source": "jupiter_search",
        "tags": list(best.get("tags") or []),
    }


async def fetch_jupiter_quote(
    session: aiohttp.ClientSession,
    *,
    input_mint: str,
    output_mint: str,
    amount_atomic: int,
    slippage_bps: int = 50,
) -> Optional[Dict[str, Any]]:
    if amount_atomic <= 0:
        return None
    cache_key = f"{input_mint}:{output_mint}:{int(amount_atomic)}:{int(slippage_bps)}"
    cached = _cache_get(_jupiter_quote_cache, cache_key, _JUPITER_QUOTE_TTL)
    if cached is not None:
        return cached if isinstance(cached, dict) else None
    url = f"{JUPITER_LITE_API_BASE}/swap/v1/quote"
    params = {
        "inputMint": input_mint,
        "outputMint": output_mint,
        "amount": str(int(amount_atomic)),
        "slippageBps": str(int(slippage_bps)),
        "restrictIntermediateTokens": "true",
    }
    payload = await _fetch_json(session, "GET", url, params=params, timeout_sec=12.0)
    if not isinstance(payload, dict) or "outAmount" not in payload:
        return None
    return _cache_set(_jupiter_quote_cache, cache_key, payload, max_size=256)


def _jupiter_route_labels(payload: Dict[str, Any]) -> List[str]:
    labels: List[str] = []
    for step in payload.get("routePlan") or []:
        swap_info = step.get("swapInfo") or {}
        label = str(swap_info.get("label") or "").strip()
        if label and label not in labels:
            labels.append(label)
    return labels


async def build_jupiter_dex_quote(
    session: aiohttp.ClientSession,
    *,
    symbol: str,
    mint: str,
    decimals: Optional[int],
    notional_usd: float,
    usd_hint: Optional[float] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    mint_clean = str(mint or "").strip()
    if not mint_clean:
        return None
    if decimals is None or decimals < 0:
        return None

    price_hint = _to_float(usd_hint)
    if price_hint is None or price_hint <= 0:
        price_data = await fetch_jupiter_price(session, mint_clean)
        if price_data:
            price_hint = _to_float(price_data.get("usdPrice"))
            if metadata is not None and not metadata.get("price"):
                metadata["price"] = price_hint
    if price_hint is None or price_hint <= 0:
        return None

    if mint_clean == JUPITER_USDC_MINT:
        out_usd = max(0.0, float(notional_usd))
        token_amount = out_usd
        route_labels = ["USDC"]
        price_exec = 1.0
        price_impact_pct = 0.0
    else:
        min_token_amount = 1.0 / (10 ** int(decimals))
        token_amount = max(float(notional_usd) / float(price_hint), min_token_amount)
        amount_atomic = max(1, int(round(token_amount * (10 ** int(decimals)))))
        payload = await fetch_jupiter_quote(
            session,
            input_mint=mint_clean,
            output_mint=JUPITER_USDC_MINT,
            amount_atomic=amount_atomic,
            slippage_bps=50,
        )
        if not payload:
            return None
        out_amount_atomic = int(payload.get("outAmount") or 0)
        out_usd = out_amount_atomic / (10 ** 6)
        if out_usd <= 0:
            return None
        quoted_tokens = amount_atomic / float(10 ** int(decimals))
        if quoted_tokens <= 0:
            return None
        token_amount = quoted_tokens
        price_exec = out_usd / quoted_tokens
        price_impact_pct = (float(_to_float(payload.get("priceImpactPct")) or 0.0)) * 100.0
        route_labels = _jupiter_route_labels(payload)

    info = metadata or {}
    liquidity_usd = max(0.0, _to_float(info.get("liquidity_usd")) or 0.0)
    volume_24h = max(0.0, _to_float(info.get("volume_24h")) or 0.0)
    score = (liquidity_usd * 10.0) + volume_24h + 1_000_000_000.0
    return {
        "asset": str(symbol or "").strip().upper(),
        "chain": "solana",
        "contract": mint_clean,
        "contract_source": "jupiter",
        "price": float(price_exec),
        "liquidity_usd": float(liquidity_usd),
        "volume_24h": float(volume_24h),
        "dex_id": "jupiter",
        "pair_address": mint_clean,
        "url": f"https://jup.ag/tokens/{mint_clean}",
        "swap_url": f"https://jup.ag/swap/USDC-{mint_clean}",
        "label": "Jupiter (solana)",
        "score": float(score),
        "quote_source": "jupiter",
        "quote_mode": "live",
        "decimals": int(decimals),
        "verified": bool(info.get("verified")),
        "reference_price_usd": float(price_hint),
        "quote_amount_tokens": float(token_amount),
        "quote_amount_out_usd": float(out_usd),
        "price_impact_pct": float(price_impact_pct),
        "route_labels": list(route_labels),
    }


async def fetch_relay_chains(session: aiohttp.ClientSession) -> List[Dict[str, Any]]:
    cached = _relay_chains_cache.get("data")
    if cached is not None and (_now() - float(_relay_chains_cache.get("ts", 0.0) or 0.0) < _RELAY_CHAINS_TTL):
        return list(cached)
    payload = await _fetch_json(session, "GET", f"{RELAY_API_BASE}/chains", timeout_sec=15.0)
    chains = payload.get("chains") if isinstance(payload, dict) else None
    if not isinstance(chains, list):
        return []
    _relay_chains_cache["ts"] = _now()
    _relay_chains_cache["data"] = list(chains)
    return list(chains)


def _relay_chain_aliases(chain: str) -> Sequence[str]:
    canon = canon_chain_name(chain)
    aliases = {
        "ethereum": ("ethereum",),
        "optimistic-ethereum": ("optimism", "optimistic-ethereum"),
        "polygon-pos": ("polygon", "polygon-pos"),
        "arbitrum-one": ("arbitrum", "arbitrum-one"),
        "base": ("base",),
        "solana": ("solana",),
        "avalanche": ("avalanche",),
        "binance-smart-chain": ("bsc", "binance-smart-chain"),
        "mantle": ("mantle",),
        "linea": ("linea",),
        "scroll": ("scroll",),
        "tron": ("tron",),
        "sui": ("sui",),
        "sonic": ("sonic",),
    }
    return aliases.get(canon, (canon,))


def get_relay_chain_info(chains: Sequence[Dict[str, Any]], chain: str) -> Optional[Dict[str, Any]]:
    aliases = {str(item).strip().lower() for item in _relay_chain_aliases(chain)}
    for item in chains:
        name = str(item.get("name") or "").strip().lower()
        if name in aliases:
            return item
    return None


def _asset_aliases(asset: str) -> Sequence[str]:
    asset_u = str(asset or "").strip().upper()
    mapping = {
        "ETH": ("ETH", "WETH"),
        "WETH": ("WETH", "ETH"),
        "MATIC": ("MATIC", "POL", "WPOL"),
        "POL": ("POL", "MATIC", "WPOL"),
        "AVAX": ("AVAX", "WAVAX"),
        "WAVAX": ("WAVAX", "AVAX"),
        "BNB": ("BNB", "WBNB"),
        "WBNB": ("WBNB", "BNB"),
        "USDC": ("USDC",),
        "USDT": ("USDT",),
        "SOL": ("SOL",),
    }
    return mapping.get(asset_u, (asset_u,))


async def fetch_mayan_init(session: aiohttp.ClientSession) -> List[Dict[str, Any]]:
    cached = _mayan_init_cache.get("data")
    if cached is not None and (_now() - float(_mayan_init_cache.get("ts", 0.0) or 0.0) < _MAYAN_INIT_TTL):
        return list(cached)

    payload = await _fetch_json(session, "GET", f"{MAYAN_SIA_API_BASE}/v10/init", timeout_sec=15.0)
    chains = payload.get("chains") if isinstance(payload, dict) else None
    if not isinstance(chains, list):
        return []

    _mayan_init_cache["ts"] = _now()
    _mayan_init_cache["data"] = list(chains)
    return list(chains)


def _mayan_chain_aliases(chain: str) -> Sequence[str]:
    canon = canon_chain_name(chain)
    aliases = {
        "ethereum": ("ethereum",),
        "solana": ("solana",),
        "base": ("base",),
        "arbitrum-one": ("arbitrum", "arbitrum-one"),
        "optimistic-ethereum": ("optimism", "optimistic-ethereum"),
        "polygon-pos": ("polygon", "polygon-pos"),
        "binance-smart-chain": ("bsc", "binance-smart-chain"),
        "avalanche": ("avalanche",),
        "linea": ("linea",),
        "sui": ("sui",),
        "aptos": ("aptos",),
        "unichain": ("unichain",),
        "monad": ("monad",),
        "hyperevm": ("hyperevm",),
        "hypercore": ("hypercore",),
        "fogo": ("fogo",),
        "sonic": ("sonic",),
    }
    return aliases.get(canon, (canon,))


def get_mayan_chain_info(chains: Sequence[Dict[str, Any]], chain: str) -> Optional[Dict[str, Any]]:
    aliases = {str(item).strip().lower() for item in _mayan_chain_aliases(chain)}
    for item in chains:
        name_id = str(item.get("nameId") or item.get("chainName") or "").strip().lower()
        if name_id in aliases or canon_chain_name(name_id) == canon_chain_name(chain):
            return item
    return None


def _find_mayan_chain_by_origin_id(chains: Sequence[Dict[str, Any]], origin_id: Any) -> Optional[Dict[str, Any]]:
    origin_str = str(origin_id or "").strip()
    if not origin_str:
        return None
    for key_name in ("wChainId", "chainId"):
        for item in chains:
            if str(item.get(key_name) or "").strip() == origin_str:
                return item
    return None


def _mayan_preferred_address(token: Dict[str, Any], chain: str) -> Optional[str]:
    chain_canon = canon_chain_name(chain)
    contract = str(token.get("contract") or "").strip()
    wrapped_address = str(token.get("wrappedAddress") or "").strip()
    mint = str(token.get("mint") or "").strip()

    if chain_canon == "solana":
        return mint or wrapped_address or (contract if not _is_zero_address(contract) else None)
    if not _is_zero_address(contract):
        return contract
    if wrapped_address and not _is_zero_address(wrapped_address):
        return wrapped_address
    return mint or None


def _mayan_origin_contract(token: Dict[str, Any], chain: str) -> Optional[str]:
    origin_contract = str(token.get("realOriginContractAddress") or "").strip()
    if origin_contract and not _is_zero_address(origin_contract):
        return origin_contract
    return _mayan_preferred_address(token, chain)


def _mayan_registry_family_key(token: Dict[str, Any]) -> Optional[str]:
    identity_key = str(token.get("identity_key") or "").strip().lower()
    if identity_key:
        return f"identity:{identity_key}"

    origin_chain = canon_chain_name(str(token.get("origin_chain") or "").strip())
    origin_contract_key = _address_key(token.get("origin_contract") or token.get("origin_contract_key"))
    if origin_chain and origin_chain not in {"", "unknown"} and origin_contract_key:
        return f"origin:{origin_chain}:{origin_contract_key}"

    coingecko_id = str(token.get("coingecko_id") or "").strip().lower()
    if coingecko_id:
        return f"cg:{coingecko_id}"

    symbol = str(token.get("symbol") or "").strip().upper()
    name_key = str(token.get("name_key") or _text_key(token.get("name"))).strip().lower()
    if token.get("verified") and symbol and name_key:
        return f"name:{symbol}:{name_key}"
    if token.get("verified") and symbol:
        return f"symbol:{symbol}"
    return None


def get_layerzero_api_key() -> Optional[str]:
    for env_name in (
        "LAYERZERO_API_KEY",
        "LAYERZERO_TRANSFER_API_KEY",
        "STARGATE_API_KEY",
    ):
        value = str(os.getenv(env_name) or "").strip()
        if value:
            return value
    return None


def layerzero_chain_key(chain: str) -> Optional[str]:
    canon = canon_chain_name(chain)
    mapping = {
        "ethereum": "ethereum",
        "solana": "solana",
        "base": "base",
        "arbitrum-one": "arbitrum",
        "optimistic-ethereum": "optimism",
        "polygon-pos": "polygon",
        "binance-smart-chain": "bsc",
        "avalanche": "avalanche",
        "linea": "linea",
        "sui": "sui",
        "sonic": "sonic",
    }
    return mapping.get(canon)


async def fetch_mayan_token_registry(session: aiohttp.ClientSession) -> List[Dict[str, Any]]:
    cached = _mayan_tokens_cache.get("data")
    if cached is not None and (_now() - float(_mayan_tokens_cache.get("ts", 0.0) or 0.0) < _MAYAN_TOKENS_TTL):
        return list(cached)

    payload = await _fetch_json(session, "GET", f"{MAYAN_PRICE_API_BASE}/tokens", timeout_sec=18.0)
    if not isinstance(payload, dict):
        return []

    chains = await fetch_mayan_init(session)
    registry: List[Dict[str, Any]] = []
    for chain_key, rows in payload.items():
        if not isinstance(rows, list):
            continue
        chain_info = get_mayan_chain_info(chains, chain_key)
        chain_name = str((chain_info or {}).get("nameId") or chain_key).strip()
        chain_canon = canon_chain_name(chain_name)
        for token in rows:
            if not isinstance(token, dict):
                continue
            address = _mayan_preferred_address(token, chain_canon)
            if not address:
                continue
            origin_chain_info = _find_mayan_chain_by_origin_id(chains, token.get("realOriginChainId"))
            origin_chain = canon_chain_name(
                (origin_chain_info or {}).get("nameId")
                or (origin_chain_info or {}).get("chainName")
                or chain_canon
            )
            origin_contract = _mayan_origin_contract(token, chain_canon)
            identity_key = (
                f"{origin_chain}:{_address_key(origin_contract)}"
                if origin_chain and origin_contract
                else None
            )
            registry.append(
                {
                    "symbol": str(token.get("symbol") or "").strip().upper(),
                    "name": str(token.get("name") or "").strip(),
                    "name_key": _text_key(token.get("name")),
                    "chain": chain_canon,
                    "mayan_chain_name": chain_name.lower(),
                    "contract": address,
                    "address": address,
                    "raw_contract": str(token.get("contract") or "").strip(),
                    "mint": str(token.get("mint") or "").strip(),
                    "wrapped_address": str(token.get("wrappedAddress") or "").strip(),
                    "decimals": int(token.get("decimals") or 0) if token.get("decimals") is not None else 0,
                    "verified": bool(token.get("verified")),
                    "coingecko_id": str(token.get("coingeckoId") or "").strip() or None,
                    "origin_chain": origin_chain,
                    "origin_contract": origin_contract,
                    "identity_key": identity_key,
                    "contract_key": _address_key(address),
                    "origin_contract_key": _address_key(origin_contract),
                    "mayan_chain_id": token.get("chainId"),
                    "mayan_w_chain_id": token.get("wChainId"),
                    "real_origin_chain_id": token.get("realOriginChainId"),
                    "supports_permit": bool(token.get("supportsPermit")),
                    "has_auction": bool(token.get("hasAuction")),
                    "source": "mayan",
                }
            )

    _mayan_tokens_cache["ts"] = _now()
    _mayan_tokens_cache["data"] = list(registry)
    return list(registry)


async def resolve_mayan_asset_tokens(
    session: aiohttp.ClientSession,
    asset: str,
    *,
    preferred_contracts: Optional[Sequence[str]] = None,
) -> List[Dict[str, Any]]:
    asset_u = str(asset or "").strip().upper()
    if not asset_u:
        return []

    registry = await fetch_mayan_token_registry(session)
    aliases = {str(symbol).strip().upper() for symbol in _asset_aliases(asset_u)}
    preferred_keys = {_address_key(item) for item in (preferred_contracts or []) if str(item or "").strip()}
    best_by_chain: Dict[str, Dict[str, Any]] = {}
    family_groups: Dict[str, List[Dict[str, Any]]] = {}

    for token in registry:
        symbol = str(token.get("symbol") or "").strip().upper()
        if symbol not in aliases:
            continue

        score = 0
        if symbol == asset_u:
            score += 400
        elif symbol in aliases:
            score += 250
        if token.get("verified"):
            score += 150
        if token.get("has_auction"):
            score += 30
        if token.get("coingecko_id"):
            score += 20
        if preferred_keys and (
            token.get("contract_key") in preferred_keys
            or token.get("origin_contract_key") in preferred_keys
        ):
            score += 600
        if token.get("identity_key"):
            score += 180
        elif token.get("origin_contract_key"):
            score += 120

        candidate = dict(token)
        candidate["match_score"] = score
        candidate["family_key"] = _mayan_registry_family_key(candidate)
        family_key = str(candidate.get("family_key") or "").strip()
        if not family_key:
            family_key = f"fallback:{candidate.get('chain')}:{candidate.get('contract_key')}"
        family_groups.setdefault(family_key, []).append(candidate)

    selected_family: List[Dict[str, Any]] = []
    if family_groups:
        def family_rank(items: List[Dict[str, Any]]) -> tuple:
            preferred_hits = 0
            identity_hits = 0
            coingecko_hits = 0
            verified_hits = 0
            exact_symbol_hits = 0
            total_score = 0
            chains = set()
            for item in items:
                chains.add(str(item.get("chain") or "").strip().lower())
                total_score += int(item.get("match_score") or 0)
                if item.get("verified"):
                    verified_hits += 1
                if str(item.get("symbol") or "").strip().upper() == asset_u:
                    exact_symbol_hits += 1
                if item.get("coingecko_id"):
                    coingecko_hits += 1
                family_key = str(item.get("family_key") or "")
                if family_key.startswith(("identity:", "origin:")):
                    identity_hits += 1
                if preferred_keys and (
                    item.get("contract_key") in preferred_keys
                    or item.get("origin_contract_key") in preferred_keys
                ):
                    preferred_hits += 1
            return (
                preferred_hits,
                identity_hits,
                coingecko_hits,
                verified_hits,
                len(chains),
                exact_symbol_hits,
                total_score,
            )

        selected_family = max(family_groups.values(), key=family_rank)

    for candidate in (selected_family or [item for group in family_groups.values() for item in group]):
        current = best_by_chain.get(candidate["chain"])
        if current is None or int(candidate["match_score"]) > int(current.get("match_score", -1)):
            best_by_chain[candidate["chain"]] = candidate

    return list(best_by_chain.values())


async def get_mayan_supported_token(
    session: aiohttp.ClientSession,
    chain: str,
    asset: str,
    *,
    contract: Optional[str] = None,
    preferred_symbols: Optional[Sequence[str]] = None,
) -> Optional[Dict[str, Any]]:
    chain_canon = canon_chain_name(chain)
    contract_key = _address_key(contract)
    aliases = {
        str(symbol).strip().upper()
        for symbol in (preferred_symbols or _asset_aliases(asset))
        if str(symbol or "").strip()
    }
    best: Optional[Dict[str, Any]] = None

    for token in await fetch_mayan_token_registry(session):
        if token.get("chain") != chain_canon:
            continue
        score = 0
        if contract_key and (
            token.get("contract_key") == contract_key
            or token.get("origin_contract_key") == contract_key
            or _address_key(token.get("mint")) == contract_key
            or _address_key(token.get("wrapped_address")) == contract_key
        ):
            score += 900
        token_symbol = str(token.get("symbol") or "").strip().upper()
        if token_symbol in aliases:
            score += 300
        if token_symbol == str(asset or "").strip().upper():
            score += 100
        if token.get("verified"):
            score += 80
        if token.get("has_auction"):
            score += 25

        if score <= 0:
            continue
        candidate = dict(token)
        candidate["match_score"] = score
        if best is None or int(candidate["match_score"]) > int(best.get("match_score", -1)):
            best = candidate

    return dict(best) if best else None


async def fetch_mayan_quote(
    session: aiohttp.ClientSession,
    *,
    source_chain: str,
    dest_chain: str,
    source_token: Dict[str, Any],
    dest_token: Dict[str, Any],
    amount_atomic: int,
    slippage_bps: int = 100,
) -> Optional[Dict[str, Any]]:
    if amount_atomic <= 0:
        return None

    chains = await fetch_mayan_init(session)
    source_info = get_mayan_chain_info(chains, source_chain)
    dest_info = get_mayan_chain_info(chains, dest_chain)
    if not source_info or not dest_info:
        return None
    if not bool(source_info.get("originActive")) or not bool(dest_info.get("destinationActive")):
        return None

    source_address = str(source_token.get("address") or source_token.get("contract") or "").strip()
    dest_address = str(dest_token.get("address") or dest_token.get("contract") or "").strip()
    if not source_address or not dest_address:
        return None

    destination_placeholder = (
        SOLANA_PLACEHOLDER_ADDRESS
        if canon_chain_name(dest_chain) == "solana"
        else EVM_PLACEHOLDER_ADDRESS
    )
    params = {
        "solanaProgram": "FC4eXxkyrMPTjiYUpp4EAnkmwMbQyZ6NDCh1kfLn6vsf",
        "forwarderAddress": "0x337685fdaB40D39bd02028545a4FfA7D287cC3E2",
        "amountIn64": str(int(amount_atomic)),
        "fromToken": source_address,
        "fromChain": str(source_info.get("nameId") or "").strip().lower(),
        "toToken": dest_address,
        "toChain": str(dest_info.get("nameId") or "").strip().lower(),
        "slippageBps": str(int(slippage_bps)),
        "gasDrop": "0",
        "gasless": "true",
        "swift": "true",
        "mctp": "true",
        "onlyDirect": "false",
        "destinationAddress": destination_placeholder,
        "sdkVersion": MAYAN_SDK_VERSION,
    }
    cache_key = (
        f"{params['fromChain']}:{params['toChain']}:"
        f"{_address_key(params['fromToken'])}:{_address_key(params['toToken'])}:{params['amountIn64']}:{params['slippageBps']}"
    )
    cached = _cache_get(_mayan_quote_cache, cache_key, _MAYAN_QUOTE_TTL)
    if cached is not None:
        return cached if isinstance(cached, dict) else None

    data = await _fetch_json(
        session,
        "GET",
        f"{MAYAN_PRICE_API_BASE}/quote",
        params=params,
        timeout_sec=10.0,
    )
    quotes = data.get("quotes") if isinstance(data, dict) else None
    if not isinstance(quotes, list) or not quotes:
        return None

    def mayan_quote_sort_key(item: Dict[str, Any]) -> Any:
        return (
            1 if item.get("recommended") else 0,
            _to_float(item.get("expectedAmountOut")) or 0.0,
            -(_to_float(item.get("protocolFeeUsd")) or 0.0),
        )

    best = max((item for item in quotes if isinstance(item, dict)), key=mayan_quote_sort_key, default=None)
    if not isinstance(best, dict):
        return None

    source_decimals = int(((best.get("fromToken") or {}).get("decimals")) or source_token.get("decimals") or 0)
    dest_decimals = int(((best.get("toToken") or {}).get("decimals")) or dest_token.get("decimals") or 0)
    if source_decimals <= 0 or dest_decimals <= 0:
        return None

    amount_in_atomic = int(best.get("effectiveAmountIn64") or amount_atomic)
    amount_out_atomic = int(
        best.get("expectedAmountOutBaseUnits")
        or round(float(_to_float(best.get("expectedAmountOut")) or 0.0) * (10 ** dest_decimals))
        or 0
    )
    minimum_out_atomic = int(
        best.get("minAmountOutBaseUnits")
        or best.get("minReceivedBaseUnits")
        or round(float(_to_float(best.get("minAmountOut")) or 0.0) * (10 ** dest_decimals))
        or 0
    )
    if amount_out_atomic <= 0:
        return None

    effective_amount_in = float(_to_float(best.get("effectiveAmountIn")) or 0.0)
    expected_amount_out = float(_to_float(best.get("expectedAmountOut")) or 0.0)
    from_token_price = _to_float(best.get("fromTokenPrice"))
    to_token_price = _to_float(best.get("toTokenPrice"))
    price_ratio = _to_float(best.get("price"))
    amount_in_usd = (
        (effective_amount_in * float(from_token_price))
        if from_token_price and effective_amount_in > 0
        else None
    )
    amount_out_usd = (
        (expected_amount_out * float(to_token_price))
        if to_token_price and expected_amount_out > 0
        else None
    )
    if amount_in_usd is None and amount_out_usd is not None and price_ratio and price_ratio > 0:
        amount_in_usd = float(amount_out_usd) / float(price_ratio)
    if amount_out_usd is None and amount_in_usd is not None and price_ratio and price_ratio > 0:
        amount_out_usd = float(amount_in_usd) * float(price_ratio)
    if amount_in_usd is None and str((best.get("fromToken") or {}).get("symbol") or "").strip().upper() in {"USDC", "USDT", "DAI"}:
        amount_in_usd = effective_amount_in
    if amount_out_usd is None and str((best.get("toToken") or {}).get("symbol") or "").strip().upper() in {"USDC", "USDT", "DAI"}:
        amount_out_usd = expected_amount_out

    protocol_parts: List[str] = []
    for raw_value in (
        best.get("type"),
        best.get("swiftVersion"),
        ((best.get("relayer") or {}).get("name")),
    ):
        item = str(raw_value or "").strip()
        if item and item not in protocol_parts:
            protocol_parts.append(item)

    explicit_fee_usd = (_to_float(best.get("protocolFeeUsd")) or 0.0) + (_to_float(best.get("referrerFeeUsd")) or 0.0)
    implied_fee_usd = (
        max(0.0, float(amount_in_usd or 0.0) - float(amount_out_usd or 0.0))
        if amount_in_usd is not None and amount_out_usd is not None
        else 0.0
    )
    relayer_fee_usd = max(explicit_fee_usd, implied_fee_usd)

    quote = {
        "provider_id": "mayan",
        "provider_name": "Mayan",
        "docs_url": "https://docs.mayan.finance/",
        "source_chain": canon_chain_name(source_chain),
        "dest_chain": canon_chain_name(dest_chain),
        "source_token": dict(source_token),
        "dest_token": dict(dest_token),
        "amount_in_atomic": amount_in_atomic,
        "amount_out_atomic": amount_out_atomic,
        "amount_in_usd": amount_in_usd,
        "amount_out_usd": amount_out_usd,
        "minimum_out_atomic": minimum_out_atomic,
        "wallet_gas_usd": 0.0,
        "relayer_fee_usd": relayer_fee_usd,
        "time_estimate_sec": int(_to_float(best.get("etaSeconds")) or _to_float(best.get("eta")) or 0),
        "rate": price_ratio,
        "protocol": protocol_parts,
        "router": str(((best.get("relayer") or {}).get("name")) or best.get("type") or "mayan").strip() or "mayan",
        "quote_id": str(best.get("quoteId") or "").strip() or None,
        "fee_breakdown": {
            "protocol_fee_usd": _to_float(best.get("protocolFeeUsd")) or 0.0,
            "referrer_fee_usd": _to_float(best.get("referrerFeeUsd")) or 0.0,
            "bridge_fee": _to_float(best.get("bridgeFee")) or 0.0,
        },
    }
    return _cache_set(_mayan_quote_cache, cache_key, quote, max_size=256)


async def fetch_mayan_rebalance_quote(
    session: aiohttp.ClientSession,
    *,
    source_chain: str,
    dest_chain: str,
    notional_usd: float,
) -> Optional[Dict[str, Any]]:
    if notional_usd <= 0:
        return None

    source_token = await get_mayan_supported_token(session, source_chain, "USDC")
    dest_token = await get_mayan_supported_token(session, dest_chain, "USDC")
    if not source_token or not dest_token:
        source_token = await get_mayan_supported_token(session, source_chain, "USDT")
        dest_token = await get_mayan_supported_token(session, dest_chain, "USDT")
    if not source_token or not dest_token:
        return None

    decimals = int(source_token.get("decimals") or 6)
    amount_atomic = max(1, int(round(float(notional_usd) * (10 ** decimals))))
    return await fetch_mayan_quote(
        session,
        source_chain=source_chain,
        dest_chain=dest_chain,
        source_token=source_token,
        dest_token=dest_token,
        amount_atomic=amount_atomic,
    )


async def fetch_wormhole_quote(
    session: aiohttp.ClientSession,
    *,
    source_chain: str,
    dest_chain: str,
    source_token: Dict[str, Any],
    dest_token: Dict[str, Any],
    amount_atomic: int,
) -> Optional[Dict[str, Any]]:
    if amount_atomic <= 0:
        return None

    source_chain_canon = canon_chain_name(source_chain)
    dest_chain_canon = canon_chain_name(dest_chain)
    source_symbol = str(source_token.get("symbol") or "").strip().upper()
    dest_symbol = str(dest_token.get("symbol") or "").strip().upper()
    source_wh_chain = wormhole_chain_name(source_chain_canon)
    dest_wh_chain = wormhole_chain_name(dest_chain_canon)
    if (
        source_symbol != "USDC"
        or dest_symbol != "USDC"
        or not source_wh_chain
        or not dest_wh_chain
        or not _wormhole_source_token_matches(source_chain_canon, source_token)
        or not _wormhole_source_token_matches(dest_chain_canon, dest_token)
    ):
        return None

    source_chain_id = int(_WORMHOLE_CHAIN_IDS.get(source_wh_chain) or 0)
    dest_chain_id = int(_WORMHOLE_CHAIN_IDS.get(dest_wh_chain) or 0)
    source_domain = _WORMHOLE_CIRCLE_V2_DOMAINS.get(source_chain_canon)
    dest_domain = _WORMHOLE_CIRCLE_V2_DOMAINS.get(dest_chain_canon)
    relay_instructions = _wormhole_relay_instructions_hex(dest_chain_canon)
    if not source_chain_id or not dest_chain_id or source_domain is None or dest_domain is None or not relay_instructions:
        return None

    cache_key = (
        f"{source_chain_canon}:{dest_chain_canon}:{int(amount_atomic)}:"
        f"{_address_key(source_token.get('address') or source_token.get('contract'))}:"
        f"{_address_key(dest_token.get('address') or dest_token.get('contract'))}"
    )
    cached = _cache_get(_wormhole_quote_cache, cache_key, _WORMHOLE_QUOTE_TTL)
    if cached is not None:
        return cached if isinstance(cached, dict) else None

    quote_payload = await _fetch_json(
        session,
        "POST",
        f"{WORMHOLE_EXECUTOR_API_BASE}/v0/quote",
        json_payload={
            "srcChain": source_chain_id,
            "dstChain": dest_chain_id,
            "relayInstructions": relay_instructions,
        },
        timeout_sec=10.0,
    )
    if not isinstance(quote_payload, dict):
        return None

    signed_quote = _decode_wormhole_signed_quote(quote_payload.get("signedQuote"))
    estimated_cost_atomic = int(quote_payload.get("estimatedCost") or 0)
    if not signed_quote or estimated_cost_atomic < 0:
        return None

    estimated_cost_usd = _wormhole_native_fee_usd(
        source_chain_canon,
        estimated_cost_atomic,
        int(signed_quote.get("src_price") or 0),
    )
    amount_in_usd = float(amount_atomic) / 1_000_000.0
    variants: List[Dict[str, Any]] = []

    standard_supported = (
        source_chain_canon in _WORMHOLE_CIRCLE_V2_DOMAINS
        and dest_chain_canon in _WORMHOLE_CIRCLE_V2_DOMAINS
        and source_wh_chain != "Linea"
    )
    if standard_supported:
        variants.append(
            {
                "variant": "standard",
                "amount_out_atomic": int(amount_atomic),
                "eta_sec": int(_WORMHOLE_STANDARD_ETA_SEC.get(source_wh_chain) or 0),
                "token_fee_atomic": 0,
                "fast_fee_bps": 0.0,
            }
        )

    fast_supported = (
        source_wh_chain in _WORMHOLE_FAST_ETA_SEC
        and source_chain_canon in _WORMHOLE_CIRCLE_V2_DOMAINS
        and dest_chain_canon in _WORMHOLE_CIRCLE_V2_DOMAINS
    )
    if fast_supported:
        allowance_payload = await _fetch_json(
            session,
            "GET",
            f"{WORMHOLE_CIRCLE_V2_API_BASE}/fastBurn/USDC/allowance",
            timeout_sec=10.0,
        )
        fees_payload = await _fetch_json(
            session,
            "GET",
            f"{WORMHOLE_CIRCLE_V2_API_BASE}/burn/USDC/fees/{int(source_domain)}/{int(dest_domain)}",
            timeout_sec=10.0,
        )
        allowance_value = _to_float((allowance_payload or {}).get("allowance")) or 0.0
        allowance_atomic = int(round(float(allowance_value) * 1_000_000))
        if allowance_atomic > int(amount_atomic) and isinstance(fees_payload, list):
            confirmed_tier = next(
                (
                    item for item in fees_payload
                    if isinstance(item, dict) and int(item.get("finalityThreshold") or 0) == 1000
                ),
                None,
            )
            fast_fee_bps = _to_float((confirmed_tier or {}).get("minimumFee")) or 0.0
            fast_fee_atomic = _wormhole_fast_fee_atomic(int(amount_atomic), fast_fee_bps)
            fast_out_atomic = max(0, int(amount_atomic) - int(fast_fee_atomic))
            if fast_out_atomic > 0:
                variants.append(
                    {
                        "variant": "fast",
                        "amount_out_atomic": fast_out_atomic,
                        "eta_sec": int(_WORMHOLE_FAST_ETA_SEC.get(source_wh_chain) or 0),
                        "token_fee_atomic": int(fast_fee_atomic),
                        "fast_fee_bps": float(fast_fee_bps),
                    }
                )

    if not variants:
        return None

    variants.sort(key=lambda item: (1 if item.get("variant") == "fast" else 0, int(item.get("amount_out_atomic") or 0)), reverse=True)
    best = variants[0]
    amount_out_atomic = int(best.get("amount_out_atomic") or 0)
    token_fee_atomic = int(best.get("token_fee_atomic") or 0)
    if amount_out_atomic <= 0:
        return None

    amount_out_usd = float(amount_out_atomic) / 1_000_000.0
    token_fee_usd = float(token_fee_atomic) / 1_000_000.0
    total_bridge_fee_usd = token_fee_usd + float(estimated_cost_usd)

    quote = {
        "provider_id": "wormhole",
        "provider_name": "Wormhole",
        "docs_url": "https://wormhole.com/docs/products/connect/concepts/routes/",
        "source_chain": source_chain_canon,
        "dest_chain": dest_chain_canon,
        "source_token": dict(source_token),
        "dest_token": dict(dest_token),
        "amount_in_atomic": int(amount_atomic),
        "amount_out_atomic": amount_out_atomic,
        "amount_in_usd": amount_in_usd,
        "amount_out_usd": amount_out_usd,
        "minimum_out_atomic": amount_out_atomic,
        "wallet_gas_usd": float(estimated_cost_usd),
        "relayer_fee_usd": total_bridge_fee_usd,
        "time_estimate_sec": int(best.get("eta_sec") or 0),
        "rate": (amount_out_usd / amount_in_usd) if amount_in_usd > 0 else None,
        "protocol": ["CCTPv2", str(best.get("variant") or "standard").upper()],
        "router": "wormhole-cctp",
        "route_variant": str(best.get("variant") or "standard"),
        "fee_breakdown": {
            "token_fee_atomic": token_fee_atomic,
            "token_fee_usd": token_fee_usd,
            "native_quote_fee_atomic": estimated_cost_atomic,
            "native_quote_fee_usd": float(estimated_cost_usd),
            "fast_fee_bps": float(best.get("fast_fee_bps") or 0.0),
            "src_price_scaled": int(signed_quote.get("src_price") or 0),
            "dst_price_scaled": int(signed_quote.get("dst_price") or 0),
            "base_fee_atomic": int(signed_quote.get("base_fee") or 0),
            "dst_gas_price": int(signed_quote.get("dst_gas_price") or 0),
            "expiry_time": int(signed_quote.get("expiry_time") or 0),
            "relay_instructions": relay_instructions,
        },
    }
    return _cache_set(_wormhole_quote_cache, cache_key, quote, max_size=256)


async def fetch_wormhole_rebalance_quote(
    session: aiohttp.ClientSession,
    *,
    source_chain: str,
    dest_chain: str,
    notional_usd: float,
) -> Optional[Dict[str, Any]]:
    if notional_usd <= 0:
        return None

    source_contract = _wormhole_usdc_contract(source_chain)
    dest_contract = _wormhole_usdc_contract(dest_chain)
    if not source_contract or not dest_contract:
        return None

    amount_atomic = max(1, int(round(float(notional_usd) * 1_000_000)))
    return await fetch_wormhole_quote(
        session,
        source_chain=source_chain,
        dest_chain=dest_chain,
        source_token={
            "address": source_contract,
            "contract": source_contract,
            "decimals": 6,
            "symbol": "USDC",
        },
        dest_token={
            "address": dest_contract,
            "contract": dest_contract,
            "decimals": 6,
            "symbol": "USDC",
        },
        amount_atomic=amount_atomic,
    )


async def fetch_layerzero_quote(
    session: aiohttp.ClientSession,
    *,
    source_chain: str,
    dest_chain: str,
    source_token: Dict[str, Any],
    dest_token: Dict[str, Any],
    amount_atomic: int,
    api_key: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    if amount_atomic <= 0:
        return None

    auth_key = str(api_key or get_layerzero_api_key() or "").strip()
    if not auth_key:
        return None

    src_chain_key = layerzero_chain_key(source_chain)
    dst_chain_key = layerzero_chain_key(dest_chain)
    source_address = str(source_token.get("address") or source_token.get("contract") or "").strip()
    dest_address = str(dest_token.get("address") or dest_token.get("contract") or "").strip()
    if not src_chain_key or not dst_chain_key or not source_address or not dest_address:
        return None

    src_wallet = SOLANA_PLACEHOLDER_ADDRESS if canon_chain_name(source_chain) == "solana" else EVM_PLACEHOLDER_ADDRESS
    dst_wallet = SOLANA_PLACEHOLDER_ADDRESS if canon_chain_name(dest_chain) == "solana" else EVM_PLACEHOLDER_ADDRESS
    payload = {
        "srcChainKey": src_chain_key,
        "dstChainKey": dst_chain_key,
        "srcTokenAddress": source_address,
        "dstTokenAddress": dest_address,
        "srcWalletAddress": src_wallet,
        "dstWalletAddress": dst_wallet,
        "amount": str(int(amount_atomic)),
    }
    cache_key = (
        f"{src_chain_key}:{dst_chain_key}:{_address_key(source_address)}:{_address_key(dest_address)}:{int(amount_atomic)}"
    )
    cached = _cache_get(_layerzero_quote_cache, cache_key, _LAYERZERO_QUOTE_TTL)
    if cached is not None:
        return cached if isinstance(cached, dict) else None

    data = await _fetch_json(
        session,
        "POST",
        f"{LAYERZERO_TRANSFER_API_BASE}/quotes",
        json_payload=payload,
        headers={"x-api-key": auth_key},
        timeout_sec=10.0,
    )
    quotes = data.get("quotes") if isinstance(data, dict) else None
    if not isinstance(quotes, list) or not quotes:
        return None

    def lz_sort_key(item: Dict[str, Any]) -> Any:
        return (
            _to_float(item.get("dstAmount")) or 0.0,
            -(_to_float(item.get("feeUsd")) or 0.0),
        )

    best = max((item for item in quotes if isinstance(item, dict)), key=lz_sort_key, default=None)
    if not isinstance(best, dict):
        return None

    source_decimals = int(source_token.get("decimals") or 0)
    dest_decimals = int(dest_token.get("decimals") or 0)
    if source_decimals <= 0 or dest_decimals <= 0:
        return None

    amount_out_atomic = int(best.get("dstAmount") or 0)
    amount_in_atomic = int(best.get("srcAmount") or amount_atomic)
    minimum_out_atomic = int(best.get("minDstAmount") or amount_out_atomic or 0)
    if amount_out_atomic <= 0:
        return None

    source_symbol = str(source_token.get("symbol") or "").strip().upper()
    dest_symbol = str(dest_token.get("symbol") or "").strip().upper()
    amount_in_tokens = float(amount_in_atomic) / float(10 ** source_decimals)
    amount_out_tokens = float(amount_out_atomic) / float(10 ** dest_decimals)
    amount_in_usd = amount_in_tokens if _stable_asset_symbol(source_symbol) else None
    amount_out_usd = amount_out_tokens if _stable_asset_symbol(dest_symbol) else None
    fee_usd = _to_float(best.get("feeUsd")) or (
        max(0.0, float(amount_in_usd or 0.0) - float(amount_out_usd or 0.0))
        if amount_in_usd is not None and amount_out_usd is not None
        else 0.0
    )

    route_steps = best.get("routeSteps") or []
    protocol: List[str] = []
    for step in route_steps:
        if not isinstance(step, dict):
            continue
        action = str(step.get("action") or "").strip()
        if action and action not in protocol:
            protocol.append(action)

    quote = {
        "provider_id": "layerzero",
        "provider_name": "LayerZero",
        "docs_url": "https://docs.layerzero.network/v2/tools/api/overview",
        "source_chain": canon_chain_name(source_chain),
        "dest_chain": canon_chain_name(dest_chain),
        "source_token": dict(source_token),
        "dest_token": dict(dest_token),
        "amount_in_atomic": amount_in_atomic,
        "amount_out_atomic": amount_out_atomic,
        "amount_in_usd": amount_in_usd,
        "amount_out_usd": amount_out_usd,
        "minimum_out_atomic": minimum_out_atomic,
        "wallet_gas_usd": 0.0,
        "relayer_fee_usd": fee_usd,
        "time_estimate_sec": int(_to_float(best.get("durationSeconds")) or _to_float(best.get("estimatedDurationSec")) or 0),
        "rate": (
            (float(amount_out_usd) / float(amount_in_usd))
            if amount_in_usd and amount_out_usd and amount_in_usd > 0
            else None
        ),
        "protocol": protocol,
        "router": "layerzero",
    }
    return _cache_set(_layerzero_quote_cache, cache_key, quote, max_size=256)


async def fetch_layerzero_rebalance_quote(
    session: aiohttp.ClientSession,
    *,
    source_chain: str,
    dest_chain: str,
    notional_usd: float,
    api_key: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    if notional_usd <= 0:
        return None

    source_token = await get_mayan_supported_token(session, source_chain, "USDC")
    dest_token = await get_mayan_supported_token(session, dest_chain, "USDC")
    if not source_token or not dest_token:
        source_token = await get_mayan_supported_token(session, source_chain, "USDT")
        dest_token = await get_mayan_supported_token(session, dest_chain, "USDT")
    if not source_token or not dest_token:
        return None

    decimals = int(source_token.get("decimals") or 6)
    amount_atomic = max(1, int(round(float(notional_usd) * (10 ** decimals))))
    return await fetch_layerzero_quote(
        session,
        source_chain=source_chain,
        dest_chain=dest_chain,
        source_token=source_token,
        dest_token=dest_token,
        amount_atomic=amount_atomic,
        api_key=api_key,
    )


def _slugify(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(value or "").strip().lower()).strip("-")


async def fetch_geckoterminal_networks(session: aiohttp.ClientSession) -> List[Dict[str, Any]]:
    cached = _geckoterminal_networks_cache.get("data")
    if cached is not None and (_now() - float(_geckoterminal_networks_cache.get("ts", 0.0) or 0.0) < _GECKOTERMINAL_NETWORKS_TTL):
        return list(cached)

    headers = {"Accept": "application/json;version=20230302"}
    networks: List[Dict[str, Any]] = []
    page = 1
    max_pages = 4
    while page <= max_pages:
        payload = await _fetch_json(
            session,
            "GET",
            f"{GECKOTERMINAL_API_BASE}/networks",
            params={"page": page},
            headers=headers,
            timeout_sec=12.0,
        )
        rows = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(rows, list) or not rows:
            break
        networks.extend([row for row in rows if isinstance(row, dict)])
        next_link = ((payload.get("links") or {}).get("next") if isinstance(payload, dict) else None) or ""
        if not str(next_link).strip():
            break
        page += 1

    if not networks:
        return []
    _geckoterminal_networks_cache["ts"] = _now()
    _geckoterminal_networks_cache["data"] = list(networks)
    return list(networks)


def get_geckoterminal_network_id(networks: Sequence[Dict[str, Any]], chain: str) -> Optional[str]:
    canon = canon_chain_name(chain)
    if not canon or canon == "unknown":
        return None

    hints = {canon}
    hints.update(_GECKOTERMINAL_NETWORK_HINTS.get(canon, ()))
    hints.update({_slugify(item).replace("-", "_") for item in hints})
    canon_slug = _slugify(canon)

    for item in networks:
        network_id = str(item.get("id") or "").strip().lower()
        attrs = item.get("attributes") or {}
        platform_id = canon_chain_name(str(attrs.get("coingecko_asset_platform_id") or "").strip())
        if platform_id == canon:
            return network_id

    for item in networks:
        network_id = str(item.get("id") or "").strip().lower()
        attrs = item.get("attributes") or {}
        name_slug = _slugify(attrs.get("name") or "")
        if network_id in hints or name_slug == canon_slug:
            return network_id
    return None


def geckoterminal_chain_from_network_id(networks: Sequence[Dict[str, Any]], network_id: str) -> Optional[str]:
    network_key = str(network_id or "").strip().lower()
    if not network_key:
        return None
    for item in networks:
        item_id = str(item.get("id") or "").strip().lower()
        if item_id != network_key:
            continue
        attrs = item.get("attributes") or {}
        platform_id = canon_chain_name(str(attrs.get("coingecko_asset_platform_id") or "").strip())
        if platform_id and platform_id != "unknown":
            return platform_id
        name = canon_chain_name(str(attrs.get("name") or "").strip())
        if name and name != "unknown":
            return name
    return canon_chain_name(network_key)


def _is_preferred_quote_symbol(symbol: Any) -> bool:
    symbol_u = str(symbol or "").strip().upper()
    return symbol_u in {
        "USDC", "USDT", "DAI", "FDUSD", "TUSD", "USDE", "USD1",
        "SOL", "WSOL", "ETH", "WETH", "BNB", "WBNB", "MATIC", "POL",
        "WPOL", "AVAX", "WAVAX", "SUI", "APT", "TON", "TRX",
    }


async def search_geckoterminal_asset_tokens(
    session: aiohttp.ClientSession,
    asset: str,
    *,
    preferred_contracts: Optional[Sequence[str]] = None,
) -> List[Dict[str, Any]]:
    asset_u = str(asset or "").strip().upper()
    if not asset_u:
        return []

    cache_key = asset_u
    cached = _cache_get(_geckoterminal_search_cache, cache_key, _GECKOTERMINAL_SEARCH_TTL)
    payload = cached if isinstance(cached, dict) else None
    if payload is None:
        payload = await _fetch_json(
            session,
            "GET",
            f"{GECKOTERMINAL_API_BASE}/search/pools",
            params={"query": asset_u, "include": "base_token,quote_token,dex"},
            headers={"Accept": "application/json;version=20230302"},
            timeout_sec=12.0,
        )
        if not isinstance(payload, dict):
            return []
        _cache_set(_geckoterminal_search_cache, cache_key, payload, max_size=128)

    networks = await fetch_geckoterminal_networks(session)
    included_lookup: Dict[tuple[str, str], Dict[str, Any]] = {}
    for item in payload.get("included") or []:
        if not isinstance(item, dict):
            continue
        included_lookup[(str(item.get("type") or "").strip(), str(item.get("id") or "").strip())] = item

    aliases = {str(symbol).strip().upper() for symbol in _asset_aliases(asset_u)}
    preferred_keys = {_address_key(item) for item in (preferred_contracts or []) if str(item or "").strip()}
    best_by_chain: Dict[str, Dict[str, Any]] = {}

    for pool in payload.get("data") or []:
        if not isinstance(pool, dict):
            continue
        pool_id = str(pool.get("id") or "").strip()
        if "_" not in pool_id:
            continue
        network_id = pool_id.split("_", 1)[0]
        chain = geckoterminal_chain_from_network_id(networks, network_id)
        if not chain or chain == "unknown":
            continue

        rel = pool.get("relationships") or {}
        base_ref = ((rel.get("base_token") or {}).get("data") or {})
        quote_ref = ((rel.get("quote_token") or {}).get("data") or {})
        base_item = included_lookup.get((str(base_ref.get("type") or "").strip(), str(base_ref.get("id") or "").strip()))
        quote_item = included_lookup.get((str(quote_ref.get("type") or "").strip(), str(quote_ref.get("id") or "").strip()))
        base_attrs = (base_item or {}).get("attributes") or {}
        quote_attrs = (quote_item or {}).get("attributes") or {}

        symbol = str(base_attrs.get("symbol") or "").strip().upper()
        if symbol not in aliases:
            continue
        contract = str(base_attrs.get("address") or "").strip()
        if not contract:
            continue

        attrs = pool.get("attributes") or {}
        liquidity_usd = max(0.0, _to_float(attrs.get("reserve_in_usd")) or 0.0)
        volume_24h = max(0.0, _to_float((attrs.get("volume_usd") or {}).get("h24")) or 0.0)
        if liquidity_usd <= 0 and volume_24h <= 0:
            continue

        coingecko_id = str(base_attrs.get("coingecko_coin_id") or "").strip() or None
        family_key = f"cg:{coingecko_id.lower()}" if coingecko_id else None
        contract_key = _address_key(contract)
        score = 0.0
        score += 400.0 if symbol == asset_u else 260.0
        score += 120.0 if coingecko_id else 0.0
        score += 80.0 if _is_preferred_quote_symbol(quote_attrs.get("symbol")) else 0.0
        score += 700.0 if contract_key in preferred_keys else 0.0
        score += math.log10(liquidity_usd + 1.0) * 60.0
        score += math.log10(volume_24h + 1.0) * 20.0

        candidate = {
            "symbol": symbol,
            "name": str(base_attrs.get("name") or "").strip(),
            "chain": chain,
            "contract": contract,
            "decimals": int(base_attrs.get("decimals") or 0) if base_attrs.get("decimals") is not None else None,
            "verified": bool(coingecko_id),
            "coingecko_id": coingecko_id,
            "family_key": family_key,
            "identity_key": None,
            "origin_chain": None,
            "origin_contract": None,
            "liquidity_usd": liquidity_usd,
            "volume_24h": volume_24h,
            "match_score": score,
            "source": "geckoterminal_search",
            "network_id": network_id,
        }
        current = best_by_chain.get(chain)
        if current is None or float(candidate["match_score"]) > float(current.get("match_score", -1.0)):
            best_by_chain[chain] = candidate

    return list(best_by_chain.values())




async def search_coingecko_platform_asset_tokens(
    session: aiohttp.ClientSession,
    asset: str,
    *,
    preferred_contracts: Optional[Sequence[str]] = None,
) -> List[Dict[str, Any]]:
    """Find token contracts via CoinGecko /coins/list?include_platform=true.

    This is safer for CEX↔DEX matching than symbol-only DEX screeners because it
    returns CoinGecko IDs plus platform contract maps. It is still only a
    discovery helper: execution/scanner must validate liquidity through live DEX
    quotes before using a candidate.
    """
    asset_u = str(asset or "").strip().upper()
    if not asset_u:
        return []

    now = _now()
    cached = _coingecko_platform_list_cache.get("data")
    if cached is None or (now - float(_coingecko_platform_list_cache.get("ts", 0.0) or 0.0) > 3600):
        payload = await _fetch_json(
            session,
            "GET",
            "https://api.coingecko.com/api/v3/coins/list",
            params={"include_platform": "true"},
            timeout_sec=25.0,
        )
        if not isinstance(payload, list):
            return []
        _coingecko_platform_list_cache["ts"] = now
        _coingecko_platform_list_cache["data"] = payload
        cached = payload

    aliases = {str(symbol).strip().upper() for symbol in _asset_aliases(asset_u)}
    preferred_keys = {_address_key(item) for item in (preferred_contracts or []) if str(item or "").strip()}
    best_by_chain: Dict[str, Dict[str, Any]] = {}
    for item in cached or []:
        if not isinstance(item, dict):
            continue
        symbol = str(item.get("symbol") or "").strip().upper()
        if symbol not in aliases:
            continue
        platforms = item.get("platforms") or {}
        if not isinstance(platforms, dict):
            continue
        for platform, contract in platforms.items():
            contract_s = str(contract or "").strip()
            if not contract_s:
                continue
            chain = canon_chain_name(platform)
            if not chain or chain == "unknown":
                continue
            contract_key = _address_key(contract_s)
            score = 500.0 if symbol == asset_u else 300.0
            score += 800.0 if contract_key in preferred_keys else 0.0
            candidate = {
                "symbol": symbol,
                "name": str(item.get("name") or "").strip(),
                "chain": chain,
                "contract": contract_s,
                "decimals": None,
                "verified": True,
                "coingecko_id": str(item.get("id") or "").strip() or None,
                "family_key": f"cg:{str(item.get('id') or '').strip().lower()}" if item.get("id") else None,
                "identity_key": None,
                "origin_chain": None,
                "origin_contract": None,
                "liquidity_usd": 0.0,
                "volume_24h": 0.0,
                "match_score": score,
                "source": "coingecko_platforms",
            }
            current = best_by_chain.get(chain)
            if current is None or float(candidate["match_score"]) > float(current.get("match_score", -1.0)):
                best_by_chain[chain] = candidate
    return list(best_by_chain.values())

async def search_dexscreener_asset_tokens(
    session: aiohttp.ClientSession,
    asset: str,
    *,
    preferred_contracts: Optional[Sequence[str]] = None,
) -> List[Dict[str, Any]]:
    asset_u = str(asset or "").strip().upper()
    if not asset_u:
        return []

    cache_key = asset_u
    cached = _cache_get(_dexscreener_search_cache, cache_key, _DEXSCREENER_SEARCH_TTL)
    payload = cached if isinstance(cached, dict) else None
    if payload is None:
        payload = await _fetch_json(
            session,
            "GET",
            "https://api.dexscreener.com/latest/dex/search",
            params={"q": asset_u},
            timeout_sec=12.0,
        )
        if not isinstance(payload, dict):
            return []
        _cache_set(_dexscreener_search_cache, cache_key, payload, max_size=128)

    aliases = {str(symbol).strip().upper() for symbol in _asset_aliases(asset_u)}
    preferred_keys = {_address_key(item) for item in (preferred_contracts or []) if str(item or "").strip()}
    best_by_chain: Dict[str, Dict[str, Any]] = {}

    for pair in payload.get("pairs") or []:
        if not isinstance(pair, dict):
            continue
        base_token = pair.get("baseToken") or {}
        symbol = str(base_token.get("symbol") or "").strip().upper()
        if symbol not in aliases:
            continue
        contract = str(base_token.get("address") or "").strip()
        chain = canon_chain_name(pair.get("chainId"))
        if not contract or not chain or chain == "unknown":
            continue
        liquidity_usd = max(0.0, _to_float((pair.get("liquidity") or {}).get("usd")) or 0.0)
        volume_24h = max(0.0, _to_float((pair.get("volume") or {}).get("h24")) or 0.0)
        if liquidity_usd <= 0 and volume_24h <= 0:
            continue
        quote_token = pair.get("quoteToken") or {}
        contract_key = _address_key(contract)
        score = 0.0
        score += 360.0 if symbol == asset_u else 220.0
        score += 60.0 if _is_preferred_quote_symbol(quote_token.get("symbol")) else 0.0
        score += 650.0 if contract_key in preferred_keys else 0.0
        score += math.log10(liquidity_usd + 1.0) * 55.0
        score += math.log10(volume_24h + 1.0) * 18.0

        candidate = {
            "symbol": symbol,
            "name": str(base_token.get("name") or "").strip(),
            "chain": chain,
            "contract": contract,
            "decimals": None,
            "verified": False,
            "coingecko_id": None,
            "family_key": None,
            "identity_key": None,
            "origin_chain": None,
            "origin_contract": None,
            "liquidity_usd": liquidity_usd,
            "volume_24h": volume_24h,
            "match_score": score,
            "source": "dexscreener_search",
        }
        current = best_by_chain.get(chain)
        if current is None or float(candidate["match_score"]) > float(current.get("match_score", -1.0)):
            best_by_chain[chain] = candidate

    return list(best_by_chain.values())


async def discover_symbol_contracts(
    session: aiohttp.ClientSession,
    asset: str,
    *,
    preferred_contracts: Optional[Sequence[str]] = None,
) -> List[Dict[str, Any]]:
    cg_candidates: List[Dict[str, Any]] = []
    gt_candidates: List[Dict[str, Any]] = []
    ds_candidates: List[Dict[str, Any]] = []
    try:
        cg_candidates = await search_coingecko_platform_asset_tokens(
            session,
            asset,
            preferred_contracts=preferred_contracts,
        )
    except Exception:
        cg_candidates = []
    try:
        gt_candidates = await search_geckoterminal_asset_tokens(
            session,
            asset,
            preferred_contracts=preferred_contracts,
        )
    except Exception:
        gt_candidates = []

    # DexScreener is disabled by default. It can be noisy for symbol-only search
    # and may create fake-looking CEX↔DEX opportunities. Enable explicitly only
    # for diagnostics: ENABLE_DEXSCREENER_DISCOVERY=true.
    if str(os.getenv("ENABLE_DEXSCREENER_DISCOVERY", "false")).strip().lower() in {"1", "true", "yes", "on"}:
        try:
            ds_candidates = await search_dexscreener_asset_tokens(
                session,
                asset,
                preferred_contracts=preferred_contracts,
            )
        except Exception:
            ds_candidates = []

    source_rank = {"coingecko_platforms": 0, "geckoterminal_search": 1, "dexscreener_search": 9}
    best_by_chain: Dict[str, Dict[str, Any]] = {}
    for candidate in [*cg_candidates, *gt_candidates, *ds_candidates]:
        chain = str(candidate.get("chain") or "").strip()
        if not chain:
            continue
        current = best_by_chain.get(chain)
        candidate_key = (
            source_rank.get(str(candidate.get("source") or ""), 99),
            -(float(candidate.get("match_score") or 0.0)),
        )
        current_key = (
            source_rank.get(str((current or {}).get("source") or ""), 99),
            -(float((current or {}).get("match_score") or 0.0)),
        ) if current else None
        if current is None or candidate_key < current_key:
            best_by_chain[chain] = candidate
    return list(best_by_chain.values())


async def fetch_geckoterminal_dex_quotes(
    session: aiohttp.ClientSession,
    *,
    asset: str,
    chain: str,
    contract: str,
    contract_source: str,
) -> List[Dict[str, Any]]:
    contract_str = str(contract or "").strip()
    chain_canon = canon_chain_name(chain)
    if not contract_str or not chain_canon:
        return []

    networks = await fetch_geckoterminal_networks(session)
    network_id = get_geckoterminal_network_id(networks, chain_canon)
    if not network_id:
        return []

    cache_key = f"{network_id}:{contract_str.lower()}"
    cached = _cache_get(_geckoterminal_pools_cache, cache_key, _GECKOTERMINAL_POOLS_TTL)
    payload = cached if isinstance(cached, dict) else None
    if payload is None:
        payload = await _fetch_json(
            session,
            "GET",
            f"{GECKOTERMINAL_API_BASE}/networks/{network_id}/tokens/{contract_str}/pools",
            params={"page": 1},
            headers={"Accept": "application/json;version=20230302"},
            timeout_sec=10.0,
        )
        if not isinstance(payload, dict):
            return []
        _cache_set(_geckoterminal_pools_cache, cache_key, payload, max_size=256)

    best_quote: Optional[Dict[str, Any]] = None
    for pool in payload.get("data") or []:
        if not isinstance(pool, dict):
            continue
        attrs = pool.get("attributes") or {}
        price = _to_float(attrs.get("token_price_usd"))
        if price is None or price <= 0:
            continue
        liquidity_usd = max(0.0, _to_float(attrs.get("reserve_in_usd")) or 0.0)
        volume_24h = max(0.0, _to_float((attrs.get("volume_usd") or {}).get("h24")) or 0.0)
        score = (liquidity_usd * 10.0) + volume_24h
        dex_data = (((pool.get("relationships") or {}).get("dex") or {}).get("data") or {})
        dex_id = str(dex_data.get("id") or "geckoterminal").strip().lower() or "geckoterminal"
        pair_address = str(attrs.get("address") or "").strip()
        if not pair_address:
            pool_id = str(pool.get("id") or "").strip()
            pair_address = pool_id.split("_", 1)[-1] if "_" in pool_id else pool_id
        dex_label = dex_id.replace("-", " ").title()
        quote = {
            "asset": str(asset or "").strip().upper(),
            "chain": chain_canon,
            "contract": contract_str,
            "contract_source": contract_source,
            "price": float(price),
            "liquidity_usd": float(liquidity_usd),
            "volume_24h": float(volume_24h),
            "dex_id": dex_id,
            "pair_address": pair_address,
            "url": f"https://www.geckoterminal.com/{network_id}/pools/{pair_address}",
            "swap_url": build_dex_swap_url(chain=chain_canon, contract=contract_str),
            "label": f"{dex_label} ({chain_canon})",
            "score": float(score),
            "quote_source": "geckoterminal",
            "quote_mode": "estimated",
            "route_labels": [],
            "network_id": network_id,
        }
        if best_quote is None or quote["score"] > best_quote["score"]:
            best_quote = quote

    return [best_quote] if best_quote else []


def get_relay_featured_token(
    chains: Sequence[Dict[str, Any]],
    chain: str,
    asset: str,
    *,
    contract: Optional[str] = None,
    preferred_symbols: Optional[Sequence[str]] = None,
) -> Optional[Dict[str, Any]]:
    chain_info = get_relay_chain_info(chains, chain)
    if not chain_info:
        return None

    featured = chain_info.get("featuredTokens") or []
    contract_lc = str(contract or "").strip().lower()
    if contract_lc:
        for token in featured:
            if str(token.get("address") or "").strip().lower() == contract_lc:
                return dict(token)

    aliases = {str(symbol).strip().upper() for symbol in (preferred_symbols or _asset_aliases(asset))}
    exact = []
    fallback = []
    for token in featured:
        token_symbol = str(token.get("symbol") or "").strip().upper()
        if token_symbol in aliases:
            exact.append(dict(token))
        elif any(token_symbol.startswith(alias) for alias in aliases):
            fallback.append(dict(token))
    if exact:
        return exact[0]
    if fallback:
        return fallback[0]

    currency = chain_info.get("currency") or {}
    currency_symbol = str(currency.get("symbol") or "").strip().upper()
    if currency_symbol in aliases:
        return dict(currency)
    return None


def _relay_placeholder_address(chain_info: Optional[Dict[str, Any]]) -> str:
    name = canon_chain_name(chain_info.get("name") if isinstance(chain_info, dict) else "")
    if name == "solana":
        return SOLANA_PLACEHOLDER_ADDRESS
    return EVM_PLACEHOLDER_ADDRESS


def get_debridge_chain_id(chain: str) -> Optional[int]:
    return _DEBRIDGE_CHAIN_IDS.get(canon_chain_name(chain))


async def fetch_relay_quote(
    session: aiohttp.ClientSession,
    *,
    source_chain: str,
    dest_chain: str,
    source_token: Dict[str, Any],
    dest_token: Dict[str, Any],
    amount_atomic: int,
) -> Optional[Dict[str, Any]]:
    if amount_atomic <= 0:
        return None

    chains = await fetch_relay_chains(session)
    source_info = get_relay_chain_info(chains, source_chain)
    dest_info = get_relay_chain_info(chains, dest_chain)
    if not source_info or not dest_info:
        return None

    payload = {
        "user": _relay_placeholder_address(source_info),
        "recipient": _relay_placeholder_address(dest_info),
        "originChainId": int(source_info.get("id")),
        "destinationChainId": int(dest_info.get("id")),
        "originCurrency": str(source_token.get("address") or "").strip(),
        "destinationCurrency": str(dest_token.get("address") or "").strip(),
        "amount": str(int(amount_atomic)),
        "tradeType": "EXACT_INPUT",
    }
    cache_key = (
        f"{payload['originChainId']}:{payload['destinationChainId']}:"
        f"{payload['originCurrency'].lower()}:{payload['destinationCurrency'].lower()}:{payload['amount']}"
    )
    cached = _cache_get(_relay_quote_cache, cache_key, _RELAY_QUOTE_TTL)
    if cached is not None:
        return cached if isinstance(cached, dict) else None

    data = await _fetch_json(session, "POST", f"{RELAY_API_BASE}/quote/v2", json_payload=payload, timeout_sec=8.0)
    if not isinstance(data, dict):
        return None
    details = data.get("details") or {}
    currency_in = details.get("currencyIn") or {}
    currency_out = details.get("currencyOut") or {}
    if not currency_out:
        return None

    fees = data.get("fees") or {}
    gas_fee = fees.get("gas") or {}
    relayer_fee = fees.get("relayer") or {}
    relayer_gas_fee = fees.get("relayerGas") or {}
    relayer_service_fee = fees.get("relayerService") or {}
    app_fee = fees.get("app") or {}

    quote = {
        "provider_id": "relay",
        "provider_name": "Relay",
        "docs_url": "https://docs.relay.link/",
        "source_chain": canon_chain_name(source_chain),
        "dest_chain": canon_chain_name(dest_chain),
        "source_token": dict(source_token),
        "dest_token": dict(dest_token),
        "amount_in_atomic": int(currency_in.get("amount") or amount_atomic),
        "amount_out_atomic": int(currency_out.get("amount") or 0),
        "amount_in_usd": _to_float(currency_in.get("amountUsd")),
        "amount_out_usd": _to_float(currency_out.get("amountUsd")),
        "minimum_out_atomic": int(currency_out.get("minimumAmount") or 0),
        "wallet_gas_usd": _to_float(gas_fee.get("amountUsd")) or 0.0,
        "relayer_fee_usd": (
            (_to_float(relayer_fee.get("amountUsd")) or 0.0)
            + (_to_float(relayer_gas_fee.get("amountUsd")) or 0.0)
            + (_to_float(relayer_service_fee.get("amountUsd")) or 0.0)
            + (_to_float(app_fee.get("amountUsd")) or 0.0)
        ),
        "time_estimate_sec": int(_to_float(details.get("timeEstimate")) or 0),
        "rate": _to_float(details.get("rate")),
        "protocol": data.get("protocol") or [],
        "router": str((((details.get("route") or {}).get("origin") or {}).get("router") or "relay")).strip() or "relay",
    }
    return _cache_set(_relay_quote_cache, cache_key, quote, max_size=256)


async def fetch_relay_rebalance_quote(
    session: aiohttp.ClientSession,
    *,
    source_chain: str,
    dest_chain: str,
    notional_usd: float,
) -> Optional[Dict[str, Any]]:
    if notional_usd <= 0:
        return None
    chains = await fetch_relay_chains(session)
    source_token = get_relay_featured_token(chains, source_chain, "USDC")
    dest_token = get_relay_featured_token(chains, dest_chain, "USDC")
    if not source_token or not dest_token:
        source_token = get_relay_featured_token(chains, source_chain, "USDT")
        dest_token = get_relay_featured_token(chains, dest_chain, "USDT")
    if not source_token or not dest_token:
        return None

    decimals = int(source_token.get("decimals") or 6)
    amount_atomic = max(1, int(round(float(notional_usd) * (10 ** decimals))))
    return await fetch_relay_quote(
        session,
        source_chain=source_chain,
        dest_chain=dest_chain,
        source_token=source_token,
        dest_token=dest_token,
        amount_atomic=amount_atomic,
    )


async def fetch_debridge_quote(
    session: aiohttp.ClientSession,
    *,
    source_chain: str,
    dest_chain: str,
    source_token: Dict[str, Any],
    dest_token: Dict[str, Any],
    amount_atomic: int,
) -> Optional[Dict[str, Any]]:
    if amount_atomic <= 0:
        return None

    source_chain_id = get_debridge_chain_id(source_chain)
    dest_chain_id = get_debridge_chain_id(dest_chain)
    source_address = str(source_token.get("address") or source_token.get("contract") or "").strip()
    dest_address = str(dest_token.get("address") or dest_token.get("contract") or "").strip()
    if not source_chain_id or not dest_chain_id or not source_address or not dest_address:
        return None

    params = {
        "srcChainId": str(int(source_chain_id)),
        "srcChainTokenIn": source_address,
        "srcChainTokenInAmount": str(int(amount_atomic)),
        "dstChainId": str(int(dest_chain_id)),
        "dstChainTokenOut": dest_address,
        "dstChainTokenOutAmount": "auto",
    }
    if canon_chain_name(source_chain) == "solana" or canon_chain_name(dest_chain) == "solana":
        params["skipSolanaRecipientValidation"] = "true"

    cache_key = (
        f"{params['srcChainId']}:{params['dstChainId']}:"
        f"{params['srcChainTokenIn'].lower()}:{params['dstChainTokenOut'].lower()}:{params['srcChainTokenInAmount']}"
    )
    cached = _cache_get(_debridge_quote_cache, cache_key, _DEBRIDGE_QUOTE_TTL)
    if cached is not None:
        return cached if isinstance(cached, dict) else None

    data = await _fetch_json(
        session,
        "GET",
        f"{DEBRIDGE_DLN_API_BASE}/order/create-tx",
        params=params,
        timeout_sec=10.0,
    )
    if not isinstance(data, dict):
        return None

    estimation = data.get("estimation") or {}
    source_meta = estimation.get("srcChainTokenIn") or {}
    dest_meta = estimation.get("dstChainTokenOut") or {}
    if not isinstance(source_meta, dict) or not isinstance(dest_meta, dict):
        return None

    amount_out_atomic = int(dest_meta.get("recommendedAmount") or dest_meta.get("amount") or 0)
    if amount_out_atomic <= 0:
        return None

    amount_in_usd = _to_float(source_meta.get("originApproximateUsdValue"))
    if amount_in_usd is None:
        amount_in_usd = _to_float(source_meta.get("approximateUsdValue"))
    amount_out_usd = _to_float(dest_meta.get("recommendedApproximateUsdValue"))
    if amount_out_usd is None:
        amount_out_usd = _to_float(dest_meta.get("approximateUsdValue"))

    costs = estimation.get("costsDetails") or []
    fee_types: List[str] = []
    for item in costs:
        if not isinstance(item, dict):
            continue
        fee_type = str(item.get("type") or "").strip()
        if fee_type and fee_type not in fee_types:
            fee_types.append(fee_type)

    quote = {
        "provider_id": "debridge",
        "provider_name": "deBridge",
        "docs_url": "https://docs.debridge.com/dln-details/integration-guidelines/order-creation/creating-orders",
        "source_chain": canon_chain_name(source_chain),
        "dest_chain": canon_chain_name(dest_chain),
        "source_token": dict(source_token),
        "dest_token": dict(dest_token),
        "amount_in_atomic": int(source_meta.get("amount") or amount_atomic),
        "amount_out_atomic": amount_out_atomic,
        "amount_in_usd": amount_in_usd,
        "amount_out_usd": amount_out_usd,
        "minimum_out_atomic": amount_out_atomic,
        "wallet_gas_usd": 0.0,
        "relayer_fee_usd": max(0.0, float(amount_in_usd or 0.0) - float(amount_out_usd or 0.0)),
        "time_estimate_sec": int((data.get("order") or {}).get("approximateFulfillmentDelay") or 0),
        "rate": (
            (float(amount_out_usd) / float(amount_in_usd))
            if amount_in_usd and amount_out_usd and amount_in_usd > 0
            else None
        ),
        "protocol": fee_types,
        "router": "dln",
        "fee_breakdown": list(costs) if isinstance(costs, list) else [],
    }
    return _cache_set(_debridge_quote_cache, cache_key, quote, max_size=256)


async def fetch_debridge_rebalance_quote(
    session: aiohttp.ClientSession,
    *,
    source_chain: str,
    dest_chain: str,
    notional_usd: float,
) -> Optional[Dict[str, Any]]:
    if notional_usd <= 0:
        return None
    chains = await fetch_relay_chains(session)
    source_token = get_relay_featured_token(chains, source_chain, "USDC")
    dest_token = get_relay_featured_token(chains, dest_chain, "USDC")
    if not source_token or not dest_token:
        source_token = get_relay_featured_token(chains, source_chain, "USDT")
        dest_token = get_relay_featured_token(chains, dest_chain, "USDT")
    if not source_token or not dest_token:
        return None

    decimals = int(source_token.get("decimals") or 6)
    amount_atomic = max(1, int(round(float(notional_usd) * (10 ** decimals))))
    return await fetch_debridge_quote(
        session,
        source_chain=source_chain,
        dest_chain=dest_chain,
        source_token=source_token,
        dest_token=dest_token,
        amount_atomic=amount_atomic,
    )
