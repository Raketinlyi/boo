"""
Unified Exchange Info Module
Fetches coin information (contract, network, deposit/withdrawal status) from exchanges.

Mix of public and authenticated endpoints is used depending on exchange support.
"""
import asyncio
from contextlib import asynccontextmanager
import hashlib
import hmac
import time
import json
import logging
import os
import threading
from typing import AsyncIterator, Dict, List, Any, Optional
import aiohttp

from database import Database

# Load API keys
API_KEYS_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "api_keys_PRIVATE.json")
ROOT_DIR = os.path.dirname(os.path.dirname(__file__))
EXCHANGE_METADATA_DB_PATH = os.path.join(ROOT_DIR, "arbitrage.db")
# Freshness window for deposit/withdraw status. Arbitrage requires current
# data — exchanges can toggle deposit/withdraw mid-maintenance. If a row is
# older than this, the caller refetches from the network (lazy refresh).
DEFAULT_EXCHANGE_METADATA_MAX_AGE_SEC = 10 * 60  # 10 minutes
# Hard ceiling for the "last-resort" persisted fallback (served only when the
# live fetch fails — e.g. geo-block, 429). Anything older is dropped so UI
# shows "НЕИЗВЕСТНО" instead of a stale lie.
PERSISTENT_FALLBACK_MAX_AGE_SEC = 30 * 60  # 30 minutes
# In-process LRU cache TTL. Caps how long the same payload can be served to
# different callers without re-checking the age of the persisted copy.
EXCHANGE_METADATA_MEMORY_TTL_SEC = 5 * 60  # 5 minutes

ENV_KEY_MAP = {
    "Bybit": {
        "api_key": "BYBIT_API_KEY",
        "secret_key": "BYBIT_SECRET_KEY",
    },
    "CoinEx": {
        "access_id": "COINEX_ACCESS_ID",
        "secret_key": "COINEX_SECRET_KEY",
    },
    "GateIO": {
        "api_key": "GATEIO_API_KEY",
        "secret_key": "GATEIO_SECRET_KEY",
    },
    "MEXC": {
        "access_key": "MEXC_ACCESS_KEY",
        "secret_key": "MEXC_SECRET_KEY",
    },
    "Bitget": {
        "api_key": "BITGET_API_KEY",
        "secret_key": "BITGET_SECRET_KEY",
        "passphrase": "BITGET_PASSPHRASE",
    },
    "KuCoin": {
        "api_key": "KUCOIN_API_KEY",
        "secret_key": "KUCOIN_SECRET_KEY",
        "passphrase": "KUCOIN_PASSPHRASE",
    },
    "OKX": {
        "api_key": "OKX_API_KEY",
        "secret_key": "OKX_SECRET_KEY",
        "passphrase": "OKX_PASSPHRASE",
    },
}

def load_api_keys() -> Dict:
    """Load API keys from file"""
    data: Dict[str, Any] = {}
    try:
        if os.path.exists(API_KEYS_PATH):
            with open(API_KEYS_PATH, 'r') as f:
                loaded = json.load(f)
                if isinstance(loaded, dict):
                    data = loaded
    except Exception as e:
        logging.warning(f"Could not load API keys: {e}")

    for exchange, mapping in ENV_KEY_MAP.items():
        target = data.setdefault(exchange, {})
        if not isinstance(target, dict):
            target = {}
            data[exchange] = target
        for field, env_name in mapping.items():
            env_value = str(os.getenv(env_name, "") or "").strip()
            if env_value and not str(target.get(field, "") or "").strip():
                target[field] = env_value

    return data

# Chain name mapping for DEX links
CHAIN_TO_NETWORK = {
    # Ethereum-based
    'ETH': 'eth', 'ERC20': 'eth', 'ETHEREUM': 'eth',
    'BSC': 'bsc', 'BEP20': 'bsc', 'BNB SMART CHAIN': 'bsc',
    'ARBITRUM': 'arbitrum', 'ARB': 'arbitrum', 'ARBI': 'arbitrum', 'ARBEVM': 'arbitrum',
    'OPTIMISM': 'optimism', 'OP': 'optimism', 'OPETH': 'optimism',
    'POLYGON': 'polygon', 'MATIC': 'polygon',
    'BASE': 'base', 'BASEEVM': 'base',
    'LINEA': 'linea', 'LINEAETH': 'linea',
    'ZKSYNC': 'zksync-era', 'ZKV2': 'zksync-era', 'ZKSERA': 'zksync-era',
    'AVALANCHE': 'avax', 'AVAX': 'avax', 'AVAX_C': 'avax', 'AVA_C': 'avax', 'CAVAX': 'avax',
    # Other L1s
    'SOL': 'solana', 'SOLANA': 'solana',
    'TRX': 'tron', 'TRON': 'tron', 'TRC20': 'tron',
    'TON': 'ton', 'TONCOIN': 'ton',
    'TON_MAINNET': 'ton', 'THE_OPEN_NETWORK': 'ton', 'OPEN_NETWORK': 'ton',
    'NEAR': 'near', 'NEAR_PROTOCOL': 'near', 'NEAR_MAINNET': 'near',
    'APT': 'aptos', 'APTOS': 'aptos',
    'APTOS_MAINNET': 'aptos', 'APT_MAINNET': 'aptos',
    'SUI': 'sui', 'SUI_MAINNET': 'sui', 'SUI_NETWORK': 'sui',
    # Native coins (no DEX needed)
    'BTC': None, 'BITCOIN': None,
    'DOGE': None, 'DOGECOIN': None,
    'XRP': None, 'RIPPLE': None,
}

