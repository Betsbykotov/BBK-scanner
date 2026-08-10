"""
oddspapi_provider.py

Sharp-money подтверждение сигналов через OddsPapi (Pinnacle), рынок h2h/1X2.

ВАЖНО про реальную структуру API (проверено по докам, не догадка):
- Эндпоинт: GET /v4/odds-by-tournaments?bookmaker=pinnacle&tournamentIds=X,Y&apiKey=...
  Отдаёт СРАЗУ все фикстуры турнира с оддами Pinnacle - 1 запрос на всю лигу,
  а не по одному матчу.
- У фикстур НЕТ имён команд - только participant1Id/participant2Id (числа).
  Прямого поиска по названию команды в API нет. Матчим по startTime (ISO8601)
  в пределах узкого окна от нашего commence_time - в рамках одного турнира
  два матча почти никогда не стартуют в одну и ту же минуту.
- Одды 1X2 лежат в:
    bookmakerOdds.pinnacle.markets["101"].outcomes["101"].players["0"].price  -> home
    bookmakerOdds.pinnacle.markets["102"].outcomes["102"].players["0"].price  -> draw
    bookmakerOdds.pinnacle.markets["103"].outcomes["103"].players["0"].price  -> away
  (подтверждено полем bookmakerOutcomeId: "home"/"draw"/"away" в реальном ответе)
- Для тоталов/others структура рынков другая и пока не подтверждена на реальных
  данных - НЕ реализовано, чтобы не гадать. Работаем только с h2h.

Бюджет: 250 запросов/мес на free-плане. Дневной лимит регулируется через
ODDSPAPI_DAILY_BUDGET (по умолчанию 12). Список турниров кешируется в SQLite
на 7 дней, чтобы не тратить бюджет на каждый цикл заново.
"""

from __future__ import annotations

import os
import json
import sqlite3
from datetime import datetime, timezone, timedelta
from urllib import request as urlrequest
from urllib.parse import urlencode
from urllib.error import URLError, HTTPError

BASE_URL = "https://api.oddspapi.io/v4"

API_KEY = (os.environ.get("ODDSPAPI_API_KEY") or "").strip()
DAILY_REQUEST_BUDGET = int(os.environ.get("ODDSPAPI_DAILY_BUDGET", "12"))
TOURNAMENT_CACHE_TTL_DAYS = 7
SOCCER_SPORT_ID = 10

_MARKET_TO_SIDE = {"101": "home", "102": "draw", "103": "away"}

# Матч по startTime считается тем же событием, если разница меньше этого окна.
_KICKOFF_MATCH_TOLERANCE_MINUTES = 10


# ---------------------------------------------------------------------------
# Инфраструктура: HTTP, дневной бюджет, кеш турниров - всё в той же SQLite,
# что использует основной сканер (db_path передаётся снаружи).
# ---------------------------------------------------------------------------

