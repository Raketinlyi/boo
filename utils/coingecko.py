"""
Модуль для работы с API CoinGecko.
"""

import logging
import asyncio
import json
import os
import time
import traceback
from typing import Optional, Dict, Any, List, Tuple, Union
import math
from datetime import datetime, timedelta

import aiohttp
from aiohttp_retry import RetryClient, ExponentialRetry
import requests
from utils.api_manager import api_manager

# Константы для CoinGecko API
COINGECKO_API_BASE = "https://api.coingecko.com/api/v3"
COINGECKO_PRO_API_BASE = "https://pro-api.coingecko.com/api/v3"
COINGECKO_LIST_FILE = "data/coingecko_list.json"
COINGECKO_CACHE_DIR = "data/coingecko_cache"
COINGECKO_LIST_TTL_SECONDS = 3600  # 1 час - только для списка монет
COINGECKO_DATA_TTL_SECONDS = 60    # legacy default; overridden by config api_settings.coingecko.cache_ttl

# Настройка таймаута для HTTP-запросов
DEFAULT_TIMEOUT = aiohttp.ClientTimeout(total=10)

# Настройка повторных попыток для HTTP-запросов
retry_options = ExponentialRetry(attempts=3, start_timeout=0.5, max_timeout=5, statuses={500, 502, 503, 504})

# Добавляем переменные для контроля частоты запросов
# Значения по умолчанию (будут переопределены на уровне экземпляра CoinGecko)
RATE_LIMIT_DELAY = 2.5  # Задержка между запросами в секундах (для 24 rpm ~ 2.5с)
MAX_REQUESTS_PER_PERIOD = 24  # Максимальное количество запросов в период (по умолчанию 24/мин)
REQUEST_PERIOD = 60  # Период в секундах (1 минута)
last_request_time = 0.0  # не используется, оставлено для совместимости
request_count = 0       # не используется, оставлено для совместимости
request_period_start = 0.0  # не используется, оставлено для совместимости

# Функции для работы с кешем списка монет CoinGecko
def load_coingecko_list_from_cache() -> Optional[List[Dict]]:
    """
    Загружает список монет из файла кэша, если он актуален.
    
    Returns:
        Optional[List[Dict]]: Список монет или None, если кэш устарел или не существует
    """
    logging.info("Попытка загрузки списка монет CoinGecko из кэша")
    if os.path.exists(COINGECKO_LIST_FILE):
        try:
            mod_time = os.path.getmtime(COINGECKO_LIST_FILE)
            # Список монет меняется редко. Даже устаревший список лучше, чем пустой:
            # позволяет корректно маппить популярные тикеры (BTC/ETH/USDT) без сетевых вызовов.
            with open(COINGECKO_LIST_FILE, "r", encoding="utf-8") as f:
                coins_list = json.load(f)
            age_sec = time.time() - mod_time
            if age_sec >= COINGECKO_LIST_TTL_SECONDS:
                logging.info(f"CoinGecko list cache is stale (age={int(age_sec)}s); refreshing from API before fallback.")
                return None
            logging.info(f"Loaded CoinGecko list from cache ({len(coins_list)} coins)")
            return coins_list
        except (json.JSONDecodeError, OSError, Exception) as e:
            logging.error(f"Ошибка загрузки кэша списка CoinGecko: {e}")
    else:
        logging.info("Файл кэша списка CoinGecko не найден.")
    return None

async def fetch_coingecko_list() -> Optional[List[Dict]]:
    """
    Получает список монет с API CoinGecko.
    
    Returns:
        Optional[List[Dict]]: Список монет или None в случае ошибки
    """
    url = "https://api.coingecko.com/api/v3/coins/list"
    logging.info("Запрос списка монет от CoinGecko API")
    
    try:
        connector = aiohttp.TCPConnector(limit=10)
        timeout = aiohttp.ClientTimeout(total=30)
        
        async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
            retry_options = ExponentialRetry(attempts=3, start_timeout=0.5, max_timeout=5)
            async with RetryClient(client_session=session, retry_options=retry_options) as client:
                async with client.get(url) as response:
                    if response.status == 200:
                        coins_list = await response.json()
                        logging.info(f"Получен список монет из CoinGecko API ({len(coins_list)} монет)")
                        return coins_list
                    else:
                        logging.error(f"Ошибка при получении списка монет: {response.status}")
                        return None
    except Exception as e:
        logging.error(f"Исключение при получении списка монет: {e}")
        return None

def save_coingecko_list_to_cache(coins_list: List[Dict]) -> bool:
    """
    Сохраняет список монет в файл кэша.
    
    Args:
        coins_list: Список монет для сохранения
        
    Returns:
        bool: True если сохранение успешно, иначе False
    """
    try:
        os.makedirs(os.path.dirname(COINGECKO_LIST_FILE) or ".", exist_ok=True)
        with open(COINGECKO_LIST_FILE, "w", encoding="utf-8") as f:
            json.dump(coins_list, f, ensure_ascii=False)
        logging.info(f"Список монет CoinGecko сохранен в кэш ({len(coins_list)} монет)")
        return True
    except (OSError, Exception) as e:
        logging.error(f"Ошибка сохранения кэша списка монет CoinGecko: {e}")
        return False

