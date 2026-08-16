from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import sys
import time

from analyzer import OddsAnalyzer
from config import Settings
from database import OddsDatabase
from models import Alert
from notifier import TelegramNotifier, format_alert
from providers import MockProvider, OddscorpProvider, ProviderError, TheOddsApiProvider, list_sports
from sportmonks_provider import SportmonksProvider
from pressure_detector import detect_pressure_alerts
from pressure_card import generate_pressure_card
import thestatsapi_provider as oddspapi_provider
from thestatsapi_pressure_provider import ThestatsapiPressureProvider
from thestatsapi_pressure_detector import detect_pressure_alerts_v3

# Карточка генерируется только для самых сильных PRESSURE-алертов —
# остальные уходят как обычно, текстом. Регулируется тут, пока нет
# отдельной переменной окружения в config.py.
REPORT_CARD_MIN_SCORE = 50.0


def _predicted_side_for_sharp_check(alert: Alert) -> str | None:
    """Определяет 'home'/'draw'/'away' для сверки с Pinnacle через TheStatsAPI.
    Работает ТОЛЬКО для рынка h2h (исход матча) — для тоталов/фор/прочих
    рынков возвращает None, проверка по Pinnacle для них не делается,
    т.к. сравнение home/draw/away там не имеет смысла.
    """
    q = alert.quote
    market = (q.market_key or "").lower()
    if market not in ("h2h", "moneyline", "1x2", "match_winner"):
        return None
    outcome = (q.outcome_name or "").strip().lower()
    if outcome in ("draw", "tie", "x", "ничья"):
        return "draw"
    if outcome == (q.home_team or "").strip().lower():
        return "home"
    if outcome == (q.away_team or "").strip().lower():
        return "away"
    return None


def _try_sharp_confirmation(alert: Alert, db_path: str) -> dict | None:
    """Пытается получить sharp-подтверждение от TheStatsAPI/Pinnacle для топовых
    сигналов. Дневной бюджет запросов регулируется внутри thestatsapi_provider
    (THESTATSAPI_DAILY_BUDGET) — как только он исчерпан, функция просто вернёт
    None для всех последующих алертов текущего дня.

    Любая ошибка -> None, алерт всё равно уходит как обычно, просто без
    пометки. Результат логируется отдельной строкой с тегом [SHARP-CHECK]
    для последующего анализа за тестовый период (грепается в Railway логах).
    """
    predicted_side = _predicted_side_for_sharp_check(alert)
    if predicted_side is None:
        return None

    q = alert.quote
    try:
        result = oddspapi_provider.check_sharp_confirmation(
            q.league_name, q.commence_time, predicted_side, db_path
        )
    except Exception as exc:
        _log(f"[SHARP-CHECK] ошибка проверки {q.home_team} — {q.away_team}: {exc}")
        return None

    if result is not None:
        _log(
            f"[SHARP-CHECK] score={alert.bbk_score:.0f} {q.home_team} — {q.away_team} "
            f"| наш прогноз={predicted_side} | pinnacle_favorite={result.get('sharp_favorite')} "
            f"| confirmed={result.get('confirmed')}"
        )
    return result


def _filter_by_horizon(quotes: list, hours_ahead_limit: float) -> list:
    now = datetime.now(timezone.utc)
    horizon = timedelta(hours=hours_ahead_limit)
    kept = []
    for quote in quotes:
        try:
            commence = datetime.fromisoformat(quote.commence_time.replace("Z", "+00:00"))
        except ValueError:
            kept.append(quote)
            continue
        if quote.is_live or commence - now <= horizon:
            kept.append(quote)
    return kept


def _filter_by_league(quotes: list, whitelist: tuple[str, ...], blacklist: tuple[str, ...]) -> list:
    kept = []
    for quote in quotes:
        league_lower = (quote.league_name or "").lower()

        if blacklist and any(bad in league_lower for bad in blacklist):
            continue

        if whitelist:
            if not league_lower or not any(good in league_lower for good in whitelist):
                continue

        kept.append(quote)
    return kept


def _filter_by_sport(quotes: list, whitelist: tuple[str, ...]) -> list:
    if not whitelist:
        return quotes
    kept = []
    for quote in quotes:
        sport_lower = (quote.sport_key or "").lower()
        if sport_lower and any(good in sport_lower for good in whitelist):
            kept.append(quote)
    return kept


