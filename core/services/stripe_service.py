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
        stripe.Subscription.modify(
            profile.stripe_subscription_id,
            cancel_at_period_end=True,
        )
        return True
    return False


def handle_webhook_event(event):
    """Process Stripe webhook events."""
    from core.services.monitor import log_info
    from core.services.alerts import send_telegram
    from core.models import Plan

    event_type = event["type"]
    data = event["data"]["object"]

    if event_type == "checkout.session.completed":
        user_id = data.get("metadata", {}).get("cirrus_user_id")
        plan_slug = data.get("metadata", {}).get("cirrus_plan")

        if user_id and plan_slug:
            from django.contrib.auth.models import User

            try:
                user = User.objects.get(id=user_id)
                plan = Plan.objects.get(slug=plan_slug)
                profile = user.perfil
                profile.plan_fk = plan
                if data.get("subscription"):
                    profile.stripe_subscription_id = data["subscription"]
                    profile.subscription_status = "active"
                profile.save()

                log_info("system", f"Plan activado: {user.email} → {plan.nombre}")
                send_telegram(
                    f"💰 Nuevo pago: *{user.email}* → {plan.nombre}", "success"
                )
            except (User.DoesNotExist, Plan.DoesNotExist) as e:
                log_info("system", f"Webhook error: {e}")

        # One-time payment (historical year)
        empresa_id = data.get("metadata", {}).get("empresa_id")
        year = data.get("metadata", {}).get("year")
        if empresa_id and year:
            from core.models import Empresa

            try:
                emp = Empresa.objects.get(id=empresa_id)
                emp.sync_desde_year = min(emp.sync_desde_year or 2026, int(year))
                emp.sync_desde_month = 1
                emp.sync_completada = False
                emp.save()
                log_info("system", f"Histórico activado: {emp.rfc} desde {year}")
                send_telegram(
                    f"💰 Año histórico: *{emp.rfc}* {year}", "success"
                )
            except Exception as e:
                log_info("system", f"Webhook historico error: {e}")

    elif event_type == "invoice.paid":
        customer_id = data.get("customer")
        amount = data.get("amount_paid", 0) / 100
        log_info("system", f"Factura pagada: customer={customer_id} ${amount} MXN")

    elif event_type == "invoice.payment_failed":
        customer_id = data.get("customer")
        send_telegram(f"⚠️ Pago fallido: {customer_id}", "warning")
        from accounts.models import ClienteProfile

        profile = ClienteProfile.objects.filter(
            stripe_customer_id=customer_id
        ).first()
        if profile:
            profile.subscription_status = "past_due"
            profile.save(update_fields=["subscription_status"])

    elif event_type == "customer.subscription.deleted":
        subscription_id = data.get("id")
        from accounts.models import ClienteProfile

        profile = ClienteProfile.objects.filter(
            stripe_subscription_id=subscription_id
        ).first()
        if profile:
            profile.subscription_status = "canceled"
            profile.plan_fk = Plan.objects.filter(slug="free").first()
            profile.save()
            log_info("system", f"Suscripción cancelada: {profile.user.email}")
            send_telegram(
                f"📉 Cancelación: {profile.user.email}", "warning"
            )
