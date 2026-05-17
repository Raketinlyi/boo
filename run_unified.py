#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
Единый запускной файл для арбитражного бота.
Запускает бота и веб-интерфейс, очищает старые логи и настраивает корректное логирование.
"""

import os
import sys
import json
import time
import signal
import logging
from logging.handlers import TimedRotatingFileHandler
import platform
import subprocess
import argparse
import webbrowser
import socket
import glob
import traceback
import shutil
import asyncio
import requests
from datetime import datetime
import atexit

try:
    import psutil  # type: ignore
except Exception:
    psutil = None

# Настройка базового логирования для начальной фазы запуска
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)

def setup_logging(keep_logs=False):
    """Configure compact logging with hourly rotation.

    Design goals (for an AI-analysable log stream on a constantly-running bot):
        * Compact format: one short prefix + message. No thread name / filename
          / line-number spam — easy for an LLM to skim and cheap in tokens.
        * Hourly rotation of the main bot.log with only 2 historical files kept
          (current + 2 previous hours = last ~3 hours of detail). Anything
          older is auto-deleted so logs never pile up.
        * Separate errors.log containing ONLY WARNING/ERROR/CRITICAL, rotated
          daily with 3 days of history. This is the long-term diagnostic trail
          — short enough for quick triage yet persistent across restarts.
        * Noisy third-party loggers (aiohttp.access, urllib3, asyncio) are
          raised to WARNING so access-log floods don't drown out real events.
    """
    log_dir = "logs"
    os.makedirs(log_dir, exist_ok=True)

    # ---- Clean up very old files from previous setups --------------------
    if not keep_logs:
        try:
            cutoff = time.time() - (2 * 86400)  # hard-delete anything >2 days old
            log_files = []
            for pattern in ("*.log*", "*.out", "*.err", "*.json", "*.txt", "*.lock"):
                log_files.extend(glob.glob(os.path.join(log_dir, pattern)))
            log_files.extend(["bot_stdout.log", "bot_stderr.log"])
            seen = set()
            for file in log_files:
                if not file or file in seen or not os.path.isfile(file):
                    continue
                seen.add(file)
                try:
                    if os.path.getmtime(file) < cutoff:
                        os.remove(file)
                except Exception:
                    pass
        except Exception as e:
            logging.error(f"log cleanup failed: {e}")

    main_log = os.path.join(log_dir, "arbitrage_bot.log")
    err_log = os.path.join(log_dir, "errors.log")

    # ---- Reset any handlers the bootstrap basicConfig may have installed --
    root_logger = logging.getLogger()
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)

    # Compact single-line format: "HH:MM:SS L logger: msg"
    formatter = logging.Formatter(
        fmt='%(asctime)s %(levelname).1s %(name)s: %(message)s',
        datefmt='%H:%M:%S',
    )

    # Main log — hourly rotation, only 2 historical files → ~3h on disk.
    file_handler = TimedRotatingFileHandler(
        main_log,
        when="H",
        interval=1,
        backupCount=2,
        encoding='utf-8',
    )
    file_handler.setFormatter(formatter)
    file_handler.setLevel(logging.INFO)

    # Error-only log — daily rotation, 3 days retained. Long-term trail.
    err_handler = TimedRotatingFileHandler(
        err_log,
        when="midnight",
        interval=1,
        backupCount=3,
        encoding='utf-8',
    )
    err_handler.setLevel(logging.WARNING)
    err_handler.setFormatter(
        logging.Formatter(
            fmt='%(asctime)s %(levelname).1s %(name)s: %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S',
        )
    )

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    console_handler.setLevel(logging.INFO)

    root_logger.setLevel(logging.INFO)
    root_logger.addHandler(file_handler)
    root_logger.addHandler(err_handler)
    root_logger.addHandler(console_handler)

    # Silence noisy third-party loggers — their INFO lines are low-signal.
    for noisy in (
        "aiohttp.access",
        "aiohttp.client",
        "aiohttp.server",
        "urllib3",
        "urllib3.connectionpool",
        "asyncio",
        "werkzeug",
    ):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    logging.info("logging ready (hourly rotation, 2 files kept; errors.log for 3 days)")
    return main_log

def is_port_in_use(port, host='127.0.0.1'):
    """Проверяет, занят ли указанный порт."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((host, port))
            return False
        except socket.error:
            return True

