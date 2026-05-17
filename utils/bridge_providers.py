from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Sequence


_STABLE_ASSETS = {"USDC"}

_PROVIDERS: List[Dict[str, Any]] = [
    {
        "id": "layerzero",
        "name": "LayerZero",
        "chains": {
            "ethereum",
            "arbitrum-one",
            "optimistic-ethereum",
            "base",
            "polygon-pos",
            "avalanche",
            "binance-smart-chain",
            "solana",
            "linea",
            "sui",
            "sonic",
        },
        "asset_mode": "any",
        "base_priority": 98,
        "docs_url": "https://docs.layerzero.network/v2/tools/api/overview",
        "note": "Official Value Transfer API for Stargate/LayerZero routes; live quotes require API access.",
    },
    {
        "id": "wormhole",
        "name": "Wormhole",
        "chains": {
            "ethereum",
            "arbitrum-one",
            "optimistic-ethereum",
            "base",
            "polygon-pos",
            "avalanche",
            "binance-smart-chain",
            "solana",
            "sui",
        },
        "asset_mode": "stable",
        "base_priority": 100,
        "docs_url": "https://wormhole.com/docs/",
        "note": "Protocol-level bridge/router with official CCTP/token-bridge routes; current live quote layer is focused on canonical stable lanes.",
    },
    {
        "id": "mayan",
        "name": "Mayan",
        "chains": {
            "ethereum",
            "arbitrum-one",
            "optimistic-ethereum",
            "base",
            "polygon-pos",
            "avalanche",
            "binance-smart-chain",
            "solana",
            "sui",
            "hypercore",
        },
        "asset_mode": "any",
        "base_priority": 96,
        "docs_url": "https://docs.mayan.finance/",
        "note": "Swap-focused cross-chain SDK for Solana/EVM/Sui; optimized for one-shot swaps.",
    },
    {
        "id": "relay",
        "name": "Relay",
        "chains": {
            "ethereum",
            "arbitrum-one",
            "optimistic-ethereum",
            "base",
            "polygon-pos",
            "avalanche",
            "binance-smart-chain",
            "solana",
            "tron",
            "sui",
        },
        "asset_mode": "any",
        "base_priority": 94,
        "docs_url": "https://docs.relay.link/",
        "note": "Unified quote/execute/status API with strong Solana onboarding flows.",
    },
    {
        "id": "bungee",
        "name": "Bungee",
        "chains": {
            "ethereum",
            "arbitrum-one",
            "optimistic-ethereum",
            "base",
            "polygon-pos",
            "avalanche",
            "binance-smart-chain",
            "solana",
            "mantle",
            "linea",
            "scroll",
            "tron",
            "sonic",
        },
        "asset_mode": "any",
        "base_priority": 90,
        "docs_url": "https://docs.bungee.exchange/",
        "note": "Aggregator with manual routing, status tracking, and chain-abstracted swaps.",
    },
    {
        "id": "squid",
        "name": "Squid",
        "chains": {
            "ethereum",
            "arbitrum-one",
            "optimistic-ethereum",
            "base",
            "polygon-pos",
            "avalanche",
            "binance-smart-chain",
            "solana",
            "mantle",
            "linea",
            "scroll",
            "fantom",
            "moonbeam",
            "celo",
            "blast",
            "gnosis",
            "sonic",
            "berachain",
        },
        "asset_mode": "any",
        "base_priority": 89,
        "docs_url": "https://docs.squidrouter.com/",
        "note": "API/SDK router with broad chain coverage and supported chains/tokens endpoints.",
    },
    {
        "id": "debridge",
        "name": "deBridge",
        "chains": {
            "ethereum",
            "arbitrum-one",
            "optimistic-ethereum",
            "base",
            "polygon-pos",
            "avalanche",
            "binance-smart-chain",
            "solana",
            "mantle",
            "linea",
            "tron",
        },
        "asset_mode": "any",
        "base_priority": 88,
        "docs_url": "https://docs.debridge.com/",
        "note": "Intent-based cross-chain execution with EVM<->Solana examples.",
    },
    {
        "id": "across",
        "name": "Across",
        "chains": {
            "ethereum",
            "arbitrum-one",
            "optimistic-ethereum",
            "base",
            "polygon-pos",
            "avalanche",
            "solana",
        },
        "asset_mode": "stable_only",
        "base_priority": 84,
        "docs_url": "https://docs.across.to/",
        "note": "Best fit for USDC lanes; Solana support is focused on bridge rails.",
    },
    {
        "id": "skip",
        "name": "Skip Go",
        "chains": {
            "ethereum",
            "arbitrum-one",
            "optimistic-ethereum",
            "base",
            "polygon-pos",
            "avalanche",
            "solana",
        },
        "asset_mode": "stable_only",
        "base_priority": 82,
        "docs_url": "https://docs.skip.build/go/general/smart-relay",
        "note": "Cross-ecosystem route layer; Smart Relay is strongest on CCTP-style stablecoin paths.",
    },
]


