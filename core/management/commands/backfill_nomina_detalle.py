"""Backfill del complemento nomina12 en NominaDetalle.

Recorre los CFDI tipo='N' existentes, descarga su XML de MinIO, extrae
los totales fiscales del complemento nomina12:Nomina y crea/actualiza
la fila correspondiente en core_nominadetalle.

Idempotente via update_or_create (OneToOne sobre cfdi_uuid). Sin --force
solo procesa CFDIs sin NominaDetalle asociado; con --force re-procesa
todos sobrescribiendo.

Uso:
    python manage.py backfill_nomina_detalle --dry-run
    python manage.py backfill_nomina_detalle --year 2026
    python manage.py backfill_nomina_detalle --rfc VEN191127M21
    python manage.py backfill_nomina_detalle --force
"""

import logging

from django.core.management.base import BaseCommand
from django.db import transaction

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Backfill complemento nomina12 en NominaDetalle para CFDIs tipo='N'"

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true",
                            help="Cuenta y muestra ejemplos, sin escribir")
        parser.add_argument("--force", action="store_true",
                            help="Re-procesa todos (update_or_create sobrescribe)")
        parser.add_argument("--rfc", type=str, default=None,
                            help="Limitar a un rfc_empresa")
        parser.add_argument("--year", type=int, default=None)
        parser.add_argument("--limit", type=int, default=None)

    def handle(self, *args, **opts):
        from core.models import CFDI, NominaDetalle
        from core.services.storage_minio import download_bytes
        from core.services.xml_processor import extract_nomina12_detalle

        qs = CFDI.objects.filter(tipo_comprobante="N").exclude(xml_minio_key="")
        if opts["rfc"]:
            qs = qs.filter(rfc_empresa=opts["rfc"])
        if opts["year"]:
            qs = qs.filter(fecha__year=opts["year"])
        if not opts["force"]:
            qs = qs.filter(nomina_detalle__isnull=True)
        qs = qs.order_by("fecha")
        if opts["limit"]:
            qs = qs[: opts["limit"]]

        total = qs.count()
        self.stdout.write(f"CFDIs N candidatos: {total}")
        if opts["dry_run"]:
            for c in qs[:5]:
                self.stdout.write(f"  - {c.uuid} {c.fecha.date()} key={c.xml_minio_key}")
            self.stdout.write(self.style.WARNING("[DRY-RUN] no se escribe"))
            return

        procesados = 0
        creados = 0
        actualizados = 0
        sin_complemento = 0
        con_isr = 0
        errores = 0

        for cfdi in qs.iterator():
            try:
                xml = download_bytes(cfdi.xml_minio_key)
            except Exception as e:
                logger.warning("ERROR descargando %s: %s", cfdi.uuid, e)
                errores += 1
                continue

            datos = extract_nomina12_detalle(xml)
            if datos is None:
                sin_complemento += 1
                procesados += 1
                continue

            with transaction.atomic():
                obj, created = NominaDetalle.objects.update_or_create(
                    cfdi=cfdi, defaults=datos,
                )
            if created:
                creados += 1
            else:
                actualizados += 1
            if datos["total_impuestos_retenidos_nomina"] > 0:
                con_isr += 1
            procesados += 1
            if procesados % 25 == 0:
                self.stdout.write(f"  ... {procesados}/{total}")

        self.stdout.write(self.style.SUCCESS(
            f"\nCFDIs N procesados:      {procesados}/{total}"
        ))
        self.stdout.write(f"  NominaDetalle creados:    {creados}")
        self.stdout.write(f"  NominaDetalle actualizados: {actualizados}")
        self.stdout.write(f"  con ISR retenido > 0:     {con_isr}")
        self.stdout.write(f"  sin complemento nomina:   {sin_complemento}")
        self.stdout.write(f"  errores descarga/parse:   {errores}")
