"""
Модуль для централизованной обработки ошибок и улучшенного логирования.
"""

import logging
import traceback
import functools
import time
import asyncio
from typing import Callable, Any, Dict, Optional, Type, Union, List, TypeVar

# Типы для декораторов
F = TypeVar('F', bound=Callable[..., Any])
AF = TypeVar('AF', bound=Callable[..., Any])

# Константы для типов ошибок
API_ERROR = "api_error"
DATABASE_ERROR = "database_error"
CALCULATION_ERROR = "calculation_error"
NETWORK_ERROR = "network_error"
GENERAL_ERROR = "general_error"

# Словарь для отслеживания ошибок
error_stats: Dict[str, Dict[str, Any]] = {
    API_ERROR: {"count": 0, "last_time": None, "sources": {}},
    DATABASE_ERROR: {"count": 0, "last_time": None, "sources": {}},
    CALCULATION_ERROR: {"count": 0, "last_time": None, "sources": {}},
    NETWORK_ERROR: {"count": 0, "last_time": None, "sources": {}},
    GENERAL_ERROR: {"count": 0, "last_time": None, "sources": {}},
}

def log_error(error_type: str, source: str, error: Exception, details: Optional[Dict[str, Any]] = None) -> None:
    """
    Логирует ошибку и обновляет статистику ошибок.
    
    Args:
        error_type: Тип ошибки (API_ERROR, DATABASE_ERROR и т.д.)
        source: Источник ошибки (имя биржи, модуля и т.д.)
        error: Объект исключения
        details: Дополнительные детали об ошибке
    """
    if error_type not in error_stats:
        error_type = GENERAL_ERROR
        
    # Обновляем общую статистику для типа ошибки
    error_stats[error_type]["count"] += 1
    error_stats[error_type]["last_time"] = time.time()
    
    # Обновляем статистику для конкретного источника
    if source not in error_stats[error_type]["sources"]:
        error_stats[error_type]["sources"][source] = {"count": 0, "last_time": None, "errors": []}
    
    source_stats = error_stats[error_type]["sources"][source]
    source_stats["count"] += 1
    source_stats["last_time"] = time.time()
    
    # Сохраняем последние 10 ошибок для источника
    error_info = {
        "time": time.time(),
        "error_type": type(error).__name__,
        "message": str(error),
        "details": details or {}
    }
    source_stats["errors"] = [error_info] + source_stats["errors"][:9]
    
    # Логируем ошибку
    error_message = f"[{error_type.upper()}] {source}: {type(error).__name__}: {str(error)}"
    if details:
        error_message += f" Details: {details}"
    
    logging.error(error_message)
    logging.debug(f"Traceback: {traceback.format_exc()}")

