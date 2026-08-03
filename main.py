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
    """Фильтр по названию лиги (регистронезависимый поиск подстроки).

    - blacklist: если название лиги СОДЕРЖИТ любую из подстрок — событие выкидывается
      (например "esoccer", "replays" — мусорные/не настоящие матчи).
    - whitelist: если список не пуст — оставляем ТОЛЬКО события, где лига содержит
      хотя бы одну из подстрок (например "premier league", "brasileirao").
      Если whitelist пуст — этот фильтр не применяется вообще.

    Если у котировки нет league_name (пусто) — она проходит blacklist, но НЕ проходит
    непустой whitelist (нет данных = не можем подтвердить, что лига разрешена).
    """
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
) -> None:
    all_quotes = provider.fetch()
    quotes = _filter_by_horizon(all_quotes, hours_ahead_limit)
    quotes = _filter_by_league(quotes, league_whitelist, league_blacklist)
    all_alerts = analyzer.analyze(quotes)
    db.insert_quotes(quotes)

    scored_alerts = [a for a in all_alerts if a.bbk_score >= min_score_to_notify]
    alerts = _dedup_alerts(db, scored_alerts, cooldown_minutes)
    skipped_by_score = len(all_alerts) - len(scored_alerts)
    skipped_by_dedup = len(scored_alerts) - len(alerts)

    # ВРЕМЕННЫЙ ДЕБАГ: топ-5 алертов по score КАЖДЫЙ цикл, независимо от порога,
    # чтобы можно было сверить с реальными матчами в БК. Удалить после проверки.
    top_debug = sorted(all_alerts, key=lambda a: a.bbk_score, reverse=True)[:5]
    for a in top_debug:
        q = a.quote
        status = "LIVE" if q.is_live else "prematch"
        _log(
            f"[DEBUG top] score={a.bbk_score:.0f} [{status}] {q.home_team} — {q.away_team} "
            f"| {q.bookmaker_title} | {q.market_key} {q.outcome_name} | "
            f"цена={q.price:.3f} | движение={a.movement_pct} | лига={q.league_name}"
        )

    _log(
        f"Получено: {len(all_quotes)} | В горизонте {hours_ahead_limit:.0f}ч: {len(quotes)} "
        f"| Алертов найдено: {len(all_alerts)} "
        f"| Ниже порога score: {skipped_by_score} | Повтор (дедуп): {skipped_by_dedup} "
        f"| Отправлено: {len(alerts)}"
    )

    for alert in alerts:
        message = format_alert(alert)
        print("\n" + message + "\n")
        notifier.send(message)

    _mark_sent(db, alerts)

    if not notifier.enabled:
        _log("Telegram отключен: заполните TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID.")


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
            sharp_bookmakers=settings.sharp_bookmakers,
            sharp_bonus_multiplier=settings.sharp_bonus_multiplier,
        )
        notifier = TelegramNotifier(
            settings.telegram_bot_token,
            settings.telegram_chat_id,
        )

        if not args.loop:
            run_cycle(
                provider, db, analyzer, notifier,
                settings.cooldown_minutes, settings.min_score_to_notify,
                settings.hours_ahead_limit,
                settings.league_whitelist, settings.league_blacklist,
            )
            return 0

        interval_minutes = args.interval_minutes or settings.poll_interval_minutes
        _log(f"Запуск в режиме --loop, интервал {interval_minutes} мин. Ctrl+C для остановки.")
        while True:
            try:
                run_cycle(
                    provider, db, analyzer, notifier,
                    settings.cooldown_minutes, settings.min_score_to_notify,
                    settings.hours_ahead_limit,
                    settings.league_whitelist, settings.league_blacklist,
                )
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
