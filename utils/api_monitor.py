#!/usr/bin/env python3
"""
Скрипт для регулярного мониторинга API бирж.
Запускает тестирование API через определенные интервалы времени.
"""

import asyncio
import time
import logging
import sys
import argparse
import json
import os
import signal
from datetime import datetime
from typing import Dict, List, Any, Optional

# Импортируем модуль обработки ошибок
from utils.error_handler import setup_advanced_logging
from utils.api_tester import ApiTester

# Настройка логирования
setup_advanced_logging(log_dir="logs/api_monitor", log_level=logging.DEBUG, console_level=logging.INFO)

class ApiMonitor:
    """Класс для мониторинга API бирж."""
    
    def __init__(self, config_file: str = "config.json", interval: int = 3600):
        """
        Инициализирует монитор API.
        
        Args:
            config_file: Путь к файлу конфигурации
            interval: Интервал между проверками в секундах (по умолчанию 1 час)
        """
        self.config_file = config_file
        self.interval = interval
        self.running = False
        self.tester = ApiTester(config_file)
    
    async def monitor_loop(self) -> None:
        """Основной цикл мониторинга."""
        self.running = True
        
        while self.running:
            logging.info(f"Запуск тестирования API бирж в {datetime.now().isoformat()}")
            
            try:
                await self.tester.run()
                
                # Сохраняем результаты с временной меткой
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                output_file = f"api_test_results_{timestamp}.json"
                self.tester.save_results(output_file)
                
                # Анализируем историю
                analysis = self.tester.analyze_history()
                self.tester.print_analysis(analysis)
                
                # Сохраняем анализ
                analysis_file = f"api_analysis_{timestamp}.json"
                try:
                    with open(analysis_file, 'w', encoding='utf-8') as f:
                        json.dump(analysis, f, indent=2, ensure_ascii=False)
                    logging.info(f"Анализ сохранен в файл {analysis_file}")
                except Exception as e:
                    logging.error(f"Ошибка при сохранении анализа в файл {analysis_file}: {e}")
            except Exception as e:
                logging.error(f"Ошибка при тестировании API: {e}")
            
            # Ждем до следующего запуска
            next_run = datetime.now().timestamp() + self.interval
            next_run_str = datetime.fromtimestamp(next_run).strftime("%Y-%m-%d %H:%M:%S")
            logging.info(f"Следующий запуск тестирования в {next_run_str} (через {self.interval} сек.)")
            
            # Ждем с проверкой флага running каждые 5 секунд
            wait_time = 0
            while wait_time < self.interval and self.running:
                await asyncio.sleep(5)
                wait_time += 5
    
    def stop(self) -> None:
        """Останавливает мониторинг."""
        self.running = False
        logging.info("Остановка мониторинга API...")

async def main():
    """Основная функция скрипта."""
    parser = argparse.ArgumentParser(description="Мониторинг API бирж")
    parser.add_argument("--config", "-c", default="config.json", help="Путь к файлу конфигурации")
    parser.add_argument("--interval", "-i", type=int, default=3600, help="Интервал между проверками в секундах")
    args = parser.parse_args()
    
    monitor = ApiMonitor(args.config, args.interval)
    
    # Обработчик сигналов для корректного завершения
    def signal_handler(sig, frame):
        logging.info(f"Получен сигнал {sig}, завершение работы...")
        monitor.stop()
    
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    try:
        await monitor.monitor_loop()
    except Exception as e:
        logging.error(f"Ошибка в цикле мониторинга: {e}")
    finally:
        logging.info("Мониторинг API завершен.")

if __name__ == "__main__":
    asyncio.run(main())
