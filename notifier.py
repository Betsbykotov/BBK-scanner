import requests

import config


def send_telegram_alert(text: str) -> bool:
    if not config.BOT_TOKEN or not config.CHAT_ID:
        print("[notifier] BOT_TOKEN/CHAT_ID missing, skipping send")
        return False
    url = f"https://api.telegram.org/bot{config.BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": config.CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        resp = requests.post(url, json=payload, timeout=10)
        resp.raise_for_status()
        return True
    except Exception as exc:
        print(f"[notifier] failed to send alert: {exc}")
        return False