def wait_for_server(url, max_attempts=30, interval=1):
    """Ожидает запуска веб-сервера и проверяет его доступность."""
    logging.info(f"Проверка доступности сервера по адресу {url}")
    
    for attempt in range(max_attempts):
        try:
            response = requests.get(url, timeout=2)
            if response.status_code == 200:
                logging.info(f"Сервер доступен после {attempt + 1} попытки")
                return True
        except requests.exceptions.RequestException:
            pass
        
        if attempt < max_attempts - 1:
            logging.debug(f"Сервер недоступен, ожидание {interval} сек (попытка {attempt + 1}/{max_attempts})")
            time.sleep(interval)
    
    logging.error(f"Сервер не стал доступен после {max_attempts} попыток")
    return False

def start_ws_market_server_if_needed(port=8090):
    """Start the standalone WebSocket market-data server if it is not already listening."""
    if is_port_in_use(port):
        logging.info("WS market server already listens on port %s", port)
        return None
    try:
        python_exe = sys.executable or "python"
        os.makedirs("logs", exist_ok=True)
        # Truncate on each restart so the .out/.err files never grow unbounded.
        # Historical lines are preserved in the main rotated arbitrage_bot.log
        # (via the root logger) and in errors.log (WARNING+).
        out_f = open(os.path.join("logs", "ws_server.out"), "w", encoding="utf-8")
        err_f = open(os.path.join("logs", "ws_server.err"), "w", encoding="utf-8")
        proc = subprocess.Popen(
            [python_exe, "run_ws_server.py", "--host", os.getenv("WS_HOST", "127.0.0.1"), "--port", str(port)],
            cwd=os.getcwd(),
            stdout=out_f,
            stderr=err_f,
            creationflags=subprocess.CREATE_NO_WINDOW if platform.system() == "Windows" else 0,
        )
        with open("ws_server.pid", "w", encoding="utf-8") as f:
            f.write(str(proc.pid))
        logging.info("Started WS market server on port %s (PID=%s)", port, proc.pid)
        return proc
    except Exception:
        logging.exception("Failed to start WS market server")
        return None

def find_available_port(start_port=8080, max_attempts=10):
    """Находит доступный порт для веб-сервера."""
    # Список портов для проверки
    ports_to_try = [start_port]  # Сначала пробуем предпочтительный порт
    
    # Добавляем альтернативные порты
    alt_ports = [8080, 8000, 8888, 5050, 3000, 9000]
    ports_to_try.extend([p for p in alt_ports if p != start_port])
    
    # Проверяем порты
    for port in ports_to_try[:max_attempts]:
        if not is_port_in_use(port):
            return port
    
    # Если не нашли доступный порт
    return start_port

def check_dependencies():
    """Проверяет наличие необходимых зависимостей."""
    try:
        import flask
        import flask_cors
        logging.info("Flask установлен, веб-интерфейс будет доступен")
    except ImportError:
        logging.warning("Flask не установлен. Веб-интерфейс не будет доступен.")
        logging.warning("Установите Flask: pip install flask flask-cors")
        print("ВНИМАНИЕ: Flask не установлен. Веб-интерфейс не будет доступен.")
        print("Установите Flask: pip install flask flask-cors")
        return False
    
    try:
        import psutil
        import aiohttp
        import aiohttp_retry
        logging.info("Все основные зависимости установлены")
    except ImportError as e:
        logging.error(f"Отсутствует важная зависимость: {e}")
        print(f"ОШИБКА: Отсутствует важная зависимость: {e}")
        print("Установите все зависимости: pip install -r requirements.txt")
        return False
    
    return True

