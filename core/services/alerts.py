"""Telegram alerts for critical Cirrus events.

Lee la configuración primero desde SystemSettings (DB, editable desde
el panel admin). Si SystemSettings no tiene token configurado, cae al
fallback de variables de entorno (backward compat).

Cada envío se registra en TelegramAlert para auditoría.
"""

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

# Categorías "de stack" que SÍ llegan al admin cuando telegram_solo_stack=True.
# Todo lo demás (eventos individuales de descarga, FIEL de clientes, probes
# individuales, reportes informativos, stripe, leads, etc.) se silencia.
STACK_CATEGORIES = {
    "sat_health",    # SAT >24h con tasa de éxito <60%
    "job_failures",  # patrón: 3+ DescargaJobs fallidos en 24h
    "service_down",  # worker/beat/web caído
    "incidentes",    # 3+ incidentes de descarga en 24h
    "stack",         # genérico de stack
    "test",          # botón de prueba del panel /panel/telegram/
}


def _get_telegram_config():
    """Return (enabled, token, chat_id, level_flags) from DB or env fallback.

    level_flags is a dict: {'info': bool, 'warning': bool, 'error': bool, 'critical': bool}
    """
    try:
        from core.models import SystemSettings
        from core.services.fiel_encryption import decrypt_password

        s = SystemSettings.load()

        if s.telegram_enabled and s.telegram_bot_token_encrypted:
            token = decrypt_password(bytes(s.telegram_bot_token_encrypted))
            chat_id = s.telegram_admin_chat_id
            level_flags = {
                "info": s.telegram_send_info,
                "warning": s.telegram_send_warning,
                "error": s.telegram_send_error,
                "critical": s.telegram_send_critical,
                "success": s.telegram_send_info,  # success usa mismo flag que info
            }
            solo_stack = s.telegram_solo_stack
            return True, token, chat_id, level_flags, solo_stack
    except Exception as e:
        logger.warning("No se pudo leer SystemSettings para Telegram: %s", e)

    # Fallback a env vars (backward compatibility)
    enabled = getattr(settings, "TELEGRAM_ALERTS_ENABLED", False)
    token = getattr(settings, "TELEGRAM_BOT_TOKEN", "")
    chat_id = getattr(settings, "TELEGRAM_CHAT_ID", "")
    level_flags = {
        "info": True, "warning": True, "error": True,
        "critical": True, "success": True,
    }
    solo_stack = getattr(settings, "TELEGRAM_SOLO_STACK", True)
    return enabled, token, chat_id, level_flags, solo_stack


def _record_alert(level: str, message: str, chat_id: str, status: str,
                  http_status: int = None, error: str = "", category: str = ""):
    """Persist send attempt to TelegramAlert table."""
    try:
        from core.models import TelegramAlert
        TelegramAlert.objects.create(
            level=level,
            category=category,
            message=message[:500],
            chat_id=chat_id or "",
            status=status,
            http_status=http_status,
            error=(error or "")[:500],
        )
    except Exception as e:
        logger.debug("Could not record TelegramAlert: %s", e)


def send_telegram(message: str, level: str = "info", category: str = "") -> bool:
    """Send alert to Telegram.

    Args:
        message: Alert text (Markdown supported)
        level: info, warning, critical, success, error
        category: optional category tag (email, download, fiel, etc.)

    Returns:
        True if sent, False otherwise
    """
    enabled, token, chat_id, level_flags, solo_stack = _get_telegram_config()

    if not enabled or not token or not chat_id:
        _record_alert(level, message, chat_id, "skipped",
                      error="alerting disabled or missing config", category=category)
        return False

    # Política "solo stack": silenciar todo lo que no sea alerta de stack.
    # Se sigue registrando en TelegramAlert (status=skipped) para auditoría.
    if solo_stack and category not in STACK_CATEGORIES:
        _record_alert(level, message, chat_id, "skipped",
                      error=f"category '{category or '(vacía)'}' no es de stack "
                            f"(telegram_solo_stack=True)",
                      category=category)
        return False

    # Respetar nivel configurado
    if not level_flags.get(level, True):
        _record_alert(level, message, chat_id, "skipped",
                      error=f"level '{level}' muted in config", category=category)
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
            _record_alert(level, message, chat_id, "failed",
                          http_status=resp.status_code,
                          error=resp.text[:200], category=category)
            return False
        _record_alert(level, message, chat_id, "sent",
                      http_status=200, category=category)
        return True
    except Exception as e:
        logger.error("Telegram alert failed: %s", e)
        _record_alert(level, message, chat_id, "failed",
                      error=str(e)[:200], category=category)
        return False