def retry_on_error(
    max_retries: int = 3, 
    retry_delay: float = 1.0, 
    backoff_factor: float = 2.0,
    exceptions: Union[Type[Exception], List[Type[Exception]]] = Exception,
    error_type: str = GENERAL_ERROR
) -> Callable[[F], F]:
    """
    Декоратор для повторных попыток выполнения функции при возникновении исключения.
    
    Args:
        max_retries: Максимальное количество повторных попыток
        retry_delay: Начальная задержка между попытками (в секундах)
        backoff_factor: Множитель для увеличения задержки с каждой попыткой
        exceptions: Тип(ы) исключений, при которых выполнять повторные попытки
        error_type: Тип ошибки для логирования
        
    Returns:
        Декорированная функция
    """
    def decorator(func: F) -> F:
        @functools.wraps(func)
        def wrapper(*args, **kwargs) -> Any:
            last_exception = None
            source = func.__module__ + "." + func.__name__
            
            for attempt in range(max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except exceptions as e:
                    last_exception = e
                    delay = retry_delay * (backoff_factor ** attempt)
                    
                    if attempt < max_retries:
                        logging.warning(
                            f"Attempt {attempt + 1}/{max_retries + 1} failed for {source}: {type(e).__name__}: {str(e)}. "
                            f"Retrying in {delay:.2f} seconds..."
                        )
                        time.sleep(delay)
                    else:
                        log_error(
                            error_type, 
                            source, 
                            e, 
                            {"attempts": attempt + 1, "args": args, "kwargs": kwargs}
                        )
            
            if last_exception:
                raise last_exception
            return None
        
        return wrapper  # type: ignore
    
    return decorator

def async_retry_on_error(
    max_retries: int = 3, 
    retry_delay: float = 1.0, 
    backoff_factor: float = 2.0,
    exceptions: Union[Type[Exception], List[Type[Exception]]] = Exception,
    error_type: str = GENERAL_ERROR
) -> Callable[[AF], AF]:
    """
    Асинхронный декоратор для повторных попыток выполнения функции при возникновении исключения.
    
    Args:
        max_retries: Максимальное количество повторных попыток
        retry_delay: Начальная задержка между попытками (в секундах)
        backoff_factor: Множитель для увеличения задержки с каждой попыткой
        exceptions: Тип(ы) исключений, при которых выполнять повторные попытки
        error_type: Тип ошибки для логирования
        
    Returns:
        Декорированная асинхронная функция
    """
    def decorator(func: AF) -> AF:
        @functools.wraps(func)
        async def wrapper(*args, **kwargs) -> Any:
            last_exception = None
            source = func.__module__ + "." + func.__name__
            
            for attempt in range(max_retries + 1):
                try:
                    return await func(*args, **kwargs)
                except exceptions as e:
                    last_exception = e
                    delay = retry_delay * (backoff_factor ** attempt)
                    
                    if attempt < max_retries:
                        logging.warning(
                            f"Attempt {attempt + 1}/{max_retries + 1} failed for {source}: {type(e).__name__}: {str(e)}. "
                            f"Retrying in {delay:.2f} seconds..."
                        )
                        await asyncio.sleep(delay)
                    else:
                        log_error(
                            error_type, 
                            source, 
                            e, 
                            {"attempts": attempt + 1, "args": args, "kwargs": kwargs}
                        )
            
            if last_exception:
                raise last_exception
            return None
        
        return wrapper  # type: ignore
    
    return decorator

def get_error_stats() -> Dict[str, Dict[str, Any]]:
    """
    Возвращает статистику ошибок.
    
    Returns:
        Словарь со статистикой ошибок
    """
    return error_stats.copy()

def reset_error_stats() -> None:
    """Сбрасывает статистику ошибок."""
    for error_type in error_stats:
        error_stats[error_type]["count"] = 0
        error_stats[error_type]["last_time"] = None
        error_stats[error_type]["sources"] = {}

# Настройка расширенного логирования
def setup_advanced_logging(log_dir: str = "logs", log_level: int = logging.INFO, console_level: int = logging.INFO) -> None:
    """
    Настраивает расширенное логирование с разными уровнями детализации.
    
    Args:
        log_dir: Директория для файлов логов
        log_level: Уровень логирования для файла
        console_level: Уровень логирования для консоли
    """
    import os
    from datetime import datetime
    import sys
    
    # Создаем директорию для логов, если она не существует
    os.makedirs(log_dir, exist_ok=True)
    
    # Создаем форматтеры для разных уровней детализации
    detailed_formatter = logging.Formatter(
        '%(asctime)s - %(levelname)s - [%(threadName)s] - %(filename)s:%(lineno)d - %(message)s'
    )
    
    simple_formatter = logging.Formatter(
        '%(asctime)s - %(levelname)s - %(message)s'
    )
    
    # Создаем обработчик для файла логов
    log_file = os.path.join(log_dir, f"arbitrage_bot_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log")
    file_handler = logging.FileHandler(log_file, encoding='utf-8')
    file_handler.setLevel(log_level)
    file_handler.setFormatter(detailed_formatter)
    
    # Создаем обработчик для консоли
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(console_level)
    console_handler.setFormatter(simple_formatter)
    
    # Создаем обработчик для ошибок (отдельный файл)
    error_file = os.path.join(log_dir, f"errors_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log")
    error_handler = logging.FileHandler(error_file, encoding='utf-8')
    error_handler.setLevel(logging.ERROR)
    error_handler.setFormatter(detailed_formatter)
    
    # Настраиваем корневой логгер
    root_logger = logging.getLogger()
    root_logger.setLevel(min(log_level, console_level))
    
    # Удаляем существующие обработчики
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)
    
    # Добавляем новые обработчики
    root_logger.addHandler(file_handler)
    root_logger.addHandler(console_handler)
    root_logger.addHandler(error_handler)
    
    logging.info(f"Расширенное логирование настроено. Файл логов: {log_file}, файл ошибок: {error_file}")