def _filter_pressure_matches(matches: list, league_blacklist: tuple[str, ...] = ()) -> list:
    """Убирает нежелательные матчи (Россия, эсокер/виртуальный футбол и всё,
    что уже сидит в LEAGUE_BLACKLIST) из данных Sportmonks перед тем, как они
    попадут в PRESSURE детектор.

    SHARP/MOMENTUM фильтруются через LEAGUE_BLACKLIST на уровне quotes
    (_filter_by_league), но PRESSURE работает с сырыми матчами Sportmonks
    и раньше такого фильтра не имел вообще — отсюда были проскоки и по
    России, и по эсокеру (Esoccer Battle и т.п.), который уже давно сидит
    в LEAGUE_BLACKLIST для остальных детекторов.
    """
    blocked_countries = {"russia", "россия"}
    filtered = []
    for m in matches:
        country = str(
            m.get("country") or m.get("country_name") or m.get("league_country") or ""
        ).strip().lower()
        league = str(m.get("league_name") or m.get("league") or "").strip().lower()

        if country in blocked_countries:
            continue
        if "russia" in league or "russian" in league:
            continue
        if league_blacklist and any(bad in league for bad in league_blacklist):
            continue

        filtered.append(m)
    return filtered


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="BBK Scanner MVP")
    parser.add_argument(
        "--provider",
        choices=("mock", "odds-api", "oddscorp"),
        default="mock",
        help="Источник коэффициентов",
    )
    parser.add_argument(
        "--list-sports",
        action="store_true",
        help="Показать доступные sport key The Odds API",
    )
    parser.add_argument(
        "--loop",
        action="store_true",
        help="Работать непрерывно: опрашивать источник каждые --interval-minutes",
    )
    parser.add_argument(
        "--interval-minutes",
        type=float,
        default=None,
        help="Интервал опроса в режиме --loop (по умолчанию — POLL_INTERVAL_MINUTES из .env)",
    )
    parser.add_argument(
        "--no-pressure",
        action="store_true",
        help="Отключить сигналы xG/Pressure от Sportmonks, даже если ключ задан",
    )
    return parser


def _log(message: str) -> None:
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"[{timestamp}] {message}")


def _dedup_alerts(db: OddsDatabase, alerts: list[Alert], cooldown_minutes: int) -> list[Alert]:
    now_iso = datetime.now(timezone.utc).isoformat()
    fresh: list[Alert] = []
    for alert in alerts:
        if db.was_recently_sent(alert.dedup_key, cooldown_minutes, now_iso):
            continue
        fresh.append(alert)
    return fresh


def _mark_sent(db: OddsDatabase, alerts: list[Alert]) -> None:
    now_iso = datetime.now(timezone.utc).isoformat()
    for alert in alerts:
        db.mark_sent(alert.dedup_key, now_iso)


def _prune_old_timestamps(timestamps: list[datetime], now: datetime, window_minutes: int = 60) -> list[datetime]:
    cutoff = now - timedelta(minutes=window_minutes)
    return [t for t in timestamps if t >= cutoff]


