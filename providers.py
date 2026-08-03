from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime, timezone
import json
import random
import re
import threading
import time as time_module
import urllib.parse
import urllib.request

from models import OddsQuote

try:
    import websocket  # пакет "websocket-client"
except ImportError:
    websocket = None


class ProviderError(RuntimeError):
    pass


class OddsProvider(ABC):
    @abstractmethod
    def fetch(self) -> list[OddsQuote]:
        raise NotImplementedError


class TheOddsApiProvider(OddsProvider):
    BASE_URL = "https://api.the-odds-api.com/v4"

    def __init__(
        self,
        api_key: str,
        sport_keys: tuple[str, ...],
        regions: str,
        markets: tuple[str, ...],
        odds_format: str,
    ):
        if not api_key:
            raise ProviderError("ODDS_API_KEY не заполнен.")
        if not sport_keys:
            raise ProviderError("Не указан ни один SPORT_KEY.")
        self.api_key = api_key
        self.sport_keys = sport_keys
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

    def _fetch_one(self, sport_key: str) -> list[OddsQuote]:
        data = self._get_json(
            f"/sports/{urllib.parse.quote(sport_key)}/odds/",
            {
                "apiKey": self.api_key,
                "regions": self.regions,
                "markets": ",".join(self.markets),
                "oddsFormat": self.odds_format,
                "dateFormat": "iso",
            },
        )
        if not isinstance(data, list):
            raise ProviderError(f"API вернул неожиданный формат данных для {sport_key}.")

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
                                sport_key=str(event.get("sport_key", sport_key)),
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

    def fetch(self) -> list[OddsQuote]:
        all_quotes: list[OddsQuote] = []
        errors: list[str] = []
        for sport_key in self.sport_keys:
            try:
                all_quotes.extend(self._fetch_one(sport_key))
            except ProviderError as exc:
                errors.append(f"{sport_key}: {exc}")
        if errors and not all_quotes:
            raise ProviderError("Все турниры не удалось получить: " + "; ".join(errors))
        return all_quotes


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


_WIN_RE = re.compile(r"^WIN__(P1|P2|X)$")
_TOTALS_RE = re.compile(r"^TOTALS__(OVER|UNDER)\(([\d.]+)\)$")


class OddscorpProvider(OddsProvider):
    """Push-фид ODDSCORP по WebSocket. В отличие от TheOddsApiProvider,
    держит постоянное соединение в фоновом потоке на каждую БК.
    fetch() не делает сетевой запрос — отдаёт снимок накопленных данных."""

    def __init__(
        self,
        auth_key: str,
        bookmakers: tuple[str, ...],
        ws_url: str = "ws://api.oddscorp.com:8001",
        initial_wait_seconds: float = 5.0,
    ):
        if websocket is None:
            raise ProviderError("Пакет 'websocket-client' не установлен — добавьте в requirements.txt.")
        if not auth_key:
            raise ProviderError("ODDSCORP_AUTH_KEY не заполнен.")
        if not bookmakers:
            raise ProviderError("ODDSCORP_BOOKMAKERS пуст.")

        self.auth_key = auth_key
        self.bookmakers = bookmakers
        self.ws_url = ws_url

        self._lock = threading.Lock()
        self._events: dict[str, dict] = {}
        self._markets: dict[str, dict[str, float]] = {}
        self._bk_by_event: dict[str, str] = {}

        for bk in self.bookmakers:
            threading.Thread(target=self._run_socket, args=(bk,), daemon=True).start()

        if initial_wait_seconds > 0:
            time_module.sleep(initial_wait_seconds)

    def _run_socket(self, bk: str) -> None:
        subscribe_msg = json.dumps({
            "cmd": "subscribe",
            "auth_key": self.auth_key,
            "needed_bk": [bk],
            "send_events_ids": True,
            "send_actual_first": True,
        })
        while True:
            try:
                ws = websocket.WebSocketApp(
                    self.ws_url,
                    on_open=lambda w: w.send(subscribe_msg),
                    on_message=lambda w, msg: self._on_message(msg),
                )
                ws.run_forever(ping_interval=None)
            except Exception:
                pass
            time_module.sleep(5)

    def _on_message(self, raw: str) -> None:
        try:
            payload = json.loads(raw)
        except (ValueError, TypeError):
            return
        if isinstance(payload, dict) or not isinstance(payload, list) or len(payload) < 4:
            return

        bk_name, msg_type, bk_event_id, data = payload[0], payload[1], payload[2], payload[3]

        with self._lock:
            if msg_type == "update_event" and isinstance(data, dict):
                self._events[bk_event_id] = data
                self._bk_by_event[bk_event_id] = bk_name
            elif msg_type == "update_markets" and isinstance(data, list):
                bucket = self._markets.setdefault(bk_event_id, {})
                for row in data:
                    if not row:
                        continue
                    name = row[0]
                    blocked = row[1] if len(row) > 1 else 0
                    price = row[2] if len(row) > 2 else None
                    if blocked or price is None:
                        continue
                    try:
                        bucket[name] = float(price)
                    except (TypeError, ValueError):
                        continue
            elif msg_type == "remove_markets" and isinstance(data, list):
                bucket = self._markets.get(bk_event_id)
                if bucket:
                    for name in data:
                        bucket.pop(name, None)
            elif msg_type in ("remove_event", "remove_event_final"):
                self._events.pop(bk_event_id, None)
                self._markets.pop(bk_event_id, None)
                self._bk_by_event.pop(bk_event_id, None)

    def fetch(self) -> list[OddsQuote]:
        captured_at = datetime.now(timezone.utc).isoformat()
        quotes: list[OddsQuote] = []

        with self._lock:
            events_snapshot = dict(self._events)
            markets_snapshot = {k: dict(v) for k, v in self._markets.items()}

        for bk_event_id, event in events_snapshot.items():
            markets = markets_snapshot.get(bk_event_id, {})
            if not markets:
                continue
            home = str(event.get("team1", ""))
            away = str(event.get("team2", ""))
            sport = str(event.get("sport", ""))
            bk_name = str(event.get("bk_name", self._bk_by_event.get(bk_event_id, "")))

            for market_name, price in markets.items():
                win_match = _WIN_RE.match(market_name)
                totals_match = _TOTALS_RE.match(market_name)
                if win_match:
                    market_key = "h2h"
                    code = win_match.group(1)
                    outcome_name = home if code == "P1" else away if code == "P2" else "Draw"
                    point = None
                elif totals_match:
                    market_key = "totals"
                    outcome_name = totals_match.group(1).capitalize()
                    point = float(totals_match.group(2))
                else:
                    continue

                quotes.append(
                    OddsQuote(
                        event_id=str(bk_event_id),
                        sport_key=sport,
                        commence_time="",
                        home_team=home,
                        away_team=away,
                        bookmaker_key=bk_name,
                        bookmaker_title=bk_name,
                        market_key=market_key,
                        outcome_name=outcome_name,
                        point=point,
                        price=price,
                        captured_at=captured_at,
                    )
                )
        return quotes
