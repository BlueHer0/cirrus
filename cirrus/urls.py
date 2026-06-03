"""Cirrus URL Configuration."""

import logging

from django.contrib import admin
from django.http import HttpResponse, JsonResponse
from django.urls import path, include
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from core.api.router import api
from core.views import landing_view, verificar_rfc_view

logger = logging.getLogger("core.stripe_webhook")


@csrf_exempt
@require_POST
def stripe_webhook(request):
    """Stripe webhook endpoint con log persistente + idempotencia.

    Flujo:
    1. Validar firma HMAC con STRIPE_WEBHOOK_SECRET (400 si falla)
    2. Persistir StripeWebhookEvent (idempotencia por stripe_event_id)
    3. Si ya fue procesado: retornar 200 (idempotente)
    4. Procesar con handle_webhook_event(event, wh_record)
       - Si éxito → 200
       - Si tipo ignorado → 200
       - Si error esperado (data mala, user/plan no existe) → 200 + estado='error'
         (Stripe NO debe reintentar eventos con datos malos)
       - Si error inesperado real → 500 (Stripe reintenta)
    """
    import stripe
    from django.conf import settings
    from django.utils import timezone
    from core.models import StripeWebhookEvent
    from core.services.stripe_service import handle_webhook_event

    payload = request.body
    sig_header = request.META.get("HTTP_STRIPE_SIGNATURE", "")

    # 1. Validar firma
    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, settings.STRIPE_WEBHOOK_SECRET,
        )
    except ValueError:
        logger.warning("Stripe webhook: payload inválido")
        return HttpResponse(status=400)
    except stripe.error.SignatureVerificationError:
        logger.warning("Stripe webhook: firma inválida (sig=%s)", sig_header[:40])
        return HttpResponse(status=400)

    event_id = event.get("id", "")
    event_type = event.get("type", "")
    data_obj = event.get("data", {}).get("object", {})
    customer_id = data_obj.get("customer", "") if isinstance(data_obj, dict) else ""

    # 2. Persistir con idempotencia
    wh, created = StripeWebhookEvent.objects.get_or_create(
        stripe_event_id=event_id,
        defaults={
            "event_type": event_type,
            "customer_id": customer_id or "",
            "payload": event.get("data", {}).get("object", {}),
            "estado": "recibido",
        },
    )

    # 3. Idempotencia: ya procesado previamente → no reprocesar
    if not created and wh.estado == "procesado":
        logger.info(
            "Stripe webhook: %s (%s) ya procesado, devolviendo 200",
            event_id, event_type,
        )
        return HttpResponse(status=200)

    wh.intentos += 1
    wh.save(update_fields=["intentos"])

    # 4. Procesar
    try:
        resultado = handle_webhook_event(event)
    except Exception as e:
        # Error INESPERADO (red caída, BD caída, bug de código) → 500, Stripe reintenta
        logger.exception("Stripe webhook: error inesperado procesando %s", event_id)
        wh.estado = "error"
        wh.error_detalle = f"{type(e).__name__}: {str(e)[:1500]}"
        wh.save(update_fields=["estado", "error_detalle"])
        return HttpResponse(status=500)

    # Resultado normal: handle_webhook_event devuelve dict con status
    estado_final = (resultado or {}).get("status", "procesado")
    detalle = (resultado or {}).get("error", "")

    if estado_final == "ignorado":
        wh.estado = "ignorado"
    elif estado_final == "procesado":
        wh.estado = "procesado"
        wh.procesado_en = timezone.now()
    else:
        # status='error' de handle_webhook_event = error DE DATOS
        # (User no existe, Plan no existe). 200 para que Stripe NO reintente.
        wh.estado = "error"
        wh.error_detalle = detalle[:1500]

    wh.save(update_fields=["estado", "error_detalle", "procesado_en"])
    return HttpResponse(status=200)


urlpatterns = [
    path("", landing_view, name="landing"),
    path("verificar-rfc/", verificar_rfc_view, name="verificar_rfc"),
    path("djadmin-8x7k/", admin.site.urls),
    path("api/v1/", api.urls),
    path("api/v1/stripe/webhook/", stripe_webhook, name="stripe_webhook"),
    path("panel/", include("core.urls")),
    path("app/", include("accounts.urls")),
    path("reportes/", include("reportes.urls")),
]