def run_pressure_cycle(
    sportmonks_provider: SportmonksProvider | None,
    db: OddsDatabase,
    notifier: TelegramNotifier,
    cooldown_minutes: int,
    league_blacklist: tuple[str, ...] = (),
) -> None:
    if sportmonks_provider is None:
        return

    try:
        matches = sportmonks_provider.get_live_pressure_data()
    except Exception as exc:
        _log(f"[PRESSURE] Ошибка получения данных Sportmonks: {exc}")
        return

    matches = _filter_pressure_matches(matches, league_blacklist)

    if not matches:
        _log("[PRESSURE] Live-матчей от Sportmonks не найдено в этом цикле.")
        return

    pressure_alerts = detect_pressure_alerts(matches)

    # Score по fixture_id для тех матчей, где сработал алерт в этом цикле —
    # нужен, чтобы пометить точку на графике карточки, где был алерт.
    alert_score_by_fixture: dict[str, float] = {}
    for a in pressure_alerts:
        fixture_id = a["dedup_key"].split(":")[1] if ":" in a["dedup_key"] else None
        if fixture_id:
            alert_score_by_fixture[fixture_id] = a["score"]

    # Сохраняем снимок КАЖДОГО матча в этом цикле — не только тех, где
    # сработал алерт. Без непрерывной истории график "давление нарастает"
    # в карточке будет не из чего строить: нужна линия ДО момента алерта,
    # а не только точка в момент срабатывания.
    now_iso = datetime.now(timezone.utc).isoformat()
    for m in matches:
        fixture_id_str = str(m.get("fixture_id") or "")
        score = alert_score_by_fixture.get(fixture_id_str)
        try:
            db.insert_pressure_snapshot(m, now_iso, alert_score=score)
        except Exception as exc:
            _log(f"[PRESSURE] Ошибка записи снимка fixture={fixture_id_str}: {exc}")

    if not pressure_alerts:
        minutes_seen = sorted(m.get("minute", 0) for m in matches)
        _log(
            f"[PRESSURE] Матчей от Sportmonks: {len(matches)} (минуты: {minutes_seen}) "
            f"— явного дисбаланса xG/Pressure не найдено."
        )
        return

    sent = 0
    matches_by_fixture = {str(m.get("fixture_id") or ""): m for m in matches}
    for alert in pressure_alerts:
        if db.was_recently_sent(alert["dedup_key"], cooldown_minutes, now_iso):
            continue

        fixture_id_str = alert["dedup_key"].split(":")[1] if ":" in alert["dedup_key"] else ""
        is_escalating = "🔥" in alert["message"]

        card_sent = False
        if alert["score"] >= REPORT_CARD_MIN_SCORE and fixture_id_str:
            match_for_card = matches_by_fixture.get(fixture_id_str)
            if match_for_card is not None:
                try:
                    history = db.get_pressure_history(fixture_id_str, window_minutes=20)
                    card_bytes = generate_pressure_card(
                        match_for_card, history, alert["score"], is_escalating
                    )
                    caption = alert["message"][:1024]
                    notifier.send_photo(card_bytes, caption)
                    card_sent = True
                except Exception as exc:
                    _log(f"[PRESSURE] Ошибка генерации/отправки карточки fixture={fixture_id_str}: {exc}")

        if not card_sent:
            print("\n" + alert["message"] + "\n")
            notifier.send(alert["message"])

        db.mark_sent(alert["dedup_key"], now_iso)
        sent += 1

    _log(f"[PRESSURE] Матчей проверено: {len(matches)} | Алертов: {len(pressure_alerts)} | Отправлено: {sent}")


def run_pressure_cycle_v3(
    thestatsapi_pressure_provider: ThestatsapiPressureProvider | None,
    db: OddsDatabase,
    notifier: TelegramNotifier,
    cooldown_minutes: int,
) -> None:
    """Параллельная v3.0-ветка PRESSURE на данных TheStatsAPI — отдельный
    поток алертов с меткой 'v3.0' в тексте, никак не пересекается с v2.0
    (Sportmonks) по dedup-ключам (pressure_v3: vs pressure:). Задумана как
    сравнение источников на тестовой неделе, не как замена v2.0."""
    if thestatsapi_pressure_provider is None:
        return

    try:
        matches = thestatsapi_pressure_provider.get_live_pressure_data()
    except Exception as exc:
        _log(f"[PRESSURE v3] Ошибка получения данных TheStatsAPI: {exc}")
        return

    if not matches:
        _log("[PRESSURE v3] Live-матчей от TheStatsAPI не найдено в этом цикле.")
        return

    pressure_alerts = detect_pressure_alerts_v3(matches)

    if not pressure_alerts:
        minutes_seen = sorted(m.get("minute", 0) for m in matches)
        _log(
            f"[PRESSURE v3] Матчей от TheStatsAPI: {len(matches)} (минуты: {minutes_seen}) "
            f"— явного дисбаланса не найдено."
        )
        return

    now_iso = datetime.now(timezone.utc).isoformat()
    sent = 0
    for alert in pressure_alerts:
        if db.was_recently_sent(alert["dedup_key"], cooldown_minutes, now_iso):
            continue
        print("\n" + alert["message"] + "\n")
        notifier.send(alert["message"])
        db.mark_sent(alert["dedup_key"], now_iso)
        sent += 1

    _log(f"[PRESSURE v3] Матчей проверено: {len(matches)} | Алертов: {len(pressure_alerts)} | Отправлено: {sent}")


