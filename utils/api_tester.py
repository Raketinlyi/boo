#!/usr/bin/env python3
"""
Скрипт для тестирования API бирж.
Проверяет доступность API, скорость ответа и обрабатывает ошибки.
"""

import asyncio
import time
import logging
import sys
import argparse
import json
import os
from datetime import datetime
from typing import Dict, List, Any, Optional, Tuple
from aiohttp import ClientSession, ClientTimeout, TCPConnector
from aiohttp_retry import RetryClient, ExponentialRetry

# Импортируем модуль обработки ошибок
from utils.error_handler import log_error, async_retry_on_error, API_ERROR, setup_advanced_logging

# Настройка логирования
setup_advanced_logging(log_dir="logs/api_tests", log_level=logging.DEBUG, console_level=logging.INFO)

class ApiTester:
    """Класс для тестирования API бирж."""
    
    def __init__(self, config_file: str = "config.json"):
        """
        Инициализирует тестер API.
        
        Args:
            config_file: Путь к файлу конфигурации
        """
        self.config_file = config_file
        self.config = self._load_config()
        self.results: Dict[str, Dict[str, Any]] = {}
        self.session: Optional[RetryClient] = None
        self.history_file = "api_test_history.json"
        self.history: Dict[str, List[Dict[str, Any]]] = self._load_history()
    
    def _load_config(self) -> Dict[str, Any]:
        """
        Загружает конфигурацию из файла.
        
        Returns:
            Словарь с конфигурацией
        """
        try:
            with open(self.config_file, 'r', encoding='utf-8') as f:
                config = json.load(f)
            logging.info(f"Конфигурация загружена из {self.config_file}")
            return config
        except Exception as e:
            log_error(API_ERROR, "ApiTester._load_config", e, {"config_file": self.config_file})
            return {}
    
    def _load_history(self) -> Dict[str, List[Dict[str, Any]]]:
        """
        Загружает историю тестирования API из файла.
        
        Returns:
            Словарь с историей тестирования
        """
        try:
            if os.path.exists(self.history_file):
                with open(self.history_file, 'r', encoding='utf-8') as f:
                    history = json.load(f)
                logging.info(f"История тестирования загружена из {self.history_file}")
                return history
            else:
                logging.info(f"Файл истории тестирования {self.history_file} не найден, создаем новый")
                return {}
        except Exception as e:
            log_error(API_ERROR, "ApiTester._load_history", e, {"history_file": self.history_file})
            return {}
    
    def _save_history(self) -> None:
        """Сохраняет историю тестирования API в файл."""
        try:
            with open(self.history_file, 'w', encoding='utf-8') as f:
                json.dump(self.history, f, indent=2, ensure_ascii=False)
            logging.info(f"История тестирования сохранена в файл {self.history_file}")
        except Exception as e:
            log_error(API_ERROR, "ApiTester._save_history", e, {"history_file": self.history_file})
    
    def _update_history(self) -> None:
        """Обновляет историю тестирования API."""
        timestamp = datetime.now().isoformat()
        
        for exchange_name, results in self.results.items():
            if exchange_name not in self.history:
                self.history[exchange_name] = []
            
            # Добавляем результаты текущего тестирования в историю
            history_entry = {
                "timestamp": timestamp,
                "success": results["success"],
                "total_time": results["total_time"],
                "errors": results["errors"]
            }
            
            # Ограничиваем историю 100 записями для каждой биржи
            self.history[exchange_name] = [history_entry] + self.history[exchange_name][:99]
        
        # Сохраняем обновленную историю
        self._save_history()
    
    async def _create_session(self) -> None:
        """Создает HTTP-сессию."""
        connector = TCPConnector(limit=100, ttl_dns_cache=300)
        timeout = ClientTimeout(total=self.config.get("price_timeout", 10))
        retry_options = ExponentialRetry(attempts=5, start_timeout=1, max_timeout=10, statuses={500, 502, 503, 504})
        
        self.session = RetryClient(
            ClientSession(connector=connector, timeout=timeout),
            retry_options=retry_options
        )
        
        logging.info(f"HTTP-сессия создана с таймаутом {timeout.total} секунд")
    
    async def _close_session(self) -> None:
        """Закрывает HTTP-сессию."""
        if self.session:
            await self.session.close()
            logging.info("HTTP-сессия закрыта")
    
    @async_retry_on_error(max_retries=2, error_type=API_ERROR)
    async def test_exchange(self, exchange_name: str, urls: Dict[str, str]) -> Dict[str, Any]:
        """
        Тестирует API биржи.
        
        Args:
            exchange_name: Название биржи
            urls: Словарь {тип_запроса: URL}
            
        Returns:
            Словарь с результатами тестирования
        """
        if not self.session:
            await self._create_session()
        
        results = {
            "name": exchange_name,
            "success": True,
            "endpoints": {},
            "total_time": 0,
            "errors": []
        }
        
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
        }
        
        for endpoint_type, url in urls.items():
            try:
                start_time = time.time()
                response = await self.session.get(url, headers=headers)
                end_time = time.time()
                
                response_time = end_time - start_time
                status_code = response.status
                
                if status_code == 200:
                    try:
                        data = await response.json(content_type=None)
                        results["endpoints"][endpoint_type] = {
                            "url": url,
                            "status": status_code,
                            "response_time": response_time,
                            "success": True
                        }
                    except Exception as e:
                        results["endpoints"][endpoint_type] = {
                            "url": url,
                            "status": status_code,
                            "response_time": response_time,
                            "success": False,
                            "error": f"Ошибка декодирования JSON: {e}"
                        }
                        results["errors"].append({
                            "endpoint": endpoint_type,
                            "error": f"Ошибка декодирования JSON: {e}"
                        })
                        results["success"] = False
                else:
                    results["endpoints"][endpoint_type] = {
                        "url": url,
                        "status": status_code,
                        "response_time": response_time,
                        "success": False,
                        "error": f"Ошибка HTTP: {status_code}"
                    }
                    results["errors"].append({
                        "endpoint": endpoint_type,
                        "error": f"Ошибка HTTP: {status_code}"
                    })
                    results["success"] = False
                
                results["total_time"] += response_time
            except Exception as e:
                log_error(API_ERROR, f"ApiTester.test_exchange.{exchange_name}.{endpoint_type}", e, {"url": url})
                results["endpoints"][endpoint_type] = {
                    "url": url,
                    "success": False,
                    "error": f"Ошибка запроса: {e}"
                }
                results["errors"].append({
                    "endpoint": endpoint_type,
                    "error": f"Ошибка запроса: {e}"
                })
                results["success"] = False
        
        return results
    
    async def test_all_exchanges(self) -> None:
        """Тестирует API всех бирж."""
        if not self.session:
            await self._create_session()
        
        exchange_configs = self._get_exchange_configs()
        
        for exchange_name, urls in exchange_configs.items():
            logging.info(f"Тестирование API биржи {exchange_name}...")
            results = await self.test_exchange(exchange_name, urls)
            self.results[exchange_name] = results
            
            if results["success"]:
                logging.info(f"API биржи {exchange_name} доступно. Время ответа: {results['total_time']:.2f} сек.")
            else:
                logging.warning(f"API биржи {exchange_name} недоступно или вернуло ошибки.")
                for error in results["errors"]:
                    logging.warning(f"  - {error['endpoint']}: {error['error']}")
    
    def _get_exchange_configs(self) -> Dict[str, Dict[str, str]]:
        """
        Получает конфигурации бирж.
        
        Returns:
            Словарь {биржа: {тип_запроса: URL}}
        """
        exchange_configs = {}
        
        # Определяем URL для каждой биржи
        exchange_configs["Gate.io"] = {
            "pairs": "https://api.gateio.ws/api/v4/spot/currency_pairs",
            "tickers": "https://api.gateio.ws/api/v4/spot/tickers"
        }
        
        exchange_configs["MEXC"] = {
            "pairs": "https://api.mexc.com/api/v3/exchangeInfo",
            "tickers": "https://api.mexc.com/api/v3/ticker/price"
        }
        
        exchange_configs["CoinEx"] = {
            "pairs": "https://api.coinex.com/v1/market/list",
            "tickers": "https://api.coinex.com/v1/market/ticker/all"
        }
        
        exchange_configs["Biconomy"] = {
            "pairs": "https://www.biconomy.com/api/v1/symbols",
            "tickers": "https://www.biconomy.com/api/v1/ticker/price"
        }
        
        exchange_configs["Bybit"] = {
            "pairs": "https://api.bybit.com/v5/market/instruments-info?category=spot",
            "tickers": "https://api.bybit.com/v5/market/tickers?category=spot"
        }
        
        exchange_configs["OKX"] = {
            "pairs": "https://www.okx.com/api/v5/public/instruments?instType=SPOT",
            "tickers": "https://www.okx.com/api/v5/market/tickers?instType=SPOT"
        }
        
        exchange_configs["BinanceUS"] = {
            "pairs": "https://api.binance.us/api/v3/exchangeInfo",
            "tickers": "https://api.binance.us/api/v3/ticker/price"
        }
        
        exchange_configs["Bitget"] = {
            "pairs": "https://api.bitget.com/api/spot/v1/public/products",
            "tickers": "https://api.bitget.com/api/spot/v1/market/tickers"
        }
        
        exchange_configs["HTX"] = {
            "pairs": "https://api.huobi.pro/v1/common/symbols",
            "tickers": "https://api.huobi.pro/market/tickers"
        }
        
        exchange_configs["KuCoin"] = {
            "pairs": "https://api.kucoin.com/api/v1/symbols",
            "tickers": "https://api.kucoin.com/api/v1/market/allTickers"
        }
        
        exchange_configs["BingX"] = {
            "pairs": "https://open-api.bingx.com/openApi/spot/v1/common/symbols",
            "tickers": "https://open-api.bingx.com/openApi/spot/v1/ticker/bookTicker"
        }
        
        # Bitrue and TradeOgre are removed: Bitrue does not publish a public
        # markets feed any longer and TradeOgre has been inactive for months.
        # If they come back, restore their entries here.
        
        return exchange_configs
    
    def print_results(self) -> None:
        """Выводит результаты тестирования."""
        logging.info("\nРезультаты тестирования API бирж:")
        
        for exchange_name, results in self.results.items():
            status = "✅ Доступно" if results["success"] else "❌ Недоступно"
            logging.info(f"\n{exchange_name}: {status}")
            
            if results["success"]:
                logging.info(f"  Общее время ответа: {results['total_time']:.2f} сек.")
                
                for endpoint_type, endpoint_results in results["endpoints"].items():
                    endpoint_status = "✅" if endpoint_results["success"] else "❌"
                    logging.info(f"  {endpoint_type}: {endpoint_status} {endpoint_results.get('response_time', 0):.2f} сек.")
            else:
                logging.info("  Ошибки:")
                for error in results["errors"]:
                    logging.info(f"  - {error['endpoint']}: {error['error']}")
    
    def save_results(self, output_file: str) -> None:
        """
        Сохраняет результаты тестирования в файл.
        
        Args:
            output_file: Путь к файлу для сохранения результатов
        """
        try:
            with open(output_file, 'w', encoding='utf-8') as f:
                json.dump(self.results, f, indent=2, ensure_ascii=False)
            
            logging.info(f"Результаты сохранены в файл {output_file}")
        except Exception as e:
            log_error(API_ERROR, "ApiTester.save_results", e, {"output_file": output_file})
    
    def analyze_history(self) -> Dict[str, Dict[str, Any]]:
        """
        Анализирует историю тестирования API.
        
        Returns:
            Словарь с результатами анализа
        """
        analysis = {}
        
        for exchange_name, history in self.history.items():
            if not history:
                continue
            
            total_tests = len(history)
            successful_tests = sum(1 for entry in history if entry["success"])
            success_rate = (successful_tests / total_tests) * 100 if total_tests > 0 else 0
            
            # Вычисляем среднее время ответа
            total_times = [entry["total_time"] for entry in history if "total_time" in entry]
            avg_time = sum(total_times) / len(total_times) if total_times else 0
            
            # Собираем все ошибки
            all_errors = []
            for entry in history:
                all_errors.extend(entry.get("errors", []))
            
            # Группируем ошибки по типу
            error_types = {}
            for error in all_errors:
                error_type = error.get("error", "Unknown error")
                if error_type not in error_types:
                    error_types[error_type] = 0
                error_types[error_type] += 1
            
            analysis[exchange_name] = {
                "total_tests": total_tests,
                "successful_tests": successful_tests,
                "success_rate": success_rate,
                "avg_response_time": avg_time,
                "error_types": error_types
            }
        
        return analysis
    
    def print_analysis(self, analysis: Dict[str, Dict[str, Any]]) -> None:
        """
        Выводит результаты анализа истории тестирования API.
        
        Args:
            analysis: Словарь с результатами анализа
        """
        logging.info("\nАнализ истории тестирования API бирж:")
        
        for exchange_name, results in analysis.items():
            logging.info(f"\n{exchange_name}:")
            logging.info(f"  Всего тестов: {results['total_tests']}")
            logging.info(f"  Успешных тестов: {results['successful_tests']}")
            logging.info(f"  Процент успеха: {results['success_rate']:.2f}%")
            logging.info(f"  Среднее время ответа: {results['avg_response_time']:.2f} сек.")
            
            if results["error_types"]:
                logging.info("  Типы ошибок:")
                for error_type, count in results["error_types"].items():
                    logging.info(f"    - {error_type}: {count} раз")
    
    async def run(self) -> None:
        """Запускает тестирование API бирж."""
        try:
            await self._create_session()
            await self.test_all_exchanges()
            self._update_history()
            
            # Анализируем историю
            analysis = self.analyze_history()
            self.print_analysis(analysis)
        finally:
            await self._close_session()

async def main():
    """Основная функция скрипта."""
    parser = argparse.ArgumentParser(description="Тестирование API бирж")
    parser.add_argument("--config", "-c", default="config.json", help="Путь к файлу конфигурации")
    parser.add_argument("--output", "-o", default="api_test_results.json", help="Файл для сохранения результатов")
    parser.add_argument("--analyze-only", "-a", action="store_true", help="Только анализ истории без тестирования")
    args = parser.parse_args()
    
    tester = ApiTester(args.config)
    
    if args.analyze_only:
        analysis = tester.analyze_history()
        tester.print_analysis(analysis)
    else:
        await tester.run()
        tester.print_results()
        tester.save_results(args.output)

if __name__ == "__main__":
    asyncio.run(main())
