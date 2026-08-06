"""
Sportmonks Provider — live xG и Pressure Index по матчам.

Работает синхронно (requests), как и остальные части сканера — никакого
asyncio, вписывается в существующий цикл run_cycle() / time.sleep().
Полностью изолирован от OddsCorp/Odds-API: если Sportmonks недоступен —
это не мешает основному циклу коэффициентов.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import requests

logger = logging.getLogger("sportmonks_provider")

BASE_URL = "https://api.sportmonks.com/v3/football"


class SportmonksProvider:
    """Провайдер live-статистики (xG, Pressure Index) через Sportmonks API."""

    def __init__(self, api_token: str, timeout_seconds: int = 15):
        if not api_token:
            raise ValueError("SPORTMONKS_API_KEY не задан")
        self.api_token = api_token.strip()
        self.timeout_seconds = timeout_seconds

    def _request(self, path: str, params: dict | None = None) -> dict | None:
        params = dict(params or {})
        params["api_token"] = self.api_token
        url = f"{BASE_URL}{path}"
        try:
            resp = requests.get(url, params=params, timeout=self.timeout_seconds)
        except requests.RequestException as exc:
            logger.warning(f"Sportmonks request error: {exc}")
            return None

        if resp.status_code != 200:
            logger.warning(f"Sportmonks HTTP {resp.status_code}: {resp.text[:300]}")
            return None

        try:
            return resp.json()
        except ValueError:
            logger.warning("Sportmonks: не удалось распарсить JSON ответа")
            return None

    def get_live_pressure_data(self) -> list[dict]:
        """
        Возвращает список live-матчей с нормализованными данными:
        [{fixture_id, home_team, away_team, minute,
          home_xg, away_xg, home_pressure, away_pressure, fetched_at, raw}, ...]

        Пустой список — если live-матчей нет или запрос не удался
        (никогда не бросает исключение наружу).
        """
        data = self._request(
            "/livescores",
            params={"include": "participants;scores;xgfixture;pressure;statistics.type"},
        )
        if not data or "data" not in data:
            return []

        results = []
        for fixture in data["data"]:
            try:
                normalized = self._normalize_fixture(fixture)
                if normalized:
                    results.append(normalized)
            except Exception as exc:
                fid = fixture.get("id", "unknown")
                logger.warning(f"Sportmonks: не смог распарсить fixture {fid}: {exc}")
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
            meta = p.get("meta", {}) or {}
            location = meta.get("location")
            if location == "home":
                home_name = p.get("name", "Home")
                home_id = p.get("id")
            elif location == "away":
                away_name = p.get("name", "Away")
                away_id = p.get("id")

        time_block = fixture.get("time")
        minute = time_block.get("minute") if isinstance(time_block, dict) else fixture.get("minute")

        home_xg, away_xg = self._extract_team_metric(fixture.get("xgfixture", []) or [], home_id, away_id)
        home_pressure, away_pressure = self._extract_team_metric(fixture.get("pressure", []) or [], home_id, away_id)

        stats = fixture.get("statistics", []) or []
        home_shots, away_shots = self._extract_stat_by_name(stats, home_id, away_id, ("shots total", "total shots"))
        home_shots_on_target, away_shots_on_target = self._extract_stat_by_name(
            stats, home_id, away_id, ("shots on target", "shots on goal", "on target")
        )
        home_corners, away_corners = self._extract_stat_by_name(stats, home_id, away_id, ("corners",))
        home_possession, away_possession = self._extract_stat_by_name(stats, home_id, away_id, ("possession", "ball possession"))

        return {
            "fixture_id": fixture_id,
            "home_team": home_name,
            "away_team": away_name,
            "minute": minute or 0,
            "home_xg": home_xg,
            "away_xg": away_xg,
            "home_pressure": home_pressure,
            "away_pressure": away_pressure,
            "home_shots": home_shots,
            "away_shots": away_shots,
            "home_shots_on_target": home_shots_on_target,
            "away_shots_on_target": away_shots_on_target,
            "home_corners": home_corners,
            "away_corners": away_corners,
            "home_possession": home_possession,
            "away_possession": away_possession,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "raw": fixture,
        }

    @staticmethod
    def _extract_team_metric(entries: list, home_id, away_id) -> tuple[float, float]:
        """
        Достаёт значение метрики (xG или pressure) по home/away.
        Формат Sportmonks для этих include не 100% зафиксирован в документации
        на всех тарифах — пробуем несколько ключей с фолбэком.
        """
        home_val, away_val = 0.0, 0.0
        for entry in entries:
            participant_id = (
                entry.get("participant_id")
                or entry.get("team_id")
                or (entry.get("participant") or {}).get("id")
            )
            data_block = entry.get("data")
            value = data_block.get("value") if isinstance(data_block, dict) else entry.get("value")
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

    @staticmethod
    def _extract_stat_by_name(entries: list, home_id, away_id, name_keywords: tuple[str, ...]) -> tuple[float, float]:
        """
        Достаёт значение статистики (удары, угловые, владение и т.п.) по названию типа.
        Формат type-имён у Sportmonks не полностью зафиксирован в документации на всех
        тарифах — сравниваем по подстроке в названии типа, регистронезависимо.
        Если статистика не найдена — возвращает (0.0, 0.0), не бросает исключение.
        """
        home_val, away_val = 0.0, 0.0
        for entry in entries:
            type_block = entry.get("type") or {}
            type_name = (type_block.get("name") or entry.get("type_name") or "").lower()
            if not type_name or not any(kw in type_name for kw in name_keywords):
                continue

            participant_id = (
                entry.get("participant_id")
                or entry.get("team_id")
                or (entry.get("participant") or {}).get("id")
            )
            data_block = entry.get("data")
            value = data_block.get("value") if isinstance(data_block, dict) else entry.get("value")
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
