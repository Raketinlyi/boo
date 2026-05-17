"""
Инициализация веб-интерфейса.
"""

import logging
import os
import sys
import threading
import webbrowser
import time

# Проверяем, установлен ли Flask
try:
    import flask
    import flask_cors
    web_interface_available = True
except ImportError:
    web_interface_available = False
    logging.warning("Flask не установлен. Веб-интерфейс не будет доступен.")

# Глобальные переменные
app = None
bot_instance = None
server_thread = None
server_running = False

# Импортируем функции из server.py, если Flask установлен
if web_interface_available:
    try:
        from .server import app, start_web_server, stop_web_server
        logging.info("Веб-интерфейс инициализирован успешно.")
    except ImportError as e:
        logging.error(f"Ошибка при импорте модулей веб-интерфейса: {e}")
        web_interface_available = False
else:
    # Создаем заглушки для функций, если Flask не установлен
    def start_web_server(*args, **kwargs):
        logging.warning("Попытка запустить веб-сервер, но Flask не установлен.")
        return False

    def stop_web_server():
        logging.warning("Попытка остановить веб-сервер, но Flask не установлен.")
        return False
