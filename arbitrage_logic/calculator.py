import time
import logging
import asyncio
import traceback
from typing import Dict, List, Set, Tuple, Optional
from datetime import datetime

from config import Config
from exchanges import GateIO, Mexc, CoinEx, Bybit, OKX, KuCoin, Bitget, BingX, SafeTrade, NonKYC, BinanceUS, BinanceAlphaManual, KrakenPro, PionexUS, LBank, Exchange
from aiohttp_retry import RetryClient
from utils.symbols import normalize_pair_symbol

class ArbitrageCalculator:
    """
    Class for calculating arbitrage opportunities between exchanges.
    
    This class is responsible for:
    - Initializing exchange objects
    - Updating the list of common trading pairs
    - Getting prices from exchanges
    - Finding arbitrage opportunities
    """
    
    def __init__(self, config: Config):
        """
        Инициализирует калькулятор арбитража.
        
        Args:
            config: Объект конфигурации
        """
        self.config = config
        self.exchanges: List[Exchange] = self._init_exchanges()
        self.common_pairs: Set[str] = set()
        self.permanent_blacklist: Set[str] = set()
        self.last_pairs_update: float = 0.0

    def set_permanent_blacklist(self, symbols) -> None:
        """Exclude permanently blacklisted pairs from scanner work."""
        blocked: Set[str] = set()
        for symbol in symbols or []:
            try:
                norm = normalize_pair_symbol(symbol)
            except Exception:
                norm = str(symbol or "").strip().upper()
            if norm:
                blocked.add(norm)
        self.permanent_blacklist = blocked
        if blocked and self.common_pairs:
            before = len(self.common_pairs)
            self.common_pairs.difference_update(blocked)
            removed = before - len(self.common_pairs)
            if removed:
                logging.info("Permanent blacklist removed %s pairs from current common_pairs", removed)

    def _init_exchanges(self) -> List[Exchange]:
        """
        Инициализирует список объектов бирж на основе конфигурации.
        
        Returns:
            Список инициализированных объектов бирж
            
        Note:
            Для добавления новой биржи необходимо:
            1. Создать класс биржи в модуле exchanges
            2. Добавить импорт класса в exchanges/__init__.py
            3. Добавить класс в словарь exchange_classes в этом методе
        """
        enabled_names = self.config.get("enabled_exchanges", [])
        logging.info(f"Received list of enabled exchanges from configuration: {enabled_names}")
        if not isinstance(enabled_names, list):
             logging.warning("Параметр 'enabled_exchanges' в конфигурации не является списком. Используются биржи по умолчанию.")
             enabled_names = []

        exchange_classes: Dict[str, type] = {
            "Gate.io": GateIO,
            "MEXC": Mexc,
            "CoinEx": CoinEx,
            "Bybit": Bybit,
            "OKX": OKX,
            "KuCoin": KuCoin,
            "Bitget": Bitget,
            "BingX": BingX,
            "Binance.US": BinanceUS,
            "Binance Alpha (manual)": BinanceAlphaManual,
            "Kraken Pro": KrakenPro,
            "Pionex.US": PionexUS,
            "LBank": LBank,
            "SafeTrade": SafeTrade,
            "NonKYC": NonKYC,
        }
        initialized_exchanges = []
        for name, ex_class in exchange_classes.items():
            is_enabled = name in enabled_names
            logging.info(f"Проверка биржи {name}: включена = {is_enabled}")
            initialized_exchanges.append(ex_class(self.config, enabled=is_enabled))
            if is_enabled:
                 logging.info(f"Биржа {name} включена.")
            else:
                 logging.info(f"Биржа {name} отключена.")
        logging.info(f"Initialized {len(initialized_exchanges)} exchanges")
        return initialized_exchanges

    def _recover_disabled_exchanges(self) -> None:
        """Восстанавливает биржи, временно отключенные из-за ошибок, если прошел таймаут."""
        current_time = time.time()
        for ex in self.exchanges:
            if not ex.enabled and ex.last_error_time:
                time_since_error = current_time - ex.last_error_time
                # Восстанавливаем биржу, если прошло достаточно времени с момента последней ошибки
                # или если биржа была отключена очень давно (более 10 минут назад).
                if time_since_error >= ex.error_timeout or time_since_error > 600:
                    logging.info(f"Attempting to restore exchange {ex.name} after error timeout ({time_since_error:.1f} seconds ago)")
                    ex.enabled = True
                    ex.error_count = 0
                    ex.last_error_time = None

    def get_enabled_exchanges(self) -> List[Exchange]:
        """
        Возвращает список включенных и не отключенных из-за ошибок бирж.
        
        Returns:
            Список активных бирж
        """
        # Проверяем, прошло ли достаточно времени для восстановления отключенных бирж
        self._recover_disabled_exchanges()
        return [ex for ex in self.exchanges if ex.enabled]

    async def update_common_pairs(self, session: RetryClient) -> Set[str]:
        """
        Обновляет список общих торговых пар между включенными биржами.
        
        Args:
            session: Сессия HTTP-клиента
            
        Returns:
            Множество символов общих торговых пар
        """
        try:
            # get_enabled_exchanges() already calls _recover_disabled_exchanges()
            enabled_exchanges = self.get_enabled_exchanges()
            if not enabled_exchanges:
                 logging.warning("No enabled exchanges for updating pairs.")
                 if self.common_pairs:
                     logging.warning("Keeping previous common pairs (%s) due to missing enabled exchanges.", len(self.common_pairs))
                     return self.common_pairs
                 self.common_pairs = set()
                 return set()

            logging.info(f"Starting update of common pairs for {len(enabled_exchanges)} exchanges...")
            
            # Сначала проверяем соединение с каждой биржей
            connection_tasks = [ex.check_connection(session) for ex in enabled_exchanges]
            connection_results = await asyncio.gather(*connection_tasks, return_exceptions=True)
            # Логируем сырые результаты проверки соединения для диагностики
            try:
                conn_debug = {ex.name: (repr(r) if not isinstance(r, Exception) else f"Exception: {repr(r)}") for ex, r in zip(enabled_exchanges, connection_results)}
                logging.debug(f"Connection check raw results: {conn_debug}")
            except Exception:
                logging.debug("Connection check raw results: (failed to build debug map)")

            # Фильтруем биржи с успешным соединением
            connected_exchanges = []
            for ex, result in zip(enabled_exchanges, connection_results):
                if isinstance(result, bool) and result:
                    connected_exchanges.append(ex)
                    logging.info(f"Connection to {ex.name} established successfully.")
                elif isinstance(result, Exception):
                    logging.error(f"Error checking connection to {ex.name}: {result}")
                else:
                    logging.warning(f"Failed to establish connection to {ex.name}.")
    
            if not connected_exchanges:
                logging.error("CRITICAL ERROR: Failed to establish connection to any exchange! Check internet connection or proxy settings.")
                if self.common_pairs:
                    logging.warning("Keeping previous common pairs (%s) because all exchanges are temporarily unavailable.", len(self.common_pairs))
                    return self.common_pairs
                self.common_pairs = set()
                return set()
    
            # Получаем пары только с бирж с успешным соединением
            tasks = [ex.get_all_pairs(session) for ex in connected_exchanges]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            pairs_list: List[Set[str]] = []
            for ex, result in zip(connected_exchanges, results):
                if isinstance(result, set):
                    pairs_list.append(result)
                    logging.info(f"Pairs for {ex.name} updated ({len(result)} pairs).")
                elif isinstance(result, Exception):
                    logging.error(f"Error updating pairs for {ex.name}: {result}")
                    tb_str = "".join(traceback.format_exception(type(result), result, result.__traceback__))
                    logging.debug("Raw exception traceback for get_all_pairs:\n%s", tb_str)
                else:
                     logging.warning(f"Неожиданный результат обновления пар для {ex.name}: {type(result)}")

            # Диагностический лог: какие биржи вернули пары (количество) и примеры
            try:
                pairs_debug = {}
                for ex, result in zip(connected_exchanges, results):
                    if isinstance(result, set):
                        sample = list(result)[:5]
                        pairs_debug[ex.name] = {"count": len(result), "sample": sample}
                    elif isinstance(result, Exception):
                        pairs_debug[ex.name] = {"error": repr(result)}
                    else:
                        pairs_debug[ex.name] = {"type": str(type(result)), "repr": repr(result)}
                logging.debug(f"Pairs fetch raw results: {pairs_debug}")
            except Exception:
                logging.debug("Failed to build pairs_debug map")

            if not pairs_list:
                logging.warning("Failed to get pairs from any exchange.")
                if self.common_pairs:
                    logging.warning("Keeping previous common pairs (%s) after empty pairs update.", len(self.common_pairs))
                    return self.common_pairs
                self.common_pairs = set()
                return set()

            frequency: Dict[str, int] = {}
            for p_set in pairs_list:
                for pair in p_set:
                    frequency[pair] = frequency.get(pair, 0) + 1

            common = {pair for pair, count in frequency.items() if count >= 2}
            if self.permanent_blacklist:
                before_common = len(common)
                common.difference_update(self.permanent_blacklist)
                removed_common = before_common - len(common)
                if removed_common:
                    logging.info("Permanent blacklist excluded %s pairs from common_pairs update", removed_common)

            if not common:
                 logging.warning("No common pairs found (present on at least 2 exchanges).")
                 if self.common_pairs:
                     logging.warning("Keeping previous common pairs (%s) because new common set is empty.", len(self.common_pairs))
                     return self.common_pairs

            self.common_pairs = common
            self.last_pairs_update = time.time()
            logging.info(f"Common pairs (>=2 exchanges) updated: {len(self.common_pairs)} pairs.")
            return self.common_pairs
        except Exception as e:
            logging.error(f"Critical error in update_common_pairs: {e}\n{traceback.format_exc()}")
            # Keep the last known-good pair set. Returning an empty set here makes
            # the scanner look dead after one transient exchange/network failure.
            if self.common_pairs:
                logging.warning("Keeping previous common pairs (%s) after critical update error.", len(self.common_pairs))
                return self.common_pairs
            return set()

    async def fetch_order_books(self, session: RetryClient) -> Dict[str, Dict[str, Dict[str, float]]]:
        """
        Асинхронно получает данные книги ордеров со всех включенных бирж для общих пар.
        
        Args:
            session: Сессия HTTP-клиента
            
        Returns:
            Словарь {биржа: {символ: {bid: цена_покупки, ask: цена_продажи, bid_volume: объем_покупки, ask_volume: объем_продажи}}}
        """
        enabled_exchanges = self.get_enabled_exchanges()
        if not enabled_exchanges:
            return {}
        
        if not self.common_pairs:
            logging.warning("Нет общих пар для получения книг ордеров.")
            return {}

        logging.info(f"Начало получения книг ордеров с {len(enabled_exchanges)} бирж...")
        
        # Преобразуем множество в список для передачи в get_order_books
        common_pairs_list = list(self.common_pairs)
        
        tasks = [ex.get_order_books(session, common_pairs_list) for ex in enabled_exchanges]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        all_order_books: Dict[str, Dict[str, Dict[str, float]]] = {}
        for ex, result in zip(enabled_exchanges, results):
            if isinstance(result, dict):
                all_order_books[ex.name] = result
                logging.info(f"Книги ордеров для {ex.name}: получено {len(result)} для общих пар.")
            elif isinstance(result, Exception):
                logging.error(f"Ошибка получения книг ордеров для {ex.name}: {result}")
                all_order_books[ex.name] = {}
            else:
                logging.warning(f"Неожиданный результат получения книг ордеров для {ex.name}: {type(result)}")
                all_order_books[ex.name] = {}

        return all_order_books

    async def fetch_order_books_for(
        self,
        session: RetryClient,
        symbols: List[str],
        *,
        exchange_names: Optional[List[str]] = None,
        per_exchange_timeout_sec: Optional[float] = None,
    ) -> Dict[str, Dict[str, Dict[str, float]]]:
        """
        Асинхронно получает данные книги ордеров со всех включенных бирж для указанных символов.
        
        Args:
            session: Сессия HTTP-клиента
            symbols: Список символов (например, ["BTC/USDT"]) для запроса
            exchange_names: Если задано, ограничивает запрос только этими биржами (по имени)
            per_exchange_timeout_sec: Таймаут (сек) на одну биржу (best-effort)
             
        Returns:
            Словарь {биржа: {символ: {bid, ask, bid_volume, ask_volume}}}
        """
        enabled_exchanges = self.get_enabled_exchanges()
        if exchange_names:
            wanted = {str(n).strip().lower() for n in exchange_names if n and str(n).strip()}
            enabled_exchanges = [ex for ex in enabled_exchanges if str(ex.name).lower() in wanted]
        if not enabled_exchanges or not symbols:
            return {}
        
        logging.info(f"Запрос книг ордеров для {len(symbols)} символов с {len(enabled_exchanges)} бирж...")
        try:
            all_order_books: Dict[str, Dict[str, Dict[str, float]]] = {ex.name: {} for ex in enabled_exchanges}
            tasks = []
            task_exchanges: List[Exchange] = []
            sym_list = list(symbols)

            for ex in enabled_exchanges:
                try:
                    syms_for_ex = sym_list
                    # Если биржа уже получила список пар, не шлём ей заведомо невалидные символы
                    avail = getattr(ex, "available_pairs", None)
                    if isinstance(avail, set) and avail:
                        syms_for_ex = [s for s in sym_list if s in avail]
                    if not syms_for_ex:
                        continue
                    coro = ex.get_order_books(session, list(syms_for_ex))
                    if per_exchange_timeout_sec and per_exchange_timeout_sec > 0:
                        coro = asyncio.wait_for(coro, timeout=float(per_exchange_timeout_sec))
                    tasks.append(coro)
                    task_exchanges.append(ex)
                except Exception:
                    continue

            results = await asyncio.gather(*tasks, return_exceptions=True) if tasks else []
        except Exception as e:
            logging.error(f"Ошибка параллельного запроса книг ордеров: {e}")
            return {}
        
        # Заполняем только те биржи, на которые реально были задачи
        for ex, result in zip(task_exchanges, results):
            if isinstance(result, dict):
                all_order_books[ex.name] = result
            elif isinstance(result, Exception):
                err_text = str(result).strip() or result.__class__.__name__
                if isinstance(result, TimeoutError) or result.__class__.__name__ == "TimeoutError":
                    logging.warning(f"Order book fetch timeout for {ex.name}: {err_text}")
                else:
                    logging.error(f"Order book fetch error for {ex.name}: {err_text}")
            else:
                logging.warning(f"Неожиданный результат получения книг ордеров для {ex.name}: {type(result)}")
        
        return all_order_books

    # Добавим метод для получения объемов торгов

    async def fetch_volumes(self, session: RetryClient) -> Dict[str, Dict[str, float]]:
        """
        Асинхронно получает объемы торгов со всех включенных бирж для общих пар.
        
        Args:
            session: Сессия HTTP-клиента
            
        Returns:
            Словарь {биржа: {символ: объем_в_USD}}
        """
        enabled_exchanges = self.get_enabled_exchanges()
        if not enabled_exchanges:
            return {}
        
        if not self.common_pairs:
            logging.warning("Нет общих пар для получения объемов торгов.")
            return {}

        logging.info(f"Начало получения объемов торгов с {len(enabled_exchanges)} бирж...")
        
        # Преобразуем множество в список для передачи в get_volumes
        common_pairs_list = list(self.common_pairs)
        
        tasks = [ex.get_volumes(session, common_pairs_list) for ex in enabled_exchanges]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        all_volumes: Dict[str, Dict[str, float]] = {}
        for ex, result in zip(enabled_exchanges, results):
            if isinstance(result, dict):
                all_volumes[ex.name] = result
                logging.info(f"Объемы торгов для {ex.name}: получено {len(result)} для общих пар.")
            elif isinstance(result, Exception):
                logging.error(f"Ошибка получения объемов торгов для {ex.name}: {result}")
                all_volumes[ex.name] = {}
            else:
                logging.warning(f"Неожиданный результат получения объемов торгов для {ex.name}: {type(result)}")
                all_volumes[ex.name] = {}

        return all_volumes

    async def fetch_batch_prices(self, session: RetryClient) -> Dict[str, Dict[str, float]]:
        """
        Асинхронно получает цены (тикеры) со всех включенных бирж.
        
        Args:
            session: Сессия HTTP-клиента
            
        Returns:
            Словарь {биржа: {символ: цена}} для всех бирж и торговых пар
            
        Note:
            Этот метод устаревает и будет заменен на fetch_order_books
        """
        enabled_exchanges = self.get_enabled_exchanges()
        if not enabled_exchanges:
            logging.warning("No enabled exchanges for fetching tickers!")
            return {}

        logging.info(f"Started fetching tickers from {len(enabled_exchanges)} exchanges: {[ex.name for ex in enabled_exchanges]}")
        tasks = [ex.get_all_tickers(session) for ex in enabled_exchanges]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        all_prices: Dict[str, Dict[str, float]] = {}
        for ex, result in zip(enabled_exchanges, results):
            if isinstance(result, dict):
                # Заменяем dict comprehension на более безопасный цикл с обработкой ошибок
                filtered_prices: Dict[str, float] = {}
                normalized_hits = 0
                for s, p in result.items():
                    try:
                        normalized_symbol = normalize_pair_symbol(s)
                        symbol_key = normalized_symbol or str(s)
                        # Проверяем, что символ общий и цена - валидное число > 0
                        if symbol_key in self.common_pairs:
                            price_float = float(p)
                            if price_float > 0:
                                filtered_prices[symbol_key] = price_float
                                if symbol_key != s:
                                    normalized_hits += 1
                            else:
                                # Логируем нулевые или отрицательные цены
                                logging.debug(f"[{ex.name}] Нулевая или отрицательная цена для '{symbol_key}': {p}")
                    except (ValueError, TypeError):
                        # Ловим ошибки преобразования цены или нечисловые типы
                        logging.warning(f"[{ex.name}] Некорректные данные цены для символа '{s}': Цена='{p}'. Пропускаем.")
                    except Exception as e:
                        # Ловим другие неожиданные ошибки при обработке этой пары
                        logging.error(f"[{ex.name}] Неожиданная ошибка обработки символа '{s}': Цена='{p}', Ошибка='{e}'. Пропускаем.")
                
                # После цикла добавляем отфильтрованные цены и логируем результат
                all_prices[ex.name] = filtered_prices
                logging.info(f"Tickers for {ex.name}: received {len(result)}, filtered {len(filtered_prices)} for common pairs.")
                if normalized_hits:
                    logging.info(f"Tickers for {ex.name}: normalized {normalized_hits} raw symbols to match common pairs.")
            elif isinstance(result, Exception):
                logging.error(f"Ошибка получения тикеров для {ex.name}: {result}")
                all_prices[ex.name] = {}
            else:
                logging.warning(f"Неожиданный результат получения тикеров для {ex.name}: {type(result)}")
                all_prices[ex.name] = {}

        return all_prices

    def find_opportunities_from_order_books(self, all_order_books: Dict[str, Dict[str, Dict[str, float]]]) -> List[dict]:
        """
        Находит арбитражные возможности на основе данных книги ордеров.
        
        Args:
            all_order_books: Словарь {биржа: {символ: {bid: цена_покупки, ask: цена_продажи, bid_volume: объем_покупки, ask_volume: объем_продажи}}}
            
        Returns:
            Список словарей с информацией о найденных арбитражных возможностях
        """
        all_opportunities = []
        
        # Проверяем входные данные
        if not isinstance(all_order_books, dict):
            logging.error(f"Неверный тип данных для all_order_books: {type(all_order_books)}")
            return all_opportunities
        
        # Получаем список всех символов, для которых есть данные книги ордеров хотя бы на двух биржах
        symbol_exchange_count = {}
        for exchange, order_books in all_order_books.items():
            if isinstance(order_books, dict):
                for symbol in order_books.keys():
                    symbol_exchange_count[symbol] = symbol_exchange_count.get(symbol, 0) + 1
        
        valid_symbols = [symbol for symbol, count in symbol_exchange_count.items() if count >= 2]
        
        for symbol in sorted(valid_symbols):
            # Собираем данные книги ордеров для текущего символа со всех бирж
            symbol_data = {}
            for exchange, order_books in all_order_books.items():
                if isinstance(order_books, dict) and symbol in order_books:
                    order_book = order_books[symbol]
                    if isinstance(order_book, dict) and "bid" in order_book and "ask" in order_book:
                        symbol_data[exchange] = order_book
            
            # Находим возможности для текущего символа
            if len(symbol_data) >= 2:
                opportunities = self._find_opportunities_for_symbol_from_order_books(symbol, symbol_data)
                all_opportunities.extend(opportunities)
        
        logging.info(f"Found {len(all_opportunities)} arbitrage opportunities based on order books.")
        return all_opportunities
    
    def _find_opportunities_for_symbol_from_order_books(self, symbol: str, order_books_by_exchange: Dict[str, Dict[str, float]]) -> List[dict]:
        """
        Находит арбитражные возможности для одного символа на основе данных книги ордеров.
        
        Args:
            symbol: Символ торговой пары
            order_books_by_exchange: Словарь {биржа: {bid: цена_покупки, ask: цена_продажи, bid_volume: объем_покупки, ask_volume: объем_продажи}}
            
        Returns:
            Список словарей с информацией о найденных арбитражных возможностях
        """
        opportunities = []

        def _is_alpha_exchange(name: str) -> bool:
            return "binance alpha" in str(name or "").lower()

        def _mid_price(book: Dict[str, float]) -> Optional[float]:
            try:
                bid = float(book.get("bid") or 0)
                ask = float(book.get("ask") or 0)
                if bid > 0 and ask > 0:
                    return (bid + ask) / 2.0
                last = float(book.get("last") or 0)
                return last if last > 0 else None
            except Exception:
                return None

        def _alpha_reference(symbol_books: Dict[str, Dict[str, float]]) -> Tuple[Optional[float], int]:
            vals = []
            for ex_name, book in symbol_books.items():
                if _is_alpha_exchange(ex_name):
                    continue
                mid = _mid_price(book)
                if mid and mid > 0:
                    vals.append(float(mid))
            if not vals:
                return None, 0
            vals.sort()
            n = len(vals)
            if n % 2:
                return vals[n // 2], n
            return (vals[n // 2 - 1] + vals[n // 2]) / 2.0, n

        def _alpha_price_guard(ex_name: str, book: Dict[str, float], symbol_books: Dict[str, Dict[str, float]]) -> Tuple[bool, str, Optional[float], Optional[float], int]:
            if not _is_alpha_exchange(ex_name):
                return True, "", None, None, 0
            ref, sources = _alpha_reference(symbol_books)
            try:
                max_dev = float(self.config.get("alpha_manual_price_match_pct", 30.0))
            except Exception:
                max_dev = 30.0
            try:
                min_sources = int(self.config.get("alpha_manual_min_cex_sources", 1))
            except Exception:
                min_sources = 1
            if bool(self.config.get("alpha_manual_reject_duplicate_symbols", True)):
                try:
                    dup_count = int(book.get("alpha_duplicate_count") or 1)
                except Exception:
                    dup_count = 1
                if dup_count > 1:
                    return False, f"alpha_match_blocked: ambiguous Alpha ticker for {symbol}, {dup_count} Alpha tokens share this symbol", _mid_price(book), None, 0
            mid = _mid_price(book)
            if ref is None or sources < min_sources:
                return False, f"alpha_match_blocked: no reliable non-Alpha reference for {symbol}", mid, ref, sources
            if not mid or mid <= 0:
                return False, f"alpha_match_blocked: no valid Alpha mid price for {symbol}", mid, ref, sources
            diff_pct = abs(mid - ref) / ref * 100.0 if ref > 0 else 999999.0
            if diff_pct > max_dev:
                return False, f"alpha_match_blocked: Alpha price differs from CEX median by {diff_pct:.2f}% > {max_dev:.2f}%", mid, ref, sources
            return True, f"alpha_match_ok: diff {diff_pct:.2f}% vs CEX median from {sources} sources", mid, ref, sources

        # Binance Alpha is a manual/read-only source. It has many short/duplicate tickers.
        # Do not compare it by ticker blindly: require Alpha mid price to be close to
        # the current CEX median for the same normalized pair.
        alpha_guard_cache = {
            ex_name: _alpha_price_guard(ex_name, book, order_books_by_exchange)
            for ex_name, book in order_books_by_exchange.items()
            if _is_alpha_exchange(ex_name)
        }

        min_notional_cfg = self.config.get("arb_min_notional_usd", 300.0)
        try:
            depth_notional = float(min_notional_cfg)
        except (TypeError, ValueError):
            depth_notional = 300.0
        if depth_notional <= 0:
            depth_notional = 300.0
        depth_filter_enabled = bool(self.config.get("ui_arb_filter_liquidity", False))

        def _calc_notional(price, volume):
            try:
                p = float(price)
                v = float(volume)
                if p <= 0 or v <= 0:
                    return None
                return p * v
            except (TypeError, ValueError):
                return None

        def _clean_levels(levels, fallback_price=None, fallback_volume=None):
            cleaned = []
            if isinstance(levels, list):
                for item in levels:
                    try:
                        if not isinstance(item, (list, tuple)) or len(item) < 2:
                            continue
                        price = float(item[0])
                        volume = float(item[1])
                        if price > 0 and volume > 0:
                            cleaned.append((price, volume))
                    except (TypeError, ValueError):
                        continue
            if not cleaned and fallback_price is not None and fallback_volume is not None:
                try:
                    price = float(fallback_price)
                    volume = float(fallback_volume)
                    if price > 0 and volume > 0:
                        cleaned.append((price, volume))
                except (TypeError, ValueError):
                    pass
            return cleaned

        def _simulate_buy_quote(asks, notional_usd):
            spent = 0.0
            qty = 0.0
            levels_used = 0
            for price, volume in asks:
                remaining = notional_usd - spent
                if remaining <= 0:
                    break
                level_quote = price * volume
                take_quote = min(remaining, level_quote)
                if take_quote <= 0:
                    continue
                spent += take_quote
                qty += take_quote / price
                levels_used += 1
            avg_price = (spent / qty) if qty > 0 else None
            return {
                "avg_price": avg_price,
                "quote_filled": spent,
                "base_qty": qty,
                "levels_used": levels_used,
                "filled": spent >= notional_usd * 0.999,
            }

        def _simulate_sell_base(bids, base_qty):
            received = 0.0
            qty = 0.0
            levels_used = 0
            for price, volume in bids:
                remaining = base_qty - qty
                if remaining <= 0:
                    break
                take_qty = min(remaining, volume)
                if take_qty <= 0:
                    continue
                received += take_qty * price
                qty += take_qty
                levels_used += 1
            avg_price = (received / qty) if qty > 0 else None
            return {
                "avg_price": avg_price,
                "quote_received": received,
                "base_qty": qty,
                "levels_used": levels_used,
                "filled": base_qty > 0 and qty >= base_qty * 0.999,
            }

        def _simulate_route(buy_data, sell_data):
            asks = _clean_levels(buy_data.get("asks"), buy_data.get("ask"), buy_data.get("ask_volume"))
            bids = _clean_levels(sell_data.get("bids"), sell_data.get("bid"), sell_data.get("bid_volume"))
            if not asks or not bids:
                return {
                    "executable": False,
                    "reason": "missing_orderbook_depth",
                }
            buy_fill = _simulate_buy_quote(asks, depth_notional)
            sell_fill = _simulate_sell_base(bids, buy_fill["base_qty"])
            executable = bool(buy_fill["filled"] and sell_fill["filled"] and buy_fill["quote_filled"] > 0)
            pnl = sell_fill["quote_received"] - buy_fill["quote_filled"]
            spread = (pnl / buy_fill["quote_filled"] * 100.0) if buy_fill["quote_filled"] > 0 else None
            return {
                "executable": executable,
                "reason": None if executable else "insufficient_depth",
                "notional_usd": depth_notional,
                "buy_avg_price": buy_fill["avg_price"],
                "sell_avg_price": sell_fill["avg_price"],
                "buy_quote_filled": buy_fill["quote_filled"],
                "sell_quote_received": sell_fill["quote_received"],
                "base_qty": min(buy_fill["base_qty"], sell_fill["base_qty"]),
                "gross_profit_usd": pnl,
                "gross_spread_pct": spread,
                "buy_levels_used": buy_fill["levels_used"],
                "sell_levels_used": sell_fill["levels_used"],
            }

        min_spread_cfg = self.config.get("min_spread", 0.1)
        max_spread_cfg = self.config.get("max_spread", 50.0)
        # Estimated transfer fee as % of notional (withdrawal fee placeholder)
        transfer_fee_pct = float(self.config.get("estimated_transfer_fee_pct", 0.0))

        def _build_opportunity(buy_ex, sell_ex, buy_data, sell_data, buy_fee, sell_fee):
            """Evaluate one direction (buy on buy_ex, sell on sell_ex) and return opportunity or None."""
            buy_price = buy_data["ask"]
            sell_price = sell_data["bid"]
            if buy_price <= 0 or sell_price <= 0:
                return None

            alpha_notes = []
            for ex_name in (buy_ex, sell_ex):
                if _is_alpha_exchange(ex_name):
                    ok, note, alpha_mid, alpha_ref, alpha_sources = alpha_guard_cache.get(ex_name, (False, "alpha_match_blocked: Alpha guard missing", None, None, 0))
                    if not ok:
                        logging.debug("Blocked Binance Alpha opportunity %s %s -> %s: %s", symbol, buy_ex, sell_ex, note)
                        return None
                    alpha_notes.append(note)

            raw_spread = ((sell_price - buy_price) / buy_price) * 100
            net_spread = raw_spread - (buy_fee + sell_fee) * 100 - transfer_fee_pct
            if not (min_spread_cfg <= raw_spread <= max_spread_cfg):
                return None

            depth = _simulate_route(buy_data, sell_data)
            if depth_filter_enabled:
                depth_spread = depth.get("gross_spread_pct")
                if (
                    not depth.get("executable")
                    or not isinstance(depth_spread, (int, float))
                    or depth_spread < float(min_spread_cfg)
                ):
                    return None

            volume = min(buy_data["ask_volume"], sell_data["bid_volume"])
            buy_notional = _calc_notional(buy_data["ask"], buy_data["ask_volume"])
            sell_notional = _calc_notional(sell_data["bid"], sell_data["bid_volume"])
            min_notional = min(buy_notional, sell_notional) if buy_notional is not None and sell_notional is not None else None

            manual_only = bool(
                getattr(next((ex for ex in self.exchanges if ex.name == buy_ex), None), "manual_only", False)
                or getattr(next((ex for ex in self.exchanges if ex.name == sell_ex), None), "manual_only", False)
            )
            execution_mode = "manual_signal" if manual_only else "market_data"

            return {
                "symbol": symbol,
                "buy_exchange": buy_ex,
                "sell_exchange": sell_ex,
                "buy_price": depth.get("buy_avg_price") if depth_filter_enabled and depth.get("buy_avg_price") else buy_price,
                "sell_price": depth.get("sell_avg_price") if depth_filter_enabled and depth.get("sell_avg_price") else sell_price,
                "spread": depth.get("gross_spread_pct") if depth_filter_enabled and isinstance(depth.get("gross_spread_pct"), (int, float)) else raw_spread,
                "net_spread": net_spread,
                "volume": volume,
                "buy_volume": buy_data.get("ask_volume"),
                "sell_volume": sell_data.get("bid_volume"),
                "buy_liquidity_usd": buy_notional,
                "sell_liquidity_usd": sell_notional,
                "min_liquidity_usd": min_notional,
                "liquidity_ok": bool(min_notional is not None and min_notional >= min_notional_cfg),
                "depth_executable": bool(depth.get("executable")),
                "depth_notional_usd": depth.get("notional_usd"),
                "depth_buy_avg_price": depth.get("buy_avg_price"),
                "depth_sell_avg_price": depth.get("sell_avg_price"),
                "depth_gross_profit_usd": depth.get("gross_profit_usd"),
                "depth_gross_spread_pct": depth.get("gross_spread_pct"),
                "depth_buy_levels_used": depth.get("buy_levels_used"),
                "depth_sell_levels_used": depth.get("sell_levels_used"),
                "depth_reason": depth.get("reason"),
                "manual_only": manual_only,
                "execution_mode": execution_mode,
                "note": ("MANUAL ONLY: source has market data but no normal trading API" if manual_only else "") + (("; " + "; ".join(alpha_notes)) if alpha_notes else ""),
                "alpha_match_note": "; ".join(alpha_notes) if alpha_notes else "",
                "timestamp": datetime.now().isoformat(),
            }

        exchanges = list(order_books_by_exchange.keys())

        for i in range(len(exchanges)):
            for j in range(i + 1, len(exchanges)):
                try:
                    ex1_name, ex1_data = exchanges[i], order_books_by_exchange[exchanges[i]]
                    ex2_name, ex2_data = exchanges[j], order_books_by_exchange[exchanges[j]]

                    ex1_obj = next((ex for ex in self.exchanges if ex.name == ex1_name), None)
                    ex2_obj = next((ex for ex in self.exchanges if ex.name == ex2_name), None)
                    fee1 = ex1_obj.trading_fee if ex1_obj else 0.001
                    fee2 = ex2_obj.trading_fee if ex2_obj else 0.001

                    if not all(key in ex1_data for key in ("bid", "ask", "bid_volume", "ask_volume")) or \
                       not all(key in ex2_data for key in ("bid", "ask", "bid_volume", "ask_volume")):
                        continue

                    opp = _build_opportunity(ex1_name, ex2_name, ex1_data, ex2_data, fee1, fee2)
                    if opp:
                        opportunities.append(opp)

                    opp = _build_opportunity(ex2_name, ex1_name, ex2_data, ex1_data, fee2, fee1)
                    if opp:
                        opportunities.append(opp)

                except ZeroDivisionError:
                    logging.warning(f"Division by zero calculating spread for {symbol} between {exchanges[i]} and {exchanges[j]}")
                except (TypeError, ValueError) as e:
                    logging.warning(f"Type error calculating spread for {symbol} between {exchanges[i]} and {exchanges[j]}: {e}")
                except KeyError as e:
                    logging.warning(f"Key error building opportunity for {symbol} between {exchanges[i]} and {exchanges[j]}: {e}")
                except Exception as e:
                    logging.error(f"Unexpected error for {symbol} between {exchanges[i]} and {exchanges[j]}: {e}")
        
        opportunities.sort(key=lambda x: x["spread"], reverse=True)
        return opportunities

    def find_opportunities_for_symbol(self, symbol: str, prices_by_exchange: Dict[str, float]) -> List[dict]:
        """
        Находит арбитражные возможности для одного символа на основе предоставленных цен.
        
        Args:
            symbol: Символ торговой пары
            prices_by_exchange: Словарь {биржа: цена} для данного символа
            
        Returns:
            Список словарей с информацией о найденных арбитражных возможностях
            
        Note:
            Этот метод устаревает и будет заменен на _find_opportunities_for_symbol_from_order_books
        """
        opportunities = []
        
        # Проверяем входные данные
        if not isinstance(prices_by_exchange, dict):
            logging.warning(f"Неверный тип данных для prices_by_exchange при обработке {symbol}: {type(prices_by_exchange)}")
            return opportunities
        
        # Фильтруем только валидные цены
        try:
            valid_prices = {ex_name: price for ex_name, price in prices_by_exchange.items() if price > 0}
        except Exception as e:
            logging.error(f"Ошибка при фильтрации цен для {symbol}: {e}")
            return opportunities

        if len(valid_prices) < 2:
            return opportunities

        exchanges = list(valid_prices.keys())
        prices = list(valid_prices.values())

        for i in range(len(exchanges)):
            for j in range(i + 1, len(exchanges)):
                try:
                    ex1_name, price1 = exchanges[i], prices[i]
                    ex2_name, price2 = exchanges[j], prices[j]

                    if price1 < price2:
                        buy_ex, sell_ex = ex1_name, ex2_name
                        buy_price, sell_price = price1, price2
                    else:
                        buy_ex, sell_ex = ex2_name, ex1_name
                        buy_price, sell_price = price2, price1

                    if buy_price <= 0:
                        logging.warning(f"Пропуск пары {symbol} {buy_ex}->{sell_ex}: цена покупки <= 0 ({buy_price})")
                        continue

                    spread = ((sell_price - buy_price) / buy_price) * 100

                    min_spread_cfg = self.config.get("min_spread", 0.1)
                    max_spread_cfg = self.config.get("max_spread", 50.0)

                    if min_spread_cfg <= spread <= max_spread_cfg:
                        manual_only = any("binance alpha" in str(x).lower() for x in (buy_ex, sell_ex))
                        opp = {
                            "symbol": symbol,
                            "buy_exchange": buy_ex,
                            "sell_exchange": sell_ex,
                            "buy_price": buy_price,
                            "sell_price": sell_price,
                            "spread": spread,
                            "manual_only": manual_only,
                            "execution_mode": "manual_signal" if manual_only else "market_data",
                            "note": "MANUAL ONLY: source has market data but no normal trading API" if manual_only else "",
                            "timestamp": datetime.now().isoformat()
                        }
                        opportunities.append(opp)
                except ZeroDivisionError:
                    logging.warning(f"Ошибка деления на ноль при расчете спреда для {symbol} между {exchanges[i]} и {exchanges[j]}")
                except (TypeError, ValueError) as e:
                    logging.warning(f"Ошибка типа данных при расчете спреда для {symbol} между {exchanges[i]} и {exchanges[j]}: {e}")
                except KeyError as e:
                    logging.warning(f"Ошибка доступа к ключу при создании словаря возможности для {symbol} между {exchanges[i]} и {exchanges[j]}: {e}")
                except Exception as e:
                    logging.error(f"Непредвиденная ошибка при расчете спреда для {symbol} между {exchanges[i]} и {exchanges[j]}: {e}")

        opportunities.sort(key=lambda x: x["spread"], reverse=True)
        return opportunities

    def find_all_opportunities(self, all_prices: Dict[str, Dict[str, float]]) -> List[dict]:
        """
        Находит все арбитражные возможности для всех общих пар.
        
        Args:
            all_prices: Словарь {биржа: {символ: цена}} для всех бирж и торговых пар
            
        Returns:
            Список словарей с информацией о всех найденных арбитражных возможностях
            
        Note:
            Этот метод устаревает и будет заменен на find_opportunities_from_order_books
        """
        all_opportunities = []
        
        # Проверяем входные данные
        if not isinstance(all_prices, dict):
            logging.error(f"Неверный тип данных для all_prices: {type(all_prices)}")
            return all_opportunities
        
        for symbol in sorted(list(self.common_pairs)):
            try:
                # Безопасно получаем цены для символа со всех бирж, где он есть
                symbol_prices = {
                    ex_name: prices.get(symbol) for ex_name, prices in all_prices.items() if isinstance(prices, dict) and prices.get(symbol)
                }
                
                # Находим возможности для текущего символа
                symbol_opps = self.find_opportunities_for_symbol(symbol, symbol_prices)
                all_opportunities.extend(symbol_opps)
            except Exception as e:
                logging.error(f"Ошибка при обработке символа {symbol}: {e}\n{traceback.format_exc()}")
                continue

        logging.info(f"Found {len(all_opportunities)} arbitrage opportunities from {len(self.common_pairs)} common pairs.")
        
        # Дополнительная статистика
        if all_opportunities:
            spreads = [opp["spread"] for opp in all_opportunities]
            avg_spread = sum(spreads) / len(spreads)
            max_spread = max(spreads)
            min_spread = min(spreads)
            logging.info(f"Spread statistics: average={avg_spread:.4f}%, maximum={max_spread:.4f}%, minimum={min_spread:.4f}%")
        
        return all_opportunities
