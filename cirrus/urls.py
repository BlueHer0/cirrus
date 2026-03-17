"""Cirrus URL Configuration."""

from django.contrib import admin
from django.urls import path, include
from django.http import HttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from core.api.router import api
from core.views import landing_view, verificar_rfc_view


@csrf_exempt
@require_POST
def stripe_webhook(request):
    """Stripe webhook endpoint."""
    import stripe
    from django.conf import settings
    from core.services.stripe_service import handle_webhook_event

    payload = request.body
    sig_header = request.META.get("HTTP_STRIPE_SIGNATURE")

    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, settings.STRIPE_WEBHOOK_SECRET
        )
    except (ValueError, stripe.error.SignatureVerificationError):
        return HttpResponse(status=400)

    handle_webhook_event(event)
    return HttpResponse(status=200)


urlpatterns = [
    path("", landing_view, name="landing"),
    path("verificar-rfc/", verificar_rfc_view, name="verificar_rfc"),
    path("djadmin-8x7k/", admin.site.urls),
    path("api/v1/", api.urls),
    path("api/v1/stripe/webhook/", stripe_webhook, name="stripe_webhook"),
    path("panel/", include("core.urls")),
    path("app/", include("accounts.urls")),
]

