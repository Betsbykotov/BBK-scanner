from __future__ import annotations

import html
import json
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta

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


# ---------------------------------------------------------------------------
# Определение страны и флага по названию лиги.
#
# У OddsCorp/Sportmonks нет отдельного поля country_code в тех данных, что
# приходят в Quote — есть только league_name (строка вида "Premier League",
# "La Liga", "2. Bundesliga"). Поэтому страна определяется эвристически:
# ищем в названии лиги подстроку из словаря ниже (регистронезависимо,
# сначала более длинные/специфичные ключи, чтобы "Premier League" не
# перепутать с "Premier League 2" и т.п. — порядок проверки от специфичного
# к общему).
#
# Международные турниры (Лига Чемпионов, ЧМ, Евро и т.п.) не привязаны к
# одной стране — для них отдельная пометка 🏆 без странового флага.
# Если лига не найдена в словаре — строка просто не показывается (не
# показываем "неизвестный" флаг, чтобы не вводить в заблуждение).
# ---------------------------------------------------------------------------

_INTERNATIONAL_MARKERS = (
    "champions league", "europa league", "conference league",
    "world cup", "euro ", "euro202", "copa america", "copa libertadores",
    "copa sudamericana", "nations league", "afc champions", "caf champions",
    "concacaf", "club world cup", "international friendlies", "friendlies",
)

