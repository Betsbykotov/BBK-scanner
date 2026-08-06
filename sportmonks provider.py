"""
Sportmonks Provider — live xG и Pressure Index по матчам.
Работает независимо от OddsCorp/Odds-API. Опрашивает REST API (не WebSocket).
"""

import asyncio
import logging
from datetime import datetime, timezone

import aiohttp

logger = logging.getLogger("sportmonks_provider")

BASE_URL = "https://api.sportmonks.com/v3/football"


class SportmonksProvider:
    """
    Провайдер live-статистики (xG, Pressure Index) через Sportmonks API.

    Не заменяет OddsCorp/Odds-API — работает параллельно как отдельный
    источник сигналов. Основной метод — get_live_pressure_data(), который
    возвращает список текущих live-матчей с их xG и pressure по обеим командам.
    """

    def __init__(self, api_token: str, poll_interval_seconds: int = 60):
        if not api_token:
            raise ValueError("SPORTMONKS_API_KEY не задан")
        self.api_token = api_token.strip()
        self.poll_interval_seconds = poll_interval_seconds
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def _request(self, path: str, params: dict | None = None) -> dict | None:
        session = await self._get_session()
        params = params or {}
        params["api_token"] = self.api_token
        url = f"{BASE_URL}{path}"
        try:
            async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.warning(f"Sportmonks HTTP {resp.status}: {body[:300]}")
                    return None
                return await resp.json()
        except asyncio.TimeoutError:
            logger.warning("Sportmonks request timeout")
            return None
        except Exception as e:
            logger.error(f"Sportmonks request error: {e}")
            return None

    async def get_live_pressure_data(self) -> list[dict]:
        """
        Возвращает список live-матчей с нормализованными данными:
        [
          {
            "fixture_id": int,
            "home_team": str,
            "away_team": str,
            "minute": int,
            "home_xg": float,
            "away_xg": float,
            "home_pressure": float,
            "away_pressure": float,
            "raw": {...}  # исходный fixture, на всякий случай
          },
          ...
        ]
        Пустой список — если live-матчей нет или запрос не удался (не бросает исключение).
        """
        data = await self._request(
            "/livescores",
            params={"include": "participants;scores;xgfixture;pressure;statistics"},
        )
        if not data or "data" not in data:
            return []

        results = []
        for fixture in data["data"]:
            try:
                normalized = self._normalize_fixture(fixture)
                if normalized:
                    results.append(normalized)
            except Exception as e:
                fid = fixture.get("id", "unknown")
                logger.warning(f"Sportmonks: не смог распарсить fixture {fid}: {e}")
                continue

        return results

    def _normalize_fixture(self, fixture: dict) -> dict | None:
        fixture_id = fixture.get("id")
        if fixture_id is None:
            return None

        participants = fixture.get("participants", []) or []
        home_name, away_name = "Home", "Away"
        home_id, away_id = None, None
        for p in participants:
            meta = p.get("meta", {})
            location = meta.get("location")
            if location == "home":
                home_name = p.get("name", "Home")
                home_id = p.get("id")
            elif location == "away":
                away_name = p.get("name", "Away")
                away_id = p.get("id")

        minute = (
            fixture.get("time", {}).get("minute")
            if isinstance(fixture.get("time"), dict)
            else fixture.get("minute")
        )

        home_xg, away_xg = self._extract_team_metric(
            fixture.get("xgfixture", []) or [], home_id, away_id
        )
        home_pressure, away_pressure = self._extract_team_metric(
            fixture.get("pressure", []) or [], home_id, away_id
        )

        return {
            "fixture_id": fixture_id,
            "home_team": home_name,
            "away_team": away_name,
            "minute": minute or 0,
            "home_xg": home_xg,
            "away_xg": away_xg,
            "home_pressure": home_pressure,
            "away_pressure": away_pressure,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "raw": fixture,
        }

    @staticmethod
    def _extract_team_metric(entries: list, home_id, away_id) -> tuple[float, float]:
        """
        Достаёт значение метрики (xG или pressure) по home/away из массива записей.
        Формат Sportmonks для этих include не 100% зафиксирован в документации на всех
        тарифах — поэтому пробуем несколько ключей с фолбэком.
        """
        home_val, away_val = 0.0, 0.0
        for entry in entries:
            participant_id = (
                entry.get("participant_id")
                or entry.get("team_id")
                or entry.get("participant", {}).get("id")
            )
            value = (
                entry.get("data", {}).get("value")
                if isinstance(entry.get("data"), dict)
                else entry.get("value")
            )
            if value is None:
                continue
            try:
                value = float(value)
            except (TypeError, ValueError):
                continue

            if participant_id == home_id:
                home_val = value
            elif participant_id == away_id:
                away_val = value

        return home_val, away_val

    async def run_loop(self, callback):
        """
        Бесконечный цикл опроса. callback(list_of_matches) вызывается каждый раз,
        когда получены свежие данные (даже если список пуст — чтобы вызывающий
        код мог сам решать, что делать).
        """
        logger.info(
            f"SportmonksProvider: старт цикла, интервал {self.poll_interval_seconds}с"
        )
        while True:
            try:
                matches = await self.get_live_pressure_data()
                await callback(matches)
            except Exception as e:
                logger.error(f"Sportmonks run_loop error: {e}")
            await asyncio.sleep(self.poll_interval_seconds)
