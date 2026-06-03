"""Stripe payment service for Cirrus."""

import stripe
from django.conf import settings

stripe.api_key = settings.STRIPE_SECRET_KEY


def get_or_create_customer(user):
    """Get or create a Stripe customer for a user."""
    profile = user.perfil
    if profile.stripe_customer_id:
        try:
            customer = stripe.Customer.retrieve(profile.stripe_customer_id)
            if not customer.get("deleted"):
                return customer
        except stripe.error.InvalidRequestError:
            pass

    customer = stripe.Customer.create(
        email=user.email,
        name=user.get_full_name() or user.email,
        metadata={"cirrus_user_id": str(user.id)},
    )
    profile.stripe_customer_id = customer.id
    profile.save(update_fields=["stripe_customer_id"])
    return customer


def create_checkout_session(user, plan_slug, success_url, cancel_url):
    """Create a Stripe Checkout session for a subscription."""
    from core.models import Plan

    plan = Plan.objects.get(slug=plan_slug)
    if not plan.stripe_price_id:
        raise ValueError(f"Plan {plan_slug} no tiene stripe_price_id configurado")

    customer = get_or_create_customer(user)

    session = stripe.checkout.Session.create(
        customer=customer.id,
        payment_method_types=["card"],
        line_items=[{
            "price": plan.stripe_price_id,
            "quantity": 1,
        }],
        mode="subscription",
        success_url=success_url,
        cancel_url=cancel_url,
        metadata={
            "cirrus_user_id": str(user.id),
            "cirrus_plan": plan_slug,
        },
        locale="es",
    )
    return session


def create_onetime_checkout(user, concept, amount_cents, success_url, cancel_url, metadata=None):
    """Create a checkout session for a one-time payment (historical year, etc.)."""
    customer = get_or_create_customer(user)

    session = stripe.checkout.Session.create(
        customer=customer.id,
        payment_method_types=["card"],
        line_items=[{
            "price_data": {
                "currency": "mxn",
                "unit_amount": amount_cents,
                "product_data": {
                    "name": concept,
                },
            },
            "quantity": 1,
        }],
        mode="payment",
        success_url=success_url,
        cancel_url=cancel_url,
        metadata=metadata or {},
        locale="es",
    )
    return session


def cancel_subscription(user):
    """Cancel subscription at end of current period."""
    profile = user.perfil
    if profile.stripe_subscription_id:
        sub = stripe.Subscription.modify(
            profile.stripe_subscription_id,
            cancel_at_period_end=True,
        )
        profile.subscription_cancel_at_period_end = True
        if sub.get("current_period_end"):
            from datetime import datetime, timezone
            profile.subscription_current_period_end = datetime.fromtimestamp(
                sub["current_period_end"], tz=timezone.utc
            )
        profile.save(update_fields=[
            "subscription_cancel_at_period_end",
            "subscription_current_period_end",
        ])
        return True
    return False


def reactivate_subscription(user):
    """Reactivate a subscription that was set to cancel at period end."""
    profile = user.perfil
    if profile.stripe_subscription_id:
        stripe.Subscription.modify(
            profile.stripe_subscription_id,
            cancel_at_period_end=False,
        )
        profile.subscription_cancel_at_period_end = False
        profile.save(update_fields=["subscription_cancel_at_period_end"])
        return True
    return False