def run_bot(web_port=8080, open_browser=True, debug_level="INFO"):
    """Запускает бота с веб-интерфейсом."""
    try:
        # Устанавливаем политику цикла событий для Windows
        if platform.system() == "Windows":
            try:
                asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
                logging.info("Установлена политика событий WindowsSelectorEventLoopPolicy")
            except AttributeError as e:
                logging.error(f"Не удалось установить WindowsSelectorEventLoopPolicy: {e}")
        
        # Импортируем основные компоненты бота
        logging.info("Импорт необходимых модулей...")
        # Сообщаем main.py, что логирование уже настроено единым запускальщиком
        os.environ["RUN_UNIFIED"] = "1"
        
        from main import ArbitrageBot
        from web_interface.server import start_web_server
        
        # Создаем экземпляр бота
        logging.info("Создание экземпляра бота...")
        bot = ArbitrageBot()
        ws_proc = None
        try:
            if bool(bot.config.get("ws_ui_auto_start", True)):
                ws_port = int(float(bot.config.get("ws_server_port", 8090) or 8090))
                ws_proc = start_ws_market_server_if_needed(ws_port)
        except Exception:
            logging.exception("WS market server auto-start skipped")
        
        # Находим доступный порт для веб-сервера
        available_port = find_available_port(web_port)
        if available_port != web_port:
            logging.warning(f"Порт {web_port} занят. Используем альтернативный порт {available_port}")
            print(f"ВНИМАНИЕ: Порт {web_port} занят. Используем альтернативный порт {available_port}")
        
        # Запускаем веб-интерфейс
        logging.info(f"Запуск веб-интерфейса на порту {available_port}...")
        web_server_started = start_web_server(bot, available_port, False)  # Не открываем браузер автоматически
        
        if web_server_started:
            logging.info(f"Веб-интерфейс запущен на порту {available_port}")
            print(f"Веб-интерфейс доступен по адресу http://localhost:{available_port}")
            
            # Проверяем доступность веб-сервера перед открытием браузера
            server_available = wait_for_server(f"http://localhost:{available_port}", max_attempts=10)
            
            if server_available and open_browser:
                logging.info("Открываем веб-интерфейс в браузере")
                webbrowser.open(f"http://localhost:{available_port}")
            elif not server_available:
                logging.error("Веб-сервер запущен, но не отвечает на запросы")
                print("ОШИБКА: Веб-сервер запущен, но не отвечает на запросы")
        else:
            logging.error("Не удалось запустить веб-интерфейс")
            print("ОШИБКА: Не удалось запустить веб-интерфейс")
        
        # Запускаем мониторинг
        logging.info("Запуск мониторинга арбитражных возможностей...")
        bot.start_monitoring()
        
        # Бесконечный цикл работы
        logging.info("Бот запущен и работает")
        
        try:
            while True:
                # Проверяем, работает ли поток мониторинга
                # If monitoring was manually stopped (bot.running == False), do not auto-restart it.
                # Auto-restart only makes sense when the bot is expected to keep running.
                manual_stop = bool(getattr(bot, '_manual_stop_requested', False))
                thread_dead = bool(bot.monitor_thread and not bot.monitor_thread.is_alive())
                crashed_stopped = bool((not bot.running) and (not manual_stop) and bot.monitor_thread is None)
                if (bot.running and thread_dead) or crashed_stopped:
                    logging.warning("Monitoring thread is not healthy, restarting...")
                    print("Monitoring stopped unexpectedly, restarting...")
                    bot.start_monitoring()
                
# Спим, чтобы не нагружать CPU
                # Keep the WS market server alive. Without this, the main bot can keep
                # serving REST data while the browser badge stays "WS: off" until a full
                # container restart.
                if bool(bot.config.get("ws_ui_auto_start", True)):
                    try:
                        ws_port = int(float(bot.config.get("ws_server_port", 8090) or 8090))
                        if (ws_proc is None or ws_proc.poll() is not None) and not is_port_in_use(ws_port):
                            logging.warning("WS market server is not running, restarting on port %s", ws_port)
                            ws_proc = start_ws_market_server_if_needed(ws_port)
                    except Exception:
                        logging.exception("WS market server watchdog failed")

                time.sleep(1)
        except KeyboardInterrupt:
            logging.info("Получен сигнал остановки (Ctrl+C), завершаем работу...")
            print("\nЗавершение работы...")
        finally:
            # Останавливаем мониторинг
            if bot.running:
                bot.stop_monitoring()
            if ws_proc is not None and ws_proc.poll() is None:
                try:
                    ws_proc.terminate()
                    ws_proc.wait(timeout=5)
                except Exception:
                    try:
                        ws_proc.kill()
                    except Exception:
                        pass
            
            # Ждем завершения потока мониторинга
            if bot.monitor_thread and bot.monitor_thread.is_alive():
                bot.monitor_thread.join(timeout=5)
            
            logging.info("Бот остановлен")
            print("Бот остановлен")
        
        return True
    
    except Exception as e:
        logging.error(f"Ошибка при запуске бота: {e}")
        logging.error(traceback.format_exc())
        print(f"КРИТИЧЕСКАЯ ОШИБКА при запуске бота: {e}")
        return False

