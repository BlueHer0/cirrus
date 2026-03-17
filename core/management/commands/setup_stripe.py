"""Create Stripe products and prices for Cirrus plans."""

import stripe
from django.conf import settings
from django.core.management.base import BaseCommand

from core.models import Plan


class Command(BaseCommand):
    help = "Create Stripe products and prices, then save IDs to Plan model."

    def handle(self, *args, **options):
        stripe.api_key = settings.STRIPE_SECRET_KEY

        planes = [
            {
                "slug": "basico",
                "nombre": "Cirrus Básico",
                "precio_mxn": 19900,  # centavos
                "descripcion": "2 empresas, 2 descargas/mes, 500 CFDIs",
            },
            {
                "slug": "pro",
                "nombre": "Cirrus Profesional",
                "precio_mxn": 49900,
                "descripcion": "6 empresas, 4 descargas/mes, 5000 CFDIs, API REST",
            },
            {
                "slug": "enterprise",
                "nombre": "Cirrus Enterprise",
                "precio_mxn": 129900,
                "descripcion": "15 empresas, 10 descargas/mes, 50000 CFDIs",
            },
        ]

        for p in planes:
            try:
                plan = Plan.objects.get(slug=p["slug"])
            except Plan.DoesNotExist:
                self.stderr.write(f"  Plan '{p['slug']}' no existe en DB, saltando...")
                continue

            if plan.stripe_product_id and plan.stripe_price_id:
                self.stdout.write(f"  {p['nombre']}: ya configurado (product={plan.stripe_product_id})")
                continue

            product = stripe.Product.create(
                name=p["nombre"],
                description=p["descripcion"],
                metadata={"cirrus_plan": p["slug"]},
            )

            price = stripe.Price.create(
                product=product.id,
                unit_amount=p["precio_mxn"],
                currency="mxn",
                recurring={"interval": "month"},
                metadata={"cirrus_plan": p["slug"]},
            )

            plan.stripe_product_id = product.id
            plan.stripe_price_id = price.id
            plan.save(update_fields=["stripe_product_id", "stripe_price_id"])

            self.stdout.write(self.style.SUCCESS(
                f"  {p['nombre']}: product={product.id} price={price.id}"
            ))

        # Producto: Año Histórico (pago único)
        hist_product = stripe.Product.create(
            name="Año Histórico Adicional",
            description="Descarga de un año completo de CFDIs para una empresa",
            metadata={"cirrus_type": "historico"},
        )
        hist_price = stripe.Price.create(
            product=hist_product.id,
            unit_amount=50000,  # $500 MXN
            currency="mxn",
            metadata={"cirrus_type": "historico"},
        )
        self.stdout.write(self.style.SUCCESS(
            f"  Año Histórico: product={hist_product.id} price={hist_price.id}"
        ))

        # Producto: RFC Extra (recurrente)
        rfc_product = stripe.Product.create(
            name="RFC Adicional",
            description="Empresa adicional sobre el límite del plan",
            metadata={"cirrus_type": "rfc_extra"},
        )
        rfc_price = stripe.Price.create(
            product=rfc_product.id,
            unit_amount=4900,  # $49 MXN
            currency="mxn",
            recurring={"interval": "month"},
            metadata={"cirrus_type": "rfc_extra"},
        )
        self.stdout.write(self.style.SUCCESS(
            f"  RFC Extra: product={rfc_product.id} price={rfc_price.id}"
        ))

        # Producto: Colaborador (recurrente)
        colab_product = stripe.Product.create(
            name="Colaborador Adicional",
            description="Usuario adicional con acceso a empresas",
            metadata={"cirrus_type": "colaborador"},
        )
        colab_price = stripe.Price.create(
            product=colab_product.id,
            unit_amount=3000,  # $30 MXN
            currency="mxn",
            recurring={"interval": "month"},
            metadata={"cirrus_type": "colaborador"},
        )
        self.stdout.write(self.style.SUCCESS(
            f"  Colaborador: product={colab_product.id} price={colab_price.id}"
        ))

        self.stdout.write(self.style.SUCCESS("\n✅ Setup Stripe completado."))
