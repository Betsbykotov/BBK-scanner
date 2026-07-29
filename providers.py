from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime, timezone
import json
import random
import urllib.parse
import urllib.request

from models import OddsQuote


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