def parse_arguments():
    """Парсер аргументов командной строки."""
    parser = argparse.ArgumentParser(description='Запуск арбитражного бота')
    
    parser.add_argument('--port', type=int, default=8080,
                        help='Порт для веб-интерфейса (по умолчанию: 8080)')
    
    parser.add_argument('--no-browser', action='store_true',
                        help='Не открывать браузер автоматически')
    
    parser.add_argument('--keep-logs', action='store_true',
                        help='Не удалять старые лог-файлы')
    
    parser.add_argument('--log-level', type=str, choices=['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'],
                        default='INFO', help='Уровень детализации логов (по умолчанию: INFO)')

    parser.add_argument('--single-instance', action='store_true',
                        help='Legacy flag: singleton mode is enabled by default')
    parser.add_argument('--kill-others', action='store_true',
                        help='Попытаться завершить другие экземпляры перед запуском (требует psutil)')
    parser.add_argument('--allow-multi-instance', action='store_true',
                        help='Разрешить несколько экземпляров run_unified.py (не рекомендуется)')
    
    return parser.parse_args()


# --------- Single-instance helpers ---------
LOCK_PATH = os.path.join("logs", "run_unified.lock")

def _write_lock(pid: int):
    try:
        os.makedirs(os.path.dirname(LOCK_PATH) or ".", exist_ok=True)
        with open(LOCK_PATH, "w", encoding="utf-8") as f:
            f.write(str(pid))
    except Exception:
        logging.debug("Не удалось записать lock-файл", exc_info=True)

def _read_lock() -> int:
    try:
        if os.path.exists(LOCK_PATH):
            with open(LOCK_PATH, "r", encoding="utf-8") as f:
                txt = (f.read() or "").strip()
                return int(txt) if txt.isdigit() else -1
    except Exception:
        logging.debug("Не удалось прочитать lock-файл", exc_info=True)
    return -1

def _remove_lock():
    try:
        if os.path.exists(LOCK_PATH):
            os.remove(LOCK_PATH)
    except Exception:
        logging.debug("Не удалось удалить lock-файл", exc_info=True)

