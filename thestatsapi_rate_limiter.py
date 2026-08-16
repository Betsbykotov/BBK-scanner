"""
Общий троттлер запросов к TheStatsAPI — используется и thestatsapi_provider.py
(SHARP-сверка по Pinnacle), и thestatsapi_pressure_provider.py (PRESSURE v3.0).

Оба модуля работают в одном процессе (main.py импортирует оба) и делят один
и тот же лимит 120 запросов/мин на аккаунт — если троттлить каждый модуль
отдельно, суммарный поток всё равно может пробить лимит. Общий модуль-синглтон
с единым таймером решает это раз и навсегда.
"""

from __future__ import annotations

import threading
import time

# 120 запросов/мин по тарифу Starter — берём заметно ниже (70/мин), чтобы
# суммарный поток от ДВУХ модулей (SHARP + PRESSURE v3) точно укладывался
# в лимит с запасом, а не балансировал на грани.
MIN_REQUEST_INTERVAL = 1.0  # секунд между ЛЮБЫМИ запросами к TheStatsAPI

_lock = threading.Lock()
_last_request_at = 0.0


def throttle() -> None:
    """Блокирует вызывающий поток до тех пор, пока не пройдёт минимальный
    интервал с прошлого запроса к TheStatsAPI (от ЛЮБОГО модуля)."""
    global _last_request_at
    with _lock:
        elapsed = time.monotonic() - _last_request_at
        wait = MIN_REQUEST_INTERVAL - elapsed
        if wait > 0:
            time.sleep(wait)
        _last_request_at = time.monotonic()