def handle_webhook_event(event):
    """Process Stripe webhook events.

    Returns dict:
        {"status": "procesado"}       — evento procesado exitosamente
        {"status": "ignorado"}        — tipo de evento no manejado (normal)
        {"status": "error", "error": str}  — error de datos (ej. User no existe);
                                             el caller retorna HTTP 200 para que
                                             Stripe NO reintente datos malos.

    Las excepciones INESPERADAS (red, BD, bugs) se dejan propagar para que
    el caller (stripe_webhook view) devuelva 500 y Stripe reintente.
    """
    from core.services.monitor import log_info, log_error
    from core.services.alerts import send_telegram
    from core.models import Plan
    from accounts.models import StripePayment

    event_type = event["type"]
    data = event["data"]["object"]

    if event_type == "checkout.session.completed":
        return _handle_checkout_completed(data)

    elif event_type == "invoice.paid":
        return _handle_invoice_paid(data)

    elif event_type == "invoice.payment_failed":
        return _handle_invoice_payment_failed(data)

    elif event_type == "customer.subscription.updated":
        return _handle_subscription_updated(data)

    elif event_type == "customer.subscription.deleted":
        return _handle_subscription_deleted(data)

    # Tipos no manejados — Stripe manda muchos eventos que no nos importan
    return {"status": "ignorado"}


def _handle_checkout_completed(data):
    """checkout.session.completed: activa suscripción o pago histórico."""
    from django.contrib.auth.models import User
    from core.services.monitor import log_info, log_error
    from core.services.alerts import send_telegram
    from core.models import Plan
    from accounts.models import StripePayment

    user_id = data.get("metadata", {}).get("cirrus_user_id")
    plan_slug = data.get("metadata", {}).get("cirrus_plan")

    if user_id and plan_slug:
        try:
            user = User.objects.get(id=user_id)
        except User.DoesNotExist:
            return {"status": "error", "error": f"User {user_id} no existe"}

        try:
            plan = Plan.objects.get(slug=plan_slug)
        except Plan.DoesNotExist:
            return {"status": "error", "error": f"Plan '{plan_slug}' no existe"}

        profile = user.perfil
        profile.plan_fk = plan
        if data.get("subscription"):
            profile.stripe_subscription_id = data["subscription"]
            profile.subscription_status = "active"
            profile.subscription_cancel_at_period_end = False
            try:
                sub = stripe.Subscription.retrieve(data["subscription"])
                if sub.get("current_period_end"):
                    from datetime import datetime, timezone
                    profile.subscription_current_period_end = (
                        datetime.fromtimestamp(
                            sub["current_period_end"], tz=timezone.utc
                        )
                    )
            except Exception:
                pass
        profile.save()

        # Actualizar limite_requests_dia de las API keys del usuario
        _actualizar_rate_limits_usuario(user, plan.slug)

        amount = (data.get("amount_total") or 0) / 100
        StripePayment.objects.create(
            user=user,
            stripe_payment_intent_id=data.get("payment_intent", ""),
            stripe_invoice_id=data.get("invoice", ""),
            amount=amount,
            currency=data.get("currency", "mxn"),
            concept=f"{plan.nombre} (mensual)",
            status="paid",
            payment_method="card",
        )

        log_info("system", f"Plan activado: {user.email} → {plan.nombre}")
        send_telegram(
            f"💰 Nuevo pago: *{user.email}* → {plan.nombre} (${amount})",
            "success",
        )
        _enviar_email_recibo(user, plan, safe=True)
        return {"status": "procesado"}

    # Pago de año histórico
    empresa_id = data.get("metadata", {}).get("empresa_id")
    year = data.get("metadata", {}).get("year")
    if empresa_id and year:
        from core.models import Empresa

        try:
            emp = Empresa.objects.get(id=empresa_id)
        except Empresa.DoesNotExist:
            return {"status": "error", "error": f"Empresa {empresa_id} no existe"}

        emp.sync_desde_year = min(emp.sync_desde_year or 2026, int(year))
        emp.sync_desde_month = 1
        emp.sync_completada = False
        emp.save()

        # Generar los jobs del año histórico comprado AHORA. El audit nocturno
        # (auditar_y_reparar_jobs) calcula su ventana desde el PLAN, no desde
        # sync_desde, y el año comprable siempre cae fuera de esa ventana — sin
        # esto, el cliente paga el histórico pero nunca se descarga.
        if emp.fiel_verificada and emp.sync_activa:
            try:
                from core.services.job_scheduler import generar_jobs_iniciales
                jobs = generar_jobs_iniciales(emp)
                log_info("system", f"Histórico {year}: {jobs} jobs generados para {emp.rfc}")
            except Exception as e:
                log_error("system", f"Histórico {year} {emp.rfc}: fallo generando jobs: {e}")

        if user_id:
            try:
                user = User.objects.get(id=user_id)
                StripePayment.objects.create(
                    user=user,
                    stripe_payment_intent_id=data.get("payment_intent", ""),
                    amount=(data.get("amount_total") or 0) / 100,
                    currency=data.get("currency", "mxn"),
                    concept=f"Año histórico {year} — {emp.rfc}",
                    status="paid",
                    payment_method="card",
                )
            except User.DoesNotExist:
                # El empresa se actualizó, pero no pudimos linkear el pago.
                # Aún así consideramos el evento procesado porque lo principal
                # (activar año histórico) sí ocurrió.
                pass

        log_info("system", f"Histórico activado: {emp.rfc} desde {year}")
        send_telegram(f"💰 Año histórico: *{emp.rfc}* {year}", "success")
        return {"status": "procesado"}

    return {"status": "error", "error": "checkout.session.completed sin metadata procesable"}