def acquire_singleton_lock(kill_others: bool = False) -> bool:
    """Гарантирует единственный экземпляр процесса.
    - Если найден активный процесс по lock-файлу — вернём False.
    - Если процесс мёртв — перезапишем lock.
    - Если kill_others=True, попытаемся завершить другие экземпляры run_unified.py.
    """
    runner_script_name = os.path.basename(__file__).lower()
    runner_script_path = os.path.normcase(os.path.abspath(__file__))

    def _resolve_runner_script_path(script_arg: str, proc_cwd: str | None = None) -> str | None:
        try:
            script_arg = str(script_arg or "").strip().strip('"')
            if not script_arg or os.path.basename(script_arg).lower() != runner_script_name:
                return None
            if os.path.isabs(script_arg):
                return os.path.normcase(os.path.abspath(script_arg))
            if proc_cwd:
                return os.path.normcase(os.path.abspath(os.path.join(proc_cwd, script_arg)))
        except Exception:
            return None
        return None

    def _is_our_runner_cmdline(cmdline, proc_cwd: str | None = None) -> bool:
        try:
            parts = [str(part or "").strip() for part in (cmdline or []) if str(part or "").strip()]
            if not parts:
                return False
            exe_name = os.path.basename(parts[0].strip('"')).lower()
            if exe_name == runner_script_name:
                scan_parts = parts
            elif "python" in exe_name or exe_name in {"py", "py.exe"}:
                scan_parts = parts[1:]
            else:
                return False
            for part in scan_parts:
                candidate_path = _resolve_runner_script_path(part, proc_cwd)
                if candidate_path and candidate_path == runner_script_path:
                    return True
            return False
        except Exception:
            return False

    def _is_our_runner_pid(pid: int) -> bool:
        if pid <= 0 or psutil is None:
            return False
        try:
            if not psutil.pid_exists(pid):
                return False
            proc = psutil.Process(pid)
            try:
                proc_cwd = proc.cwd()
            except Exception:
                proc_cwd = None
            return _is_our_runner_cmdline(proc.cmdline() or [], proc_cwd)
        except Exception:
            return False

    def _terminate_pid(pid: int) -> bool:
        if pid <= 0 or psutil is None:
            return False
        try:
            proc = psutil.Process(pid)
            try:
                proc_cwd = proc.cwd()
            except Exception:
                proc_cwd = None
            if not _is_our_runner_cmdline(proc.cmdline() or [], proc_cwd):
                return False
            logging.warning(f"Обнаружен другой экземпляр (PID={pid}), завершаю...")
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except Exception:
                proc.kill()
            return not _is_our_runner_pid(pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return True
        except Exception:
            logging.debug("Не удалось завершить процесс по PID", exc_info=True)
            return False

    # 1) Проверка lock-файла (с опциональным убийством владельца lock)
    old_pid = _read_lock()
    if old_pid > 0 and _is_our_runner_pid(old_pid):
        if kill_others:
            _terminate_pid(old_pid)
        # Дадим ОС чуть времени добить процесс и освободить PID
        for _ in range(10):
            if not _is_our_runner_pid(old_pid):
                break
            time.sleep(0.2)

    # 2) Optional broad process scan. Disabled by default because shell/wrapper
    # processes on Windows can include "run_unified.py" in their cmdline and
    # trigger false positives despite a healthy lock-file flow above.
    broad_kill_scan_enabled = os.environ.get("RUN_UNIFIED_BROAD_KILL_SCAN", "").strip().lower() in {"1", "true", "yes", "on"}
    if kill_others and broad_kill_scan_enabled and psutil is not None:
        cur_pid = os.getpid()
        try:
            for p in psutil.process_iter(attrs=["pid", "cmdline"]):
                try:
                    pid = p.info.get("pid")
                    if not pid or pid == cur_pid:
                        continue
                    try:
                        proc_cwd = p.cwd()
                    except Exception:
                        proc_cwd = None
                    if _is_our_runner_cmdline(p.info.get("cmdline") or [], proc_cwd):
                        _terminate_pid(pid)
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
        except Exception:
            logging.debug("Не удалось выполнить завершение других экземпляров", exc_info=True)

    # 3) Повторная проверка lock-файла (после kill_others)
    old_pid = _read_lock()
    if old_pid > 0:
        if old_pid == os.getpid():
            logging.warning(f"Найден stale lock с текущим PID={old_pid}; это типично после Docker restart, перезаписываю.")
            _remove_lock()
            old_pid = -1
    if old_pid > 0:
        if _is_our_runner_pid(old_pid):
            logging.error(f"Другой экземпляр уже запущен (PID={old_pid}). Выходим.")
            return False
        logging.warning(f"Найден устаревший lock для PID={old_pid}, перезаписываю.")
        _remove_lock()

    # 4) Optional strict fallback. Disabled by default because transient Python
    # helper processes on Windows can trigger false positives despite a healthy
    # lock-file check above.
    strict_fallback_enabled = os.environ.get("RUN_UNIFIED_STRICT_FALLBACK", "").strip().lower() in {"1", "true", "yes", "on"}
    if strict_fallback_enabled and psutil is not None:
        cur_pid = os.getpid()
        try:
            for p in psutil.process_iter(attrs=["pid", "cmdline"]):
                try:
                    pid = p.info.get("pid")
                    if not pid or pid == cur_pid:
                        continue
                    cmdline = p.info.get("cmdline") or []
                    try:
                        proc_cwd = p.cwd()
                    except Exception:
                        proc_cwd = None
                    if _is_our_runner_cmdline(cmdline, proc_cwd):
                        logging.error(f"Найден уже работающий run_unified.py (PID={pid}). Выходим.")
                        return False
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
        except Exception:
            logging.debug("Не удалось проверить список процессов для singleton guard", exc_info=True)

    _write_lock(os.getpid())
    atexit.register(_remove_lock)
    try:
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda *_: (_remove_lock(), sys.exit(0)))
    except Exception:
        # Windows может не поддерживать все сигналы — не критично
        pass
    return True


if __name__ == "__main__":
    # Парсим аргументы командной строки
    args = parse_arguments()
    
    # Настраиваем логирование
    log_file = setup_logging(keep_logs=args.keep_logs)
    
    # Устанавливаем уровень логирования из аргументов
    logging.getLogger().setLevel(getattr(logging, args.log_level))
    
    # Проверяем зависимости
    if not check_dependencies():
        logging.error("Не все зависимости установлены. Проверьте лог-файл для деталей.")
        sys.exit(1)
    # By default run only one unified instance; user can opt out explicitly.
    if not args.allow_multi_instance or args.single_instance or args.kill_others:
        ok = acquire_singleton_lock(kill_others=args.kill_others)
        if not ok:
            print("Другой экземпляр уже запущен. Завершаю этот процесс.")
            sys.exit(2)
    
    # Запускаем бота
    success = run_bot(
        web_port=args.port,
        open_browser=not args.no_browser,
        debug_level=args.log_level
    )
    
    # Выходим с соответствующим кодом
    sys.exit(0 if success else 1) 
