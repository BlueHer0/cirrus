"""Cirrus System Monitor — centralized logging helpers.

Usage:
    from core.services.monitor import log_info, log_warning, log_error

    log_info("email", "Email sent to user@test.com")
    log_warning("fiel", "SAT no disponible, reintentando", user_email="admin@test.com")
    log_error("download", "Descarga falló", detail=traceback_str, ip="1.2.3.4")
"""

import logging

logger = logging.getLogger("core.monitor")


def _log(level: str, category: str, message: str, **kwargs):
    """Create a SystemLog entry. Sends Telegram for error/critical."""
    from core.models import SystemLog

    entry = SystemLog.objects.create(
        level=level,
        category=category,
        message=message[:500],
        detail=kwargs.get("detail", ""),
        user_email=kwargs.get("user_email", ""),
        ip=kwargs.get("ip", None),
    )

    # Also emit to Python logger
    py_level = getattr(logging, level.upper(), logging.INFO)
    logger.log(py_level, "[%s] %s: %s", level.upper(), category, message)

    # Send Telegram for error and critical
    if level in ("error", "critical"):
        try:
            from core.services.alerts import send_telegram
            tg_level = "critical" if level == "critical" else "error"
            detail = kwargs.get("detail", "")
            tg_msg = f"*{category}*: {message}"
            if detail:
                tg_msg += f"\n```\n{detail[:300]}\n```"
            send_telegram(tg_msg, tg_level)
        except Exception:
            pass  # Never let Telegram failure break logging

    return entry


def log_info(category: str, message: str, **kwargs):
    return _log("info", category, message, **kwargs)


def log_warning(category: str, message: str, **kwargs):
    return _log("warning", category, message, **kwargs)


def log_error(category: str, message: str, **kwargs):
    return _log("error", category, message, **kwargs)


def log_critical(category: str, message: str, **kwargs):
    return _log("critical", category, message, **kwargs)
