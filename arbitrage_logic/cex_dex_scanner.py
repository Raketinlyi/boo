"""
CEX-DEX Arbitrage Scanner

Flow: CEX -> Token Contract -> DEX -> Bridge -> CEX

1. Fetches token contract addresses from CEX APIs (via exchange_info).
2. Finds DEX liquidity/prices for those contracts (Jupiter, GeckoTerminal, DexScreener).
3. Gets bridge quotes (Mayan, Wormhole, LayerZero, Relay, deBridge).
4. Calculates net arbitrage profit after all fees (CEX fees, DEX slippage, bridge fees, gas).
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional, Sequence

import aiohttp

from utils.exchange_info import exchange_info_fetcher, get_dex_network
from utils.interchain_live_quotes import (
    build_jupiter_dex_quote,
    canon_chain_name,
    discover_symbol_contracts,
    fetch_debridge_rebalance_quote,
    fetch_geckoterminal_dex_quotes,
    fetch_layerzero_rebalance_quote,
    fetch_mayan_rebalance_quote,
    fetch_relay_rebalance_quote,
    fetch_wormhole_rebalance_quote,
    resolve_solana_token,
)
from utils.symbols import split_pair_symbol

logger = logging.getLogger(__name__)

# Chains where we can query DEX prices
DEX_SUPPORTED_CHAINS = {
    "solana", "ethereum", "binance-smart-chain", "arbitrum-one",
    "optimistic-ethereum", "base", "polygon-pos", "avalanche",
    "linea", "scroll", "fantom", "sui", "sonic", "berachain",
    "mantle", "blast", "zksync",
}

# Chains that support bridging (have bridge provider coverage)
BRIDGE_SUPPORTED_CHAINS = {
    "solana", "ethereum", "binance-smart-chain", "arbitrum-one",
    "optimistic-ethereum", "base", "polygon-pos", "avalanche",
    "linea", "sui",
}

# Map exchange chain names -> canonical chain names
_EXCHANGE_CHAIN_CANON: Dict[str, str] = {}


def _canon_exchange_chain(chain_raw: str) -> str:
    """Normalize exchange-reported chain name to canonical form."""
    if not chain_raw:
        return ""
    key = chain_raw.strip().upper()
    cached = _EXCHANGE_CHAIN_CANON.get(key)
    if cached is not None:
        return cached
    result = canon_chain_name(chain_raw)
    _EXCHANGE_CHAIN_CANON[key] = result
    return result


def extract_contracts_from_exchange_info(
    exchange_info: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Extract contract addresses and chain info from exchange_info payload.

    Returns a list of dicts with keys:
        chain, contract, exchange, deposit_enabled, withdraw_enabled,
        withdraw_fee, min_withdraw
    """
    rows = exchange_info.get("exchanges") or []
    results: List[Dict[str, Any]] = []
    seen = set()

    for row in rows:
        if not isinstance(row, dict):
            continue
        contract = str(row.get("contract") or "").strip()
        chain_raw = str(row.get("chain") or "").strip()
        if not contract or contract in ("Native coin", "Native", "None", ""):
            continue

        chain = _canon_exchange_chain(chain_raw)
        if not chain or chain == "unknown":
            continue

        dedup_key = f"{chain}:{contract.lower()}"
        if dedup_key in seen:
            continue
        seen.add(dedup_key)

        results.append({
            "chain": chain,
            "contract": contract,
            "exchange": str(row.get("exchange") or "").strip(),
            "deposit_enabled": bool(row.get("deposit_enabled")),
            "withdraw_enabled": bool(row.get("withdraw_enabled")),
            "withdraw_fee": row.get("withdraw_fee"),
            "min_withdraw": row.get("min_withdraw"),
        })
    return results


