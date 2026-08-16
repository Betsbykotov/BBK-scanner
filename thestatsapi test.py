"""
Тестовый скрипт для проверки TheStatsAPI перед интеграцией в BBK Scanner.
Не трогает боевой main.py — запускается отдельно, просто печатает сырые данные.

Использование:
    export THESTATSAPI_KEY="твой_ключ"
    python thestatsapi_test.py
"""

import os
import requests
import json

API_KEY = os.environ.get("THESTATSAPI_KEY")
BASE_URL = "https://api.thestatsapi.com"

if not API_KEY:
    raise SystemExit("Ошибка: переменная THESTATSAPI_KEY не задана")

HEADERS = {"Authorization": f"Bearer {API_KEY}"}


def pretty(data):
    print(json.dumps(data, indent=2, ensure_ascii=False)[:3000])
    print("...\n")


def test_fixtures():
    """Проверяем список ближайших матчей"""
    print("=== FIXTURES (ближайшие матчи) ===")
    resp = requests.get(f"{BASE_URL}/football/matches", headers=HEADERS)
    print(f"Status: {resp.status_code}")
    if resp.ok:
        data = resp.json()
        pretty(data)
        # Возвращаем id первого матча для дальнейших тестов
        matches = data.get("data", [])
        if matches:
            return matches[0].get("id") or matches[0].get("match_id")
    else:
        print(resp.text)
    return None


def test_odds(match_id):
    """Проверяем коэффициенты по конкретному матчу — ищем bet365 и Pinnacle"""
    if not match_id:
        print("Нет match_id, пропускаем тест odds")
        return
    print(f"=== ODDS для матча {match_id} ===")
    resp = requests.get(f"{BASE_URL}/football/matches/{match_id}/odds", headers=HEADERS)
    print(f"Status: {resp.status_code}")
    if resp.ok:
        data = resp.json()
        pretty(data)
        # Проверяем какие букмекеры реально пришли
        bookmakers_found = set()
        raw = json.dumps(data).lower()
        for bk in ["bet365", "pinnacle", "paddy power", "betfair", "kambi", "parimatch"]:
            if bk in raw:
                bookmakers_found.add(bk)
        print(f"Найденные букмекеры в ответе: {bookmakers_found}")
    else:
        print(resp.text)


def test_xg(match_id):
    """Проверяем xG данные по матчу"""
    if not match_id:
        print("Нет match_id, пропускаем тест xG")
        return
    print(f"=== xG для матча {match_id} ===")
    resp = requests.get(f"{BASE_URL}/football/matches/{match_id}/stats", headers=HEADERS)
    print(f"Status: {resp.status_code}")
    if resp.ok:
        data = resp.json()
        pretty(data)
    else:
        print(resp.text)


def test_live_matches():
    """Проверяем live-эндпоинт отдельно (важно для SHARP/PRESSURE детекции в реальном времени)"""
    print("=== LIVE MATCHES ===")
    resp = requests.get(f"{BASE_URL}/football/matches/live", headers=HEADERS)
    print(f"Status: {resp.status_code}")
    if resp.ok:
        pretty(resp.json())
    else:
        print(resp.text)
        print("(если 404 — проверь точное название live-эндпоинта в документации thestatsapi.com/docs)")


if __name__ == "__main__":
    print(f"Тестируем TheStatsAPI, ключ: {API_KEY[:10]}...\n")

    match_id = test_fixtures()
    print("\n" + "=" * 50 + "\n")

    test_odds(match_id)
    print("\n" + "=" * 50 + "\n")

    test_xg(match_id)
    print("\n" + "=" * 50 + "\n")

    test_live_matches()

    print("\n=== ИТОГ ===")
    print("1. Проверь выше, есть ли Parimatch в списке найденных букмекеров")
    print("2. Проверь структуру xG-ответа — совпадает ли с тем, что парсит PRESSURE в main.py")
    print("3. Проверь live-эндпоинт — это критично для детекции в реальном времени")