def _init_tables(db_path: str) -> None:
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
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS oddspapi_tournaments (
                tournament_id INTEGER PRIMARY KEY,
                tournament_name TEXT,
                category_name TEXT,
                cached_at TEXT NOT NULL
            )
            """
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[oddspapi] WARN: failed to init tables: {e}", flush=True)


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
    _init_tables(db_path)
    used = _get_today_count(db_path)
    ok = used < DAILY_REQUEST_BUDGET
    if not ok:
        print(f"[oddspapi] дневной бюджет исчерпан: {used}/{DAILY_REQUEST_BUDGET}", flush=True)
    return ok


def _http_get(path: str, params: dict) -> dict | list | None:
    if not API_KEY:
        print("[oddspapi] ERROR: ODDSPAPI_API_KEY не задан", flush=True)
        return None
    query = dict(params)
    query["apiKey"] = API_KEY
    url = f"{BASE_URL}{path}?{urlencode(query)}"
    try:
        req = urlrequest.Request(url, headers={"User-Agent": "bbk-scanner/1.0"})
        with urlrequest.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except HTTPError as e:
        print(f"[oddspapi] HTTP error {e.code} on {path}: {e.reason}", flush=True)
        return None
    except URLError as e:
        print(f"[oddspapi] Network error on {path}: {e.reason}", flush=True)
        return None
    except Exception as e:
        print(f"[oddspapi] Unexpected error on {path}: {e}", flush=True)
        return None


# ---------------------------------------------------------------------------
# Кеш турниров: подтягиваем список раз в 7 дней (1 запрос), дальше матчим
# league_name из наших алертов на tournamentId без траты бюджета.
# ---------------------------------------------------------------------------

def _tournament_cache_is_fresh(db_path: str) -> bool:
    try:
        conn = sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT MAX(cached_at) FROM oddspapi_tournaments"
        ).fetchone()
        conn.close()
        if not row or not row[0]:
            return False
        cached_at = datetime.fromisoformat(row[0])
        return datetime.now(timezone.utc) - cached_at < timedelta(days=TOURNAMENT_CACHE_TTL_DAYS)
    except Exception:
        return False


def _refresh_tournament_cache(db_path: str) -> bool:
    """Тянет список турниров (1 запрос из дневного бюджета) и сохраняет в SQLite."""
    if not budget_available(db_path):
        return False

    data = _http_get("/tournaments", {"sportId": SOCCER_SPORT_ID})
    _increment_today_count(db_path)

    if not data or not isinstance(data, list):
        print("[oddspapi] не удалось обновить кеш турниров", flush=True)
        return False

    try:
        conn = sqlite3.connect(db_path)
        now_iso = datetime.now(timezone.utc).isoformat()
        for t in data:
            conn.execute(
                """
                INSERT INTO oddspapi_tournaments (tournament_id, tournament_name, category_name, cached_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(tournament_id) DO UPDATE SET
                    tournament_name = excluded.tournament_name,
                    category_name = excluded.category_name,
                    cached_at = excluded.cached_at
                """,
                (t.get("tournamentId"), t.get("tournamentName"), t.get("categoryName"), now_iso),
            )
        conn.commit()
        conn.close()
        print(f"[oddspapi] кеш турниров обновлён: {len(data)} записей", flush=True)
        return True
    except Exception as e:
        print(f"[oddspapi] WARN: не удалось сохранить кеш турниров: {e}", flush=True)
        return False


def find_tournament_id(league_name: str, db_path: str) -> int | None:
    """Ищет tournamentId по нашему league_name (регистронезависимое вхождение
    подстроки в любую сторону). Обновляет кеш турниров, если он устарел или пуст.
    """
    if not league_name:
        return None

    _init_tables(db_path)
    if not _tournament_cache_is_fresh(db_path):
        _refresh_tournament_cache(db_path)

    league_lower = league_name.lower()
    try:
        conn = sqlite3.connect(db_path)
        rows = conn.execute(
            "SELECT tournament_id, tournament_name FROM oddspapi_tournaments"
        ).fetchall()
        conn.close()
    except Exception as e:
        print(f"[oddspapi] WARN: не удалось прочитать кеш турниров: {e}", flush=True)
        return None

    for tournament_id, tournament_name in rows:
        if not tournament_name:
            continue
        name_lower = tournament_name.lower()
        if name_lower in league_lower or league_lower in name_lower:
            return tournament_id
    return None


# ---------------------------------------------------------------------------
# Главная функция: получаем одды Pinnacle по турниру и матчим на наш алерт
# по времени начала матча.
# ---------------------------------------------------------------------------

def _extract_1x2_prices(fixture: dict) -> dict[str, float] | None:
    pinnacle = fixture.get("bookmakerOdds", {}).get("pinnacle")
    if not pinnacle:
        return None
    markets = pinnacle.get("markets", {})

    prices: dict[str, float] = {}
    for market_id, side in _MARKET_TO_SIDE.items():
        market = markets.get(market_id)
        if not market:
            continue
        try:
            outcome = market["outcomes"][market_id]
            price = outcome["players"]["0"]["price"]
            prices[side] = float(price)
        except (KeyError, TypeError, ValueError):
            continue

    return prices or None


def _find_matching_fixture(fixtures: list, commence_time_iso: str) -> dict | None:
    try:
        target = datetime.fromisoformat(commence_time_iso.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None

    best_fixture = None
    best_diff = timedelta(minutes=_KICKOFF_MATCH_TOLERANCE_MINUTES)

    for fixture in fixtures:
        start_time = fixture.get("startTime")
        if not start_time:
            continue
        try:
            candidate = datetime.fromisoformat(start_time.replace("Z", "+00:00"))
        except ValueError:
            continue
        diff = abs(candidate - target)
        if diff < best_diff:
            best_diff = diff
            best_fixture = fixture

    return best_fixture


def check_sharp_confirmation(
    league_name: str,
    commence_time: str,
    predicted_side: str,
    db_path: str,
) -> dict | None:
    """
    league_name: наше поле quote.league_name (например "Premier League")
    commence_time: наше поле quote.commence_time (ISO8601)
    predicted_side: 'home' | 'draw' | 'away'
    db_path: путь к той же SQLite базе, что использует основной сканер

    Возвращает {"confirmed": bool, "sharp_favorite": str, "pinnacle_odds": {...}}
    или None при любой проблеме (бюджет/не нашли турнир/не нашли матч/нет данных).
    """
    tournament_id = find_tournament_id(league_name, db_path)
    if tournament_id is None:
        print(f"[oddspapi] турнир не найден в кеше: {league_name!r}", flush=True)
        return None

    if not budget_available(db_path):
        return None

    data = _http_get(
        "/odds-by-tournaments",
        {"bookmaker": "pinnacle", "tournamentIds": str(tournament_id)},
    )
    _increment_today_count(db_path)

    if not data or not isinstance(data, list):
        return None

    fixture = _find_matching_fixture(data, commence_time)
    if fixture is None:
        print(f"[oddspapi] матч не найден по времени старта: {commence_time}", flush=True)
        return None

    prices = _extract_1x2_prices(fixture)
    if not prices:
        return None

    sharp_favorite = min(prices, key=prices.get)
    confirmed = sharp_favorite == predicted_side

    return {
        "confirmed": confirmed,
        "sharp_favorite": sharp_favorite,
        "pinnacle_odds": prices,
    }