class HttpClient:
    """
    Класс для выполнения HTTP-запросов с поддержкой повторных попыток.
    """
    
    def __init__(self, session: Optional[aiohttp.ClientSession] = None):
        """
        Инициализирует HTTP-клиент.
        
        Args:
            session: Существующая сессия aiohttp, если есть
        """
        # Используем переданную сессию или создаем новую
        self._session = session
        # Флаг, указывающий, создали ли мы сессию внутри
        self._created_session = session is None
        self.client = None

    async def __aenter__(self):
        """
        Входит в контекст асинхронного клиента.
        
        Returns:
            RetryClient для выполнения запросов
        """
        if self._created_session:
            connector = aiohttp.TCPConnector(limit=50, ttl_dns_cache=300)
            self._session = aiohttp.ClientSession(connector=connector, timeout=DEFAULT_TIMEOUT)
        # Создаем RetryClient на основе существующей или новой сессии
        self.client = RetryClient(client_session=self._session, retry_options=retry_options)
        await self.client.__aenter__()  # Важно войти в контекст RetryClient
        return self.client  # Возвращаем RetryClient для использования

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """
        Выходит из контекста асинхронного клиента.
        """
        # Закрываем RetryClient
        if self.client:
            await self.client.__aexit__(exc_type, exc_val, exc_tb)
        # Закрываем сессию, только если мы ее создали
        if self._created_session and self._session and not self._session.closed:
            await self._session.close()

    @staticmethod
    def make_sync_request(
        method: str, 
        url: str, 
        headers: Optional[Dict] = None, 
        params: Optional[Dict] = None, 
        data: Optional[Any] = None, 
        timeout: int = 10
    ) -> Optional[Any]:
        """
        Выполняет синхронный HTTP-запрос с использованием requests.
        
        Args:
            method: HTTP-метод (GET, POST и т.д.)
            url: URL для запроса
            headers: Заголовки запроса
            params: Параметры запроса
            data: Данные для отправки
            timeout: Таймаут запроса в секундах
            
        Returns:
            Результат запроса или None в случае ошибки
        """
        try:
            response = requests.request(method, url, headers=headers, params=params, json=data, timeout=timeout)
            response.raise_for_status()  # Вызовет исключение для 4xx/5xx

            # Содержимое и Content-Type
            content_type = (response.headers.get('Content-Type') or '').lower()
            text = response.text or ""

            # Если ответ с правильным Content-Type, пробуем стандартный парсинг
            if 'application/json' in content_type or 'application/problem+json' in content_type:
                try:
                    return response.json()
                except json.JSONDecodeError:
                    # Попробуем распарсить вручную на основе текста
                    txt = text.strip()
                    if txt.startswith('{') or txt.startswith('['):
                        try:
                            return json.loads(txt)
                        except json.JSONDecodeError:
                            logging.error(f"Ошибка декодирования JSON от {url}. Ответ: {txt[:200]}...")
                            return None
                    logging.error(f"Ошибка декодирования JSON от {url}. Ответ не JSON: {txt[:200]}...")
                    return None

            # Если Content-Type не JSON — иногда сервер отвечает 'text/plain' но с JSON в теле.
            txt = text.strip()
            if txt.startswith('{') or txt.startswith('['):
                try:
                    return json.loads(txt)
                except json.JSONDecodeError:
                    logging.error(f"Ответ от {url} содержит текст, похожий на JSON, но парсинг не удался. Preview: {txt[:200]}...")
                    return None

            # Иначе возвращаем сырой текст (вызователь решит, что с ним делать)
            logging.debug(f"Ответ от {url} не является JSON ({content_type}). Возвращаем текст-предпросмотр.")
            return text
        except requests.exceptions.Timeout:
            logging.error(f"Таймаут синхронного запроса к {url}")
            return None
        except requests.exceptions.HTTPError as e:
            try:
                status = getattr(e.response, "status_code", "unknown")
                text = getattr(e.response, "text", "")
                preview = text[:200] if isinstance(text, str) else str(text)
            except Exception:
                status = "unknown"
                preview = ""
            logging.error(f"Ошибка HTTP {status} для {url}: {e}. Ответ: {preview}...")
            return None
        except requests.exceptions.RequestException as e:
            logging.error(f"Ошибка синхронного запроса к {url}: {e}")
            return None
        except Exception as e:
            logging.error(f"Непредвиденная ошибка синхронного запроса к {url}: {e}", exc_info=True)
            return None

