"""Telegram alerts for critical Cirrus events."""

import logging

import requests
from django.conf import settings

logger = logging.getLogger("core.alerts")

EMOJIS = {
    "info": "ℹ️",
    "warning": "⚠️",
    "critical": "🚨",
    "success": "✅",
    "error": "🔴",
}


def send_telegram(message: str, level: str = "info") -> bool:
    """Send alert to Telegram.

    Args:
        message: Alert text (Markdown supported)
        level: info, warning, critical, success, error

    Returns:
        True if sent, False otherwise
    """
    token = getattr(settings, "TELEGRAM_BOT_TOKEN", "")
    chat_id = getattr(settings, "TELEGRAM_CHAT_ID", "")
    enabled = getattr(settings, "TELEGRAM_ALERTS_ENABLED", False)

    if not enabled or not token or not chat_id:
        return False

    emoji = EMOJIS.get(level, "📌")
    text = f"{emoji} *CIRRUS*\n{message}"

    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "Markdown",
                "disable_web_page_preview": True,
            },
            timeout=10,
        )
        if resp.status_code != 200:
            logger.warning("Telegram API returned %s: %s", resp.status_code, resp.text[:200])
            return False
        return True
    except Exception as e:
        logger.error("Telegram alert failed: %s", e)
        return False
