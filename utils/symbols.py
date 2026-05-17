from __future__ import annotations

import re
from typing import Iterable, Optional

# Common quote currencies seen across exchanges. Used to split symbols like BTCUSDT → BTC/USDT.
# Keep longest-first matching to avoid USD being matched before USDT/USDC.
_DEFAULT_QUOTE_CURRENCIES = (
    "USDT",
    "USDC",
    "BUSD",
    "TUSD",
    "FDUSD",
    "USD",
    "EUR",
    "TRY",
    "BTC",
    "ETH",
    "BNB",
)


def split_pair_symbol(
    symbol: str | None,
    *,
    quote_currencies: Optional[Iterable[str]] = None,
) -> tuple[str, str]:
    """Split a pair symbol into (base, quote).

    Supports formats:
      - "BTCUSDT"
      - "BTC_USDT"
      - "BTC-USDT"
      - "BTC/USDT"

    If quote cannot be inferred, returns (SYMBOL, "").
    """
    if not symbol:
        return ("", "")

    s = str(symbol).strip().upper()
    if not s:
        return ("", "")

    for sep in ("/", "_", "-", ":"):
        if sep in s:
            base, quote = s.split(sep, 1)
            return (base.strip(), quote.strip())

    quotes = [str(q).upper() for q in (quote_currencies or _DEFAULT_QUOTE_CURRENCIES)]
    quotes.sort(key=len, reverse=True)
    for q in quotes:
        if s.endswith(q) and len(s) > len(q):
            base = s[: -len(q)]
            if base:
                return (base, q)

    return (s, "")


def normalize_pair_symbol(
    symbol: str | None,
    *,
    quote_currencies: Optional[Iterable[str]] = None,
) -> str:
    """Normalize a pair symbol to a stable internal format: BASEQUOTE (no separators)."""
    base, quote = split_pair_symbol(symbol, quote_currencies=quote_currencies)
    if base and quote:
        return f"{base}{quote}"
    if not symbol:
        return ""
    return re.sub(r"[^A-Z0-9]", "", str(symbol).strip().upper())


def format_pair_symbol(
    symbol: str | None,
    *,
    sep: str,
    quote_currencies: Optional[Iterable[str]] = None,
) -> str:
    """Format a normalized (or raw) pair symbol into BASE{sep}QUOTE when possible."""
    base, quote = split_pair_symbol(symbol, quote_currencies=quote_currencies)
    if base and quote:
        return f"{base}{sep}{quote}"
    return str(symbol or "").strip().upper()


def extract_base_asset(
    symbol: str | None,
    *,
    assume_pair: bool = True,
    quote_currencies: Optional[Iterable[str]] = None,
) -> str:
    """Extract base asset from a pair symbol.

    Examples:
      - "BTCUSDT" -> "BTC"
      - "BTC_USDT" -> "BTC"
      - "BTC-USDT" -> "BTC"
      - "BTC/USDT" -> "BTC"

    If `assume_pair=False`, suffix-based splitting is disabled to avoid turning
    assets like "WETH" into "W".
    """
    if not symbol:
        return ""

    s = str(symbol).strip().upper()
    if not s:
        return ""

    for sep in ("/", "_", "-"):
        if sep in s:
            base = s.split(sep, 1)[0].strip()
            return base

    if not assume_pair:
        return s

    quotes = [str(q).upper() for q in (quote_currencies or _DEFAULT_QUOTE_CURRENCIES)]
    quotes.sort(key=len, reverse=True)
    for q in quotes:
        if s.endswith(q) and len(s) > len(q):
            base = s[: -len(q)]
            # Basic sanity: avoid returning too-short artifacts.
            if len(base) >= 2:
                return base
    return s
