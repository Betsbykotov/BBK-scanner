"""
oddspapi_provider.py

Изолированный провайдер для OddsPapi (sharp-money подтверждение по Pinnacle).
Работает как ДОПОЛНИТЕЛЬНЫЙ слой валидации топ-сигналов, а не как основной источник.

Ключевые ограничения free-плана OddsPapi (учтено в этом модуле):
- 250 запросов/месяц (жёсткий лимит)
- Между запросами к одному эндпоинту нужна пауза >= 1 сек
- WebSocket недоступен -> используем обычный REST-поллинг, как с Sportmonks

Дизайн:
- Вызывается ТОЛЬКО для топ-N сигналов дня (по BBK Score), не для каждого алерта
- Дневной счётчик запросов хранится в SQLite (db_path передаётся снаружи)
- Любая ошибка/исключение молча возвращает None -> основной цикл никогда не падает
"""

import os
import sqlite3
import time
import json
from datetime import datetime, timezone
from urllib import request as urlrequest
from urllib.parse import urlencode
from urllib.error import URLError, HTTPError

BASE_URL = "https://api.oddspapi.io/v4"

# Читаем ключ из окружения, убираем случайные пробелы/переносы (iOS paste artifacts)
API_KEY = (os.environ.get("ODDSPAPI_API_KEY") or "").strip()

# Дневной бюджет запросов. 250/мес -> ~8/день безопасно, но мы бюджетируем
# по топ-сигналам, поэтому даём немного больше суточного окна с запасом на месяц.
DAILY_REQUEST_BUDGET = int(os.environ.get("ODDSPAPI_DAILY_BUDGET", "7"))

# Минимальная пауза между запросами (сек), free tier требует >= ~0.88s
MIN_REQUEST_GAP_SECONDS = 1.2

SHARP_BOOKMAKER_KEY = "pinnacle"


def _init_usage_table(db_path: str) -> None:
    """Создаёт таблицу учёта дневного лимита, если её ещё нет."""
    try:
        conn = sqlite3.connect(db_path)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS oddspapi_usage (
                usage_date TEXT PRIMARY KEY,
                request_count INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[oddspapi] WARN: failed to init usage table: {e}", flush=True)


def _today_key() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _get_today_count(db_path: str) -> int:
    try:
        conn = sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT request_count FROM oddspapi_usage WHERE usage_date = ?",
            (_today_key(),),
        ).fetchone()
        conn.close()
        return row[0] if row else 0
    except Exception as e:
        print(f"[oddspapi] WARN: failed to read usage: {e}", flush=True)
        return 0


def _increment_today_count(db_path: str) -> None:
    try:
        conn = sqlite3.connect(db_path)
        conn.execute(
            """
            INSERT INTO oddspapi_usage (usage_date, request_count)
            VALUES (?, 1)
            ON CONFLICT(usage_date) DO UPDATE SET request_count = request_count + 1
            """,
            (_today_key(),),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[oddspapi] WARN: failed to increment usage: {e}", flush=True)


def budget_available(db_path: str) -> bool:
    """Проверка ДО запроса: остался ли дневной бюджет."""
    _init_usage_table(db_path)
    used = _get_today_count(db_path)
    ok = used < DAILY_REQUEST_BUDGET
    if not ok:
        print(
            f"[oddspapi] budget exhausted for today: {used}/{DAILY_REQUEST_BUDGET}",
            flush=True,
        )
    return ok


def _http_get(path: str, params: dict) -> dict | None:
    """Синхронный GET с ключом API. Возвращает dict или None при любой ошибке."""
    if not API_KEY:
        print("[oddspapi] ERROR: ODDSPAPI_API_KEY is not set", flush=True)
        return None

    query = dict(params)
    query["apiKey"] = API_KEY
    url = f"{BASE_URL}{path}?{urlencode(query)}"

    try:
        req = urlrequest.Request(url, headers={"User-Agent": "bbk-scanner/1.0"})
        with urlrequest.urlopen(req, timeout=10) as resp:
            raw = resp.read()
            return json.loads(raw)
    except HTTPError as e:
        print(f"[oddspapi] HTTP error {e.code} on {path}: {e.reason}", flush=True)
        return None
    except URLError as e:
        print(f"[oddspapi] Network error on {path}: {e.reason}", flush=True)
        return None
    except Exception as e:
        print(f"[oddspapi] Unexpected error on {path}: {e}", flush=True)
        return None


def find_fixture_id(home_team: str, away_team: str, sport_id: int = 10) -> str | None:
    """
    Пытается найти fixtureId по названиям команд (soccer по умолчанию, sportId=10).
    Не тратит отдельный запрос из дневного бюджета намеренно НЕ вызывается автоматически -
    предполагается, что fixtureId уже известен из основного пайплайна, если возможно.
    Оставлено как fallback-утилита.
    """
    data = _http_get(
        "/fixtures",
        {
            "sportId": sport_id,
            "search": f"{home_team} {away_team}",
        },
    )
    if not data:
        return None
    fixtures = data if isinstance(data, list) else data.get("fixtures", [])
    if fixtures:
        return fixtures[0].get("fixtureId")
    return None


def check_sharp_confirmation(
    fixture_id: str,
    predicted_side: str,
    db_path: str,
) -> dict | None:
    """
    Главная функция модуля. Дёргается ТОЛЬКО для топ-сигналов дня.

    fixture_id: id матча в OddsPapi
    predicted_side: 'home' | 'draw' | 'away' - что предсказывает твой сигнал
    db_path: путь к той же SQLite базе, что использует основной сканер

    Возвращает:
        {
            "confirmed": bool,          # совпадает ли Pinnacle с направлением сигнала
            "pinnacle_odds": {...},     # сырые одды Pinnacle для лога
        }
    или None, если бюджет исчерпан / ошибка / нет данных от Pinnacle.
    """
    if not budget_available(db_path):
        return None

    data = _http_get("/odds", {"fixtureId": fixture_id})
    _increment_today_count(db_path)  # считаем попытку независимо от результата

    if not data:
        return None

    bookmaker_odds = data.get("bookmakerOdds", {})
    pinnacle = bookmaker_odds.get(SHARP_BOOKMAKER_KEY)
    if not pinnacle:
        print(f"[oddspapi] no Pinnacle data for fixture {fixture_id}", flush=True)
        return None

    # Sanity: ищем сторону с минимальным коэффициентом = сторона, куда идут деньги/модель
    # Формат полей уточняется по факту первого реального ответа API - маппинг ниже
    # намеренно defensive, чтобы не упасть на неожиданной структуре.
    try:
        home_odds = float(pinnacle.get("home") or pinnacle.get("1") or 0)
        draw_odds = float(pinnacle.get("draw") or pinnacle.get("X") or 0)
        away_odds = float(pinnacle.get("away") or pinnacle.get("2") or 0)
    except (TypeError, ValueError):
        print(f"[oddspapi] unexpected odds format for fixture {fixture_id}", flush=True)
        return None

    candidates = {"home": home_odds, "draw": draw_odds, "away": away_odds}
    candidates = {k: v for k, v in candidates.items() if v > 0}
    if not candidates:
        return None

    sharp_favorite = min(candidates, key=candidates.get)
    confirmed = sharp_favorite == predicted_side

    return {
        "confirmed": confirmed,
        "sharp_favorite": sharp_favorite,
        "pinnacle_odds": candidates,
    }
