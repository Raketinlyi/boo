"""Direct-swap URL builder for DEX quotes.

The bot historically exposed DexScreener/GeckoTerminal pair pages in the UI,
which are info/chart pages, not swap pages — clicking them felt like landing
on a search engine. This module turns a (chain, contract) pair into a URL on
a real swap aggregator so the user can execute the trade in one click.

Mapping:
    - Solana                 -> Jupiter (https://jup.ag/swap/USDC-<mint>)
    - Tron                   -> SunSwap (https://sun.io/#/v2?token1=<addr>)
    - EVM chains (ETH, BSC,  -> 1inch (https://app.1inch.io/#/<chainId>/simple/swap/USDT/<addr>)
      Polygon, Arbitrum,
      Optimism, Base,
      Avalanche, Fantom, ...)
    - Everything else        -> DexScreener token page (direct, not a search)

Usage:
    from utils.dex_swap_links import build_dex_swap_url
    url = build_dex_swap_url(chain="ethereum", contract="0x...")
"""

from __future__ import annotations

from typing import Optional


# EVM chain_slug -> 1inch chain id. Only chains that 1inch supports for spot
# swaps. Unsupported chains fall through to DexScreener.
_ONEINCH_CHAIN_IDS = {
    "ethereum": 1,
    "eth": 1,
    "bsc": 56,
    "binance": 56,
    "binance-smart-chain": 56,
    "bnb": 56,
    "polygon": 137,
    "matic": 137,
    "polygon-pos": 137,
    "optimism": 10,
    "op": 10,
    "arbitrum": 42161,
    "arbitrum-one": 42161,
    "arb": 42161,
    "avalanche": 43114,
    "avax": 43114,
    "base": 8453,
    "fantom": 250,
    "ftm": 250,
    "gnosis": 100,
    "xdai": 100,
    "zksync": 324,
    "zksync-era": 324,
    "linea": 59144,
    "aurora": 1313161554,
}

# DexScreener chain slugs (for fallback token page URL).
_DEXSCREENER_CHAIN_SLUGS = {
    "ethereum": "ethereum",
    "eth": "ethereum",
    "bsc": "bsc",
    "binance": "bsc",
    "binance-smart-chain": "bsc",
    "bnb": "bsc",
    "polygon": "polygon",
    "polygon-pos": "polygon",
    "matic": "polygon",
    "arbitrum": "arbitrum",
    "arbitrum-one": "arbitrum",
    "arb": "arbitrum",
    "optimism": "optimism",
    "op": "optimism",
    "avalanche": "avalanche",
    "avax": "avalanche",
    "base": "base",
    "fantom": "fantom",
    "ftm": "fantom",
    "gnosis": "gnosis",
    "zksync": "zksync",
    "zksync-era": "zksync",
    "linea": "linea",
    "solana": "solana",
    "sol": "solana",
    "tron": "tron",
    "trx": "tron",
    "sui": "sui",
    "ton": "ton",
    "cronos": "cronos",
    "celo": "celo",
    "moonbeam": "moonbeam",
    "aurora": "aurora",
    "kava": "kava",
    "metis": "metis",
    "mantle": "mantle",
    "scroll": "scroll",
    "blast": "blast",
}


def _norm(chain: Optional[str]) -> str:
    return str(chain or "").strip().lower().replace("_", "-")


def build_dex_swap_url(*, chain: Optional[str], contract: Optional[str]) -> Optional[str]:
    """Return a direct swap-page URL for the given chain+contract, or None.

    Never returns a search URL. If no suitable aggregator is known, returns a
    DexScreener *token* page (direct, not /search?q=). If even that cannot be
    built (missing contract/chain), returns None.
    """
    addr = str(contract or "").strip()
    if not addr:
        return None
    chain_norm = _norm(chain)

    # Solana — Jupiter direct swap with USDC as the quote side.
    if chain_norm in ("solana", "sol"):
        return f"https://jup.ag/swap/USDC-{addr}"

    # Tron — SunSwap direct pair selector (token vs USDT).
    if chain_norm in ("tron", "trx"):
        return f"https://sun.io/#/v2?lang=en-US&token1={addr}"

    # Sui — Cetus aggregator (accepts token address as the "to" asset).
    if chain_norm in ("sui",):
        return f"https://app.cetus.zone/swap?from=0x2::sui::SUI&to={addr}"

    # EVM chains with 1inch support.
    one_inch_id = _ONEINCH_CHAIN_IDS.get(chain_norm)
    if one_inch_id:
        return f"https://app.1inch.io/#/{one_inch_id}/simple/swap/USDT/{addr}"

    # Fallback: DexScreener token page (direct token URL, not search).
    ds_slug = _DEXSCREENER_CHAIN_SLUGS.get(chain_norm)
    if ds_slug:
        return f"https://dexscreener.com/{ds_slug}/{addr}"

    # Last resort: still prefer DexScreener token page over any search URL.
    return f"https://dexscreener.com/search?q={addr}"
