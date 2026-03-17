"""
Accounts Models — ClienteProfile and EmailConfirmation for client users.
"""

import uuid

from django.contrib.auth.models import User
from django.db import models


class ClienteProfile(models.Model):
    """Profile for client users (non-staff)."""

    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="perfil")
    empresa_nombre = models.CharField(
        max_length=200, blank=True,
        help_text="Nombre comercial del cliente",
    )
    telefono = models.CharField(max_length=20, blank=True)

    # Datos fiscales (para facturarles)
    rfc_facturacion = models.CharField(max_length=13, blank=True)
    razon_social = models.CharField(max_length=300, blank=True)
    regimen_fiscal = models.CharField(max_length=10, blank=True)
    codigo_postal = models.CharField(max_length=5, blank=True)
    uso_cfdi = models.CharField(max_length=10, default="G03")
    email_facturacion = models.EmailField(blank=True)

    # Plan y límites
    plan_legacy = models.CharField(
        max_length=20,
        default="free",
        choices=[
            ("free", "Gratis"),
            ("basico", "Básico"),
            ("pro", "Profesional"),
            ("enterprise", "Enterprise"),
        ],
        help_text="DEPRECATED — usar plan_fk",
    )
    plan_fk = models.ForeignKey(
        "core.Plan", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="clientes", help_text="Plan de suscripción actual",
    )

    # Tracking
    conversiones_este_mes = models.IntegerField(default=0)
    descargas_este_mes = models.IntegerField(default=0)

    # Stripe
    stripe_customer_id = models.CharField(max_length=100, blank=True)
    stripe_subscription_id = models.CharField(max_length=100, blank=True)
    subscription_status = models.CharField(
        max_length=20, default="none",
        choices=[
            ("none", "Sin suscripción"),
            ("active", "Activa"),
            ("past_due", "Pago pendiente"),
            ("canceled", "Cancelada"),
            ("trialing", "Prueba"),
        ],
    )
    subscription_current_period_end = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Perfil de Cliente"
        verbose_name_plural = "Perfiles de Clientes"

    def __str__(self):
        plan_name = self.plan_fk.nombre if self.plan_fk else self.plan_legacy
        return f"{self.user.email} — {plan_name}"

    def get_plan(self):
        """Return the Plan object for this profile."""
        if self.plan_fk:
            return self.plan_fk
        from core.models import Plan
        return Plan.objects.filter(slug=self.plan_legacy or "free").first()


class StripePayment(models.Model):
    """Registro de pagos procesados por Stripe."""

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="pagos")
    stripe_payment_intent_id = models.CharField(max_length=100, blank=True)
    stripe_invoice_id = models.CharField(max_length=100, blank=True)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    currency = models.CharField(max_length=3, default="mxn")
    concept = models.CharField(max_length=200)
    status = models.CharField(max_length=20, default="pending")
    payment_method = models.CharField(max_length=50, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Pago Stripe"
        verbose_name_plural = "Pagos Stripe"

    def __str__(self):
        return f"{self.user.email} — ${self.amount} {self.currency} — {self.status}"


class EmailConfirmation(models.Model):
    """Token for email confirmation during registration."""

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="confirmations")
    token = models.UUIDField(default=uuid.uuid4, unique=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    confirmed = models.BooleanField(default=False)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.user.email} — {'✓' if self.confirmed else '✗'}"

