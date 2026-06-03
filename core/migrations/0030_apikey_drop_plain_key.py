# Cirrus — Auditoría 1 (2026-04-20)
# Elimina el campo `key` (texto plano) de APIKey. El hash_key queda como
# la única forma de autenticar.

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0029_apikey_hash_ratelimit_stripewebhook"),
    ]

    operations = [
        # Drop directo del campo `key` — Postgres se encarga de eliminar
        # también el índice unique asociado. No necesitamos wipe previo.
        migrations.RemoveField(
            model_name="apikey",
            name="key",
        ),
    ]