def _normalize_list(values: Optional[Sequence[str]]) -> List[str]:
    if not values:
        return []
    result = []
    for value in values:
        item = str(value or "").strip().lower()
        if item:
            result.append(item)
    return result


def _supports_asset(provider: Dict[str, Any], asset: str) -> bool:
    mode = provider.get("asset_mode", "any")
    asset_u = str(asset or "").strip().upper()
    if mode == "any":
        return True
    if mode == "stable_only":
        return asset_u in _STABLE_ASSETS
    return False


def _score_provider(provider: Dict[str, Any], asset: str, source_chain: str, dest_chain: str) -> int:
    score = int(provider.get("base_priority", 0))
    asset_u = str(asset or "").strip().upper()
    chains = {source_chain, dest_chain}
    if "solana" in chains:
        if provider["id"] == "layerzero":
            score += 11
        if provider["id"] == "mayan":
            score += 12
        elif provider["id"] == "wormhole":
            score += 10
        elif provider["id"] == "relay":
            score += 8
        elif provider["id"] == "debridge":
            score += 6
        elif provider["id"] == "bungee":
            score += 5
        elif provider["id"] == "across" and asset_u in _STABLE_ASSETS:
            score += 6
        elif provider["id"] == "skip" and asset_u in _STABLE_ASSETS:
            score += 4
    if asset_u in _STABLE_ASSETS:
        if provider["id"] in {"wormhole", "relay", "layerzero"}:
            score += 6
        elif provider["id"] == "across":
            score += 10
        elif provider["id"] == "skip":
            score += 8
    return score


def get_bridge_candidates(
    asset: str,
    source_chain: str,
    dest_chain: str,
    *,
    preferred: Optional[Sequence[str]] = None,
    blacklist: Optional[Sequence[str]] = None,
) -> List[Dict[str, Any]]:
    source = str(source_chain or "").strip().lower()
    dest = str(dest_chain or "").strip().lower()
    if not source or not dest or source == dest:
        return []

    preferred_ids = _normalize_list(preferred)
    blacklist_ids = set(_normalize_list(blacklist))
    preferred_index = {provider_id: idx for idx, provider_id in enumerate(preferred_ids)}

    candidates: List[Dict[str, Any]] = []
    for provider in _PROVIDERS:
        provider_id = provider["id"]
        if provider_id in blacklist_ids:
            continue
        supported_chains = provider.get("chains") or set()
        if source not in supported_chains or dest not in supported_chains:
            continue
        if not _supports_asset(provider, asset):
            continue

        item = dict(provider)
        item["score"] = _score_provider(provider, asset, source, dest)
        item["preferred_index"] = preferred_index.get(provider_id, 10_000)
        candidates.append(item)

    candidates.sort(
        key=lambda item: (
            int(item.get("preferred_index", 10_000)),
            -int(item.get("score", 0)),
            str(item.get("name", "")),
        )
    )
    for item in candidates:
        item.pop("preferred_index", None)
    return candidates
