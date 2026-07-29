from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime, timezone
import json
import random
import urllib.parse
import urllib.request

from .models import OddsQuote


class ProviderError(RuntimeError):
    pass


class OddsProvider(ABC):
    @abstractmethod
    def fetch(self) -> list[OddsQuote]:
        raise NotImplementedError


class TheOddsApiProvider(OddsProvider):
    BASE_URL = "https://api.the-odds-api.com/v4"

    def __init__(self, api_key: str, sport_key: str, regions: str, markets: tuple[str, ...], odds_format: str):
        if not api_key:
            raise ProviderError("ODDS_API_KEY не заполнен.")
        self.api_key = api_key
        self.sport_key = sport_key
        self.regions = regions
        self.markets = markets
        self.odds_format = odds_format

    def _get_json(self, path: str, params: dict[str, str]) -> object:
        query = urllib.parse.urlencode(params)
        request = urllib.request.Request(
            f"{self.BASE_URL}{path}?{query}",
            headers={"User-Agent": "BBK-Scanner-MVP/0.1"},
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            raise ProviderError(f"Ошибка API коэффициентов: {exc}") from exc

    def fetch(self) -> list[OddsQuote]:
        data = self._get_json(
            f"/sports/{urllib.parse.quote(self.sport_key)}/odds/",
            {
                "apiKey": self.api_key,
                "regions": self.regions,
                "markets": ",".join(self.markets),
                "oddsFormat": self.odds_format,
                "dateFormat": "iso",
            },
        )
        if not isinstance(data, list):
            raise ProviderError("API вернул неожиданный формат данных.")

        captured_at = datetime.now(timezone.utc).isoformat()
        quotes: list[OddsQuote] = []
        for event in data:
            for bookmaker in event.get("bookmakers", []):
                for market in bookmaker.get("markets", []):
                    for outcome in market.get("outcomes", []):
                        price = outcome.get("price")
                        if not isinstance(price, (int, float)) or price <= 1:
                            continue
                        point = outcome.get("point")
                        quotes.append(
                            OddsQuote(
                                event_id=str(event["id"]),
                                sport_key=str(event.get("sport_key", self.sport_key)),
                                commence_time=str(event.get("commence_time", "")),
                                home_team=str(event.get("home_team", "")),
                                away_team=str(event.get("away_team", "")),
                                bookmaker_key=str(bookmaker.get("key", "")),
                                bookmaker_title=str(bookmaker.get("title", "")),
                                market_key=str(market.get("key", "")),
                                outcome_name=str(outcome.get("name", "")),
                                point=float(point) if isinstance(point, (int, float)) else None,
                                price=float(price),
                                captured_at=captured_at,
                            )
                        )
        return quotes


class MockProvider(OddsProvider):
    """Демонстрационные данные. Цены слегка меняются при каждом запуске."""

    def fetch(self) -> list[OddsQuote]:
        captured_at = datetime.now(timezone.utc).isoformat()
        events = [
            ("demo-001", "Копенгаген", "Полесье"),
            ("demo-002", "Динамо Загреб", "Ференцварош"),
        ]
        books = [("alpha", "Alpha Bet"), ("beta", "Beta Bet"), ("gamma", "Gamma Bet")]
        quotes: list[OddsQuote] = []

        for event_id, home, away in events:
            event_bias = 0.08 if event_id == "demo-001" else 0
            for idx, (book_key, book_title) in enumerate(books):
                jitter = random.uniform(-0.035, 0.035)
                book_bias = event_bias if book_key == "alpha" else 0
                outcomes = [
                    ("h2h", home, None, 1.85 + idx * 0.03 + jitter),
                    ("h2h", "Draw", None, 3.45 + idx * 0.04 + jitter),
                    ("h2h", away, None, 4.20 - idx * 0.05 + jitter),
                    ("totals", "Over", 2.5, 1.88 + book_bias + idx * 0.02 + jitter),
                    ("totals", "Under", 2.5, 1.96 - book_bias - idx * 0.01 - jitter),
                ]
                for market, outcome, point, price in outcomes:
                    quotes.append(
                        OddsQuote(
                            event_id=event_id,
                            sport_key="soccer_demo",
                            commence_time="2026-08-01T19:00:00Z",
                            home_team=home,
                            away_team=away,
                            bookmaker_key=book_key,
                            bookmaker_title=book_title,
                            market_key=market,
                            outcome_name=outcome,
                            point=point,
                            price=round(price, 3),
                            captured_at=captured_at,
                        )
                    )
        return quotes


def list_sports(api_key: str) -> list[dict]:
    if not api_key:
        raise ProviderError("ODDS_API_KEY не заполнен.")
    query = urllib.parse.urlencode({"apiKey": api_key})
    request = urllib.request.Request(
        f"{TheOddsApiProvider.BASE_URL}/sports/?{query}",
        headers={"User-Agent": "BBK-Scanner-MVP/0.1"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            data = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        raise ProviderError(f"Не удалось получить список видов спорта: {exc}") from exc
    return data if isinstance(data, list) else []
