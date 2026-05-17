import logging
logger = logging.getLogger(__name__)
import asyncio
import random
import time
import json
import traceback
from typing import Dict, Set, Optional, List, Any
from aiohttp import ClientError, ClientTimeout, ClientConnectorError
from aiohttp_retry import RetryClient

from config import Config


class Exchange:
    """Р‘Р°Р·РѕРІС‹Р№ РєР»Р°СЃСЃ РґР»СЏ РІСЃРµС… Р±РёСЂР¶."""

    def __init__(self, name: str, pairs_url: str, ticker_url: str, orderbook_url: str, config: Config, enabled: bool = True):
        self.name = name
        self.pairs_url = pairs_url
        self.ticker_url = ticker_url
        self.orderbook_url = orderbook_url
        self.config = config
        self.enabled = enabled

        # Internal trading_fee is stored as a rate, while config values are usually given in percent.
        fee_percent = config.get_exchange_fee(name, config.get("default_trading_fee", 0.1))
        try:
            self.trading_fee = max(0.0, float(fee_percent)) / 100.0
        except (TypeError, ValueError):
            self.trading_fee = 0.001

        # Internal storage for available pairs вЂ” use property to avoid accidental clearing
        self._available_pairs: Set[str] = set()
        self.last_error_time: Optional[float] = None
        self.error_count: int = 0
        self.max_consecutive_errors = 3
        self.error_timeout = 300  # 5 minutes
        self._cooldown_warning_last_logged: float = 0.0
        self._cooldown_warning_interval_sec = 60.0
        # Retry parameters for transient errors (per-request level)
        self._max_retries = 2
        self._retry_base_delay = 0.5  # seconds

        # Timeout (increased default to improve resilience)
        price_timeout = config.get("price_timeout", 20)
        self.timeout = ClientTimeout(total=price_timeout)

        # A real browser UA is required for Bybit/MEXC (CloudFlare blocks generic
        # bot UAs with 403 Forbidden on /v5/market/tickers and /api/v3/ticker/price).
        self.headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
        }

        # API keys if present
        self.api_keys = {}
        try:
            api_keys_all = self.config.get("api_keys", {})
            if isinstance(api_keys_all, dict):
                aliases = {
                    self.name,
                    self.name.replace(".", ""),
                    self.name.replace(" ", ""),
                    self.name.replace(".", "").replace(" ", ""),
                }
                explicit_aliases = {
                    "Gate.io": ["GateIO", "gateio", "gate.io"],
                    "Kraken Pro": ["Kraken", "KrakenPro", "krakenpro", "kraken"],
                    "Binance.US": ["BinanceUS", "Binance.US", "binanceus"],
                    "Pionex.US": ["Pionex", "PionexUS", "Pionex.US", "pionexus"],
                    "LBank": ["LBank", "LBANK", "lbank"],
                }
                aliases.update(explicit_aliases.get(self.name, []))
                for key_name in aliases:
                    if key_name in api_keys_all and isinstance(api_keys_all.get(key_name), dict):
                        self.api_keys = api_keys_all.get(key_name) or {}
                        break
        except Exception:
            self.api_keys = {}

        if self.api_keys:
            logger.info(f"Р”Р»СЏ Р±РёСЂР¶Рё {self.name} РЅР°Р№РґРµРЅС‹ API РєР»СЋС‡Рё")

    async def check_connection(self, session: RetryClient) -> bool:
        try:
            response = await session.get(self.pairs_url, headers=self.headers, timeout=self.timeout)
            if response.status != 200:
                logger.warning(f"{self.name}: Connection check failed. Status: {response.status}")
                return False
            return True
        except Exception as e:
            import traceback
            logger.error(f"{self.name}: РћС€РёР±РєР° РїСЂРё РїСЂРѕРІРµСЂРєРµ СЃРѕРµРґРёРЅРµРЅРёСЏ: {e}\n{traceback.format_exc()}")
            return False

    async def _make_request(self, session: RetryClient, url: str, params: Optional[Dict[str, Any]] = None, method: str = "GET", headers: Optional[Dict[str, str]] = None) -> Optional[Any]:
        if not self.enabled:
            return None

        # Р•СЃР»Рё Р±РёСЂР¶Р° РЅРµРґР°РІРЅРѕ РІС‹РґР°РІР°Р»Р° РјРЅРѕРіРѕ РѕС€РёР±РѕРє вЂ” РІСЂРµРјРµРЅРЅРѕ РїСЂРѕРїСѓСЃРєР°РµРј Р·Р°РїСЂРѕСЃС‹
        current_time = time.time()
        if self.error_count >= self.max_consecutive_errors and self.last_error_time and current_time - self.last_error_time < self.error_timeout:
            remaining = int(self.error_timeout - (current_time - self.last_error_time))
            if (current_time - self._cooldown_warning_last_logged) >= self._cooldown_warning_interval_sec:
                logger.warning(f"{self.name}: Р‘РёСЂР¶Р° РІСЂРµРјРµРЅРЅРѕ РѕС‚РєР»СЋС‡РµРЅР° РёР·-Р·Р° РѕС€РёР±РѕРє. РџРѕРІС‚РѕСЂРЅР°СЏ РїРѕРїС‹С‚РєР° С‡РµСЂРµР· {remaining} СЃРµРє.")
                self._cooldown_warning_last_logged = current_time
            return None

        # РћР±СЉРµРґРёРЅСЏРµРј Р±Р°Р·РѕРІС‹Рµ Р·Р°РіРѕР»РѕРІРєРё СЃ РґРѕРїРѕР»РЅРёС‚РµР»СЊРЅС‹РјРё
        request_headers = self.headers.copy()
        if headers:
            request_headers.update(headers)

        for _attempt in range(1, self._max_retries + 2):  # 1..max_retries+1
          try:
            if method.upper() == "GET":
                response = await session.get(url, params=params, headers=request_headers, timeout=self.timeout)
            elif method.upper() == "POST":
                response = await session.post(url, json=params, headers=request_headers, timeout=self.timeout)
            else:
                logger.error(f"{self.name}: РќРµРїРѕРґРґРµСЂР¶РёРІР°РµРјС‹Р№ HTTP-РјРµС‚РѕРґ: {method}")
                return None

            if response.status == 403:
                logger.error(f"{self.name}: РћС€РёР±РєР° РґРѕСЃС‚СѓРїР° (403 Forbidden) РґР»СЏ URL {url}")
                self._handle_error("РћС€РёР±РєР° РґРѕСЃС‚СѓРїР° (403 Forbidden)")
                return None
            if response.status == 418:
                logger.error(f"{self.name}: РћС€РёР±РєР° API (418 I'm a teapot) РґР»СЏ URL {url}. Р’РѕР·РјРѕР¶РЅРѕ, РїСЂРµРІС‹С€РµРЅ Р»РёРјРёС‚ Р·Р°РїСЂРѕСЃРѕРІ.")
                self._handle_error("РћС€РёР±РєР° API (418 I'm a teapot)")
                try:
                    text = await response.text()
                    logger.debug(f"{self.name} 418 Response: {text[:500]}")
                except Exception:
                    pass
                return None
            if response.status != 200:
                try:
                    text = await response.text()
                    logger.warning(f"{self.name}: HTTP {response.status} for {url}: {text[:300]}")
                except Exception:
                    logger.warning(f"{self.name}: HTTP {response.status} for {url}")
                # 400 = bad params (wrong symbol), don't count as exchange-level error
                if response.status != 400:
                    self._handle_error(f"HTTP error: {response.status}")
                return None

            try:
                data = await response.json(content_type=None)
                # РЎР±СЂРѕСЃ СЃС‡РµС‚С‡РёРєР° РѕС€РёР±РѕРє РїСЂРё СѓСЃРїРµС€РЅРѕРј Р·Р°РїСЂРѕСЃРµ
                self.error_count = 0
                self.last_error_time = None
                self.error_timeout = 300
                self._cooldown_warning_last_logged = 0.0

                if self.config.get("log_api_responses", False):
                    logger.debug("[%s] API response preview from %s: %s", self.name, url, str(data)[:500])

                return data
            except json.JSONDecodeError:
                response_text = "РќРµ СѓРґР°Р»РѕСЃСЊ РїРѕР»СѓС‡РёС‚СЊ С‚РµРєСЃС‚ РѕС‚РІРµС‚Р°"
                try:
                    response_text = await response.text()
                except Exception:
                    pass
                logger.error(f"{self.name}: РћС€РёР±РєР° РґРµРєРѕРґРёСЂРѕРІР°РЅРёСЏ JSON РґР»СЏ URL {url}. Status: {response.status}. Response preview: {response_text[:500]}...")
                self._handle_error("РћС€РёР±РєР° РґРµРєРѕРґРёСЂРѕРІР°РЅРёСЏ JSON")
                return None

          except asyncio.TimeoutError as te:
            if _attempt <= self._max_retries:
                delay = self._retry_base_delay * (2 ** (_attempt - 1)) + random.uniform(0, 0.3)
                logger.warning(f"{self.name}: Timeout for {url}, retry {_attempt}/{self._max_retries} in {delay:.1f}s")
                await asyncio.sleep(delay)
                continue
            logger.error(f"{self.name}: Timeout for URL {url} after {self._max_retries} retries. Timeout={getattr(self.timeout, 'total', 'unknown')}s. Exception: {te}")
            self._handle_error("Request timeout")
            return None
          except ClientConnectorError as e:
            if _attempt <= self._max_retries:
                delay = self._retry_base_delay * (2 ** (_attempt - 1)) + random.uniform(0, 0.3)
                logger.warning(f"{self.name}: Connection error for {url}, retry {_attempt}/{self._max_retries} in {delay:.1f}s")
                await asyncio.sleep(delay)
                continue
            logger.error(f"{self.name}: Connection error for URL {url}: {e}")
            self._handle_error(f"Connection error: {e}")
            return None
          except ClientError as e:
            logger.error(f"{self.name}: aiohttp client error for URL {url}: {e}")
            self._handle_error(f"aiohttp client error: {e}")
            return None
          except Exception as e:
            logger.error(f"{self.name}: Unexpected error for URL {url}: {e}\n{traceback.format_exc()}")
            self._handle_error(f"Unexpected error: {e}")
            return None

    @property
    def available_pairs(self) -> Set[str]:
       """Property getter for available_pairs."""
       return self._available_pairs

    @available_pairs.setter
    def available_pairs(self, pairs: Set[str]):
       """Setter that avoids overwriting existing pairs with an empty result.

       If a child exchange returns an empty set due to a transient network issue,
       we keep the previous known pairs and log the situation instead of
       clearing data used by the rest of the system.
       """
       try:
           if pairs:
               self._available_pairs = set(pairs)
               logger.info(f"[{self.name}] РћР±РЅРѕРІР»РµРЅРѕ available_pairs: {len(self._available_pairs)} РїР°СЂ")
           else:
               # If the update returned an empty set, keep previous pairs and warn.
               logger.warning(f"[{self.name}] РџРѕР»СѓС‡РµРЅ РїСѓСЃС‚РѕР№ СЃРїРёСЃРѕРє РїР°СЂ вЂ” СЃРѕС…СЂР°РЅСЏРµРј РїСЂРµРґС‹РґСѓС‰РёРµ {len(self._available_pairs)} РїР°СЂ")
       except Exception as e:
           logger.error(f"[{self.name}] РћС€РёР±РєР° РїСЂРё СѓСЃС‚Р°РЅРѕРІРєРµ available_pairs: {e}")

    def _handle_error(self, error_msg: str):
       """
       РћР±СЂР°Р±Р°С‚С‹РІР°РµС‚ РѕС€РёР±РєСѓ Р·Р°РїСЂРѕСЃР°, СѓРІРµР»РёС‡РёРІР°СЏ СЃС‡РµС‚С‡РёРє РѕС€РёР±РѕРє Рё Р»РѕРіРёСЂСѓСЏ СЃРѕРѕР±С‰РµРЅРёРµ.

       Args:
           error_msg: РЎРѕРѕР±С‰РµРЅРёРµ РѕР± РѕС€РёР±РєРµ
       """
       # Don't keep incrementing once already in cooldown (concurrent requests)
       if self.error_count >= self.max_consecutive_errors:
           return
       self.error_count += 1
       self.last_error_time = time.time()
       # Exponential backoff when threshold reached: 300s (fixed)
       if self.error_count >= self.max_consecutive_errors:
           self.error_timeout = 300
           logger.error(f"{self.name}: {error_msg}. Р‘РёСЂР¶Р° РІСЂРµРјРµРЅРЅРѕ РѕС‚РєР»СЋС‡РµРЅР° РїРѕСЃР»Рµ {self.error_count} РїРѕСЃР»РµРґРѕРІР°С‚РµР»СЊРЅС‹С… РѕС€РёР±РѕРє.")
       else:
           logger.warning(f"{self.name}: {error_msg}. РћС€РёР±РєР° {self.error_count}/{self.max_consecutive_errors}.")

    async def get_all_pairs(self, session: RetryClient) -> Set[str]:
       """
       РџРѕР»СѓС‡Р°РµС‚ СЃРїРёСЃРѕРє РІСЃРµС… С‚РѕСЂРіРѕРІС‹С… РїР°СЂ СЃ Р±РёСЂР¶Рё.

       Args:
           session: РЎРµСЃСЃРёСЏ HTTP-РєР»РёРµРЅС‚Р°

       Returns:
           РњРЅРѕР¶РµСЃС‚РІРѕ СЃРёРјРІРѕР»РѕРІ С‚РѕСЂРіРѕРІС‹С… РїР°СЂ
       """
       # Р­С‚РѕС‚ РјРµС‚РѕРґ РґРѕР»Р¶РµРЅ Р±С‹С‚СЊ РїРµСЂРµРѕРїСЂРµРґРµР»РµРЅ РІ РґРѕС‡РµСЂРЅРёС… РєР»Р°СЃСЃР°С…
       raise NotImplementedError("РњРµС‚РѕРґ get_all_pairs РґРѕР»Р¶РµРЅ Р±С‹С‚СЊ РїРµСЂРµРѕРїСЂРµРґРµР»РµРЅ РІ РґРѕС‡РµСЂРЅРµРј РєР»Р°СЃСЃРµ")

    async def get_all_tickers(self, session: RetryClient) -> Dict[str, float]:
       """
       РџРѕР»СѓС‡Р°РµС‚ С†РµРЅС‹ РІСЃРµС… С‚РѕСЂРіРѕРІС‹С… РїР°СЂ СЃ Р±РёСЂР¶Рё.

       Args:
           session: РЎРµСЃСЃРёСЏ HTTP-РєР»РёРµРЅС‚Р°

       Returns:
           РЎР»РѕРІР°СЂСЊ {СЃРёРјРІРѕР»: С†РµРЅР°} РґР»СЏ РІСЃРµС… С‚РѕСЂРіРѕРІС‹С… РїР°СЂ

       Note:
           Р­С‚РѕС‚ РјРµС‚РѕРґ СѓСЃС‚Р°СЂРµРІР°РµС‚ Рё Р±СѓРґРµС‚ Р·Р°РјРµРЅРµРЅ РЅР° get_order_books
       """
       # Р­С‚РѕС‚ РјРµС‚РѕРґ РґРѕР»Р¶РµРЅ Р±С‹С‚СЊ РїРµСЂРµРѕРїСЂРµРґРµР»РµРЅ РІ РґРѕС‡РµСЂРЅРёС… РєР»Р°СЃСЃР°С…
       raise NotImplementedError("РњРµС‚РѕРґ get_all_tickers РґРѕР»Р¶РµРЅ Р±С‹С‚СЊ РїРµСЂРµРѕРїСЂРµРґРµР»РµРЅ РІ РґРѕС‡РµСЂРЅРµРј РєР»Р°СЃСЃРµ")

    async def get_order_books(self, session: RetryClient, symbols: List[str]) -> Dict[str, Dict[str, float]]:
       """
       РџРѕР»СѓС‡Р°РµС‚ РґР°РЅРЅС‹Рµ РєРЅРёРіРё РѕСЂРґРµСЂРѕРІ РґР»СЏ СѓРєР°Р·Р°РЅРЅС‹С… СЃРёРјРІРѕР»РѕРІ.

       Args:
           session: РЎРµСЃСЃРёСЏ HTTP-РєР»РёРµРЅС‚Р°
           symbols: РЎРїРёСЃРѕРє СЃРёРјРІРѕР»РѕРІ РґР»СЏ РїРѕР»СѓС‡РµРЅРёСЏ РєРЅРёРіРё РѕСЂРґРµСЂРѕРІ

       Returns:
           РЎР»РѕРІР°СЂСЊ {СЃРёРјРІРѕР»: {bid: С†РµРЅР°_РїРѕРєСѓРїРєРё, ask: С†РµРЅР°_РїСЂРѕРґР°Р¶Рё, bid_volume: РѕР±СЉРµРј_РїРѕРєСѓРїРєРё, ask_volume: РѕР±СЉРµРј_РїСЂРѕРґР°Р¶Рё}}
       """
       # Р­С‚РѕС‚ РјРµС‚РѕРґ РґРѕР»Р¶РµРЅ Р±С‹С‚СЊ РїРµСЂРµРѕРїСЂРµРґРµР»РµРЅ РІ РґРѕС‡РµСЂРЅРёС… РєР»Р°СЃСЃР°С…
       raise NotImplementedError("РњРµС‚РѕРґ get_order_books РґРѕР»Р¶РµРЅ Р±С‹С‚СЊ РїРµСЂРµРѕРїСЂРµРґРµР»РµРЅ РІ РґРѕС‡РµСЂРЅРµРј РєР»Р°СЃСЃРµ")

    async def get_volumes(self, session: RetryClient, symbols: List[str]) -> Dict[str, float]:
       """
       РџРѕР»СѓС‡Р°РµС‚ РѕР±СЉРµРјС‹ С‚РѕСЂРіРѕРІ РґР»СЏ СѓРєР°Р·Р°РЅРЅС‹С… СЃРёРјРІРѕР»РѕРІ.

       Args:
           session: РЎРµСЃСЃРёСЏ HTTP-РєР»РёРµРЅС‚Р°
           symbols: РЎРїРёСЃРѕРє СЃРёРјРІРѕР»РѕРІ РґР»СЏ РїРѕР»СѓС‡РµРЅРёСЏ РѕР±СЉРµРјРѕРІ

       Returns:
           РЎР»РѕРІР°СЂСЊ {СЃРёРјРІРѕР»: РѕР±СЉРµРј_РІ_USD}
       """
       # Р­С‚РѕС‚ РјРµС‚РѕРґ РґРѕР»Р¶РµРЅ Р±С‹С‚СЊ РїРµСЂРµРѕРїСЂРµРґРµР»РµРЅ РІ РґРѕС‡РµСЂРЅРёС… РєР»Р°СЃСЃР°С…
       # РџРѕ СѓРјРѕР»С‡Р°РЅРёСЋ РІРѕР·РІСЂР°С‰Р°РµРј РїСѓСЃС‚РѕР№ СЃР»РѕРІР°СЂСЊ
       logger.warning(f"{self.name}: РњРµС‚РѕРґ get_volumes РЅРµ СЂРµР°Р»РёР·РѕРІР°РЅ, РІРѕР·РІСЂР°С‰Р°РµРј РїСѓСЃС‚РѕР№ СЃР»РѕРІР°СЂСЊ")
       return {}

    def _format_symbol_for_orderbook(self, symbol: str) -> str:
       """
       Р¤РѕСЂРјР°С‚РёСЂСѓРµС‚ СЃРёРјРІРѕР» РґР»СЏ РёСЃРїРѕР»СЊР·РѕРІР°РЅРёСЏ РІ Р·Р°РїСЂРѕСЃРµ РєРЅРёРіРё РѕСЂРґРµСЂРѕРІ.

       Args:
           symbol: РЎРёРјРІРѕР» С‚РѕСЂРіРѕРІРѕР№ РїР°СЂС‹

       Returns:
           РћС‚С„РѕСЂРјР°С‚РёСЂРѕРІР°РЅРЅС‹Р№ СЃРёРјРІРѕР»
       """
       # Р­С‚РѕС‚ РјРµС‚РѕРґ РјРѕР¶РµС‚ Р±С‹С‚СЊ РїРµСЂРµРѕРїСЂРµРґРµР»РµРЅ РІ РґРѕС‡РµСЂРЅРёС… РєР»Р°СЃСЃР°С…
       return symbol.upper()

    def _get_auth_headers(self) -> Dict[str, str]:
       """
       РџРѕР»СѓС‡Р°РµС‚ Р·Р°РіРѕР»РѕРІРєРё РґР»СЏ Р°РІС‚РѕСЂРёР·Р°С†РёРё РЅР° Р±РёСЂР¶Рµ.

       Returns:
           РЎР»РѕРІР°СЂСЊ СЃ Р·Р°РіРѕР»РѕРІРєР°РјРё Р°РІС‚РѕСЂРёР·Р°С†РёРё

       Note:
           Р­С‚РѕС‚ РјРµС‚РѕРґ РґРѕР»Р¶РµРЅ Р±С‹С‚СЊ РїРµСЂРµРѕРїСЂРµРґРµР»РµРЅ РІ РґРѕС‡РµСЂРЅРµРј РєР»Р°СЃСЃРµ,
           РµСЃР»Рё Р±РёСЂР¶Р° С‚СЂРµР±СѓРµС‚ Р°РІС‚РѕСЂРёР·Р°С†РёРё РґР»СЏ РґРѕСЃС‚СѓРїР° Рє API.
       """
       return {}
