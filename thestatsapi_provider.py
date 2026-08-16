"""
Провайдер TheStatsAPI — замена oddspapi_provider.py.

Переписан по официальной документации (api.thestatsapi.com/llms.txt)
после того как первая версия падала с 404 (не хватало /api в base URL).

Даёт ту же функцию check_sharp_confirmation(league_name, commence_time,
predicted_side, db_path) -> dict | None, что и старый oddspapi_provider,
чтобы main.py менялся минимально (буквально имя импорта).

ВАЖНО: у TheStatsAPI нет Parimatch среди букмекеров — только bet365,
paddy-power, betmgm-uk, pinnacle, betfair-exchange. Поэтому sharp-сверка
идёт по Pinnacle (как и раньше через OddsPapi) — это не потеря
функциональности, просто другой источник тех же самых Pinnacle-данных.

Логика:
1. Ищем соревнование по названию лиги через /football/competitions?search=
2. Ищем матч в этом соревновании на нужную дату через /football/matches
3. Берём коэффициенты Pinnacle через /football/matches/{id}/odds?bookmaker=pinnacle
   (для live-матчей — через /odds/live)
4. Сравниваем фаворита по Pinnacle с нашим predicted_side
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone

import requests

from thestatsapi_rate_limiter import throttle as _shared_throttle

BASE_URL = "https://api.thestatsapi.com/api"
API_KEY = os.environ.get("THESTATSAPI_KEY", "")
DAILY_BUDGET = int(os.environ.get("THESTATSAPI_DAILY_BUDGET", "2000"))

_budget_state = {"date": None, "used": 0}

# Кеш поиска соревнований по названию лиги — не долбим /competitions
# на каждый алерт с одной и той же лигой.
_competition_cache: dict[str, str | None] = {}

# Кеш матчей по (competition_id, date) — на случай нескольких алертов
# по одной лиге/дню подряд.
_matches_cache: dict[tuple, tuple[float, list]] = {}
CACHE_TTL_SECONDS = 300


def _log(message: str) -> None:
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"[{timestamp}] [thestatsapi] {message}")


def _check_and_consume_budget() -> bool:
    today = datetime.now(timezone.utc).date().isoformat()
    if _budget_state["date"] != today:
        _budget_state["date"] = today
        _budget_state["used"] = 0
    if _budget_state["used"] >= DAILY_BUDGET:
        _log(f"дневной бюджет исчерпан: {_budget_state['used']}/{DAILY_BUDGET}")
        return False
    _budget_state["used"] += 1
    return True


def _headers() -> dict:
    return {"Authorization": f"Bearer {API_KEY}"}


def _get(path: str, params: dict | None = None) -> dict | None:
    if not _check_and_consume_budget():
        return None
    _shared_throttle()
    try:
        resp = requests.get(f"{BASE_URL}{path}", headers=_headers(), params=params or {}, timeout=10)
        if resp.status_code != 200:
            _log(f"HTTP error {resp.status_code} on {path}: {resp.text[:200]}")
            return None
        return resp.json()
    except requests.RequestException as exc:
        _log(f"сетевая ошибка на {path}: {exc}")
        return None


def _find_competition_id(league_name: str) -> str | None:
    league_name = (league_name or "").strip()
    if not league_name:
        return None

    if league_name in _competition_cache:
        return _competition_cache[league_name]

    # OddsCorp отдаёт лиги в формате "England. Premier League" -> разбиваем
    # на страну и название, чтобы искать точнее и фильтровать кандидатов
    # по стране (иначе "Premier League" находит канадскую/египетскую лигу
    # раньше английской).
    country_hint = None
    search_term = league_name
    if "." in league_name:
        parts = league_name.split(".", 1)
        country_hint = parts[0].strip().lower()
        search_term = parts[1].strip()

    data = _get("/football/competitions", {"search": search_term, "per_page": 10})
    comp_id = None
    if data and data.get("data"):
        candidates = data["data"]
        if country_hint:
            matched = [
                c for c in candidates
                if country_hint in str(c.get("country") or "").strip().lower()
            ]
            if matched:
                comp_id = matched[0].get("id")
        if comp_id is None:
            comp_id = candidates[0].get("id")

    _competition_cache[league_name] = comp_id
    if comp_id is None:
        _log(f"соревнование не найдено: {league_name!r} (поиск: {search_term!r}, страна: {country_hint!r})")
    return comp_id


def _find_match(competition_id: str, commence_time: str) -> dict | None:
    try:
        target_dt = datetime.fromisoformat(commence_time.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None

    date_str = target_dt.date().isoformat()
    cache_key = (competition_id, date_str)
    now = time.time()

    cached = _matches_cache.get(cache_key)
    if cached and now - cached[0] < CACHE_TTL_SECONDS:
        matches = cached[1]
    else:
        data = _get(
            "/football/matches",
            {
                "competition_id": competition_id,
                "date_from": date_str,
                "date_to": date_str,
                "per_page": 100,
            },
        )
        matches = data.get("data", []) if data else []
        _matches_cache[cache_key] = (now, matches)

    best_match = None
    best_diff = None
    for m in matches:
        m_time_raw = m.get("utc_date")
        if not m_time_raw:
            continue
        try:
            m_dt = datetime.fromisoformat(str(m_time_raw).replace("Z", "+00:00"))
        except ValueError:
            continue
        diff = abs((m_dt - target_dt).total_seconds())
        if diff <= 900 and (best_diff is None or diff < best_diff):
            best_match = m
            best_diff = diff

    if best_match is None:
        _log(f"матч не найден: competition={competition_id} дата={date_str}")
    return best_match


def _extract_pinnacle_favorite(bookmakers: list, price_field: str) -> str | None:
    pinnacle = None
    for bk in bookmakers or []:
        if str(bk.get("bookmaker", "")).strip().lower() == "pinnacle":
            pinnacle = bk
            break
    if pinnacle is None:
        return None

    match_odds = pinnacle.get("markets", {}).get("match_odds")
    if not match_odds:
        return None

    prices = {}
    for side in ("home", "draw", "away"):
        entry = match_odds.get(side)
        if not entry:
            continue
        raw_price = entry.get(price_field)
        if raw_price is None:
            continue
        try:
            prices[side] = float(raw_price)
        except (TypeError, ValueError):
            continue

    if not prices:
        return None

    # Фаворит = сторона с наименьшим коэффициентом
    return min(prices, key=prices.get)


def _fetch_pinnacle_favorite(match_id: str, is_live: bool) -> str | None:
    if is_live:
        data = _get(f"/football/matches/{match_id}/odds/live")
        if not data:
            return None
        bookmakers = data.get("data", {}).get("bookmakers", [])
        return _extract_pinnacle_favorite(bookmakers, "live")
    else:
        data = _get(f"/football/matches/{match_id}/odds", {"bookmaker": "pinnacle"})
        if not data:
            return None
        bookmakers = data.get("data", {}).get("bookmakers", [])
        return _extract_pinnacle_favorite(bookmakers, "last_seen")


def check_sharp_confirmation(
    league_name: str, commence_time: str, predicted_side: str, db_path: str
) -> dict | None:
    """Совместимая замена oddspapi_provider.check_sharp_confirmation.

    Возвращает {"sharp_favorite": str, "confirmed": bool} либо None.
    """
    if not API_KEY:
        return None

    competition_id = _find_competition_id(league_name)
    if competition_id is None:
        return None

    match = _find_match(competition_id, commence_time)
    if match is None:
        return None

    match_id = match.get("id")
    if not match_id:
        return None

    is_live = match.get("status") == "live"
    sharp_favorite = _fetch_pinnacle_favorite(match_id, is_live)
    if sharp_favorite is None:
        return None

    return {
        "sharp_favorite": sharp_favorite,
        "confirmed": sharp_favorite == predicted_side,
    }