async def fetch_dex_quotes_for_asset(
    session: aiohttp.ClientSession,
    asset: str,
    contracts: List[Dict[str, Any]],
    *,
    notional_usd: float = 100.0,
) -> List[Dict[str, Any]]:
    """Fetch DEX price quotes for an asset using known contracts.

    Queries Jupiter (Solana) and GeckoTerminal (EVM chains) in parallel.
    """
    quotes: List[Dict[str, Any]] = []
    tasks = []

    # Group contracts by chain
    by_chain: Dict[str, Dict[str, Any]] = {}
    for c in contracts:
        chain = c.get("chain", "")
        if chain in DEX_SUPPORTED_CHAINS:
            existing = by_chain.get(chain)
            if existing is None or c.get("withdraw_enabled"):
                by_chain[chain] = c

    preferred_contracts = [c["contract"] for c in contracts if c.get("contract")]

    # 1) Jupiter (Solana) — live quote with slippage
    solana_contract = by_chain.get("solana")
    if solana_contract and solana_contract.get("contract"):
        async def _jupiter_quote():
            try:
                token = await resolve_solana_token(
                    session, asset,
                    preferred_mint=solana_contract["contract"],
                    allow_symbol_only=True,
                )
                if not token or not token.get("contract"):
                    return
                quote = await build_jupiter_dex_quote(
                    session,
                    symbol=asset,
                    mint=token["contract"],
                    decimals=token.get("decimals"),
                    notional_usd=notional_usd,
                    metadata=token,
                )
                if quote:
                    quote["_source_contract_info"] = solana_contract
                    quotes.append(quote)
            except Exception as e:
                logger.debug("Jupiter quote failed for %s: %s", asset, e)
        tasks.append(_jupiter_quote())

    # 2) GeckoTerminal for each EVM chain with a known contract
    for chain, info in by_chain.items():
        if chain == "solana":
            continue
        contract = info.get("contract", "")
        if not contract:
            continue

        async def _gecko_quote(ch=chain, ct=contract, inf=info):
            try:
                results = await fetch_geckoterminal_dex_quotes(
                    session,
                    asset=asset,
                    chain=ch,
                    contract=ct,
                    contract_source="cex_api",
                )
                for q in results:
                    q["_source_contract_info"] = inf
                    quotes.append(q)
            except Exception as e:
                logger.debug("GeckoTerminal quote failed for %s on %s: %s", asset, ch, e)
        tasks.append(_gecko_quote())

    # 3) Discover additional contracts via GeckoTerminal/DexScreener search
    async def _discover():
        try:
            discovered = await discover_symbol_contracts(
                session, asset,
                preferred_contracts=preferred_contracts,
            )
            for token in discovered:
                chain = token.get("chain", "")
                contract = token.get("contract", "")
                if not chain or not contract or chain in by_chain:
                    continue
                if chain not in DEX_SUPPORTED_CHAINS:
                    continue
                dex_results = await fetch_geckoterminal_dex_quotes(
                    session,
                    asset=asset,
                    chain=chain,
                    contract=contract,
                    contract_source=str(token.get("source", "discovery")),
                )
                for q in dex_results:
                    q["_source_contract_info"] = token
                    quotes.append(q)
        except Exception as e:
            logger.debug("Contract discovery failed for %s: %s", asset, e)
    tasks.append(_discover())

    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)

    return quotes


async def fetch_bridge_quotes(
    session: aiohttp.ClientSession,
    source_chain: str,
    dest_chain: str,
    notional_usd: float,
    *,
    provider_priority: Optional[Sequence[str]] = None,
    provider_blacklist: Optional[Sequence[str]] = None,
) -> List[Dict[str, Any]]:
    """Fetch bridge quotes from all supported providers for a chain pair."""
    if notional_usd <= 0:
        return []

    src = canon_chain_name(source_chain)
    dst = canon_chain_name(dest_chain)
    if src == dst or not src or not dst:
        return []
    if src not in BRIDGE_SUPPORTED_CHAINS or dst not in BRIDGE_SUPPORTED_CHAINS:
        return []

    blacklist = set(str(p).strip().lower() for p in (provider_blacklist or []))
    providers = {
        "mayan": fetch_mayan_rebalance_quote,
        "wormhole": fetch_wormhole_rebalance_quote,
        "layerzero": fetch_layerzero_rebalance_quote,
        "relay": fetch_relay_rebalance_quote,
        "debridge": fetch_debridge_rebalance_quote,
    }

    tasks = []
    task_names = []
    for name, fn in providers.items():
        if name in blacklist:
            continue
        tasks.append(fn(session, source_chain=src, dest_chain=dst, notional_usd=notional_usd))
        task_names.append(name)

    if not tasks:
        return []

    results = await asyncio.gather(*tasks, return_exceptions=True)
    quotes: List[Dict[str, Any]] = []
    for name, result in zip(task_names, results):
        if isinstance(result, dict):
            quotes.append(result)
        elif isinstance(result, Exception):
            logger.debug("Bridge quote from %s failed (%s->%s): %s", name, src, dst, result)

    # Sort by best output (lowest fee)
    quotes.sort(key=lambda q: float(q.get("relayer_fee_usd") or 999999))
    return quotes