def run_cycle(
    provider,
    db: OddsDatabase,
    analyzer: OddsAnalyzer,
    notifier: TelegramNotifier,
    cooldown_minutes: int,
    min_score_to_notify: float = 0.0,
    hours_ahead_limit: float = 24.0,
    league_whitelist: tuple[str, ...] = (),
    league_blacklist: tuple[str, ...] = (),
    sport_whitelist: tuple[str, ...] = (),
    max_alerts_per_hour: int = 20,
    sent_timestamps: list[datetime] | None = None,
    db_path: str = "",
) -> list[datetime]:
    if sent_timestamps is None:
        sent_timestamps = []

    all_quotes = provider.fetch()
    quotes = _filter_by_horizon(all_quotes, hours_ahead_limit)
    quotes = _filter_by_league(quotes, league_whitelist, league_blacklist)
    quotes = _filter_by_sport(quotes, sport_whitelist)
    all_alerts = analyzer.analyze(quotes)
    db.insert_quotes(quotes)

    scored_alerts = [a for a in all_alerts if a.bbk_score >= min_score_to_notify]
    alerts = _dedup_alerts(db, scored_alerts, cooldown_minutes)
    skipped_by_score = len(all_alerts) - len(scored_alerts)
    skipped_by_dedup = len(scored_alerts) - len(alerts)

    now_dt = datetime.now(timezone.utc)
    sent_timestamps = _prune_old_timestamps(sent_timestamps, now_dt)
    remaining_capacity = max(0, max_alerts_per_hour - len(sent_timestamps))
    alerts_sorted = sorted(alerts, key=lambda a: a.bbk_score, reverse=True)
    alerts_to_send = alerts_sorted[:remaining_capacity]
    skipped_by_hourly_cap = len(alerts_sorted) - len(alerts_to_send)

    top_debug = sorted(all_alerts, key=lambda a: a.bbk_score, reverse=True)[:5]
    for a in top_debug:
        q = a.quote
        status = "LIVE" if q.is_live else "prematch"
        _log(
            f"[DEBUG top] score={a.bbk_score:.0f} [{status}] {q.home_team} — {q.away_team} "
            f"| {q.bookmaker_title} | {q.market_key} {q.outcome_name} | "
            f"цена={q.price:.3f} | движение={a.movement_pct} | "
            f"спорт={q.sport_key!r} | лига={q.league_name!r} | тип={a.signal_type}"
        )

    _log(
        f"Получено: {len(all_quotes)} | В горизонте {hours_ahead_limit:.0f}ч и после фильтров: {len(quotes)} "
        f"| Алертов найдено: {len(all_alerts)} "
        f"| Ниже порога score: {skipped_by_score} | Повтор (дедуп): {skipped_by_dedup} "
        f"| Срезано лимитом {max_alerts_per_hour}/час: {skipped_by_hourly_cap} "
        f"| Отправлено: {len(alerts_to_send)}"
    )

    for alert in alerts_to_send:
        sharp_confirmation = _try_sharp_confirmation(alert, db_path)
        message = format_alert(alert, sharp_confirmation)
        print("\n" + message + "\n")
        notifier.send(message)
        sent_timestamps.append(now_dt)

    _mark_sent(db, alerts_to_send)

    if not notifier.enabled:
        _log("Telegram отключен: заполните TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID.")

    return sent_timestamps


