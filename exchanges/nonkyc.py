import logging
from typing import Dict, Set, List, Optional, Any, Tuple
from aiohttp_retry import RetryClient

from exchanges.base_exchange import Exchange
from config import Config

class NonKYC(Exchange):
    """
    Адаптер для биржи NonKYC.
    Публичные API-эндпоинты не документированы, поэтому используется набор
    устойчивых эвристик и несколько возможных путей с фоллбэками.

    Цели адаптера:
    - Получать список рынков и выделять базовые активы, котируемые в USDT
    - Для заданных базовых символов получать верхние уровни книги ордеров (bid/ask)
    - Работать устойчиво: разные варианты форматов ответов, названий ключей и путей
    """

    def __init__(self, config: Config, enabled: bool = True):
        super().__init__(
            "NonKYC",
            # Базовые предположительные пути для рынков/тикеров/стаканов
            # Фактические запросы выполняются с фоллбэками внутри методов
            "https://nonkyc.io/api/markets",
            "https://nonkyc.io/api/tickers",
            "https://nonkyc.io/api/orderbook/{market}",
            config,
            enabled
        )
        # Сопоставление базового символа -> идентификатор рынка (используется в запросах стакана)
        self.symbol_to_market: Dict[str, str] = {}
        # Более правдоподобные заголовки для снижения вероятности блокировки
        self.headers.update({
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
            "Origin": "https://nonkyc.io",
            "Referer": "https://nonkyc.io/",
            "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7"
        })

    # ---------------------------
    # Внутренние вспомогательные методы
    # ---------------------------
    async def _fetch_markets(self, session: RetryClient) -> Optional[Any]:
        """Пробует получить список рынков по нескольким предположительным путям."""
        candidates = [
            "https://nonkyc.io/api/markets",
            "https://nonkyc.io/api/v1/markets",
            "https://nonkyc.io/api/public/markets",
            # На случай Peatio-совместимого API (как у SafeTrade)
            "https://nonkyc.io/api/v2/peatio/public/markets",
        ]
        for url in candidates:
            data = await self._make_request(session, url)
            if data:
                logging.info(f"[{self.name}] Получили ответ рынков по {url}")
                return data
        logging.warning(f"[{self.name}] Не удалось получить список рынков ни по одному из кандидатов")
        return None

    def _parse_pair_string(self, pair_str: str) -> Optional[Tuple[str, str]]:
        """Разбирает строку пары в формате 'BASE-USDT', 'BASE_USDT' или 'BASEUSDT'."""
        if not isinstance(pair_str, str):
            return None
        s = pair_str.strip().upper()
        # Популярные разделители
        for sep in ('-', '_', '/'):
            if sep in s:
                parts = s.split(sep)
                if len(parts) == 2:
                    return parts[0], parts[1]
        # Без разделителя: попробуем угадать окончание USDT
        if s.endswith('USDT') and len(s) > 4:
            return s[:-4], 'USDT'
        return None

    def _extract_base_quote(self, item: Dict[str, Any]) -> Optional[Tuple[str, str]]:
        """Извлекает базовый/квотируемый актив из объекта рынка с разнообразными ключами."""
        base = item.get("base") or item.get("base_currency") or item.get("baseAsset") or item.get("base_unit") or item.get("coin")
        quote = item.get("quote") or item.get("quote_currency") or item.get("quoteAsset") or item.get("quote_unit") or item.get("counter")
        if isinstance(base, str) and isinstance(quote, str):
            return base.upper(), quote.upper()
        # Попытка через поле id/symbol/name
        for key in ("id", "symbol", "name", "market", "pair"):
            v = item.get(key)
            if isinstance(v, str):
                parsed = self._parse_pair_string(v)
                if parsed:
                    return parsed[0].upper(), parsed[1].upper()
        return None

    def _extract_market_id(self, item: Dict[str, Any], base: str, quote: str) -> str:
        """Возвращает пригодный идентификатор рынка для запросов стакана."""
        for key in ("id", "symbol", "name", "market", "pair"):
            v = item.get(key)
            if isinstance(v, str) and len(v) >= 3:
                return v
        # Фоллбэк: используем формат с нижним подчеркиванием
        return f"{base.lower()}_{quote.lower()}"

    # ---------------------------
    # Публичные методы API
    # ---------------------------
    async def check_connection(self, session: RetryClient) -> bool:
        """Простая проверка соединения — пытаемся получить рынки и убедиться, что формат корректный."""
        try:
            data = await self._fetch_markets(session)
            if isinstance(data, (list, dict)):
                return True
            return False
        except Exception as e:
            logging.error(f"[{self.name}] Ошибка при проверке соединения: {e}")
            return False

    async def get_all_pairs(self, session: RetryClient) -> Set[str]:
        """
        Возвращает множество базовых активов, котируемых в USDT.
        Также заполняет self.symbol_to_market для дальнейших запросов стаканов.
        """
        data = await self._fetch_markets(session)
        pairs: Set[str] = set()
        self.symbol_to_market.clear()

        def handle_item(item: Dict[str, Any]):
            extracted = self._extract_base_quote(item)
            if not extracted:
                return
            base_u, quote_u = extracted
            if quote_u != "USDT":
                return
            market_id = self._extract_market_id(item, base_u, quote_u)
            pairs.add(base_u)
            self.symbol_to_market[base_u] = market_id

        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    handle_item(item)
                elif isinstance(item, str):
                    parsed = self._parse_pair_string(item)
                    if parsed and parsed[1] == 'USDT':
                        base_u = parsed[0].upper()
                        pairs.add(base_u)
                        self.symbol_to_market[base_u] = f"{parsed[0].lower()}_{parsed[1].lower()}"
        elif isinstance(data, dict):
            # Возможные структуры: {"markets": [...]}, {"data": [...]}, либо словарь пар
            seq = None
            for key in ("markets", "data", "result"):
                v = data.get(key)
                if isinstance(v, list):
                    seq = v
                    break
            if seq is None:
                # Словарь вида {"BTC-USDT": {...}, ...}
                for k, v in data.items():
                    if isinstance(k, str):
                        parsed = self._parse_pair_string(k)
                        if parsed and parsed[1] == 'USDT':
                            base_u = parsed[0].upper()
                            pairs.add(base_u)
                            self.symbol_to_market[base_u] = f"{parsed[0].lower()}_{parsed[1].lower()}"
                    if isinstance(v, dict):
                        handle_item(v)
            else:
                for item in seq:
                    if isinstance(item, dict):
                        handle_item(item)
        else:
            logging.warning(f"[{self.name}] Неожиданный формат ответа рынков: {type(data)}")

        logging.info(f"Получено {len(pairs)} USDT-базовых символов для {self.name}")
        self.available_pairs = pairs
        return pairs

    async def get_all_tickers(self, session: RetryClient) -> Dict[str, float]:
        """NonKYC: батч-тикеры не используются — полагаемся на книги ордеров."""
        return {}

    def _format_symbol_for_orderbook(self, symbol: str) -> Optional[str]:
        if not symbol:
            return None
        m = self.symbol_to_market.get(symbol.upper())
        if m:
            return m
        # Фоллбэки: разные форматы market id
        s = symbol.lower()
        return f"{s}_usdt"

    async def _fetch_orderbook_any(self, session: RetryClient, market_id: str) -> Optional[Dict[str, Any]]:
        """Пробует несколько путей получения стакана для market_id и возвращает первый валидный ответ."""
        candidates = [
            f"https://nonkyc.io/api/orderbook/{market_id}?limit=5",
            f"https://nonkyc.io/api/v1/orderbook/{market_id}?limit=5",
            f"https://nonkyc.io/api/orders/{market_id}",
            f"https://nonkyc.io/api/v1/orders/{market_id}",
            # Peatio-совместимый фоллбэк
            f"https://nonkyc.io/api/v2/peatio/public/markets/{market_id}/order-book",
        ]
        for url in candidates:
            data = await self._make_request(session, url)
            if isinstance(data, dict):
                return data
        return None

    def _extract_top(self, data: Dict[str, Any]) -> Optional[Tuple[Tuple[float, float], Tuple[float, float]]]:
        """Извлекает верхние уровни bid/ask из структуры данных разных форматов."""
        bids = data.get("bids") or data.get("buy") or data.get("bid")
        asks = data.get("asks") or data.get("sell") or data.get("ask")

        def first_price_amount(seq) -> Optional[Tuple[float, float]]:
            if not seq:
                return None
            # Форматы: [[price, amount], ...] или {price: amount, ...}
            if isinstance(seq, list):
                try:
                    elem = seq[0]
                    if isinstance(elem, (list, tuple)) and len(elem) >= 2:
                        return float(elem[0]), float(elem[1])
                    if isinstance(elem, dict):
                        # Попробуем ключи 'price'/'amount' или похожие
                        p = elem.get('price') or elem.get('rate') or elem.get('p')
                        a = elem.get('amount') or elem.get('quantity') or elem.get('q')
                        if p is not None and a is not None:
                            return float(p), float(a)
                except Exception:
                    return None
            elif isinstance(seq, dict):
                try:
                    # Берем первую пару price->amount
                    for k, v in seq.items():
                        return float(k), float(v)
                except Exception:
                    return None
            return None

        top_bid = first_price_amount(bids)
        top_ask = first_price_amount(asks)
        if top_bid and top_ask:
            return top_bid, top_ask
        return None

    async def get_order_books(self, session: RetryClient, symbols: List[str]) -> Dict[str, Dict[str, float]]:
        """
        Возвращает {symbol: {bid, ask, bid_volume, ask_volume}} для списка базовых символов.
        """
        result: Dict[str, Dict[str, float]] = {}
        for symbol in symbols:
            try:
                market_id = self._format_symbol_for_orderbook(symbol)
                if not market_id:
                    continue
                # Пробуем разные форматы market_id
                candidates_ids = [
                    market_id,
                    market_id.replace('_', '-'),
                    market_id.replace('_', ''),
                    f"usdt_{symbol.lower()}",
                    f"usdt-{symbol.lower()}"
                ]
                data = None
                for mid in candidates_ids:
                    data = await self._fetch_orderbook_any(session, mid)
                    if isinstance(data, dict):
                        break
                if not isinstance(data, dict):
                    continue

                top = self._extract_top(data)
                if not top:
                    continue
                bid_price, bid_vol = top[0]
                ask_price, ask_vol = top[1]

                if bid_price > 0 and ask_price > 0:
                    result[symbol] = {
                        "bid": bid_price,
                        "ask": ask_price,
                        "bid_volume": bid_vol,
                        "ask_volume": ask_vol
                    }
            except Exception as e:
                logging.error(f"[{self.name}] Ошибка получения стакана для {symbol}: {e}")
        return result