def calculate_cex_dex_route(
    *,
    asset: str,
    cex_buy_price: float,
    cex_buy_exchange: str,
    cex_sell_price: float,
    cex_sell_exchange: str,
    dex_quote: Dict[str, Any],
    bridge_quote: Optional[Dict[str, Any]],
    cex_buy_fee_rate: float = 0.001,
    cex_sell_fee_rate: float = 0.001,
    withdraw_fee_usd: float = 0.0,
    notional_usd: float = 100.0,
) -> Optional[Dict[str, Any]]:
    """Calculate net profit for a CEX-DEX arbitrage route.

    Route: Buy on CEX -> Withdraw -> DEX swap -> Bridge -> Deposit on CEX -> Sell

    Returns a route dict with net_profit_usd, spread, fee breakdown, or None if unprofitable.
    """
    dex_price = float(dex_quote.get("price") or 0)
    dex_chain = str(dex_quote.get("chain") or "").strip()
    if dex_price <= 0 or cex_buy_price <= 0 or cex_sell_price <= 0:
        return None

    # CEX trading fees
    buy_fee_usd = notional_usd * cex_buy_fee_rate
    sell_fee_usd = notional_usd * cex_sell_fee_rate

    # DEX price impact / slippage
    dex_impact_pct = float(dex_quote.get("price_impact_pct") or 0)
    dex_slippage_usd = notional_usd * abs(dex_impact_pct) / 100.0

    # Bridge fees
    bridge_fee_usd = 0.0
    bridge_provider = None
    bridge_time_sec = 0
    if bridge_quote:
        bridge_fee_usd = float(bridge_quote.get("relayer_fee_usd") or 0) + float(bridge_quote.get("wallet_gas_usd") or 0)
        bridge_provider = str(bridge_quote.get("provider_name") or bridge_quote.get("provider_id") or "").strip()
        bridge_time_sec = int(bridge_quote.get("time_estimate_sec") or 0)

    total_fees_usd = buy_fee_usd + sell_fee_usd + withdraw_fee_usd + dex_slippage_usd + bridge_fee_usd

    # Route A: CEX(buy) -> withdraw to chain -> sell on DEX
    # Profit = (dex_price - cex_buy_price) / cex_buy_price * notional - fees
    spread_cex_to_dex = ((dex_price - cex_buy_price) / cex_buy_price) * 100.0
    gross_profit_a = (spread_cex_to_dex / 100.0) * notional_usd
    net_profit_a = gross_profit_a - total_fees_usd

    # Route B: Buy on DEX -> bridge -> deposit to CEX -> sell on CEX
    spread_dex_to_cex = ((cex_sell_price - dex_price) / dex_price) * 100.0
    gross_profit_b = (spread_dex_to_cex / 100.0) * notional_usd
    net_profit_b = gross_profit_b - total_fees_usd

    # Pick the more profitable direction
    if net_profit_a >= net_profit_b:
        direction = "cex_to_dex"
        net_profit = net_profit_a
        gross_spread = spread_cex_to_dex
        buy_venue = cex_buy_exchange
        sell_venue = f"DEX:{dex_chain}"
        buy_price = cex_buy_price
        sell_price = dex_price
    else:
        direction = "dex_to_cex"
        net_profit = net_profit_b
        gross_spread = spread_dex_to_cex
        buy_venue = f"DEX:{dex_chain}"
        sell_venue = cex_sell_exchange
        buy_price = dex_price
        sell_price = cex_sell_price

    if net_profit <= 0:
        return None

    net_spread = (net_profit / notional_usd) * 100.0

    return {
        "asset": asset,
        "route_type": "cex_dex",
        "direction": direction,
        "buy_venue": buy_venue,
        "sell_venue": sell_venue,
        "buy_price": buy_price,
        "sell_price": sell_price,
        "dex_chain": dex_chain,
        "dex_id": str(dex_quote.get("dex_id") or "").strip(),
        "dex_label": str(dex_quote.get("label") or "").strip(),
        "dex_contract": str(dex_quote.get("contract") or "").strip(),
        "dex_liquidity_usd": float(dex_quote.get("liquidity_usd") or 0),
        "dex_volume_24h": float(dex_quote.get("volume_24h") or 0),
        "dex_url": str(dex_quote.get("url") or "").strip(),
        "gross_spread_pct": round(gross_spread, 4),
        "net_spread_pct": round(net_spread, 4),
        "net_profit_usd": round(net_profit, 4),
        "notional_usd": notional_usd,
        "fee_breakdown": {
            "cex_buy_fee_usd": round(buy_fee_usd, 4),
            "cex_sell_fee_usd": round(sell_fee_usd, 4),
            "withdraw_fee_usd": round(withdraw_fee_usd, 4),
            "dex_slippage_usd": round(dex_slippage_usd, 4),
            "bridge_fee_usd": round(bridge_fee_usd, 4),
            "total_fees_usd": round(total_fees_usd, 4),
        },
        "bridge_provider": bridge_provider,
        "bridge_time_sec": bridge_time_sec,
        "dex_price_impact_pct": dex_impact_pct,
        "quote_mode": str(dex_quote.get("quote_mode") or "estimated").strip(),
        "timestamp": time.time(),
    }


