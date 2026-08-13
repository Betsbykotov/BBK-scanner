from __future__ import annotations

import html
import json
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta

import requests

from models import Alert


def _esc(text: str) -> str:
    return html.escape(str(text), quote=False)


_ODESSA_TZ = timezone(timedelta(hours=3))


def _format_kickoff(commence_time: str) -> str:
    if not commence_time:
        return "время неизвестно"
    try:
        dt = datetime.fromisoformat(commence_time.replace("Z", "+00:00"))
        dt_local = dt.astimezone(_ODESSA_TZ)
        return dt_local.strftime("%d.%m.%Y %H:%M")
    except ValueError:
        return "время неизвестно"


_INTERNATIONAL_MARKERS = (
    "champions league", "europa league", "conference league",
    "world cup", "euro ", "euro202", "copa america", "copa libertadores",
    "copa sudamericana", "nations league", "afc champions", "caf champions",
    "concacaf", "club world cup", "international friendlies", "friendlies",
)

_LEAGUE_COUNTRY_MAP: list[tuple[str, str, str]] = [
    ("premier league", "Англия", "EN"),
    ("championship", "Англия", "EN"),
    ("league one", "Англия", "EN"),
    ("league two", "Англия", "EN"),
    ("fa cup", "Англия", "EN"),
    ("efl cup", "Англия", "EN"),
    ("carabao cup", "Англия", "EN"),
    ("scottish premiership", "Шотландия", "SC"),
    ("scottish", "Шотландия", "SC"),
    ("welsh premier", "Уэльс", "WL"),
    ("la liga", "Испания", "ES"),
    ("laliga", "Испания", "ES"),
    ("segunda division", "Испания", "ES"),
    ("copa del rey", "Испания", "ES"),
    ("bundesliga", "Германия", "DE"),
    ("dfb pokal", "Германия", "DE"),
    ("dfb-pokal", "Германия", "DE"),
    ("serie a", "Италия", "IT"),
    ("serie b", "Италия", "IT"),
    ("coppa italia", "Италия", "IT"),
    ("ligue 1", "Франция", "FR"),
    ("ligue 2", "Франция", "FR"),
    ("coupe de france", "Франция", "FR"),
    ("eredivisie", "Нидерланды", "NL"),
    ("eerste divisie", "Нидерланды", "NL"),
    ("primeira liga", "Португалия", "PT"),
    ("liga portugal", "Португалия", "PT"),
    ("jupiler", "Бельгия", "BE"),
    ("belgian pro league", "Бельгия", "BE"),
    ("super lig", "Турция", "TR"),
    ("ukrainian premier", "Украина", "UA"),
    ("upl", "Украина", "UA"),
    ("ekstraklasa", "Польша", "PL"),
    ("austrian bundesliga", "Австрия", "AT"),
    ("swiss super league", "Швейцария", "CH"),
    ("danish superliga", "Дания", "DK"),
    ("eliteserien", "Норвегия", "NO"),
    ("allsvenskan", "Швеция", "SE"),
    ("super league greece", "Греция", "GR"),
    ("czech first league", "Чехия", "CZ"),
    ("hnl", "Хорватия", "HR"),
    ("serbian superliga", "Сербия", "RS"),
    ("liga i", "Румыния", "RO"),
    ("israeli premier", "Израиль", "IL"),
    ("saudi pro league", "Саудовская Аравия", "SA"),
    ("brasileirao", "Бразилия", "BR"),
    ("liga profesional", "Аргентина", "AR"),
    ("mls", "США", "US"),
    ("liga mx", "Мексика", "MX"),
    ("j1 league", "Япония", "JP"),
    ("k league", "Южная Корея", "KR"),
    ("chinese super league", "Китай", "CN"),
    ("a-league", "Австралия", "AU"),
    ("russian premier", "Россия", "RU"),
]


def _detect_country_flag(league_name: str | None) -> str:
    if not league_name:
        return ""
    name_lower = league_name.lower()
    for marker in _INTERNATIONAL_MARKERS:
        if marker in name_lower:
            return "🏆 Международный"
    for substring, country, flag in _LEAGUE_COUNTRY_MAP:
        if substring in name_lower:
            return f"{country} {flag}"
    return ""


