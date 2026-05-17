import aiohttp
import asyncio
import logging
import time
from typing import Tuple, Dict, Any, Optional, List, Union

class HttpUtil:
    def __init__(self, timeout=10):
        self.timeout = timeout
        self._session = None
        # API ключ CoinGecko берём из переменной окружения (если задан)
        import os
        self.coingecko_api_key = os.environ.get("COINGECKO_API_KEY", "")
        # Время последней ошибки 429 для CoinGecko API
        self.last_coingecko_error_time = 0
        # Время удержания после ошибки 429 (в секундах)
        self.coingecko_error_timeout = 60  # 1 минута
        # Использовать ли Pro API или публичный
        self.use_pro_api = True
        
    async def get_session(self):
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=self.timeout))
        return self._session
        
    async def make_async_request(self, url: str, headers: Optional[Dict[str, str]] = None) -> Tuple[int, Any]:
        """Make an async HTTP request and return status code and response data."""
        session = await self.get_session()
        
        # Проверяем, можно ли использовать Pro API для CoinGecko
        if "api.coingecko.com" in url:
            current_time = time.time()
            # Если прошло достаточно времени с последней ошибки, сбрасываем флаг и пробуем Pro API снова
            if not self.use_pro_api and current_time - self.last_coingecko_error_time > self.coingecko_error_timeout:
                self.use_pro_api = True
                logging.info("Таймаут ошибки CoinGecko API истек, пробуем Pro API снова")
            
            # Если используем Pro API и есть ключ, используем его
            if self.use_pro_api and self.coingecko_api_key:
                if "pro-api.coingecko.com" not in url:
                    # Заменяем базовый URL на Pro API
                    url = url.replace("api.coingecko.com", "pro-api.coingecko.com")
                
                # Добавляем или обновляем заголовок с API ключом
                if headers is None:
                    headers = {"x-cg-pro-api-key": self.coingecko_api_key}
                else:
                    headers["x-cg-pro-api-key"] = self.coingecko_api_key
                
                # Делаем запрос к Pro API
                try:
                    async with session.get(url, headers=headers) as response:
                        status = response.status
                        # Если получили 429 или ошибку авторизации, переключаемся на публичный API
                        if status in [429, 401, 403]:
                            logging.warning(f"Ошибка Pro API CoinGecko {status}, переключаемся на публичный API")
                            self.use_pro_api = False
                            self.last_coingecko_error_time = current_time
                            
                            # Меняем URL для повторного запроса к публичному API
                            public_url = url.replace("pro-api.coingecko.com", "api.coingecko.com")
                            # Убираем ключ API из заголовков
                            if headers and "x-cg-pro-api-key" in headers:
                                del headers["x-cg-pro-api-key"]
                                
                            # Повторяем запрос к публичному API
                            async with session.get(public_url, headers=headers) as public_response:
                                public_status = public_response.status
                                try:
                                    public_data = await public_response.json()
                                except:
                                    public_data = await public_response.text()
                                return public_status, public_data
                        
                        # Если успешный ответ или другая ошибка
                        try:
                            data = await response.json()
                        except:
                            data = await response.text()
                        return status, data
                except Exception as e:
                    logging.error(f"Ошибка запроса к Pro API CoinGecko {url}: {e}")
                    # В случае ошибки попробуем публичный API
                    self.use_pro_api = False
                    self.last_coingecko_error_time = current_time
        
        # Для всех остальных запросов или в случае ошибки Pro API
        try:
            # Если это запрос к CoinGecko и мы перешли сюда из-за ошибки Pro API
            if "pro-api.coingecko.com" in url:
                url = url.replace("pro-api.coingecko.com", "api.coingecko.com")
                if headers and "x-cg-pro-api-key" in headers:
                    del headers["x-cg-pro-api-key"]
            
            async with session.get(url, headers=headers) as response:
                status = response.status
                try:
                    data = await response.json()
                except:
                    data = await response.text()
                return status, data
        except Exception as e:
            logging.error(f"Error making request to {url}: {e}")
            return 0, None
            
    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
            
    # Volume fetching helpers for different exchanges
    async def get_volume_from_mexc(self, symbol: str) -> Optional[float]:
        """Gets 24h volume (in USDT) for a symbol from MEXC."""
        volume = None
        try:
            pair = f"{symbol.upper()}USDT"
            url = f"https://api.mexc.com/api/v3/ticker/24hr?symbol={pair}"
            status, data = await self.make_async_request(url)
            if status == 200 and isinstance(data, dict) and 'quoteVolume' in data:
                volume_str = data.get('quoteVolume')
                if volume_str:
                    volume = float(volume_str)
                    logging.debug(f"Volume {pair} from MEXC: {volume}")
        except Exception as e:
            logging.error(f"Error getting volume from MEXC for {symbol}: {e}")
        return volume

    async def get_volume_from_gateio(self, symbol: str) -> Optional[float]:
        """Gets 24h volume (in USDT) for a symbol from Gate.io."""
        volume = None
        try:
            pair_gate = f"{symbol.upper()}_USDT"
            url = f"https://api.gateio.ws/api/v4/spot/tickers?currency_pair={pair_gate}"
            status, data = await self.make_async_request(url)
            if status == 200 and isinstance(data, list) and len(data) > 0:
                volume_str = data[0].get('quote_volume')
                if volume_str:
                    volume = float(volume_str)
                    logging.debug(f"Volume {pair_gate} from Gate.io: {volume}")
        except Exception as e:
            logging.error(f"Error getting volume from Gate.io for {symbol}: {e}")
        return volume
        
    async def get_volume_from_okx(self, symbol: str) -> Optional[float]:
        """Gets 24h volume (in USDT) for a symbol from OKX."""
        volume = None
        try:
            pair = f"{symbol.upper()}-USDT"
            url = f"https://www.okx.com/api/v5/market/ticker?instId={pair}"
            status, data = await self.make_async_request(url)
            if status == 200 and isinstance(data, dict) and 'data' in data:
                data_list = data.get('data', [])
                if data_list and len(data_list) > 0:
                    volume_str = data_list[0].get('volCcy24h')
                    if volume_str:
                        volume = float(volume_str)
                        logging.debug(f"Volume {pair} from OKX: {volume}")
        except Exception as e:
            logging.error(f"Error getting volume from OKX for {symbol}: {e}")
        return volume
        
    async def get_volume_from_bybit(self, symbol: str) -> Optional[float]:
        """Gets 24h volume (in USDT) for a symbol from Bybit."""
        volume = None
        try:
            pair = f"{symbol.upper()}USDT"
            url = f"https://api.bybit.com/v5/market/tickers?category=spot&symbol={pair}"
            status, data = await self.make_async_request(url)
            if status == 200 and isinstance(data, dict) and 'result' in data:
                result = data.get('result', {})
                tickers = result.get('list', [])
                if tickers and len(tickers) > 0:
                    volume_str = tickers[0].get('volume24h')
                    if volume_str:
                        volume = float(volume_str)
                        logging.debug(f"Volume {pair} from Bybit: {volume}")
        except Exception as e:
            logging.error(f"Error getting volume from Bybit for {symbol}: {e}")
        return volume
        
    async def get_volume_from_coinex(self, symbol: str) -> Optional[float]:
        """Gets 24h volume (in USDT) for a symbol from CoinEx."""
        volume = None
        try:
            pair = f"{symbol.upper()}USDT"
            url = f"https://api.coinex.com/v1/market/ticker?market={pair}"
            status, data = await self.make_async_request(url)
            if status == 200 and isinstance(data, dict) and 'data' in data:
                ticker_data = data.get('data', {})
                if 'ticker' in ticker_data:
                    ticker = ticker_data.get('ticker', {})
                    volume_str = ticker.get('vol')
                    if volume_str:
                        volume = float(volume_str)
                        logging.debug(f"Volume {pair} from CoinEx: {volume}")
        except Exception as e:
            logging.error(f"Error getting volume from CoinEx for {symbol}: {e}")
        return volume
