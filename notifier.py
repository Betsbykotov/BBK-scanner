from __future__ import annotations

import json
import urllib.parse
import urllib.request

from models import Alert


def format_alert(alert: Alert) -> str:
    q = alert.quote
    point = "" if q.point is None else f" {q.point:g}"
    movement = "нет истории" if alert.movement_pct is None else f"{alert.movement_pct:+.2f}%"
    average = "н/д" if alert.market_average is None else f"{alert.market_average:.3f}"
    deviation = "н/д" if alert.market_deviation_pct is None else f"{alert.market_deviation_pct:+.2f}%"
    velocity = (
        "н/д" if alert.velocity_pct_per_min is None else f"{alert.velocity_pct_per_min:.2f}%/мин"
    )
    sharp_mark = " 🎯 sharp-источник" if alert.is_sharp_source else ""
    return (
        f"{alert.bbk_tier} BBK SCORE: {alert.bbk_score:.0f}/100\n\n"
        f"{q.home_team} — {q.away_team}\n"
        f"БК: {q.bookmaker_title}{sharp_mark}\n"
        f"Рынок: {q.market_key} | {q.outcome_name}{point}\n"
        f"Коэффициент: {q.price:.3f}\n"
        f"Движение: {movement}\n"
        f"Скорость: {velocity}\n"
        f"Средний рынок: {average}\n"
        f"Отклонение: {deviation}\n"
        f"Консенсус БК: {alert.consensus_pct:.0f}%\n"
        f"Причина: {alert.reason}\n\n"
        "Статус: проверить вручную"
    )


class TelegramNotifier:
    def __init__(self, bot_token: str, chat_id: str):
        self.bot_token = bot_token
        self.chat_id = chat_id

    @property
    def enabled(self) -> bool:
        return bool(self.bot_token and self.chat_id)

    def send(self, text: str) -> None:
        if not self.enabled:
            return
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        payload = urllib.parse.urlencode(
            {"chat_id": self.chat_id, "text": text}
        ).encode("utf-8")
        request = urllib.request.Request(url, data=payload, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                result = json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            raise RuntimeError(f"Ошибка отправки Telegram: {exc}") from exc
        if not result.get("ok"):
            raise RuntimeError(f"Telegram отклонил сообщение: {result}")