def _format_sharp_confirmation(sharp_confirmation: dict | None) -> str:
    """Строка для топ-сигналов, прошедших проверку по Pinnacle через OddsPapi.
    sharp_confirmation — то, что вернул oddspapi_provider.check_sharp_confirmation(),
    либо None если сигнал не входил в топ-3 дня, либо проверка не удалась
    (бюджет/сеть/нет данных Pinnacle) — в этом случае строка просто не добавляется.
    """
    if sharp_confirmation is None:
        return ""
    if sharp_confirmation.get("confirmed"):
        return "\n✅ <b>Sharp confirmed</b> (Pinnacle согласен)\n"
    return "\n⚠️ <b>Sharp расходится</b> (Pinnacle видит иначе)\n"


def format_alert(alert: Alert, sharp_confirmation: dict | None = None) -> str:
    q = alert.quote
    point = "" if q.point is None else f" {q.point:g}"
    movement = "нет истории" if alert.movement_pct is None else f"{alert.movement_pct:+.2f}%"
    average = "н/д" if alert.market_average is None else f"{alert.market_average:.3f}"
    deviation = "н/д" if alert.market_deviation_pct is None else f"{alert.market_deviation_pct:+.2f}%"
    velocity = (
        "н/д" if alert.velocity_pct_per_min is None else f"{alert.velocity_pct_per_min:.2f}%/мин"
    )
    sharp_mark = " 🎯 <i>sharp-источник</i>" if alert.is_sharp_source else ""
    match_status = "🔴 <b>LIVE</b> ⚽" if q.is_live else "⏳ <b>Прематч</b>"

    country_flag = _detect_country_flag(q.league_name)
    if q.league_name and country_flag:
        league_line = f"🌍 {_esc(q.league_name)} | {country_flag}\n"
    elif q.league_name:
        league_line = f"🌍 {_esc(q.league_name)}\n"
    else:
        league_line = ""

    if q.is_live:
        kickoff_line = ""
    else:
        kickoff_line = f"🕐 Начало: {_format_kickoff(q.commence_time)}\n"

    momentum_siren = "🚨 " if getattr(alert, "signal_type", "") == "MOMENTUM" else ""
    header = f"{momentum_siren}{alert.bbk_tier} <b>BBK SCORE: {alert.bbk_score:.0f}/100</b>"

    market_line = f"{_esc(q.market_key)} | <b>{_esc(q.outcome_name)}</b>{point}"

    return (
        f"{header}\n"
        f"{match_status}\n"
        f"{kickoff_line}"
        f"{league_line}\n"
        f"<b>⚽ {_esc(q.home_team)} — {_esc(q.away_team)}</b>\n"
        f"🏦 {_esc(q.bookmaker_title)}{sharp_mark}\n"
        f"📍 Рынок: {market_line}\n\n"
        f"💰 Коэффициент сейчас: <b>{q.price:.3f}</b>\n"
        f"📈 Движение: <b>{movement}</b>\n"
        f"⚡ Скорость движения: {velocity}\n"
        f"📊 Средний по рынку: {average}\n"
        f"↔️ Отклонение от рынка: <b>{deviation}</b>\n"
        f"🤝 Согласие других БК: {alert.consensus_pct:.0f}%\n"
        f"{_format_sharp_confirmation(sharp_confirmation)}"
        f"\n<i>{_esc(alert.reason)}</i>\n\n"
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

    def send_photo(self, photo_bytes: bytes, caption: str = "") -> None:
        """Отправляет PNG-картинку (карточку матча) с подписью.

        Telegram ограничивает caption ~1024 символами — если сообщение
        format_alert длиннее, Telegram сам обрежет его при отправке, так что
        для длинных алертов есть смысл сначала слать send_photo с короткой
        подписью, а следом send() с полным текстом. Решаем на месте вызова.
        """
        if not self.enabled:
            return
        url = f"https://api.telegram.org/bot{self.bot_token}/sendPhoto"
        files = {"photo": ("card.png", photo_bytes, "image/png")}
        data = {"chat_id": self.chat_id, "caption": caption, "parse_mode": "HTML"}
        try:
            response = requests.post(url, data=data, files=files, timeout=30)
            result = response.json()
        except Exception as exc:
            raise RuntimeError(f"Ошибка отправки фото в Telegram: {exc}") from exc
        if not result.get("ok"):
            raise RuntimeError(f"Telegram отклонил фото: {result}")