class CoinGecko:
    """
    Класс для работы с API CoinGecko.
    """
    
    def __init__(self, config):
        """
        Инициализирует клиент CoinGecko.
        
        Args:
            config: Объект конфигурации с API-ключом
        """
        self.config = config
        self.coins_list = []
        self.coins_list_by_id = {}
        self.coins_list_by_symbol = {}
        self.last_list_update = 0.0
        self.last_request_time = 0.0
        
        # Загружаем настройки API
        self.api_settings = config.get_api_settings("coingecko")
        self.use_pro = self.api_settings["use_pro"]
        self.rate_limit = self.api_settings["pro_rate_limit"] if self.use_pro else self.api_settings["rate_limit"]
        self.retry_delay = self.api_settings["retry_delay"]
        self.cache_ttl = self.api_settings["cache_ttl"]
        self._warned_http_statuses = set()
        self._recompute_rate_limit_params()
        self._rl_period = 60.0
        self._rl_last_ts = 0.0
        self._rl_cnt = 0
        self._rl_start_ts = 0.0
        
        # Создаем директории для кэша
        os.makedirs(COINGECKO_CACHE_DIR, exist_ok=True)
        
        # Загружаем список монет из кэша при инициализации
        self._load_list_from_cache()

    def _get_cache_path(self, coin_id: str, *, variant: str = "full") -> str:
        """Возвращает путь к файлу кэша для конкретной монеты.

        variant:
          - "full": ответ с tickers (default behaviour)
          - "lite": ответ без tickers (для веб-интерфейса)
        """
        suffix = ""
        v = str(variant or "full").strip().lower()
        if v and v != "full":
            suffix = f"_{v}"
        return os.path.join(COINGECKO_CACHE_DIR, f"{coin_id}{suffix}.json")

    def _recompute_rate_limit_params(self) -> None:
        """Recompute local pacing based on current `use_pro` + `rate_limit`."""
        try:
            rate = float(max(1, int(self.rate_limit)))
        except Exception:
            rate = 24.0
        # Pacing between calls (seconds)
        self._rl_min_delay = max(60.0 / rate, 0.5)
        # Hard cap per 60s window
        default_max_rpm = 24 if not self.use_pro else max(24, int(rate))
        self._rl_max_per_period = min(int(rate), default_max_rpm) if not self.use_pro else int(rate)

    def _load_from_cache(self, coin_id: str, *, variant: str = "full") -> Optional[Dict]:
        """Загружает данные монеты из кэша, если они актуальны."""
        cache_path = self._get_cache_path(coin_id, variant=variant)
        if os.path.exists(cache_path):
            try:
                mod_time = os.path.getmtime(cache_path)
                ttl = int(self.cache_ttl) if self.cache_ttl is not None else COINGECKO_DATA_TTL_SECONDS
                if (time.time() - mod_time) < max(0, ttl):
                    with open(cache_path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    logging.info(f"Загружены данные для {coin_id} из кэша")
                    return data
            except Exception as e:
                logging.error(f"Ошибка загрузки кэша для {coin_id}: {e}")
        return None

    def _save_to_cache(self, coin_id: str, data: Dict, *, variant: str = "full") -> None:
        """
        Сохраняет данные монеты в кэш с очень коротким сроком жизни.
        Кеширование минимизировано для получения более актуальных данных.
        """
        try:
            # Проверяем, включено ли кеширование
            ttl = int(self.cache_ttl) if self.cache_ttl is not None else COINGECKO_DATA_TTL_SECONDS
            if ttl <= 0:
                logging.debug(f"Кеширование данных отключено для {coin_id}")
                return
                
            cache_path = self._get_cache_path(coin_id, variant=variant)
            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            logging.info(f"Данные для {coin_id} сохранены в кэш на {ttl} секунд")
        except Exception as e:
            logging.error(f"Ошибка сохранения кэша для {coin_id}: {e}")

    def _get_api_url(self, endpoint: str) -> str:
        """Возвращает URL для API запроса."""
        base = COINGECKO_PRO_API_BASE if self.use_pro and self.config.is_api_key_available("coingecko") else COINGECKO_API_BASE
        return f"{base}{endpoint}"
        
    def _get_headers(self) -> Dict[str, str]:
        """Возвращает заголовки для API запроса."""
        headers = {
            "Accept": "application/json",
            "User-Agent": "arbitrage-bot/1.0"
        }
        if self.use_pro and self.config.is_api_key_available("coingecko"):
            headers["X-Cg-Pro-Api-Key"] = self.config.get("coingecko_api_key")
        return headers
        
    def _handle_rate_limit(self):
        """
        Обрабатывает ограничение частоты запросов с учетом максимального количества запросов в период.
        Реализует двойное ограничение:
        1. Не более MAX_REQUESTS_PER_PERIOD запросов за REQUEST_PERIOD секунд
        2. Минимальная задержка между запросами RATE_LIMIT_DELAY
        """
        current_time = time.time()
        # Минимальная задержка между запросами
        since_last = current_time - self._rl_last_ts
        if since_last < self._rl_min_delay:
            delay = self._rl_min_delay - since_last
            logging.debug(f"Ожидание {delay:.2f} сек перед следующим запросом к CoinGecko (min delay)")
            time.sleep(delay)
            current_time = time.time()
        # Окно в 60 секунд
        if current_time - self._rl_start_ts > self._rl_period:
            self._rl_cnt = 0
            self._rl_start_ts = current_time
        # Если достигли лимита в окне — ждём до конца окна
        if self._rl_cnt >= self._rl_max_per_period:
            wait_time = self._rl_period - (current_time - self._rl_start_ts) + 0.01
            if wait_time > 0:
                logging.warning(f"Достигнут лимит CoinGecko: {self._rl_cnt}/{self._rl_max_per_period} за {int(self._rl_period)}с. Ждём {wait_time:.2f}с")
                time.sleep(wait_time)
                self._rl_cnt = 0
                self._rl_start_ts = time.time()
        # Учёт запроса
        self._rl_cnt += 1
        self._rl_last_ts = time.time()
        
    def _update_rate_limits(self, response: requests.Response):
        """Обновляет информацию о лимитах из заголовков ответа."""
        try:
            remaining = response.headers.get("X-RateLimit-Remaining")
            reset_time = response.headers.get("X-RateLimit-Reset")
            
            if remaining is not None:
                remaining = int(remaining)
            if reset_time is not None:
                reset_time = float(reset_time)
                
            self.config.update_api_limits("coingecko", remaining, reset_time)
        except Exception as e:
            logging.debug(f"Не удалось обновить информацию о лимитах: {e}")
            
    def _handle_error_response(self, response: requests.Response, url: str):
        """Обрабатывает ошибочные ответы от API."""
        if response.status_code == 429:
            logging.warning(f"Превышен лимит запросов к CoinGecko API ({url})")
            if self.use_pro:
                self.config.mark_api_key_invalid("coingecko")
        elif response.status_code in [401, 403]:
            if self.use_pro:
                logging.error(f"CoinGecko API authentication error ({url})")
                self.config.mark_api_key_invalid("coingecko")
            else:
                warn_key = f"{response.status_code}:{url}"
                if warn_key not in self._warned_http_statuses:
                    logging.warning(f"CoinGecko Public API returned HTTP {response.status_code} ({url})")
                    self._warned_http_statuses.add(warn_key)
        else:
            logging.error(f"Ошибка запроса к CoinGecko API ({url}): {response.status_code}")
            
    def _make_request(self, endpoint: str, params: Optional[Dict] = None) -> Optional[Any]:
        """Выполняет запрос к API с обработкой ошибок и ограничений."""
        url = self._get_api_url(endpoint)
        headers = self._get_headers()
        
        # Проверяем ограничение частоты запросов
        self._handle_rate_limit()
        
        try:
            response = requests.get(url, headers=headers, params=params, timeout=10)

            # Текст и content-type
            content_type = (response.headers.get('Content-Type') or '').lower()
            text = response.text or ""

            # Обновляем информацию о лимитах (если есть)
            self._update_rate_limits(response)

            # Обрабатываем успешный ответ (200)
            if response.status_code == 200:
                # Сначала стандартный парсинг для явного JSON
                if 'application/json' in content_type or 'application/problem+json' in content_type:
                    try:
                        return response.json()
                    except json.JSONDecodeError as e:
                        logging.error(f"Ошибка декодирования JSON от CoinGecko API ({url}): {e}")
                        # Попробуем парсить текст вручную
                        txt = text.strip()
                        if txt.startswith('{') or txt.startswith('['):
                            try:
                                return json.loads(txt)
                            except json.JSONDecodeError:
                                logging.error(f"Не получилось распарсить JSON из текстового ответа ({url}). Preview: {txt[:200]}...")
                                return None
                        return None

                # Если Content-Type не JSON, попробуем распарсить, если тело похоже на JSON
                txt = text.strip()
                if txt.startswith('{') or txt.startswith('['):
                    try:
                        return json.loads(txt)
                    except json.JSONDecodeError:
                        logging.error(f"Ответ от {url} невалидный JSON, хотя тело похоже на JSON. Preview: {txt[:200]}...")
                        return None

                # Иначе — неверный тип контента
                logging.error(f"Неверный тип контента от API ({url}): {content_type}")
                return None
            else:
                self._handle_error_response(response, url)
                return None
                
        except requests.exceptions.RequestException as e:
            logging.error(f"Ошибка сети при запросе к CoinGecko API ({url}): {e}")
            return None
        except Exception as e:
            logging.error(f"Непредвиденная ошибка при запросе к CoinGecko API ({url}): {e}", exc_info=True)
            return None
            
    def _load_list_from_cache(self) -> bool:
        """
        Загружает список монет из файла кэша, если он актуален.
        
        Returns:
            True, если список успешно загружен, иначе False
        """
        if os.path.exists(COINGECKO_LIST_FILE):
            try:
                mod_time = os.path.getmtime(COINGECKO_LIST_FILE)
                # Список монет меняется редко: даже если TTL истёк, используем локальный файл
                # как fallback, чтобы не ломать UI при сетевых/лимитных проблемах.
                with open(COINGECKO_LIST_FILE, "r", encoding="utf-8") as f:
                    self.coins_list = json.load(f)
                self._build_indices()
                self.last_list_update = mod_time
                age_sec = time.time() - mod_time
                if age_sec >= COINGECKO_LIST_TTL_SECONDS:
                    logging.info(f"CoinGecko list cache is stale (age={int(age_sec)}s); temporary local list loaded until API refresh completes.")
                logging.info(f"Loaded CoinGecko list from cache ({len(self.coins_list)} coins)")
                return True
            except (json.JSONDecodeError, OSError, Exception) as e:
                logging.error(f"Ошибка загрузки кэша списка CoinGecko: {e}")
        else:
            logging.info("Файл кэша списка CoinGecko не найден.")
        return False

    def _build_indices(self) -> None:
        """
        Строит индексы для быстрого поиска по ID и символу.
        """
        self.coins_list_by_id = {coin['id']: coin for coin in self.coins_list if 'id' in coin}
        self.coins_list_by_symbol = {}
        for coin in self.coins_list:
            symbol = coin.get('symbol', '').lower()
            if symbol:
                if symbol not in self.coins_list_by_symbol:
                    self.coins_list_by_symbol[symbol] = []
                self.coins_list_by_symbol[symbol].append(coin)

    def find_coin_id(self, symbol: str, exchange_volumes: Optional[Dict[str, float]] = None) -> Optional[str]:
        """
        Ищет ID монеты по символу, всегда используя данные об объемах торгов для более точного определения.
        Если не можем определить по объемам, используем эвристики или возвращаем самую популярную монету.
        
        Args:
            symbol: Символ монеты
            exchange_volumes: Словарь с объемами торгов {биржа: объем} (опционально)
            
        Returns:
            ID монеты или None, если not найдена
        """
        if not self.coins_list:
            logging.warning("Список монет CoinGecko не загружен. Попытка обновить список.")
            # Попытка обновить список монет
            if not self._update_coins_list():
                # Если не получилось обновить, возвращаем None
                logging.error("Не удалось обновить список монет. Поиск невозможен.")
                return None
            
        # Стандартный поиск должен всегда учитывать объемы если они есть
        if exchange_volumes:
            logging.info(f"Поиск ID для '{symbol}' с проверкой объемов торгов")
            # Попытка найти монету по объемам торгов
            coin_id = self.find_coin_id_by_volume(symbol, exchange_volumes)
            if coin_id:
                return coin_id
            else:
                # Если не нашли по объемам, продолжаем поиск по другим критериям
                logging.warning(f"Не удалось найти монету '{symbol}' по объемам торгов. Используем стандартный поиск.")

        # Стандартный поиск без данных об объемах или если поиск по объемам не дал результатов
        lower_symbol = symbol.lower()
        matches = self.coins_list_by_symbol.get(lower_symbol, [])

        if not matches:
            logging.info(f"Монета с символом '{symbol}' не найдена в списке CoinGecko.")
            # При выключенном CoinMarketCap не пытаемся фолбэк
            try:
                cmc_enabled = bool(self.config.get_api_settings("coinmarketcap").get("enabled", False))
            except Exception:
                cmc_enabled = False
            if not cmc_enabled:
                return None
            # CoinMarketCap adapter removed — do not attempt fallback
            logging.debug(f"CoinMarketCap fallback requested for find_coin_id('{symbol}'), but adapter is removed.")
            return None
        elif len(matches) == 1:
            coin_id = matches[0]['id']
            logging.debug(f"Найдена одна монета CoinGecko для '{symbol}': ID = {coin_id}")
            return coin_id
        else:
            # Эвристика: ищем точное совпадение по имени (регистронезависимо)
            for coin in matches:
                if coin.get('name', '').lower() == symbol.lower():
                    coin_id = coin['id']
                    logging.info(f"Найдено несколько совпадений для '{symbol}', выбрано по имени: ID = {coin_id}")
                    return coin_id
                    
            # Дополнительная эвристика: проверка популярности по списку
            # Для некоторых общих символов (например, BTC) первая монета обычно более популярна
            popular_coins = {
                "btc": "bitcoin",
                "eth": "ethereum",
                "usdt": "tether",
                "bnb": "binancecoin",
                "sol": "solana",
                "xrp": "ripple",
                "ada": "cardano",
                "doge": "dogecoin",
                "dot": "polkadot",
                "ltc": "litecoin"
            }
            
            if lower_symbol in popular_coins:
                popular_id = popular_coins[lower_symbol]
                for coin in matches:
                    if coin['id'] == popular_id:
                        logging.info(f"Выбрана популярная монета для '{symbol}': ID = {popular_id}")
                        return popular_id

            candidate_ids = [
                coin.get('id')
                for coin in matches
                if isinstance(coin, dict) and coin.get('id')
            ]
            if candidate_ids:
                try:
                    markets_data = self.fetch_markets_by_ids(candidate_ids, retries=1) or []
                except Exception:
                    markets_data = []

                if markets_data:
                    markets_by_id = {
                        str(item.get('id')): item
                        for item in markets_data
                        if isinstance(item, dict) and item.get('id')
                    }

                    def rank_key(cid: str):
                        market = markets_by_id.get(str(cid)) or {}
                        try:
                            rank = int(market.get("market_cap_rank") or 10**9)
                        except Exception:
                            rank = 10**9
                        try:
                            cap = float(market.get("market_cap") or 0.0)
                        except Exception:
                            cap = 0.0
                        return (rank, -cap, str(cid))

                    ranked = sorted([str(cid) for cid in candidate_ids], key=rank_key)
                    if ranked:
                        selected_id = ranked[0]
                        logging.warning(
                            f"Найдено несколько ({len(matches)}) монет CoinGecko для '{symbol}'. "
                            f"Выбрана самая крупная по рынку: {selected_id}"
                        )
                        return selected_id

            logging.warning(
                f"Найдено несколько ({len(matches)}) монет CoinGecko для '{symbol}', "
                "но безопасно выбрать ID не удалось."
            )
            return None

    def find_coin_id_by_context(
        self,
        symbol: str,
        *,
        exchange_names: Optional[List[str]] = None,
        reference_price_usd: Optional[float] = None,
        price_tolerance: float = 0.25,
        max_candidates: int = 30,
    ) -> Optional[str]:
        """Выбор coin_id для неоднозначных символов по контексту (быстро и без тяжёлых тикеров).

        Основная идея:
        1) Если есть референс-цена (обычно из USDT пары) — фильтруем кандидатов по price-guard (±tol).
        2) Если после фильтра осталось >1 — используем пересечение бирж (если в кэше есть tickers).
        3) Если всё ещё неоднозначно — берём наиболее «крупную» по рынку (market_cap_rank).
        """
        try:
            if not symbol:
                return None

            if not self.coins_list:
                logging.warning("Список монет CoinGecko не загружен. Попытка обновить список.")
                if not self._update_coins_list():
                    return None

            lower_symbol = str(symbol).strip().lower()
            matches = self.coins_list_by_symbol.get(lower_symbol, [])
            if not matches:
                return None
            if len(matches) == 1:
                return matches[0].get("id")

            candidate_ids = [
                c.get("id") for c in matches
                if isinstance(c, dict) and c.get("id")
            ]
            # Уникализируем и ограничиваем размер (на редких символах кандидатов может быть много)
            candidate_ids = list(dict.fromkeys(candidate_ids))[: max(1, int(max_candidates))]
            if not candidate_ids:
                return None

            ref = None
            try:
                if reference_price_usd is not None:
                    ref = float(reference_price_usd)
            except Exception:
                ref = None
            if ref is not None and (not math.isfinite(ref) or ref <= 0):
                ref = None

            tol = 0.25
            try:
                if price_tolerance is not None:
                    tol = float(price_tolerance)
            except Exception:
                tol = 0.25
            if tol <= 0:
                tol = 0.25

            markets_by_id: Dict[str, Dict[str, Any]] = {}
            if ref is not None:
                prices_by_id: Dict[str, float] = {}

                # Price-guard #1 (fast): /simple/price батчем
                try:
                    params = {"ids": ",".join(candidate_ids[:250]), "vs_currencies": "usd"}
                    sp = self._make_request("/simple/price", params=params)
                    if isinstance(sp, dict):
                        for cid, payload in sp.items():
                            try:
                                p = float((payload or {}).get("usd"))
                            except Exception:
                                p = None
                            if p is not None and math.isfinite(p) and p > 0:
                                prices_by_id[str(cid)] = p
                except Exception as e:
                    logging.debug(f"Context match: simple/price failed for {symbol}: {e}")

                # Price-guard #2 (fallback): /coins/markets батчем
                if not prices_by_id:
                    try:
                        markets_data = self.fetch_markets_by_ids(candidate_ids, retries=1) or []
                        for m in markets_data:
                            if isinstance(m, dict) and m.get("id"):
                                cid = str(m["id"])
                                markets_by_id[cid] = m
                                try:
                                    p = float(m.get("current_price"))
                                except Exception:
                                    p = None
                                if p is not None and math.isfinite(p) and p > 0:
                                    prices_by_id[cid] = p
                    except Exception as e:
                        logging.debug(f"Context match: markets batch failed for {symbol}: {e}")

                # Price-guard: выбираем ближайшую по цене монету (в пределах tol).
                best_cid: Optional[str] = None
                best_diff: Optional[float] = None
                for cid in candidate_ids:
                    p = prices_by_id.get(cid)
                    if p is None or (not math.isfinite(p)) or p <= 0:
                        continue
                    diff = abs(p - ref) / ref
                    if diff <= tol:
                        if best_diff is None or diff < best_diff:
                            best_diff = diff
                            best_cid = cid
                if best_cid:
                    return best_cid

            # Exchange overlap: используем ТОЛЬКО кэш full (если он есть), без дополнительных запросов.
            wanted = [str(x).strip() for x in (exchange_names or []) if str(x).strip()]
            wanted_lower = [w.lower() for w in wanted]

            def exchange_aliases(ex_lower: str) -> List[str]:
                exchange_variants = {
                    "gate.io": ["gate", "gateio", "gate_io", "gate-io", "gate.io"],
                    "mexc": ["mexc", "mxc", "mexcglobal", "mexc-global"],
                    "kucoin": ["kucoin", "kcs"],
                    "bybit": ["bybit"],
                    "okx": ["okx", "okex", "okcoin"],
                    "binance": ["binance", "bnb", "binance-us"],
                    "coinex": ["coinex"],
                    "bitget": ["bitget"],
                    "htx": ["htx", "huobi"],
                    "bingx": ["bingx"],
                }
                for key, variants in exchange_variants.items():
                    if ex_lower == key or ex_lower in variants:
                        return list(dict.fromkeys(variants + [key]))
                return [ex_lower]

            overlap_by_id: Dict[str, int] = {}
            if wanted_lower and len(candidate_ids) > 1:
                for cid in candidate_ids:
                    overlap = 0
                    try:
                        cached_full = self._load_from_cache(cid, variant="full")
                        tickers = (cached_full or {}).get("tickers") or []
                        if isinstance(tickers, list) and tickers:
                            market_strs: List[str] = []
                            for t in tickers:
                                m = (t or {}).get("market") or {}
                                ident = m.get("identifier")
                                name = m.get("name")
                                if ident:
                                    market_strs.append(str(ident).lower())
                                if name:
                                    market_strs.append(str(name).lower())
                            for ex in wanted_lower:
                                aliases = exchange_aliases(ex)
                                if any(any(a in ms for ms in market_strs) for a in aliases):
                                    overlap += 1
                    except Exception:
                        overlap = 0
                    overlap_by_id[cid] = overlap

                best_overlap = max(overlap_by_id.values()) if overlap_by_id else 0
                if best_overlap > 0:
                    best_ids = [cid for cid, v in overlap_by_id.items() if v == best_overlap]
                    candidate_ids = best_ids
                    if len(candidate_ids) == 1:
                        return candidate_ids[0]

            # Если до сих пор неоднозначно — пробуем догрузить /coins/markets для tie-break (best-effort).
            if len(candidate_ids) > 1 and not markets_by_id:
                try:
                    markets_data = self.fetch_markets_by_ids(candidate_ids, retries=1) or []
                    for m in markets_data:
                        if isinstance(m, dict) and m.get("id"):
                            markets_by_id[str(m["id"])] = m
                except Exception:
                    pass

            # Финальный тай-брейк: market_cap_rank -> market_cap -> id
            def rank_key(cid: str):
                m = markets_by_id.get(cid) or {}
                try:
                    r = int(m.get("market_cap_rank") or 10**9)
                except Exception:
                    r = 10**9
                try:
                    cap = float(m.get("market_cap") or 0.0)
                except Exception:
                    cap = 0.0
                # Чем меньше rank, тем лучше; чем больше cap, тем лучше
                return (r, -cap, cid)

            candidate_ids.sort(key=rank_key)

            chosen = candidate_ids[0] if candidate_ids else None
            if chosen:
                logging.info(
                    f"Context match for '{symbol}': candidates={len(matches)} -> chosen={chosen}"
                    f"{' (price-guard)' if ref is not None else ''}"
                )
            return chosen
        except Exception as e:
            logging.error(f"Context match failed for '{symbol}': {e}")
            return None

    def _update_coins_list(self) -> bool:
        """
        Обновляет список монет с CoinGecko API.
        
        Returns:
            bool: True если список успешно обновлен
        """
        logging.info("Обновление списка монет CoinGecko")
        try:
            coins_list = HttpClient.make_sync_request("GET", f"{COINGECKO_API_BASE}/coins/list")
            if coins_list:
                self.coins_list = coins_list
                self._build_indices()
                self.last_list_update = time.time()
                
                # Сохраняем только список монет в кэш (остальные данные не кешируем)
                try:
                    os.makedirs(os.path.dirname(COINGECKO_LIST_FILE) or ".", exist_ok=True)
                    with open(COINGECKO_LIST_FILE, "w", encoding="utf-8") as f:
                        json.dump(self.coins_list, f, ensure_ascii=False)
                    logging.info(f"Список монет CoinGecko обновлен и сохранен ({len(self.coins_list)} монет)")
                    return True
                except Exception as e:
                    logging.error(f"Ошибка сохранения списка монет: {e}")
                    return True  # Все равно считаем успешным, т.к. список в памяти обновлен
            else:
                logging.error("Не удалось получить список монет от CoinGecko API")
                return False
        except Exception as e:
            logging.error(f"Ошибка при обновлении списка монет CoinGecko: {e}")
            return False
            
    def _find_coin_id_via_coinmarketcap(self, symbol: str, exchange_volumes: Optional[Dict[str, float]] = None) -> Optional[str]:
        """
        Ищет ID монеты через CoinMarketCap API при недоступности данных в CoinGecko.
        
        Args:
            symbol: Символ монеты
            exchange_volumes: Словарь с объемами торгов (опционально)
            
        Returns:
            ID монеты в формате CoinGecko (приблизительное соответствие) или None
        """
        try:
            # NOTE: CoinMarketCap adapter has been removed from the codebase.
            # Historically we fell back to CoinMarketCap here, but that adapter
            # is intentionally deleted to avoid depending on a paid/unstable API.
            # If you need CoinMarketCap support again, re-add an adapter and set
            # the config flag 'api_settings.coinmarketcap.enabled' and the API key.
            logging.debug("CoinMarketCap fallback requested, but adapter is removed. Returning None.")
            return None
        except Exception as e:
            logging.error(f"Ошибка при поиске монеты через CoinMarketCap: {e}")
            return None

    def _use_public_api(self):
        """Временно переключает на использование публичного API."""
        self.use_pro = False
        self.rate_limit = self.api_settings["rate_limit"]
        self._recompute_rate_limit_params()
        logging.info("Переключение на публичный API CoinGecko")

    def _use_pro_api(self):
        """Переключает обратно на Pro API."""
        self.use_pro = True
        self.rate_limit = self.api_settings["pro_rate_limit"]
        self._recompute_rate_limit_params()
        logging.info("Переключение на Pro API CoinGecko")

    def get_coingecko_data_for_symbol_sync(
        self,
        symbol: str,
        exchange_volumes: Optional[Dict[str, float]] = None,
        include_tickers: bool = True,
        *,
        exchange_names: Optional[List[str]] = None,
        reference_price_usd: Optional[float] = None,
        price_tolerance: float = 0.25,
    ) -> Optional[Dict]:
        """
        Получает данные о монете по символу с последовательным переключением между API.
        В случае неудачи автоматически переключается на CoinMarketCap.
        
        Args:
            symbol: Символ монеты
            exchange_volumes: Словарь с объемами торгов для уточнения поиска
            
        Returns:
            Dict: Данные о монете или None если не найдена
        """
        # Ищем ID монеты.
        # Если передан символ, похожий на адрес контракта, пробуем сначала поиск по контракту
        coin_id = None
        if symbol and (symbol.startswith('0x') or len(symbol) > 30):
            coin_id = self.find_coin_id_by_contract(symbol)
            
        try:
            if not coin_id and (exchange_names or reference_price_usd is not None):
                coin_id = self.find_coin_id_by_context(
                    symbol,
                    exchange_names=exchange_names,
                    reference_price_usd=reference_price_usd,
                    price_tolerance=price_tolerance,
                )
        except Exception:
            coin_id = None
            
        if not coin_id:
            coin_id = self.find_coin_id(symbol, exchange_volumes)
        if not coin_id:
            logging.info(f"CoinGecko: не удалось сопоставить symbol='{symbol}' с coin_id, пропускаем metadata lookup")
            # Пробуем получить данные напрямую из CoinMarketCap в формате совместимом с CoinGecko
            return self._get_coinmarketcap_data_as_coingecko(symbol, exchange_volumes)

        cache_variant = "full" if include_tickers else "lite"

        # Проверяем кэш
        cache_data = self._load_from_cache(coin_id, variant=cache_variant)
        if cache_data:
            # Safety: avoid returning "lite" payload from a legacy cache file when tickers are required.
            if include_tickers and "tickers" not in cache_data:
                cache_data = None
            else:
                return cache_data
        
        logging.info(f"[CG_INFO] Запрашиваем данные для {symbol} (ID: {coin_id})")
        
        # Параметры запроса
        params = {
            "localization": "false",
            "tickers": "true" if include_tickers else "false",
            "market_data": "true",
            "community_data": "false",
            "developer_data": "false",
            "sparkline": "false"
        }

        data = self._make_request(f"/coins/{coin_id}", params)

        if data:
            self._save_to_cache(coin_id, data, variant=cache_variant)
            return data

        # Фолбэк: попытаться другие кандидаты с таким же символом (если они есть)
        logging.warning(f"[CG_INFO] Первичный ID {coin_id} не вернул данных для {symbol}. Попытка альтернативных кандидатов.")
        try:
            lower_symbol = (symbol or "").lower()
            matches = self.coins_list_by_symbol.get(lower_symbol, []) if lower_symbol else []
            tried = {str(coin_id)} if coin_id else set()
            for m in matches:
                cid = m.get('id')
                if not cid or cid in tried:
                    continue
                logging.info(f"[CG_INFO] Попытка альтернативного ID {cid} для {symbol}")
                alt = self._make_request(f"/coins/{cid}", params)
                tried.add(str(cid))
                if alt:
                    self._save_to_cache(cid, alt, variant=cache_variant)
                    logging.info(f"[CG_INFO] Альтернативный ID {cid} успешно вернул данные для {symbol}")
                    return alt
                else:
                    logging.debug(f"[CG_INFO] Альтернативный ID {cid} для {symbol} не дал данных.")
        except Exception as e:
            logging.debug(f"Ошибка попыток альтернативных ID для {symbol}: {e}")

        logging.warning(f"[CG_INFO] Не удалось получить данные CoinGecko Public для {symbol} (ID: {coin_id})")
        return None
        
    def _get_coinmarketcap_data_as_coingecko(self, symbol: str, exchange_volumes: Optional[Dict[str, float]] = None) -> Optional[Dict]:
        """
        Получает данные из CoinMarketCap и конвертирует их в формат, похожий на CoinGecko.
        
        Args:
            symbol: Символ монеты
            exchange_volumes: Словарь с объемами торгов
            
        Returns:
            Данные в формате CoinGecko или None
        """
        try:
            # CoinMarketCap adapter has been removed; we cannot convert CMC data.
            logging.debug("Conversion from CoinMarketCap to CoinGecko format requested, but adapter is removed.")
            return None
        except Exception as e:
            logging.error(f"Ошибка при получении данных из CoinMarketCap: {e}")
            return None

    def get_coin_data_by_id_sync(self, coin_id: str) -> Optional[Dict]:
        """
        Получает данные монеты по её ID (без поиска по символу).
        
        Args:
            coin_id: ID монеты в CoinGecko
            
        Returns:
            Данные о монете или None, если не найдена
        """
        logging.info(f"Синхронный запрос данных CoinGecko для ID: {coin_id}")
        url = f"/coins/{coin_id}"
        params = {
            "localization": "false",
            "tickers": "true",  # Запрашиваем тикеры
            "market_data": "true",
            "community_data": "false",
            "developer_data": "false",
            "sparkline": "false"
        }
        # Используем СИНХРОННЫЙ запрос
        data = self._make_request(url, params=params)

        if data:
            logging.info(f"Полные данные CoinGecko для {coin_id} успешно получены (синхронно).")
            return data
        else:
            logging.error(f"Не удалось получить данные CoinGecko для {coin_id} (синхронно).")
            return None

    def get_market_chart_sync(
        self,
        coin_id: str,
        *,
        vs_currency: str = "usd",
        days: Union[int, str] = 1,
        interval: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Возвращает историю цен для монеты через /coins/{id}/market_chart.

        Args:
            coin_id: идентификатор монеты в CoinGecko
            vs_currency: валюта (по умолчанию usd)
            days: число дней, либо строка ('1', '7', '30', 'max')
            interval: minutely|hourly|daily — опционально, CoinGecko сам подбирает при отсутствии

        Returns:
            Словарь с ключами 'prices', 'market_caps', 'total_volumes' (если успешно), иначе None
        """
        try:
            params: Dict[str, Any] = {
                "vs_currency": vs_currency,
                "days": days,
            }
            if interval:
                params["interval"] = interval
            data = self._make_request(f"/coins/{coin_id}/market_chart", params=params)
            if isinstance(data, dict) and isinstance(data.get("prices"), list):
                return data
            return None
        except Exception as e:
            logging.error(f"Ошибка запроса market_chart для {coin_id}: {e}")
            return None

    def get_market_chart_for_symbol_sync(
        self,
        symbol: str,
        *,
        vs_currency: str = "usd",
        days: Union[int, str] = 1,
        interval: Optional[str] = None,
        exchange_volumes: Optional[Dict[str, float]] = None,
    ) -> Optional[Dict[str, Any]]:
        """Ищет coin_id по символу (с учётом объёмов, если заданы) и возвращает историю цен.

        Args:
            symbol: тикер монеты
            vs_currency: валюта
            days: период в днях или 'max'
            interval: желаемая агрегация (minutely|hourly|daily)
            exchange_volumes: подсказки по объёмам бирж
        """
        try:
            coin_id = self.find_coin_id(symbol, exchange_volumes)
            if not coin_id:
                logging.warning(f"Не удалось определить coin_id для символа {symbol} при запросе market_chart")
                return None
            return self.get_market_chart_sync(coin_id, vs_currency=vs_currency, days=days, interval=interval)
        except Exception as e:
            logging.error(f"Ошибка при получении market_chart для символа {symbol}: {e}")
            return None

    def _coins_markets(self, ids: Optional[List[str]] = None, per_page: int = 250, page: int = 1) -> Optional[List[Dict]]:
        """Лёгкая сводка рынков: /coins/markets. Возвращает список монет с полями (id, symbol, total_volume, market_cap, ...).
        Учитывает rate-limit. Работает через текущий источник (Pro/Public).
        """
        try:
            params = {
                "vs_currency": "usd",
                "order": "market_cap_desc",
                "per_page": max(1, min(250, int(per_page))),
                "page": max(1, int(page)),
                "sparkline": "false",
                "price_change_percentage": "1h,24h,7d"
            }
            if ids:
                # Для выборки конкретного списка
                params["ids"] = ",".join(ids[:250])
                # per_page в этом случае CoinGecko может игнорировать — но не мешает
                params["per_page"] = min(250, len(ids))
            data = self._make_request("/coins/markets", params=params)
            if isinstance(data, list):
                return data
            return None
        except Exception as e:
            logging.error(f"Ошибка запроса /coins/markets: {e}")
            return None

    def fetch_top_markets(self, count: int = 250, retries: int = 3) -> List[Dict]:
        """Возвращает top-N монет по капе через /coins/markets (батчами по 250).
        Повторяет попытки при сбоях. Ограничение по умолчанию: не более 24 запросов/мин.
        """
        results: List[Dict] = []
        remaining = max(1, int(count))
        page = 1
        while remaining > 0:
            want = min(250, remaining)
            attempt = 0
            page_data: Optional[List[Dict]] = None
            while attempt < max(1, retries):
                page_data = self._coins_markets(per_page=want, page=page)
                if page_data is not None:
                    break
                attempt += 1
                time.sleep(self.retry_delay)
            if not page_data:
                # Прерываем на первой неудачной странице, чтобы не тратить лимиты
                break
            results.extend(page_data)
            got = len(page_data)
            if got < want:
                # Дальше страниц нет
                break
            remaining -= got
            page += 1
        return results

    def fetch_markets_by_ids(self, ids: List[str], retries: int = 2) -> List[Dict]:
        """Возвращает рынки по конкретным id (до 250 за один запрос). Делит список на чанки."""
        out: List[Dict] = []
        if not ids:
            return out
        # Уникальные, не пустые ID
        ids = list(dict.fromkeys(str(i).strip() for i in ids if i and str(i).strip()))
        chunk_size = 200 # Уменьшаем до 200 для стабильности на Pro API
        for i in range(0, len(ids), chunk_size):
            chunk = ids[i:i+chunk_size]
            attempt = 0
            data = None
            while attempt < max(1, retries):
                data = self._coins_markets(ids=chunk, per_page=len(chunk), page=1)
                if data is not None:
                    break
                attempt += 1
                time.sleep(self.retry_delay)
            if data:
                out.extend(data)
        return out
            
    def find_coin_id_by_contract(self, contract_address: str, platform: Optional[str] = None) -> Optional[str]:
        """
        Ищет ID монеты по адресу контракта.
        
        Args:
            contract_address: Адрес смарт-контракта
            platform: ID платформы CoinGecko (например, 'ethereum', 'binance-smart-chain')
            
        Returns:
            ID монеты в CoinGecko или None
        """
        if not contract_address or len(contract_address) < 10:
            return None
            
        addr = contract_address.strip().lower()
        logging.info(f"Поиск ID монеты по контракту: {addr} (платформа: {platform or 'любая'})")
        
        # Если платформа указана, пробуем прямой запрос к эндпоинту контракта
        platforms = [platform] if platform else [
            'ethereum', 'binance-smart-chain', 'polygon-pos', 'arbitrum-one', 
            'base', 'optimistic-ethereum', 'solana', 'tron', 'avalanche'
        ]
        
        for p in platforms:
            try:
                endpoint = f"/coins/{p}/contract/{addr}"
                # Мы не хотим спамить запросами, поэтому если платформы нет, 
                # пробуем только если адрес похож на формат этой платформы
                if not platform:
                    if p in ('ethereum', 'binance-smart-chain', 'polygon-pos', 'arbitrum-one', 'base', 'optimistic-ethereum', 'avalanche'):
                        if not addr.startswith('0x'): continue
                    elif p == 'solana':
                        if addr.startswith('0x') or len(addr) < 32: continue
                    elif p == 'tron':
                        if not addr.startswith('T') or len(addr) != 34: continue

                data = self._make_request(endpoint)
                if data and isinstance(data, dict) and data.get('id'):
                    logging.info(f"Найдена монета {data['id']} по контракту {addr} на платформе {p}")
                    return data['id']
            except Exception as e:
                logging.debug(f"Ошибка при поиске по контракту на {p}: {e}")
                
        return None

    def find_coin_id_by_volume(self, symbol: str, exchange_volumes: Dict[str, float]) -> Optional[str]:
        """
        Улучшенный алгоритм поиска ID монеты по символу и объему торгов на биржах.
        Ищет монету, которая торгуется на указанных биржах с близкими объемами торгов (±25%).
        Достаточно соответствия хотя бы на одной бирже, но приоритет отдается монетам с большим 
        количеством совпадений.
        
        Args:
            symbol: Символ монеты (тикер)
            exchange_volumes: Словарь {биржа: объем_торгов}
                Пример: {'mexc': 20000, 'gate.io': 30000}
        
        Returns:
            ID монеты в CoinGecko или None, если не найдена
        """
        if not self.coins_list:
            logging.warning("Список монет CoinGecko не загружен. Попытка обновить список.")
            if not self._update_coins_list():
                return None
            
        # Находим все монеты с таким символом
        lower_symbol = symbol.lower()
        candidates = self.coins_list_by_symbol.get(lower_symbol, [])
        
        if not candidates:
            logging.info(f"Монета с символом '{symbol}' не найдена в списке CoinGecko.")
            return None
            
        logging.info(f"Найдено {len(candidates)} кандидатов для символа '{symbol}'. Проверка объемов торгов...")
        
        # Если нет данных об объемах или нет бирж с объемами
        if not exchange_volumes or not exchange_volumes.keys():
            if len(candidates) == 1:
                return candidates[0]['id']
            else:
                # Если несколько кандидатов и нет данных об объемах, используем эвристики
                # Предпочитаем монеты с коротким ID и без цифр (обычно более известные)
                for candidate in sorted(candidates, key=lambda x: (len(x['id']), sum(1 for c in x['id'] if c.isdigit()))):
                    return candidate['id']
                
                # Если не сработала эвристика, вернем первого кандидата
                return candidates[0]['id']
        
        # Сортируем биржи по убыванию объема торгов
        exchanges = sorted(exchange_volumes.keys(), key=lambda x: exchange_volumes.get(x, 0), reverse=True)
        
        # Создаем словарь для оценки кандидатов: {id: количество совпадений по биржам}
        candidate_ids = [c['id'] for c in candidates]
        candidate_scores = {cid: 0 for cid in candidate_ids}
        
        # Получаем ДАННЫЕ В БАТЧЕ (существенно быстрее чем по одному)
        markets_data = self.fetch_markets_by_ids(candidate_ids)
        markets_by_id = {m['id']: m for m in markets_data}
        
        # Для детального анализа тикеров (если в markets недостаточно) 
        # или если данных в markets нет, будем использовать кэш или догружать.
        # Но сначала попробуем Price Guard по данным рынков (markets_data).
        
        # 1-й ЭТАП: Фильтр по Price Guard (±20% от текущей цены на биржах)
        # Находим референсную цену (среднюю по нашим биржам)
        ref_prices = []
        # Пытаемся получить живую цену из exchange_ticker_prices (если есть) или из аргументов
        # Для простоты возьмём среднее из переданных объёмов? Нет, объёмы не цены.
        # В этой функции у нас нет текущих цен. Но мы можем получить их от вызывающего?
        # В find_coin_id нет цен. 
        # Но мы можем добавить аргумент current_price.
        
        final_candidates = candidates
        
        # Если данных о рынках нет, фоллбэк к исходной логике (но батчем)
        # Но мы уже получили markets_by_id.
        
        # 2nd ЭТАП: Проверка наличия на биржах и объёмов
        coin_data_cache = {} # Для детальных данных тикеров (только если нужно)
        
        for cid in candidate_ids:
            # Сначала проверяем VOL/MCAP из markets (если есть)
            mdata = markets_by_id.get(cid)
            if not mdata:
                # Если в рынках нет, пробуем искать детально
                coin_data = self.get_coin_data_by_id_sync(cid)
                coin_data_cache[cid] = coin_data
            else:
                # В markets_data нет детальных тикеров по каждой бирже, 
                # там только общие данные (total_volume, mcap).
                # Но мы можем сопоставить общий объем.
                coin_data = self.get_coin_data_by_id_sync(cid) 
                coin_data_cache[cid] = coin_data

            if not coin_data or 'tickers' not in coin_data:
                continue

            # Проверяем каждую биржу
            for exchange in exchanges:
                exchange_volume = exchange_volumes.get(exchange, 0)
                if exchange_volume == 0: continue
                
                exchange_lower = exchange.lower()
                exchange_variants = {
                    'gate.io': ['gate', 'gateio', 'gate_io', 'gate-io'],
                    'mexc': ['mexc', 'mxc', 'mexcglobal', 'mexc-global'],
                    'kucoin': ['kucoin', 'kcs'],
                    'bybit': ['bybit'],
                    'okx': ['okx', 'okex', 'okcoin'],
                    'binance': ['binance', 'bnb', 'binance-us'],
                    'coinex': ['coinex'],
                    'bitget': ['bitget'],
                    'htx': ['htx', 'huobi'],
                    'bingx': ['bingx']
                }
                
                exchange_aliases = []
                for key, variants in exchange_variants.items():
                    if exchange_lower in variants or exchange_lower == key:
                        exchange_aliases = variants + [key]
                        break
                if not exchange_aliases: exchange_aliases = [exchange_lower]

                # Match tickers
                exchange_tickers = [t for t in coin_data['tickers'] if any(alias in (t['market']['identifier'] or '').lower() or alias in (t['market']['name'] or '').lower() for alias in exchange_aliases)]
                
                if exchange_tickers:
                    # Проверка объёма
                    for ticker in exchange_tickers:
                        cg_vol = ticker.get('converted_volume', {}).get('usd', 0)
                        if cg_vol > 0:
                            diff = abs(exchange_volume - cg_vol) / max(cg_vol, 0.0001) * 100
                            if diff <= 30: # Увеличим чуть до 30% для гибкости
                                candidate_scores[cid] += 2 # Совпадение по объёму весомее
                                break
                    else:
                        candidate_scores[cid] += 1 # Просто торгуется на этой бирже
        
        # Выбираем лучшего
        scored = sorted([(cid, score) for cid, score in candidate_scores.items() if score > 0], key=lambda x: x[1], reverse=True)
        if scored:
            logging.info(f"Matched {scored[0][0]} with score {scored[0][1]}")
            return scored[0][0]

        return candidates[0]['id']

async def fetch_coin_price(coin_id: str, vs_currency: str = "usd") -> Optional[Dict]:
    """
    Получает цену монеты с учетом возможного переключения на альтернативный API
    
    Args:
        coin_id: Идентификатор монеты
        vs_currency: Валюта для конвертации (по умолчанию USD)
        
    Returns:
        Optional[Dict]: Данные о цене монеты или None в случае ошибки
    """
    url = f"{COINGECKO_API_BASE}/simple/price?ids={coin_id}&vs_currencies={vs_currency}"
    success = False
    retry_count = 0
    max_retries = 3

    while not success and retry_count < max_retries:
        try:
            async with aiohttp.ClientSession(timeout=DEFAULT_TIMEOUT) as session:
                async with RetryClient(client_session=session, retry_options=retry_options) as client:
                    async with client.get(url) as response:
                        if response.status == 200:
                            price_data = await response.json()
                            api_manager.report_success('coingecko')
                            return price_data
                        elif response.status in {429, 502, 503, 504}:
                            api_manager.report_failure('coingecko')
                            success, message = api_manager.switch_source()
                            
                            if success and retry_count < max_retries - 1:
                                # api_manager attempted to switch source; CoinMarketCap adapter
                                # has been removed from the codebase, so do not attempt to use it.
                                if api_manager.get_current_source_info()['name'] == 'coinmarketcap':
                                    logging.debug("api_manager switched to 'coinmarketcap', but adapter was removed; skipping.")
                            else:
                                logging.error(f"Не удалось переключиться на альтернативный источник: {message}")
                        
                        retry_count += 1
                        if retry_count < max_retries:
                            await asyncio.sleep(2 ** retry_count)  # Экспоненциальная задержка
        except Exception as e:
            logging.error(f"Исключение при получении цены монеты {coin_id}: {e}")
            retry_count += 1
            if retry_count < max_retries:
                await asyncio.sleep(2 ** retry_count)
            
    return None

def get_coin_info_url(coin_id: str) -> str:
    """
    Возвращает URL для просмотра информации о монете
    
    Args:
        coin_id: Идентификатор монеты
        
    Returns:
        str: URL страницы с информацией о монете
    """
    return api_manager.get_coin_url(coin_id)
