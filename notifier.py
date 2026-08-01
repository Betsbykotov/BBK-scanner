from __future__ import annotations

import html
import json
import urllib.parse
import urllib.request

from models import Alert


def _esc(text: str) -> str:
    return html.escape(str(text), quote=False)


def format_alert(alert: Alert) -> str:
    q = alert.quote
    point = "" if q.point is None else f" {q.point:g}"
    movement = "нет истории" if alert.movement_pct is None else f"{alert.movement_pct:+.2f}%"
    average = "н/д" if alert.market_average is None else f"{alert.market_average:.3f}"
    deviation = "н/д" if alert.market_deviation_pct is None else f"{alert.market_deviation_pct:+.2f}%"
    velocity = (
        "н/д" if alert.velocity_pct_per_min is None else f"{alert.velocity_pct_per_min:.2f}%/мин"
    )
    sharp_mark = " 🎯 <i>sharp-источник</i>" if alert.is_sharp_source else ""
    match_status = "🔴 <b>LIVE</b>" if q.is_live else "⏳ <b>Прематч</b>"

    # Заголовок: тир + score одной строкой, крупно и понятно с первого взгляда.
    header = f"{alert.bbk_tier} <b>BBK SCORE: {alert.bbk_score:.0f}/100</b>"

    # Короткая расшифровка рынка человеческим языком, без жаргона —
    # чтобы было понятно, о какой именно линии речь, даже не разбираясь в терминах.
    market_line = f"{_esc(q.market_key)} | <b>{_esc(q.outcome_name)}</b>{point}"

    return (
        f"{header}\n"
        f"{match_status}\n\n"
        f"<b>⚽ {_esc(q.home_team)} — {_esc(q.away_team)}</b>\n"
        f"🏦 {_esc(q.bookmaker_title)}{sharp_mark}\n"
        f"📍 Рынок: {market_line}\n\n"
        f"💰 Коэффициент сейчас: <b>{q.price:.3f}</b>\n"
        f"📈 Движение: <b>{movement}</b>\n"
        f"⚡ Скорость движения: {velocity}\n"
        f"📊 Средний по рынку: {average}\n"
        f"↔️ Отклонение от рынка: <b>{deviation}</b>\n"
        f"🤝 Согласие других БК: {alert.consensus_pct:.0f}%\n\n"
        f"<i>{_esc(alert.reason)}</i>\n\n"
        "👉 Проверить вручную перед решением"
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
            {"chat_id": self.chat_id, "text": text, "parse_mode": "HTML"}
        ).encode("utf-8")
        request = urllib.request.Request(url, data=payload, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                result = json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            raise RuntimeError(f"Ошибка отправки Telegram: {exc}") from exc
        if not result.get("ok"):
            raise RuntimeError(f"Telegram отклонил сообщение: {result}")
