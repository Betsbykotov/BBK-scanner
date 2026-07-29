from __future__ import annotations

import argparse
from datetime import datetime, timezone
import sys
import time

from .analyzer import OddsAnalyzer
from .config import Settings
from .database import OddsDatabase
from .models import Alert
from .notifier import TelegramNotifier, format_alert
from .providers import MockProvider, ProviderError, TheOddsApiProvider, list_sports


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="BBK Scanner MVP")
    parser.add_argument(
        "--provider",
        choices=("mock", "odds-api"),
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
    """Отсекает алерты, которые уже отправлялись по этой линии внутри cooldown-окна."""
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
) -> None:
    quotes = provider.fetch()
    all_alerts = analyzer.analyze(quotes)
    db.insert_quotes(quotes)

    # Сначала режем по BBK Score (режим "только топ" и т.п.), потом по дедупу —
    # так дедуп не тратит запись в базу на алерты, которые всё равно не пошлём.
    scored_alerts = [a for a in all_alerts if a.bbk_score >= min_score_to_notify]
    alerts = _dedup_alerts(db, scored_alerts, cooldown_minutes)
    skipped_by_score = len(all_alerts) - len(scored_alerts)
    skipped_by_dedup = len(scored_alerts) - len(alerts)

    _log(
        f"Коэффициентов: {len(quotes)} | Алертов найдено: {len(all_alerts)} "
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

        provider = (
            MockProvider()
            if args.provider == "mock"
            else TheOddsApiProvider(
                api_key=settings.odds_api_key,
                sport_key=settings.sport_key,
                regions=settings.odds_region,
                markets=settings.markets,
                odds_format=settings.odds_format,
            )
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
            )
            return 0

        interval_minutes = args.interval_minutes or settings.poll_interval_minutes
        _log(f"Запуск в режиме --loop, интервал {interval_minutes} мин. Ctrl+C для остановки.")
        while True:
            try:
                run_cycle(
                    provider, db, analyzer, notifier,
                    settings.cooldown_minutes, settings.min_score_to_notify,
                )
            except (ProviderError, RuntimeError, ValueError) as exc:
                # Одна неудачная итерация (сбой API, таймаут Telegram и т.п.)
                # не должна убивать процесс на Railway — просто логируем и ждём
                # следующий цикл.
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
