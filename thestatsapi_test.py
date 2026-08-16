"""
Тестовый скрипт для проверки TheStatsAPI (v2, с исправленными путями).
Base URL правильный: https://api.thestatsapi.com/api

Использование на Railway: Start Command = python thestatsapi_test.py
"""

import os
import requests
import json

API_KEY = os.environ.get("THESTATSAPI_KEY")
BASE_URL = "https://api.thestatsapi.com/api"

if not API_KEY:
    raise SystemExit("Ошибка: переменная THESTATSAPI_KEY не задана")

HEADERS = {"Authorization": f"Bearer {API_KEY}"}


def pretty(data):
    print(json.dumps(data, indent=2, ensure_ascii=False)[:2500])
    print("...\n")


def test_health():
    print("=== HEALTH CHECK ===")
    resp = requests.get(f"{BASE_URL}/health", headers=HEADERS)
    print(f"Status: {resp.status_code}")
    if resp.ok:
        pretty(resp.json())


def test_competitions():
    print("=== COMPETITIONS (поиск Premier League) ===")
    resp = requests.get(f"{BASE_URL}/football/competitions", headers=HEADERS, params={"search": "Premier League", "per_page": 3})
    print(f"Status: {resp.status_code}")
    comp_id = None
    if resp.ok:
        data = resp.json()
        pretty(data)
        items = data.get("data", [])
        if items:
            comp_id = items[0].get("id")
    else:
        print(resp.text)
    return comp_id


def test_matches(comp_id):
    print(f"=== MATCHES для competition_id={comp_id} ===")
    if not comp_id:
        print("Нет comp_id, пропускаем")
        return None
    resp = requests.get(
        f"{BASE_URL}/football/matches",
        headers=HEADERS,
        params={"competition_id": comp_id, "per_page": 5},
    )
    print(f"Status: {resp.status_code}")
    match_id = None
    if resp.ok:
        data = resp.json()
        pretty(data)
        items = data.get("data", [])
        if items:
            match_id = items[0].get("id")
    else:
        print(resp.text)
    return match_id


def test_odds(match_id):
    print(f"=== ODDS для match_id={match_id} (bookmaker=pinnacle) ===")
    if not match_id:
        print("Нет match_id, пропускаем")
        return
    resp = requests.get(
        f"{BASE_URL}/football/matches/{match_id}/odds",
        headers=HEADERS,
        params={"bookmaker": "pinnacle"},
    )
    print(f"Status: {resp.status_code}")
    if resp.ok:
        pretty(resp.json())
    else:
        print(resp.text)


def test_stats(match_id):
    print(f"=== STATS/xG для match_id={match_id} ===")
    if not match_id:
        print("Нет match_id, пропускаем")
        return
    resp = requests.get(f"{BASE_URL}/football/matches/{match_id}/stats", headers=HEADERS)
    print(f"Status: {resp.status_code}")
    if resp.ok:
        pretty(resp.json())
    else:
        print(resp.text)


if __name__ == "__main__":
    print(f"Тестируем TheStatsAPI, ключ: {API_KEY[:10]}...\n")

    test_health()
    print("\n" + "=" * 50 + "\n")

    comp_id = test_competitions()
    print("\n" + "=" * 50 + "\n")

    match_id = test_matches(comp_id)
    print("\n" + "=" * 50 + "\n")

    test_odds(match_id)
    print("\n" + "=" * 50 + "\n")

    test_stats(match_id)

    print("\n=== ИТОГ ===")
    print("Проверь: приходят ли реальные данные, есть ли Pinnacle в odds,")
    print("совпадает ли структура match_odds/last_seen с ожидаемой.")