# (подстрока_в_названии_лиги, страна, флаг) — порядок важен: сверху вниз,
# первое совпадение побеждает, поэтому специфичные лиги идут раньше общих.
_LEAGUE_COUNTRY_MAP: list[tuple[str, str, str]] = [
    # Англия
    ("premier league", "Англия", "🏴󠁧󠁢󠁥󠁮󠁧󠁿"),
    ("championship", "Англия", "🏴󠁧󠁢󠁥󠁮󠁧󠁿"),
    ("league one", "Англия", "🏴󠁧󠁢󠁥󠁮󠁧󠁿"),
    ("league two", "Англия", "🏴󠁧󠁢󠁥󠁮󠁧󠁿"),
    ("fa cup", "Англия", "🏴󠁧󠁢󠁥󠁮󠁧󠁿"),
    ("efl cup", "Англия", "🏴󠁧󠁢󠁥󠁮󠁧󠁿"),
    ("carabao cup", "Англия", "🏴󠁧󠁢󠁥󠁮󠁧󠁿"),
    # Шотландия
    ("scottish premiership", "Шотландия", "🏴󠁧󠁢󠁳󠁣󠁴󠁿"),
    ("scottish", "Шотландия", "🏴󠁧󠁢󠁳󠁣󠁴󠁿"),
    # Уэльс
    ("welsh premier", "Уэльс", "🏴󠁧󠁢󠁷󠁬󠁳󠁿"),
    # Испания
    ("la liga", "Испания", "🇪🇸"),
    ("laliga", "Испания", "🇪🇸"),
    ("segunda division", "Испания", "🇪🇸"),
    ("copa del rey", "Испания", "🇪🇸"),
    # Германия
    ("bundesliga", "Германия", "🇩🇪"),
    ("dfb pokal", "Германия", "🇩🇪"),
    ("dfb-pokal", "Германия", "🇩🇪"),
    # Италия
    ("serie a", "Италия", "🇮🇹"),
    ("serie b", "Италия", "🇮🇹"),
    ("coppa italia", "Италия", "🇮🇹"),
    # Франция
    ("ligue 1", "Франция", "🇫🇷"),
    ("ligue 2", "Франция", "🇫🇷"),
    ("coupe de france", "Франция", "🇫🇷"),
    # Нидерланды
    ("eredivisie", "Нидерланды", "🇳🇱"),
    ("eerste divisie", "Нидерланды", "🇳🇱"),
    # Португалия
    ("primeira liga", "Португалия", "🇵🇹"),
    ("liga portugal", "Португалия", "🇵🇹"),
    # Бельгия
    ("jupiler", "Бельгия", "🇧🇪"),
    ("belgian pro league", "Бельгия", "🇧🇪"),
    # Турция
    ("super lig", "Турция", "🇹🇷"),
    ("süper lig", "Турция", "🇹🇷"),
    # Украина
    ("ukrainian premier", "Украина", "🇺🇦"),
    ("upl", "Украина", "🇺🇦"),
    ("уперша", "Украина", "🇺🇦"),
    # Польша
    ("ekstraklasa", "Польша", "🇵🇱"),
    # Австрия
    ("austrian bundesliga", "Австрия", "🇦🇹"),
    # Швейцария
    ("swiss super league", "Швейцария", "🇨🇭"),
    # Дания
    ("danish superliga", "Дания", "🇩🇰"),
    ("superliga", "Дания", "🇩🇰"),
    # Норвегия
    ("eliteserien", "Норвегия", "🇳🇴"),
    # Швеция
    ("allsvenskan", "Швеция", "🇸🇪"),
    # Греция
    ("super league greece", "Греция", "🇬🇷"),
    ("greek super league", "Греция", "🇬🇷"),
    # Чехия
    ("czech first league", "Чехия", "🇨🇿"),
    ("fortuna liga", "Чехия", "🇨🇿"),
    # Хорватия
    ("hnl", "Хорватия", "🇭🇷"),
    ("croatian first", "Хорватия", "🇭🇷"),
    # Сербия
    ("serbian superliga", "Сербия", "🇷🇸"),
    # Румыния
    ("liga i", "Румыния", "🇷🇴"),
    # Израиль
    ("israeli premier", "Израиль", "🇮🇱"),
    # Саудовская Аравия
    ("saudi pro league", "Саудовская Аравия", "🇸🇦"),
    ("saudi professional", "Саудовская Аравия", "🇸🇦"),
    # ОАЭ / Катар
    ("uae pro league", "ОАЭ", "🇦🇪"),
    ("qatar stars league", "Катар", "🇶🇦"),
    # Бразилия
    ("brasileirao", "Бразилия", "🇧🇷"),
    ("serie a brazil", "Бразилия", "🇧🇷"),
    ("campeonato brasileiro", "Бразилия", "🇧🇷"),
    ("copa do brasil", "Бразилия", "🇧🇷"),
    # Аргентина
    ("liga profesional", "Аргентина", "🇦🇷"),
    ("primera division argentina", "Аргентина", "🇦🇷"),
    ("argentina primera", "Аргентина", "🇦🇷"),
    # Прочая Южная Америка
    ("primera division uruguay", "Уругвай", "🇺🇾"),
    ("primera division chile", "Чили", "🇨🇱"),
    ("categoria primera a", "Колумбия", "🇨🇴"),
    ("liga pro ecuador", "Эквадор", "🇪🇨"),
    ("liga 1 peru", "Перу", "🇵🇪"),
    ("paraguay", "Парагвай", "🇵🇾"),
    ("venezuela", "Венесуэла", "🇻🇪"),
    ("bolivia", "Боливия", "🇧🇴"),
    # США / Мексика / Канада
    ("mls", "США", "🇺🇸"),
    ("major league soccer", "США", "🇺🇸"),
    ("usl championship", "США", "🇺🇸"),
    ("liga mx", "Мексика", "🇲🇽"),
    ("canadian premier", "Канада", "🇨🇦"),
    # Азия
    ("j1 league", "Япония", "🇯🇵"),
    ("j2 league", "Япония", "🇯🇵"),
    ("k league", "Южная Корея", "🇰🇷"),
    ("chinese super league", "Китай", "🇨🇳"),
    ("csl", "Китай", "🇨🇳"),
    ("thai league", "Таиланд", "🇹🇭"),
    ("v.league", "Вьетнам", "🇻🇳"),
    ("indonesian liga", "Индонезия", "🇮🇩"),
    ("indian super league", "Индия", "🇮🇳"),
    # Африка
    ("egyptian premier", "Египет", "🇪🇬"),
    ("south african premier", "ЮАР", "🇿🇦"),
    ("botola", "Марокко", "🇲🇦"),
    ("tunisian ligue", "Тунис", "🇹🇳"),
    ("nigerian professional", "Нигерия", "🇳🇬"),
    # Океания
    ("a-league", "Австралия", "🇦🇺"),
    ("new zealand", "Новая Зеландия", "🇳🇿"),
    # СНГ
    ("russian premier", "Россия", "🇷🇺"),
    ("kazakhstan premier", "Казахстан", "🇰🇿"),
    ("belarusian premier", "Беларусь", "🇧🇾"),
    ("uzbekistan super", "Узбекистан", "🇺🇿"),
    ("georgian erovnuli", "Грузия", "🇬🇪"),
    ("armenian premier", "Армения", "🇦🇲"),
    ("azerbaijan premier", "Азербайджан", "🇦🇿"),
    # Прочая Европа
    ("finnish veikkausliiga", "Финляндия", "🇫🇮"),
    ("hungarian nb", "Венгрия", "🇭🇺"),
    ("slovak super liga", "Словакия", "🇸🇰"),
    ("slovenian prvaliga", "Словения", "🇸🇮"),
    ("bulgarian first", "Болгария", "🇧🇬"),
    ("icelandic urvalsdeild", "Исландия", "🇮🇸"),
    ("irish premier", "Ирландия", "🇮🇪"),
    ("league of ireland", "Ирландия", "🇮🇪"),
    ("cypriot first", "Кипр", "🇨🇾"),
    ("moldovan", "Молдова", "🇲🇩"),
    ("estonian meistriliiga", "Эстония", "🇪🇪"),
    ("latvian virsliga", "Латвия", "🇱🇻"),
    ("lithuanian a lyga", "Литва", "🇱🇹"),
    ("bosnian premier", "Босния", "🇧🇦"),
    ("albanian superliga", "Албания", "🇦🇱"),
    ("montenegrin", "Черногория", "🇲🇪"),
    ("macedonian first", "Северная Македония", "🇲🇰"),
    ("kosovo", "Косово", "🇽🇰"),
    ("faroe islands", "Фарерские острова", "🇫🇴"),
    ("maltese premier", "Мальта", "🇲🇹"),
    ("luxembourg", "Люксембург", "🇱🇺"),
    ("andorra", "Андорра", "🇦🇩"),
    ("gibraltar", "Гибралтар", "🇬🇮"),
    ("san marino", "Сан-Марино", "🇸🇲"),
]