async def scan_cex_dex_opportunities(
    session: aiohttp.ClientSession,
    *,
    assets: Sequence[str],
    cex_prices: Dict[str, Dict[str, float]],
    exchange_fees: Optional[Dict[str, float]] = None,
    notional_usd: float = 100.0,
    min_net_spread_pct: float = 0.3,
    bridge_provider_priority: Optional[Sequence[str]] = None,
    bridge_provider_blacklist: Optional[Sequence[str]] = None,
    max_parallel_assets: int = 4,
) -> List[Dict[str, Any]]:
    """Main scanner: find CEX-DEX arbitrage for a list of assets.

    Args:
        session: aiohttp session
        assets: list of base assets to scan (e.g. ["SOL", "BONK", "WIF"])
        cex_prices: {exchange_name: {symbol: price}} from calculator
        exchange_fees: {exchange_name: fee_rate} (0.001 = 0.1%)
        notional_usd: trade size for calculations
        min_net_spread_pct: minimum net spread to include in results
        bridge_provider_priority: ordered list of bridge providers
        bridge_provider_blacklist: providers to skip
        max_parallel_assets: concurrency limit

    Returns:
        List of profitable route dicts sorted by net_profit_usd descending.
    """
    if not assets or not cex_prices:
        return []

    fees = exchange_fees or {}
    all_routes: List[Dict[str, Any]] = []
    semaphore = asyncio.Semaphore(max_parallel_assets)

    async def _scan_asset(asset: str) -> List[Dict[str, Any]]:
        async with semaphore:
            routes: List[Dict[str, Any]] = []
            try:
                # 1. Get CEX prices for this asset
                asset_cex_prices: Dict[str, float] = {}
                for ex_name, ex_prices in cex_prices.items():
                    for quote in ("USDT", "USDC"):
                        symbol = f"{asset}{quote}"
                        price = ex_prices.get(symbol)
                        if price and float(price) > 0:
                            if ex_name not in asset_cex_prices or quote == "USDT":
                                asset_cex_prices[ex_name] = float(price)
                            break

                if len(asset_cex_prices) < 1:
                    return routes

                # 2. Fetch contract addresses from CEX APIs
                info = await exchange_info_fetcher.get_all_exchange_info(asset)
                contracts = extract_contracts_from_exchange_info(info)
                if not contracts:
                    return routes

                # 3. Fetch DEX quotes using those contracts
                dex_quotes = await fetch_dex_quotes_for_asset(
                    session, asset, contracts,
                    notional_usd=notional_usd,
                )
                if not dex_quotes:
                    return routes

                # 4. Find best CEX buy/sell prices
                sorted_prices = sorted(asset_cex_prices.items(), key=lambda x: x[1])
                best_buy_ex, best_buy_price = sorted_prices[0]
                best_sell_ex, best_sell_price = sorted_prices[-1]

                # 5. For each DEX quote, check if bridging is needed and calculate profit
                for dex_q in dex_quotes:
                    dex_chain = str(dex_q.get("chain") or "").strip()
                    if not dex_chain:
                        continue

                    # Check if any CEX supports this chain for deposit/withdraw
                    chain_contracts = [
                        c for c in contracts
                        if c.get("chain") == dex_chain
                    ]
                    has_withdraw = any(c.get("withdraw_enabled") for c in chain_contracts)
                    has_deposit = any(c.get("deposit_enabled") for c in chain_contracts)

                    # Estimate withdrawal fee
                    withdraw_fee_usd = 0.0
                    for c in chain_contracts:
                        raw_fee = c.get("withdraw_fee")
                        if raw_fee is not None:
                            try:
                                withdraw_fee_usd = float(raw_fee)
                                # If fee is in token units, convert to USD
                                if withdraw_fee_usd > 10 and best_buy_price < 1:
                                    withdraw_fee_usd = withdraw_fee_usd * best_buy_price
                                elif withdraw_fee_usd < 0.001 and best_buy_price > 100:
                                    withdraw_fee_usd = withdraw_fee_usd * best_buy_price
                            except (ValueError, TypeError):
                                withdraw_fee_usd = 0.0
                            break

                    # Bridge quote if needed (same chain = no bridge needed)
                    bridge_quote = None

                    # Calculate route for each CEX pair
                    for buy_ex, buy_price in sorted_prices[:3]:  # top 3 cheapest
                        for sell_ex, sell_price in sorted_prices[-3:]:  # top 3 most expensive
                            if buy_ex == sell_ex:
                                continue

                            buy_fee = fees.get(buy_ex, 0.001)
                            sell_fee = fees.get(sell_ex, 0.001)

                            route = calculate_cex_dex_route(
                                asset=asset,
                                cex_buy_price=buy_price,
                                cex_buy_exchange=buy_ex,
                                cex_sell_price=sell_price,
                                cex_sell_exchange=sell_ex,
                                dex_quote=dex_q,
                                bridge_quote=bridge_quote,
                                cex_buy_fee_rate=buy_fee,
                                cex_sell_fee_rate=sell_fee,
                                withdraw_fee_usd=withdraw_fee_usd,
                                notional_usd=notional_usd,
                            )
                            if route and route["net_spread_pct"] >= min_net_spread_pct:
                                route["has_withdraw"] = has_withdraw
                                route["has_deposit"] = has_deposit
                                route["transfer_viable"] = has_withdraw or has_deposit
                                routes.append(route)

            except Exception as e:
                logger.error("CEX-DEX scan failed for %s: %s", asset, e)
            return routes

    tasks = [_scan_asset(a) for a in assets]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    for result in results:
        if isinstance(result, list):
            all_routes.extend(result)
        elif isinstance(result, Exception):
            logger.error("CEX-DEX scan task error: %s", result)

    # Sort by net profit descending
    all_routes.sort(key=lambda r: float(r.get("net_profit_usd", 0)), reverse=True)
    return all_routes
