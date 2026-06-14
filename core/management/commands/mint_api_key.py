"""Acuña una API key de solo-lectura para una app externa (ej. ZeroLatency).

CIRRUS no tenía forma por CLI de crear API keys (solo vía UI). Este comando
usa el servicio sancionado `core.services.api_keys_service.crear_api_key` y
muestra la key plana UNA sola vez (en BD solo queda el hash SHA-256).

Uso:
    python manage.py mint_api_key \
        --nombre "ZeroLatency Sync CFDIs" \
        --rfc VEN191127M21 \
        --owner admin

Por defecto la key es de solo lectura (puede_leer=True,
puede_trigger_descarga=False). El scope se limita a las RFC indicadas.
"""

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError

from core.models import Empresa
from core.services.api_keys_service import crear_api_key


class Command(BaseCommand):
    help = "Acuña una API key de solo-lectura para una app externa."

    def add_arguments(self, parser):
        parser.add_argument("--nombre", required=True, help="Nombre descriptivo de la key")
        parser.add_argument(
            "--rfc", action="append", default=[], required=True,
            help="RFC de empresa a la que da acceso (repetible)",
        )
        parser.add_argument("--owner", default="admin", help="username del owner (default: admin)")
        parser.add_argument(
            "--trigger-descarga", action="store_true",
            help="Permitir disparar descargas (default: NO, solo lectura)",
        )

    def handle(self, *args, **opts):
        try:
            owner = User.objects.get(username=opts["owner"])
        except User.DoesNotExist:
            raise CommandError(f"Usuario '{opts['owner']}' no existe")

        rfcs = [r.upper() for r in opts["rfc"]]
        empresas = list(Empresa.objects.filter(rfc__in=rfcs))
        encontradas = {e.rfc for e in empresas}
        faltantes = set(rfcs) - encontradas
        if faltantes:
            raise CommandError(f"RFC(s) sin empresa en CIRRUS: {', '.join(sorted(faltantes))}")

        apikey, key_plain = crear_api_key(
            owner=owner,
            nombre=opts["nombre"],
            empresas=empresas,
            puede_leer=True,
            puede_trigger_descarga=opts["trigger_descarga"],
        )

        self.stdout.write(self.style.SUCCESS("API key creada."))
        self.stdout.write(f"  nombre : {apikey.nombre}")
        self.stdout.write(f"  prefix : {apikey.key_prefix}")
        self.stdout.write(f"  rfcs   : {', '.join(sorted(encontradas))}")
        self.stdout.write(f"  límite : {apikey.limite_requests_dia}/día")
        self.stdout.write(self.style.WARNING("  KEY PLANA (se muestra UNA sola vez):"))
        self.stdout.write(f"  {key_plain}")