def _detect_country_flag(league_name: str | None) -> str:
    """Возвращает строку вида 'Англия 🏴󠁧󠁢󠁥󠁮󠁧󠁿' или '' если не удалось определить.

    Для международных турниров (ЛЧ, ЛЕ, ЧМ и т.п.) возвращает '🏆 Международный'.
    """
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
    match_status = "🔴 <b>LIVE</b> ⚽" if q.is_live else "⏳ <b>Прематч</b>"

    country_flag = _detect_country_flag(q.league_name)
    if q.league_name and country_flag:
        league_line = f"🌍 {_esc(q.league_name)} | {country_flag}\n"
    elif q.league_name:
        league_line = f"🌍 {_esc(q.league_name)}\n"
    else:
        league_line = ""

    # ИСПРАВЛЕНО: для LIVE-матчей OddsCorp часто не присылает start_at в meta
    # (матч уже начался, "время начала в будущем" не имеет смысла) — раньше
    # это превращалось в бессмысленную строку "🕐 Начало: время неизвестно".
    # Теперь для LIVE строка времени не показывается вообще — статус "🔴 LIVE"
    # уже говорит, что матч идёт сейчас. Для prematch — показываем время как раньше.
    if q.is_live:
        kickoff_line = ""
    else:
        kickoff_line = f"🕐 Начало: {_format_kickoff(q.commence_time)}\n"

    # Заголовок: тир + score одной строкой, крупно и понятно с первого взгляда.
    header = f"{alert.bbk_tier} <b>BBK SCORE: {alert.bbk_score:.0f}/100</b>"

    # Короткая расшифровка рынка человеческим языком, без жаргона —
    # чтобы было понятно, о какой именно линии речь, даже не разбираясь в терминах.
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
