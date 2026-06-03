# Cirrus — Auditoría 1 + 2 (2026-04-20)
# APIKey: agrega key_hash (unique), key_prefix, rate limit, vigencia de plan
# StripeWebhookEvent: nueva tabla de eventos Stripe con idempotencia

import hashlib
import uuid

from django.db import migrations, models


def backfill_key_hash_and_prefix(apps, schema_editor):
    """Llena key_hash (SHA-256) y key_prefix a partir del key plano existente."""
    APIKey = apps.get_model("core", "APIKey")
    for k in APIKey.objects.all():
        if not k.key:
            continue
        k.key_hash = hashlib.sha256(k.key.encode("utf-8")).hexdigest()
        k.key_prefix = f"legacy_{k.key[:8]}"
        k.save(update_fields=["key_hash", "key_prefix"])


def reverse_backfill(apps, schema_editor):
    APIKey = apps.get_model("core", "APIKey")
    APIKey.objects.update(key_hash=None, key_prefix="")


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0028_alter_chunkfiscal_embedding"),
    ]

    operations = [
        # ── APIKey: agregar key_hash nullable, SIN db_index para evitar
        # conflicto con el índice _like que Django genera automáticamente
        # al transicionar a unique=True más abajo.
        migrations.AddField(
            model_name="apikey",
            name="key_hash",
            field=models.CharField(blank=True, null=True, max_length=64),
        ),
        migrations.AddField(
            model_name="apikey",
            name="key_prefix",
            field=models.CharField(blank=True, max_length=32),
        ),
        migrations.AddField(
            model_name="apikey",
            name="requests_hoy",
            field=models.IntegerField(default=0),
        ),
        migrations.AddField(
            model_name="apikey",
            name="limite_requests_dia",
            field=models.IntegerField(default=1000),
        ),
        migrations.AddField(
            model_name="apikey",
            name="ultimo_reset_requests",
            field=models.DateField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="apikey",
            name="plan_slug_al_crear",
            field=models.CharField(blank=True, max_length=20),
        ),
        migrations.AddField(
            model_name="apikey",
            name="revocada_en",
            field=models.DateTimeField(blank=True, null=True),
        ),

        # Permitir que `key` quede en blanco (se desactiva en 0030)
        migrations.AlterField(
            model_name="apikey",
            name="key",
            field=models.CharField(blank=True, db_index=True, max_length=64, unique=True),
        ),

        # Backfill de key_hash + key_prefix
        migrations.RunPython(backfill_key_hash_and_prefix, reverse_code=reverse_backfill),

        # Tras backfill, promover key_hash a unique+indexed con SQL raw
        # (evita el doble-índice que Django generaría al hacer AlterField)
        migrations.RunSQL(
            sql=(
                "CREATE UNIQUE INDEX IF NOT EXISTS core_apikey_key_hash_uniq "
                "ON core_apikey (key_hash) WHERE key_hash IS NOT NULL;"
            ),
            reverse_sql="DROP INDEX IF EXISTS core_apikey_key_hash_uniq;",
        ),
        migrations.RunSQL(
            sql="CREATE INDEX IF NOT EXISTS core_apikey_key_prefix_idx ON core_apikey (key_prefix);",
            reverse_sql="DROP INDEX IF EXISTS core_apikey_key_prefix_idx;",
        ),
        # Informar a Django del estado final para que el model.state coincida
        migrations.AlterField(
            model_name="apikey",
            name="key_hash",
            field=models.CharField(
                blank=True, db_index=True, max_length=64, null=True,
                help_text="SHA-256 hex de la key plana",
            ),
        ),
        migrations.AlterField(
            model_name="apikey",
            name="key_prefix",
            field=models.CharField(
                blank=True, db_index=True, max_length=32,
                help_text="Primeros chars de la key para identificación visual",
            ),
        ),

        # ── Nuevo modelo StripeWebhookEvent ─────────────────────────
        migrations.CreateModel(
            name="StripeWebhookEvent",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("stripe_event_id", models.CharField(
                    db_index=True, max_length=200, unique=True,
                    help_text="ID del evento en Stripe — garantiza idempotencia",
                )),
                ("event_type", models.CharField(db_index=True, max_length=100)),
                ("customer_id", models.CharField(blank=True, db_index=True, max_length=100)),
                ("estado", models.CharField(
                    choices=[
                        ("recibido", "Recibido"), ("procesado", "Procesado"),
                        ("error", "Error"), ("ignorado", "Ignorado (tipo no manejado)"),
                    ],
                    db_index=True, default="recibido", max_length=20,
                )),
                ("payload", models.JSONField()),
                ("error_detalle", models.TextField(blank=True, null=True)),
                ("intentos", models.IntegerField(default=0)),
                ("recibido_en", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("procesado_en", models.DateTimeField(blank=True, null=True)),
            ],
            options={
                "verbose_name": "Evento Stripe",
                "verbose_name_plural": "Eventos Stripe",
                "ordering": ["-recibido_en"],
            },
        ),
        migrations.AddIndex(
            model_name="stripewebhookevent",
            index=models.Index(fields=["estado", "recibido_en"], name="core_stripe_estado_idx"),
        ),
        migrations.AddIndex(
            model_name="stripewebhookevent",
            index=models.Index(fields=["event_type", "recibido_en"], name="core_stripe_type_idx"),
        ),
    ]