def _handle_invoice_paid(data):
    """invoice.paid: renovación de suscripción."""
    from core.services.monitor import log_info
    from accounts.models import ClienteProfile, StripePayment

    customer_id = data.get("customer")
    amount = data.get("amount_paid", 0) / 100

    profile = ClienteProfile.objects.filter(stripe_customer_id=customer_id).first()
    if not profile:
        # Invoice de un customer que no corresponde a un perfil — posiblemente
        # pago de prueba o cliente borrado. No es error crítico.
        log_info("system", f"invoice.paid sin profile: customer={customer_id}")
        return {"status": "error", "error": f"Customer {customer_id} sin perfil"}

    if amount > 0:
        plan = profile.get_plan()
        StripePayment.objects.create(
            user=profile.user,
            stripe_invoice_id=data.get("id", ""),
            stripe_payment_intent_id=data.get("payment_intent", ""),
            amount=amount,
            currency=data.get("currency", "mxn"),
            concept=f"{plan.nombre if plan else 'Suscripción'} (renovación)",
            status="paid",
            payment_method="card",
        )

    # Si estaba past_due, al pagar se vuelve active
    if profile.subscription_status == "past_due":
        profile.subscription_status = "active"
        profile.save(update_fields=["subscription_status"])
        _actualizar_rate_limits_usuario(
            profile.user,
            profile.plan_fk.slug if profile.plan_fk else "free",
        )

    log_info("system", f"Factura pagada: {profile.user.email} ${amount}")
    return {"status": "procesado"}


def _handle_invoice_payment_failed(data):
    """invoice.payment_failed: pago rebotó."""
    from core.services.monitor import log_error
    from core.services.alerts import send_telegram
    from accounts.models import ClienteProfile

    customer_id = data.get("customer")
    profile = ClienteProfile.objects.filter(stripe_customer_id=customer_id).first()
    if not profile:
        return {"status": "error", "error": f"Customer {customer_id} sin perfil"}

    profile.subscription_status = "past_due"
    profile.save(update_fields=["subscription_status"])

    send_telegram(
        f"⚠️ Pago fallido: *{profile.user.email}*", "warning",
    )

    # Notificar al cliente. fail_silently=False para que se propague como
    # excepción y quede marcado en el log; PERO NO queremos que esto tire
    # el webhook completo. Wrap en try/except interno y log si falla.
    try:
        from django.core.mail import send_mail
        send_mail(
            "Problema con tu pago — Cirrus",
            "No pudimos procesar tu pago mensual.\n\n"
            "Por favor actualiza tu método de pago en tu panel.\n"
            "Si no se regulariza en 7 días, tu cuenta será degradada al plan gratuito.\n\n"
            "— Equipo Cirrus\ncirrus.nubex.me",
            "Cirrus <cirrus@nubex.me>",
            [profile.user.email],
            fail_silently=False,
        )
    except Exception as e:
        log_error(
            "email",
            f"No se pudo enviar email de pago fallido a {profile.user.email}",
            detail=str(e),
        )

    return {"status": "procesado"}


