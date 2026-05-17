import time
from typing import Dict, Optional, Tuple
from utils.error_handler import log_error

class APIManager:
    def __init__(self):
        self.sources = {
            'coingecko': {
                'base_url': 'https://api.coingecko.com/api/v3',
                'coin_url_template': 'https://www.coingecko.com/en/coins/{coin_id}',
                'failures': 0,
                'last_failure': 0,
                'is_available': True,
                'cooldown': 300
            }
        }
        self.current_source = 'coingecko'
        self._source_change_callbacks = []

    def add_source_change_callback(self, callback):
        """Добавляет callback для уведомления об изменении источника данных"""
        self._source_change_callbacks.append(callback)

    def get_current_source_info(self) -> Dict:
        """Возвращает информацию о текущем источнике данных"""
        return {
            'name': self.current_source,
            'base_url': self.sources[self.current_source]['base_url'],
            'is_available': self.sources[self.current_source]['is_available']
        }

    def get_coin_url(self, coin_id: str) -> str:
        """Возвращает URL для просмотра информации о монете на текущем источнике"""
        return self.sources[self.current_source]['coin_url_template'].format(coin_id=coin_id)

    def get_detailed_source_info(self) -> Dict:
        """
        Возвращает подробную информацию о текущем источнике данных
        
        Returns:
            Dict: Расширенная информация о текущем источнике
        """
        source = self.sources[self.current_source]
        return {
            'name': self.current_source,
            'base_url': source['base_url'],
            'is_available': source['is_available'],
            'failures': source['failures'],
            'last_failure': source['last_failure'],
            'cooldown': source['cooldown'],
            'coin_url_template': source['coin_url_template']
        }

    def _notify_source_change(self, old_source: str, new_source: str):
        """Уведомляет подписчиков об изменении источника данных"""
        for callback in self._source_change_callbacks:
            try:
                callback(old_source, new_source)
            except Exception as e:
                log_error(f"Ошибка в callback при смене источника: {e}")

    def switch_source(self) -> Tuple[bool, str]:
        """Переключает на альтернативный источник данных"""
        current_time = time.time()
        old_source = self.current_source
        
        for name, source in self.sources.items():
            if name == self.current_source:
                continue
                
            if source['is_available'] or (current_time - source['last_failure'] > source['cooldown']):
                self.current_source = name
                print(f"Переключение API с {old_source} на {name}")
                self._notify_source_change(old_source, name)
                return True, f"Успешное переключение на {name}"
        
        return False, "Нет доступных альтернативных источников"

    def report_failure(self, source_name: Optional[str] = None):
        """Регистрирует сбой текущего или указанного источника"""
        source_name = source_name or self.current_source
        if source_name not in self.sources:
            return
        source = self.sources[source_name]
        source['failures'] += 1
        source['last_failure'] = time.time()
        
        if source['failures'] >= 5:
            source['is_available'] = False
            print(f"Источник {source_name} помечен как недоступный")

    def report_success(self, source_name: Optional[str] = None):
        """Регистрирует успешный запрос"""
        source_name = source_name or self.current_source
        if source_name not in self.sources:
            return
        source = self.sources[source_name]
        if source['failures'] > 0:
            source['failures'] = max(0, source['failures'] - 1)
        source['is_available'] = True

api_manager = APIManager()