def get_dex_network(chain: str) -> Optional[str]:
    """Convert exchange chain name to DEX network ID"""
    if not chain:
        return None
    chain_upper = chain.upper().replace(' ', '_').replace('-', '_')
    return CHAIN_TO_NETWORK.get(chain_upper)


class ExchangeInfoFetcher:
    """Fetches coin info from exchanges using authenticated APIs"""
    
    def __init__(self):
        self.api_keys = load_api_keys()
        self.db = Database(EXCHANGE_METADATA_DB_PATH)
        self._warned_missing_keys = set()
        self._warned_http = set()
        self._warned_api_errors = set()
        self._warned_notes = set()
        self._memory_cache: Dict[str, Dict[str, Any]] = {}
        self._cache_lock = threading.Lock()

    def _format_exception(self, exc: Exception) -> str:
        message = str(exc).strip()
        return message or exc.__class__.__name__

    @asynccontextmanager
    async def _session_scope(
        self,
        session: Optional[aiohttp.ClientSession] = None,
    ) -> AsyncIterator[aiohttp.ClientSession]:
        if session is not None:
            yield session
            return

        created = aiohttp.ClientSession()
        try:
            yield created
        finally:
            if not created.closed:
                await created.close()

    def _warn_missing(self, exchange: str) -> None:
        if exchange not in self._warned_missing_keys:
            logging.warning(f"{exchange}: API keys missing; deposit/withdraw status will be unknown.")
            self._warned_missing_keys.add(exchange)

    def _warn_http(self, exchange: str, status: int) -> None:
        key = f"{exchange}:{status}"
        if key not in self._warned_http:
            logging.warning(f"{exchange}: HTTP {status} from asset status endpoint.")
            self._warned_http.add(key)

    def _warn_api_error(self, exchange: str, code: str) -> None:
        key = f"{exchange}:{code}"
        if key not in self._warned_api_errors:
            if exchange == "Bybit" and str(code) == "33004":
                logging.warning(f"{exchange}: asset status endpoint is unavailable for the current IP/region or account scope.")
            else:
                logging.warning(f"{exchange}: API error code {code} from asset status endpoint.")
            self._warned_api_errors.add(key)

    def _warn_note(self, exchange: str, note: str, *, key: Optional[str] = None) -> None:
        dedupe_key = key or f"{exchange}:{note}"
        if dedupe_key not in self._warned_notes:
            logging.warning(f"{exchange}: {note}")
            self._warned_notes.add(dedupe_key)

    def _log_info_error(self, exchange: str, ticker: str, exc: Exception) -> None:
        message = self._format_exception(exc)
        if isinstance(exc, TimeoutError) or exc.__class__.__name__ == "TimeoutError":
            self._warn_note(exchange, f"info timeout for {ticker}: {message}", key=f"timeout:{exchange}:{ticker}")
            return
        logging.error(f"{exchange} info error for {ticker}: {message}")

    async def close(self):
        return None

    def _normalize_ticker(self, ticker: str) -> str:
        return str(ticker or "").strip().upper()

    def _build_payload(
        self,
        ticker: str,
        rows: List[Dict[str, Any]],
        *,
        cache_source: str,
        refreshed_at: Optional[int] = None,
        cache_stale: bool = False,
    ) -> Dict[str, Any]:
        all_data = []
        contracts = {}
        for item in rows:
            if not isinstance(item, dict):
                continue
            all_data.append(item)
            chain = item.get('chain', '')
            contract = item.get('contract')
            if chain and contract and contract not in ('Native coin', 'Native', None):
                network = get_dex_network(chain)
                if network:
                    contracts[network] = contract

        dex_links = []
        for network, contract in contracts.items():
            if network and contract:
                dex_links.append({
                    'network': network,
                    'contract': contract,
                    'geckoterminal': f"https://www.geckoterminal.com/{network}/tokens/{contract}",
                    'dexscreener': f"https://dexscreener.com/{network}/{contract}",
                })

        return {
            'ticker': ticker,
            'exchanges': all_data,
            'dex_links': dex_links,
            'contracts': contracts,
            'cache_source': cache_source,
            'cache_refreshed_at': int(refreshed_at or time.time()),
            'cache_stale': bool(cache_stale),
        }

    def _get_memory_cached_payload(self, ticker: str, max_age_sec: int) -> Optional[Dict[str, Any]]:
        ticker_u = self._normalize_ticker(ticker)
        if not ticker_u:
            return None
        with self._cache_lock:
            cached = self._memory_cache.get(ticker_u)
            if not cached:
                return None
            ts = float(cached.get('ts', 0.0) or 0.0)
            if ts <= 0 or (time.time() - ts) > float(min(max_age_sec, EXCHANGE_METADATA_MEMORY_TTL_SEC)):
                self._memory_cache.pop(ticker_u, None)
                return None
            payload = cached.get('payload')
            return dict(payload) if isinstance(payload, dict) else None

    def _set_memory_cached_payload(self, ticker: str, payload: Dict[str, Any]) -> None:
        ticker_u = self._normalize_ticker(ticker)
        if not ticker_u or not isinstance(payload, dict):
            return
        with self._cache_lock:
            self._memory_cache[ticker_u] = {
                'ts': time.time(),
                'payload': dict(payload),
            }
            if len(self._memory_cache) > 2048:
                oldest_key = min(self._memory_cache.items(), key=lambda item: float(item[1].get('ts', 0.0) or 0.0))[0]
                self._memory_cache.pop(oldest_key, None)

    def _get_persistent_cached_payload(self, ticker: str, max_age_sec: Optional[int]) -> Optional[Dict[str, Any]]:
        ticker_u = self._normalize_ticker(ticker)
        cached = self.db.get_exchange_asset_metadata(ticker_u, max_age_sec=max_age_sec)
        if not cached:
            return None
        rows = cached.get('rows') or []
        refreshed_at = int(cached.get('refreshed_at') or 0)
        return self._build_payload(
            ticker_u,
            list(rows) if isinstance(rows, list) else [],
            cache_source='persistent',
            refreshed_at=refreshed_at,
            cache_stale=False,
        )

    def is_asset_fresh_in_cache(self, ticker: str, max_age_sec: int = DEFAULT_EXCHANGE_METADATA_MAX_AGE_SEC) -> bool:
        ticker_u = self._normalize_ticker(ticker)
        if not ticker_u:
            return False
        return self.db.is_exchange_asset_metadata_fresh(ticker_u, max_age_sec)

    async def _fetch_all_exchange_info_rows(
        self,
        ticker: str,
        session: Optional[aiohttp.ClientSession] = None,
    ) -> List[Dict[str, Any]]:
        async with self._session_scope(session) as client:
            tasks = [
                self.get_coinex_info(ticker, client),
                self.get_mexc_info(ticker, client),
                self.get_gateio_info(ticker, client),
                self.get_bybit_info(ticker, client),
                self.get_bitget_info(ticker, client),
                self.get_kucoin_info(ticker, client),
                self.get_okx_info(ticker, client),
                self.get_lbank_info(ticker, client),
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)

        rows: List[Dict[str, Any]] = []
        for result in results:
            if isinstance(result, Exception):
                continue
            if isinstance(result, list):
                for item in result:
                    if isinstance(item, dict):
                        rows.append(item)
        return rows

    async def warm_assets(
        self,
        assets: List[str],
        *,
        max_age_sec: int = DEFAULT_EXCHANGE_METADATA_MAX_AGE_SEC,
        pause_sec: float = 2.0,
        force_refresh: bool = False,
        limit: Optional[int] = None,
    ) -> Dict[str, Any]:
        seen = set()
        selected_assets: List[str] = []
        for asset in assets or []:
            ticker_u = self._normalize_ticker(asset)
            if not ticker_u or ticker_u in seen:
                continue
            seen.add(ticker_u)
            selected_assets.append(ticker_u)
            if limit is not None and len(selected_assets) >= int(limit):
                break

        stats = {
            'assets_requested': len(selected_assets),
            'assets_processed': 0,
            'network_fetches': 0,
            'cache_hits': 0,
            'errors': 0,
        }
        async with self._session_scope() as client:
            for ticker_u in selected_assets:
                try:
                    payload = await self.get_all_exchange_info(
                        ticker_u,
                        force_refresh=force_refresh,
                        max_age_sec=max_age_sec,
                        session=client,
                    )
                    stats['assets_processed'] += 1
                    if str(payload.get('cache_source') or '') == 'network':
                        stats['network_fetches'] += 1
                    else:
                        stats['cache_hits'] += 1
                except Exception:
                    stats['errors'] += 1
                if pause_sec > 0:
                    await asyncio.sleep(pause_sec)
        return stats
    
    # ==================== CoinEx V2 API ====================
    def _coinex_signature(self, method: str, path: str, body: str, timestamp: int) -> str:
        keys = self.api_keys.get('CoinEx', {})
        secret = keys.get('secret_key', '')
        prepared = f"{method}{path}{body}{timestamp}"
        return hmac.new(secret.encode(), prepared.encode(), hashlib.sha256).hexdigest().lower()
    
    async def get_coinex_info(self, ticker: str, session: Optional[aiohttp.ClientSession] = None) -> List[Dict]:
        """Get coin info from CoinEx (public endpoint; auth optional).

        Endpoint currently works without keys. If keys are present, we still send
        signed headers, but we do not require them.
        """
        try:
            async with self._session_scope(session) as client:
                path = f"/v2/assets/deposit-withdraw-config?ccy={ticker}"
                url = f"https://api.coinex.com{path}"
                async with client.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    if resp.status != 200:
                        self._warn_http("CoinEx", resp.status)
                        return []
                    data = await resp.json()
                    if data.get('code') != 0:
                        message = str(data.get('message') or '').strip().lower()
                        if data.get('code') == 11002 and 'asset not found' in message:
                            return []
                        self._warn_api_error("CoinEx", str(data.get('code')))
                        return []
                    
                    import re
                    def _extract_contract_from_url(u: str) -> str:
                        if not isinstance(u, str):
                            return ''
                        u = u.strip()
                        if not u:
                            return ''
                        m = re.search(r'/(?:token|address|account|coins|mint)[s]?/([^/?#]+)', u, re.IGNORECASE)
                        candidate = (m.group(1) if m else u.rstrip('/').rsplit('/', 1)[-1]) or ''
                        candidate = candidate.strip()
                        if not candidate:
                            return ''
                        # Filter out obvious non-addresses (the url may point
                        # to a generic coin page like "/coins/spark").
                        if re.fullmatch(r'0x[0-9a-fA-F]{40}', candidate):
                            return candidate
                        if re.fullmatch(r'[1-9A-HJ-NP-Za-km-z]{32,44}', candidate):  # Solana-ish
                            return candidate
                        if '::' in candidate:  # Sui/Aptos object::module::name
                            return candidate
                        if re.fullmatch(r'[0-9a-fA-F]{40,}', candidate):
                            return candidate
                        return ''

                    result = []
                    for chain in data.get('data', {}).get('chains', []):
                        contract = (
                            chain.get('contract_address')
                            or chain.get('identity')
                            or _extract_contract_from_url(chain.get('explorer_asset_url') or '')
                        )
                        result.append({
                            'exchange': 'CoinEx',
                            'asset': ticker,
                            'chain': chain.get('chain', '-'),
                            'contract': contract or None,
                            'deposit_enabled': chain.get('deposit_enabled', False),
                            'withdraw_enabled': chain.get('withdraw_enabled', False),
                            'withdraw_fee': chain.get('withdrawal_fee') or chain.get('withdraw_fee'),
                            'min_withdraw': chain.get('min_withdraw_amount') or chain.get('withdraw_min'),
                        })
                    return result
        except Exception as e:
            self._log_info_error("CoinEx", ticker, e)
            return []
    
    # ==================== MEXC V3 API ====================
    def _mexc_signature(self, query_string: str) -> str:
        keys = self.api_keys.get('MEXC', {})
        secret = keys.get('secret_key', '')
        return hmac.new(secret.encode(), query_string.encode(), hashlib.sha256).hexdigest()
    
    async def get_mexc_info(self, ticker: str, session: Optional[aiohttp.ClientSession] = None) -> List[Dict]:
        """Get coin info from MEXC using V3 authenticated API"""
        keys = self.api_keys.get('MEXC', {})
        if not keys.get('access_key') or not keys.get('secret_key'):
            self._warn_missing("MEXC")
            return []
        
        try:
            async with self._session_scope(session) as client:
                timestamp = int(time.time() * 1000)
                query = f"timestamp={timestamp}"
                signature = self._mexc_signature(query)
                
                headers = {
                    "X-MEXC-APIKEY": keys['access_key'],
                }
                
                url = f"https://api.mexc.com/api/v3/capital/config/getall?{query}&signature={signature}"
                async with client.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                    if resp.status != 200:
                        self._warn_http("MEXC", resp.status)
                        return []
                    all_coins = await resp.json(content_type=None)
                    
                    result = []
                    for coin_data in all_coins:
                        if coin_data.get('coin', '').upper() != ticker.upper():
                            continue
                        for net in coin_data.get('networkList', []):
                            result.append({
                                'exchange': 'MEXC',
                                'asset': ticker,
                                'chain': net.get('network', '-'),
                                'contract': net.get('contract'),
                                'deposit_enabled': net.get('depositEnable', False),
                                'withdraw_enabled': net.get('withdrawEnable', False),
                                'withdraw_fee': net.get('withdrawFee'),
                                'min_withdraw': net.get('withdrawMin'),
                            })
                        break
                    return result
        except Exception as e:
            self._log_info_error("MEXC", ticker, e)
            return []
    
    # ==================== Gate.io V4 API ====================
    def _gateio_signature(self, method: str, url: str, query: str, body: str, timestamp: str) -> str:
        keys = self.api_keys.get('GateIO', {})
        secret = keys.get('secret_key', '')
        body_hash = hashlib.sha512(body.encode()).hexdigest()
        sign_str = f"{method}\n{url}\n{query}\n{body_hash}\n{timestamp}"
        return hmac.new(secret.encode(), sign_str.encode(), hashlib.sha512).hexdigest()
    
    async def get_gateio_info(self, ticker: str, session: Optional[aiohttp.ClientSession] = None) -> List[Dict]:
        """Get coin info from Gate.io currency chains.

        Primary source:   GET /api/v4/wallet/currency_chains?currency=X (per-chain
                          flags: is_deposit_disabled / is_withdraw_disabled).
        Fallback source:  GET /api/v4/spot/currencies/X (per-chain flags:
                          deposit_disabled / withdraw_disabled). Needed when the
                          primary returns HTTP 400 for weird/delisted tickers
                          (e.g. TIME) or responds with an empty list while the
                          /spot endpoint still has the truth.

        Both endpoints are public (no auth required).
        """
        async def _try_wallet_chains(client: aiohttp.ClientSession) -> List[Dict]:
            url = f"https://api.gateio.ws/api/v4/wallet/currency_chains?currency={ticker}"
            async with client.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status != 200:
                    try:
                        payload = await resp.json(content_type=None)
                    except Exception:
                        payload = {}
                    label = str((payload or {}).get('label') or '').strip().upper()
                    message = str((payload or {}).get('message') or '').strip().lower()
                    # 400 INVALID_CURRENCY is a normal "unknown coin" response.
                    # Do not log an HTTP warning — callers will fall through to
                    # the /spot/currencies fallback below.
                    if not (resp.status == 400 and (
                        label == 'INVALID_CURRENCY'
                        or 'invalid currency' in message
                        or 'delisted coin' in message
                    )):
                        self._warn_http("Gate.io", resp.status)
                    return []
                chains = await resp.json(content_type=None)

                out: List[Dict] = []
                for chain in chains or []:
                    out.append({
                        'exchange': 'Gate.io',
                        'asset': ticker,
                        'chain': chain.get('chain', '-'),
                        'contract': chain.get('contract_address'),
                        'deposit_enabled': not chain.get('is_deposit_disabled', True),
                        'withdraw_enabled': not chain.get('is_withdraw_disabled', True),
                        'withdraw_fee': chain.get('withdraw_fix') or chain.get('withdraw_percent'),
                        'min_withdraw': chain.get('withdraw_min'),
                    })
                return out

        async def _try_spot_currency(client: aiohttp.ClientSession) -> List[Dict]:
            url = f"https://api.gateio.ws/api/v4/spot/currencies/{ticker}"
            async with client.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status != 200:
                    return []
                payload = await resp.json(content_type=None)
                if not isinstance(payload, dict) or not payload.get('currency'):
                    return []

                # Top-level flags apply to the currency as a whole. Per-chain
                # overrides live in payload["chains"][*].{deposit,withdraw}_disabled.
                top_dep_disabled = bool(payload.get('deposit_disabled'))
                top_wd_disabled = bool(payload.get('withdraw_disabled'))
                top_delisted = bool(payload.get('delisted'))
                top_trade_disabled = bool(payload.get('trade_disabled'))

                chains_list = payload.get('chains') or []
                if not chains_list:
                    # No chain breakdown — surface a single generic row so the
                    # UI still sees Gate.io flags instead of "unknown".
                    return [{
                        'exchange': 'Gate.io',
                        'asset': ticker,
                        'chain': '-',
                        'contract': None,
                        'deposit_enabled': not (top_dep_disabled or top_delisted),
                        'withdraw_enabled': not (top_wd_disabled or top_delisted),
                        'withdraw_fee': None,
                        'min_withdraw': None,
                    }]

                out: List[Dict] = []
                for chain in chains_list:
                    if not isinstance(chain, dict):
                        continue
                    chain_dep = bool(chain.get('deposit_disabled') or top_dep_disabled or top_delisted)
                    chain_wd = bool(chain.get('withdraw_disabled') or top_wd_disabled or top_delisted)
                    out.append({
                        'exchange': 'Gate.io',
                        'asset': ticker,
                        'chain': chain.get('name', '-'),
                        'contract': chain.get('addr') or None,
                        'deposit_enabled': not chain_dep,
                        'withdraw_enabled': not chain_wd,
                        'withdraw_fee': None,
                        'min_withdraw': None,
                    })
                # If trading is globally disabled we keep the per-chain rows
                # as-is but annotate via the 'trade_disabled' notes — UI does
                # not surface this yet, and the per-chain flags already cover
                # the deposit/withdraw bug we set out to fix.
                _ = top_trade_disabled
                return out

        try:
            async with self._session_scope(session) as client:
                primary = await _try_wallet_chains(client)
                if primary:
                    return primary
                # Primary returned nothing (400 / empty). Try /spot/currencies.
                fallback = await _try_spot_currency(client)
                return fallback
        except Exception as e:
            self._log_info_error("Gate.io", ticker, e)
            return []
    
    # ==================== Bybit V5 API ====================
    def _bybit_signature(self, timestamp: str, params: str) -> str:
        keys = self.api_keys.get('Bybit', {})
        secret = keys.get('secret_key', '')
        recv_window = "5000"
        sign_str = f"{timestamp}{keys.get('api_key', '')}{recv_window}{params}"
        return hmac.new(secret.encode(), sign_str.encode(), hashlib.sha256).hexdigest()
    
    async def get_bybit_info(self, ticker: str, session: Optional[aiohttp.ClientSession] = None) -> List[Dict]:
        """Get coin info from Bybit using V5 authenticated API"""
        keys = self.api_keys.get('Bybit', {})
        if not keys.get('api_key') or not keys.get('secret_key'):
            self._warn_missing("Bybit")
            return []
        
        try:
            async with self._session_scope(session) as client:
                timestamp = str(int(time.time() * 1000))
                params = f"coin={ticker}"
                signature = self._bybit_signature(timestamp, params)
                
                headers = {
                    "X-BAPI-API-KEY": keys['api_key'],
                    "X-BAPI-TIMESTAMP": timestamp,
                    "X-BAPI-SIGN": signature,
                    "X-BAPI-SIGN-TYPE": "2",
                    "X-BAPI-RECV-WINDOW": "5000",
                }
                
                url = f"https://api.bybit.com/v5/asset/coin/query-info?{params}"
                async with client.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    if resp.status != 200:
                        body_text = ""
                        try:
                            body_text = (await resp.text())[:500]
                        except Exception:
                            body_text = ""
                        if resp.status == 403 and "cloudfront" in body_text.lower() and "block access from your country" in body_text.lower():
                            self._warn_note("Bybit", "asset status endpoint is geo-blocked for the current IP/region.", key="Bybit:geo-blocked")
                            return []
                        self._warn_http("Bybit", resp.status)
                        return []
                    data = await resp.json(content_type=None)
                    if data.get('retCode') != 0:
                        self._warn_api_error("Bybit", str(data.get('retCode')))
                        return []
                    
                    result = []
                    for row in data.get('result', {}).get('rows', []):
                        if row.get('coin', '').upper() != ticker.upper():
                            continue
                        for chain in row.get('chains', []):
                            result.append({
                                'exchange': 'Bybit',
                                'asset': ticker,
                                'chain': chain.get('chain', '-'),
                                'contract': chain.get('contractAddress'),
                                'deposit_enabled': chain.get('chainDeposit') == '1',
                                'withdraw_enabled': chain.get('chainWithdraw') == '1',
                                'withdraw_fee': chain.get('withdrawFee'),
                                'min_withdraw': chain.get('withdrawMin'),
                            })
                    return result
        except Exception as e:
            self._log_info_error("Bybit", ticker, e)
            return []

    # ==================== Bitget Public API V2 ====================
    async def get_bitget_info(self, ticker: str, session: Optional[aiohttp.ClientSession] = None) -> List[Dict]:
        """Get coin info from Bitget using the public spot coin endpoint."""
        try:
            async with self._session_scope(session) as client:
                url = f"https://api.bitget.com/api/v2/spot/public/coins?coin={ticker}"
                async with client.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    if resp.status != 200:
                        self._warn_http("Bitget", resp.status)
                        return []
                    data = await resp.json(content_type=None)
                    if str(data.get('code') or '') != '00000':
                        self._warn_api_error("Bitget", str(data.get('code')))
                        return []

                    result = []
                    for row in data.get('data', []):
                        if str(row.get('coin') or '').upper() != ticker.upper():
                            continue
                        for chain in row.get('chains', []):
                            result.append({
                                'exchange': 'Bitget',
                                'asset': ticker,
                                'chain': chain.get('chain', '-'),
                                'contract': chain.get('contractAddress'),
                                'deposit_enabled': str(chain.get('rechargeable')).lower() == 'true',
                                'withdraw_enabled': str(chain.get('withdrawable')).lower() == 'true',
                                'withdraw_fee': chain.get('withdrawFee'),
                                'min_withdraw': chain.get('minWithdrawAmount'),
                            })
                    return result
        except Exception as e:
            self._log_info_error("Bitget", ticker, e)
            return []
    
    # ==================== KuCoin Public API V2 ====================
    async def get_kucoin_info(self, ticker: str, session: Optional[aiohttp.ClientSession] = None) -> List[Dict]:
        """Get coin info from KuCoin using Public API V2"""
        try:
            async with self._session_scope(session) as client:
                url = f"https://api.kucoin.com/api/v2/currencies/{ticker}"
                async with client.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    if resp.status != 200:
                        self._warn_http("KuCoin", resp.status)
                        return []
                    data = await resp.json(content_type=None)
                    d = data.get('data')
                    if not d:
                        return []
                    
                    result = []
                    for chain in d.get('chains', []):
                        result.append({
                            'exchange': 'KuCoin',
                            'asset': ticker,
                            'chain': chain.get('chainName', '-'),
                            'contract': chain.get('contractAddress'),
                            'deposit_enabled': chain.get('isDepositEnabled', False),
                            'withdraw_enabled': chain.get('isWithdrawEnabled', False),
                            'withdraw_fee': chain.get('withdrawalMinFee'),
                            'min_withdraw': chain.get('withdrawalMinSize'),
                        })
                    return result
        except Exception as e:
            self._log_info_error("KuCoin", ticker, e)
            return []

    # ==================== OKX API V5 ====================
    def _okx_signature(self, timestamp: str, method: str, path: str, body: str) -> str:
        keys = self.api_keys.get('OKX', {})
        secret = keys.get('secret_key', '')
        message = f"{timestamp}{method.upper()}{path}{body}"
        mac = hmac.new(secret.encode(), message.encode(), hashlib.sha256)
        import base64
        return base64.b64encode(mac.digest()).decode()

    async def get_okx_info(self, ticker: str, session: Optional[aiohttp.ClientSession] = None) -> List[Dict]:
        """Get coin info from OKX using API V5 (authenticated if keys available)"""
        try:
            async with self._session_scope(session) as client:
                keys = self.api_keys.get('OKX', {})
                if not keys.get('api_key') or not keys.get('secret_key') or not keys.get('passphrase'):
                    self._warn_missing("OKX")
                    return []
                path = f"/api/v5/asset/currencies?ccy={ticker}"
                url = f"https://www.okx.com{path}"
                
                timestamp = time.strftime('%Y-%m-%dT%H:%M:%S.000Z', time.gmtime())
                headers = {
                    "OK-ACCESS-KEY": keys['api_key'],
                    "OK-ACCESS-SIGN": self._okx_signature(timestamp, "GET", path, ""),
                    "OK-ACCESS-TIMESTAMP": timestamp,
                    "OK-ACCESS-PASSPHRASE": keys['passphrase'],
                }
                
                async with client.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    if resp.status != 200:
                        self._warn_http("OKX", resp.status)
                        return []
                    data = await resp.json(content_type=None)
                    if data.get('code') != '0':
                        self._warn_api_error("OKX", str(data.get('code')))
                        return []
                    
                    result = []
                    for d in data.get('data', []):
                        chain_value = str(d.get('chain') or '').strip()
                        chain_name = chain_value.split('-', 1)[-1] if '-' in chain_value else (chain_value or '-')
                        result.append({
                            'exchange': 'OKX',
                            'asset': ticker,
                            'chain': chain_name or '-',
                            'contract': d.get('ctAddr'),
                            'deposit_enabled': str(d.get('canDep')).lower() == 'true',
                            'withdraw_enabled': str(d.get('canWd')).lower() == 'true',
                            'withdraw_fee': d.get('fee'),
                            'min_withdraw': d.get('minWd'),
                        })
                    return result
        except Exception as e:
            self._log_info_error("OKX", ticker, e)
            return []

    # ==================== LBank Public API V2 ====================
    async def get_lbank_info(self, ticker: str, session: Optional[aiohttp.ClientSession] = None) -> List[Dict]:
        """Get coin deposit/withdraw info from LBank public assetConfigs endpoint."""
        try:
            async with self._session_scope(session) as client:
                url = f"https://api.lbkex.com/v2/assetConfigs.do?assetCode={ticker.lower()}"
                async with client.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    if resp.status != 200:
                        self._warn_http("LBank", resp.status)
                        return []
                    data = await resp.json(content_type=None)
                    if str(data.get('result')).lower() not in {'true', '1'}:
                        self._warn_api_error("LBank", str(data.get('error_code') or data.get('msg') or 'unknown'))
                        return []
                    result = []
                    for d in data.get('data', []) or []:
                        if str(d.get('assetCode') or '').upper() != ticker.upper():
                            continue
                        fee = d.get('assetFee') or {}
                        result.append({
                            'exchange': 'LBank',
                            'asset': ticker,
                            'chain': d.get('chainName') or '-',
                            'contract': None,
                            'deposit_enabled': bool(d.get('canDeposit')),
                            'withdraw_enabled': bool(d.get('canDraw')),
                            'withdraw_fee': fee.get('feeAmt') or fee.get('feeRate'),
                            'min_withdraw': fee.get('minAmt'),
                        })
                    return result
        except Exception as e:
            self._log_info_error("LBank", ticker, e)
            return []

    # ==================== Unified fetch ====================
    async def get_all_exchange_info(
        self,
        ticker: str,
        *,
        force_refresh: bool = False,
        max_age_sec: Optional[int] = None,
        session: Optional[aiohttp.ClientSession] = None,
    ) -> Dict[str, Any]:
        """Get coin info from all configured exchanges with persistent caching."""
        ticker_u = self._normalize_ticker(ticker)
        if not ticker_u:
            return self._build_payload("", [], cache_source='empty', refreshed_at=0, cache_stale=False)

        effective_max_age = DEFAULT_EXCHANGE_METADATA_MAX_AGE_SEC if max_age_sec is None else int(max_age_sec)
        stale_cached = self._get_persistent_cached_payload(ticker_u, None)
        now_ts = time.time()

        def _is_fresh(payload: Dict[str, Any]) -> bool:
            """True if the persisted payload was refreshed within effective_max_age."""
            if not isinstance(payload, dict):
                return False
            ts = int(payload.get('cache_refreshed_at') or 0)
            if ts <= 0:
                return False
            return (now_ts - ts) <= float(effective_max_age)

        if not force_refresh:
            # 1. Process-local memory cache (already age-limited by _get_memory_cached_payload).
            cached = self._get_memory_cached_payload(ticker_u, effective_max_age)
            if cached:
                return cached
            # 2. Persistent (SQLite) cache — only return it if it is still FRESH. Previously
            # this was returned regardless of age, which caused the UI to show stale
            # deposit/withdraw flags for coins that haven't been hit for hours/days.
            if stale_cached and _is_fresh(stale_cached):
                stale_cached['cache_source'] = 'persistent'
                stale_cached['cache_stale'] = False
                self._set_memory_cached_payload(ticker_u, stale_cached)
                return stale_cached

        refreshed_at = int(now_ts)
        rows = await self._fetch_all_exchange_info_rows(ticker_u, session=session)
        payload = self._build_payload(
            ticker_u,
            rows,
            cache_source='network',
            refreshed_at=refreshed_at,
            cache_stale=False,
        )

        if rows:
            self.db.save_exchange_asset_metadata(ticker_u, rows, refreshed_at=refreshed_at)
            self._set_memory_cached_payload(ticker_u, payload)
            return payload

        # Network fetch returned no rows (rate-limit, geo-block, missing keys).
        # Fall back to the most recent persisted snapshot, but only if it is
        # within PERSISTENT_FALLBACK_MAX_AGE_SEC. Anything older is worse than
        # returning empty — UI will render "НЕИЗВЕСТНО" instead of a stale lie.
        if stale_cached:
            ts = int(stale_cached.get('cache_refreshed_at') or 0)
            age_sec = (now_ts - ts) if ts > 0 else float('inf')
            if age_sec <= PERSISTENT_FALLBACK_MAX_AGE_SEC:
                stale_cached['cache_source'] = 'stale_persistent'
                stale_cached['cache_stale'] = True
                self._set_memory_cached_payload(ticker_u, stale_cached)
                return stale_cached

        # No fresh network data and no acceptable fallback — persist the empty
        # snapshot so subsequent short-interval requests short-circuit here.
        self.db.save_exchange_asset_metadata(ticker_u, rows, refreshed_at=refreshed_at)
        self._set_memory_cached_payload(ticker_u, payload)
        return payload


# Global instance
exchange_info_fetcher = ExchangeInfoFetcher()


async def get_coin_info(ticker: str) -> Dict[str, Any]:
    """Main entry point for getting coin info"""
    return await exchange_info_fetcher.get_all_exchange_info(ticker)