def _handle_subscription_updated(data):
    """customer.subscription.updated: cambios en la suscripción."""
    from accounts.models import ClienteProfile

    subscription_id = data.get("id")
    profile = ClienteProfile.objects.filter(
        stripe_subscription_id=subscription_id
    ).first()
    if not profile:
        return {"status": "error", "error": f"Subscription {subscription_id} sin perfil"}

    cancel_at = data.get("cancel_at_period_end", False)
    profile.subscription_cancel_at_period_end = cancel_at
    if data.get("current_period_end"):
        from datetime import datetime, timezone
        profile.subscription_current_period_end = (
            datetime.fromtimestamp(data["current_period_end"], tz=timezone.utc)
        )
    profile.save(update_fields=[
        "subscription_cancel_at_period_end",
        "subscription_current_period_end",
    ])
    return {"status": "procesado"}


def _handle_subscription_deleted(data):
    """customer.subscription.deleted: suscripción cancelada definitivamente."""
    from core.services.monitor import log_info
    from core.services.alerts import send_telegram
    from core.models import Plan
    from accounts.models import ClienteProfile

    subscription_id = data.get("id")
    profile = ClienteProfile.objects.filter(
        stripe_subscription_id=subscription_id
    ).first()
    if not profile:
        return {"status": "error", "error": f"Subscription {subscription_id} sin perfil"}

    profile.subscription_status = "canceled"
    profile.subscription_cancel_at_period_end = False
    profile.plan_fk = Plan.objects.filter(slug="free").first()
    profile.save()

    # Desactivar TODAS las API keys del usuario inmediatamente
    from core.models import APIKey
    from django.utils import timezone
    APIKey.objects.filter(owner=profile.user, activa=True).update(
        activa=False, revocada_en=timezone.now(),
    )

    log_info("system", f"Suscripción cancelada: {profile.user.email}")
    send_telegram(f"📉 Cancelación: {profile.user.email}", "warning")
    return {"status": "procesado"}


# ── Helpers ──────────────────────────────────────────────────────────


def _actualizar_rate_limits_usuario(user, plan_slug: str):
    """Actualiza limite_requests_dia de todas las API keys del usuario."""
    from core.models import APIKey
    from core.services.api_keys_service import RATE_LIMITS_POR_PLAN

    limite = RATE_LIMITS_POR_PLAN.get(plan_slug, 0)
    APIKey.objects.filter(owner=user).update(limite_requests_dia=limite)


def _enviar_email_recibo(user, plan, safe: bool = True):
    """Envía recibo PDF tras pago. Si safe=True, errores se loguean pero no
    tiran el webhook completo."""
    from django.core.mail import EmailMessage as DjangoEmail
    from core.services.monitor import log_error
    from core.services.recibo_pdf import generar_recibo
    from accounts.models import StripePayment

    try:
        payment = StripePayment.objects.filter(user=user).order_by("-created_at").first()
        if not payment:
            return
        pdf = generar_recibo(payment)
        email = DjangoEmail(
            subject=f"Tu plan {plan.nombre} está activo — Cirrus",
            body=(
                f"¡Gracias por tu pago! Tu plan {plan.nombre} ya está activo.\n\n"
                f"Adjuntamos tu recibo de pago.\n\n"
                f"— Equipo Cirrus\ncirrus.nubex.me"
            ),
            from_email="Cirrus <cirrus@nubex.me>",
            to=[user.email],
        )
        email.attach(f"Recibo_Cirrus_{payment.id:06d}.pdf", pdf, "application/pdf")
        email.send(fail_silently=False)
    except Exception as e:
        if safe:
            log_error(
                "email",
                f"No se pudo enviar recibo a {user.email}",
                detail=str(e),
            )
        else:
            raise