def main() -> int:
    args = build_parser().parse_args()
    settings = Settings.from_env()

    try:
        if args.list_sports:
            sports = list_sports(settings.odds_api_key)
            for sport in sports:
                if sport.get("active"):
                    print(f"{sport.get('key')}: {sport.get('title')}")
            return 0

        if args.provider == "mock":
            provider = MockProvider()
        elif args.provider == "odds-api":
            provider = TheOddsApiProvider(
                api_key=settings.odds_api_key,
                sport_keys=settings.sport_keys,
                regions=settings.odds_region,
                markets=settings.markets,
                odds_format=settings.odds_format,
            )
        else:
            provider = OddscorpProvider(
                auth_key=settings.oddscorp_auth_key,
                bookmakers=settings.oddscorp_bookmakers,
                ws_url=settings.oddscorp_ws_url,
            )

        db = OddsDatabase(settings.database_path)
        analyzer = OddsAnalyzer(
            db=db,
            movement_threshold_pct=settings.movement_threshold_pct,
            market_deviation_threshold_pct=settings.market_deviation_threshold_pct,
            lookback_minutes=settings.lookback_minutes,
            min_bookmakers=settings.min_bookmakers,
            velocity_threshold_pct_per_min=settings.velocity_threshold_pct_per_min,
            sharp_live_max_velocity_pct_per_min=settings.sharp_live_max_velocity_pct_per_min,
            sharp_bookmakers=settings.sharp_bookmakers,
            sharp_bonus_multiplier=settings.sharp_bonus_multiplier,
            momentum_window_minutes=settings.momentum_window_minutes,
            momentum_min_bookmakers=settings.momentum_min_bookmakers,
            momentum_total_shift_pct=settings.momentum_total_shift_pct,
            momentum_max_velocity_pct_per_min=settings.momentum_max_velocity_pct_per_min,
        )
        notifier = TelegramNotifier(
            settings.telegram_bot_token,
            settings.telegram_chat_id,
        )

        sportmonks_provider = None
        sportmonks_key = getattr(settings, "sportmonks_api_key", "") or ""
        if sportmonks_key and not args.no_pressure:
            sportmonks_provider = SportmonksProvider(api_token=sportmonks_key)
            _log("[PRESSURE] Sportmonks провайдер активирован (xG/Pressure Index).")
        elif not sportmonks_key:
            _log("[PRESSURE] SPORTMONKS_API_KEY не задан — сигналы xG/Pressure отключены.")

        thestatsapi_pressure_provider = None
        thestatsapi_key = getattr(settings, "thestatsapi_key", "") or __import__("os").environ.get("THESTATSAPI_KEY", "")
        if thestatsapi_key:
            thestatsapi_pressure_provider = ThestatsapiPressureProvider(api_key=thestatsapi_key)
            _log("[PRESSURE v3] TheStatsAPI провайдер активирован.")
        else:
            _log("[PRESSURE v3] THESTATSAPI_KEY не задан — v3.0 сигналы отключены.")

        sent_timestamps: list[datetime] = []

        if not args.loop:
            sent_timestamps = run_cycle(
                provider, db, analyzer, notifier,
                settings.cooldown_minutes, settings.min_score_to_notify,
                settings.hours_ahead_limit,
                settings.league_whitelist, settings.league_blacklist,
                settings.sport_whitelist,
                settings.max_alerts_per_hour,
                sent_timestamps,
                settings.database_path,
            )
            run_pressure_cycle(sportmonks_provider, db, notifier, settings.cooldown_minutes, settings.league_blacklist)
            run_pressure_cycle_v3(thestatsapi_pressure_provider, db, notifier, settings.cooldown_minutes)
            return 0

        interval_minutes = args.interval_minutes or settings.poll_interval_minutes
        _log(f"Запуск в режиме --loop, интервал {interval_minutes} мин. Ctrl+C для остановки.")
        while True:
            try:
                sent_timestamps = run_cycle(
                    provider, db, analyzer, notifier,
                    settings.cooldown_minutes, settings.min_score_to_notify,
                    settings.hours_ahead_limit,
                    settings.league_whitelist, settings.league_blacklist,
                    settings.sport_whitelist,
                    settings.max_alerts_per_hour,
                    sent_timestamps,
                    settings.database_path,
                )
                run_pressure_cycle(sportmonks_provider, db, notifier, settings.cooldown_minutes, settings.league_blacklist)
                run_pressure_cycle_v3(thestatsapi_pressure_provider, db, notifier, settings.cooldown_minutes)
            except (ProviderError, RuntimeError, ValueError) as exc:
                _log(f"Ошибка в цикле сканирования: {exc}")
            except KeyboardInterrupt:
                _log("Остановлено пользователем.")
                return 0
            time.sleep(interval_minutes * 60)

    except (ProviderError, RuntimeError, ValueError) as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
