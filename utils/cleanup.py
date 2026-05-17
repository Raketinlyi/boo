from __future__ import annotations

import os
import time
import logging
from pathlib import Path
from typing import Iterable, Optional


def _remove_file_safe(path: Path) -> None:
    try:
        if path.exists():
            path.unlink(missing_ok=True)
    except Exception:
        try:
            os.remove(path.as_posix())
        except Exception:
            pass


def _iter_files(dir_path: Path, patterns: Optional[Iterable[str]] = None) -> Iterable[Path]:
    if not dir_path.exists() or not dir_path.is_dir():
        return []
    pats = list(patterns or ["*"])
    res: list[Path] = []
    try:
        for p in pats:
            res.extend(dir_path.glob(p))
    except Exception:
        return []
    return res


def cleanup_logs(log_dir: str = "logs", keep_days: int = 7, max_files: int = 50) -> None:
    """Удалить лог-файлы старше keep_days и ограничить общее количество.

    - Сохраняем последние max_files по дате изменения
    - Удаляем остальные
    """
    try:
        base = Path(log_dir)
        if not base.exists():
            return
        files = [p for p in base.glob("*.log") if p.is_file()]
        files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        now = time.time()
        # Удалим старые
        for p in files:
            try:
                age_days = (now - p.stat().st_mtime) / 86400.0
            except Exception:
                age_days = 0.0
            if age_days > float(keep_days):
                _remove_file_safe(p)
        # Ограничим количество
        files = [p for p in base.glob("*.log") if p.is_file()]
        files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        for p in files[max_files:]:
            _remove_file_safe(p)
    except Exception:
        logging.debug("cleanup_logs: failed", exc_info=True)


def purge_cache_dir(dir_path: str, max_age_seconds: int = 7 * 86400) -> None:
    """Очистить кэш: удалить файлы старше заданного TTL.

    Безопасно: удаляются только файлы, директории не трогаем.
    """
    try:
        base = Path(dir_path)
        if not base.exists() or not base.is_dir():
            return
        now = time.time()
        for p in base.iterdir():
            try:
                if not p.is_file():
                    continue
                age = now - p.stat().st_mtime
                if age > float(max_age_seconds):
                    _remove_file_safe(p)
            except Exception:
                continue
    except Exception:
        logging.debug("purge_cache_dir: failed for %s", dir_path, exc_info=True)


def cleanup_data_dir(data_dir: str = "data", keep_days: int = 14) -> None:
    """Очистить папку data от устаревших и временных файлов.

    Удаляем:
      - *.tmp
      - *.bak старше keep_days
      - временные чекпоинты *.pt.tmp
      - пустые/битые jsonl (размер 0)

    Примечание: AI/self-training артефакты удалены из проекта, поэтому здесь нет
    специальных исключений под training-файлы.
    """
    try:
        base = Path(data_dir)
        if not base.exists() or not base.is_dir():
            return
        keep: set[str] = {
            "arbitrage.db",  # База не трогаем
        }
        now = time.time()
        # Удаление .tmp
        for p in _iter_files(base, patterns=["*.tmp", "*.pt.tmp", "*.json.tmp"]):
            _remove_file_safe(p)
        # Удаление старых .bak
        for p in _iter_files(base, patterns=["*.bak"]):
            try:
                age_days = (now - p.stat().st_mtime) / 86400.0
            except Exception:
                age_days = 0.0
            if age_days > float(keep_days):
                _remove_file_safe(p)
        # Удаление пустых jsonl
        for p in _iter_files(base, patterns=["*.jsonl"]):
            if p.name in keep:
                continue
            try:
                if p.stat().st_size == 0:
                    _remove_file_safe(p)
            except Exception:
                pass
        # Безопасное удаление ненужных файлов (всё, что не в keep и не .jsonl dataset/backup)
        for p in base.iterdir():
            try:
                if p.is_dir():
                    continue
                if p.name in keep:
                    continue
                if p.suffix.lower() in (".jsonl",):
                    # dataset и его бэкапы обрабатываются отдельными правилами
                    continue
                if p.suffix.lower() in (".json", ".pt", ".db"):
                    # потенциально используемые файлы — не трогаем
                    continue
                # Удаляем прочие файлы (например, старые кеши или артефакты)
                _remove_file_safe(p)
            except Exception:
                continue
    except Exception:
        logging.debug("cleanup_data_dir: failed", exc_info=True)


def cleanup_workspace() -> None:
    """Запустить комплексную очистку воркспейса: логи, дата, кеши."""
    try:
        cleanup_logs("logs", keep_days=7, max_files=30)
        cleanup_data_dir("data", keep_days=14)
        # Кеши CoinGecko / CMC
        purge_cache_dir(os.path.join("data", "coingecko_cache"), max_age_seconds=7 * 86400)
        purge_cache_dir(os.path.join("data", "coinmarketcap_cache"), max_age_seconds=7 * 86400)
    except Exception:
        logging.debug("cleanup_workspace: failed", exc_info=True)
