"""Celery tasks para mantenimiento del sistema de API keys.

Mantenido separado de core/tasks.py para no tocarlo.
Registrado en cirrus/celery.py vía app.conf.include.

Tasks:
- reset_apikey_requests_diarios: resetea requests_hoy cada medianoche
- desactivar_apikeys_plan_cancelado: apaga keys de clientes con suscripción cancelada
"""

import logging

from celery import shared_task

logger = logging.getLogger("core.tasks_api_keys")


@shared_task(soft_time_limit=120, time_limit=180)
def reset_apikey_requests_diarios():
    """Reset del contador requests_hoy. Corre cada día a medianoche."""
    from core.services.api_keys_service import reset_requests_diarios

    count = reset_requests_diarios()
    logger.info("Reset diario completado: %d keys", count)
    return {"keys_resetadas": count}


@shared_task(soft_time_limit=120, time_limit=180)
def desactivar_apikeys_plan_cancelado():
    """Desactiva keys de usuarios con subscription_status='canceled'.

    Corre cada hora. Sirve de red de seguridad por si el webhook de Stripe
    cancelación no llegó o falló.
    """
    from core.services.api_keys_service import desactivar_keys_por_plan_cancelado

    count = desactivar_keys_por_plan_cancelado()
    if count > 0:
        from core.services.alerts import send_telegram
        send_telegram(
            f"🔒 {count} API key(s) desactivada(s) por plan cancelado",
            level="warning",
            category="api_keys",
        )
    return {"keys_desactivadas": count}
