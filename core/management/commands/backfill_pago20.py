"""Backfill del complemento pago20 (REP) en PagoDoctoRelacionado.

Recorre los CFDI tipo='P' existentes, descarga su XML de MinIO, extrae
el bloque pago20:Pagos y puebla la tabla core_pagodoctorelacionado.

Idempotente: el unique_together (rep_cfdi, pago_index, docto_index) impide
duplicados. Sin --force solo procesa REPs sin filas asociadas; con --force
re-procesa borrando primero las filas previas del REP.

Uso:
    python manage.py backfill_pago20 --dry-run
    python manage.py backfill_pago20 --year 2026
    python manage.py backfill_pago20 --rfc VEN191127M21
    python manage.py backfill_pago20 --force  # re-procesa todos
"""

import logging

from django.core.management.base import BaseCommand
from django.db import transaction

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Backfill complemento pago20 (REP) en PagoDoctoRelacionado para CFDIs tipo='P'"

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true",
                            help="Solo cuenta y muestra ejemplos, sin escribir")
        parser.add_argument("--force", action="store_true",
                            help="Re-procesa todos los REPs (borra filas previas)")
        parser.add_argument("--rfc", type=str, default=None,
                            help="Limitar a un rfc_empresa")
        parser.add_argument("--year", type=int, default=None,
                            help="Limitar a un anio (filtra por fecha)")
        parser.add_argument("--limit", type=int, default=None,
                            help="Limitar numero de REPs a procesar")

    def handle(self, *args, **opts):
        from core.models import CFDI, PagoDoctoRelacionado
        from core.services.storage_minio import download_bytes
        from core.services.xml_processor import extract_pago20

        qs = CFDI.objects.filter(tipo_comprobante="P").exclude(xml_minio_key="")
        if opts["rfc"]:
            qs = qs.filter(rfc_empresa=opts["rfc"])
        if opts["year"]:
            qs = qs.filter(fecha__year=opts["year"])
        if not opts["force"]:
            qs = qs.exclude(doctos_relacionados__isnull=False)
        qs = qs.order_by("fecha")
        if opts["limit"]:
            qs = qs[: opts["limit"]]

        total = qs.count()
        self.stdout.write(f"REPs candidatos a procesar: {total}")
        if opts["dry_run"]:
            for c in qs[:5]:
                self.stdout.write(f"  - {c.uuid} {c.fecha.date()} key={c.xml_minio_key}")
            self.stdout.write(self.style.WARNING("[DRY-RUN] no se escribe"))
            return

        procesados = 0
        filas_creadas = 0
        vinculados = 0
        huerfanos = 0
        errores = 0
        sin_complemento = 0

        for cfdi in qs.iterator():
            try:
                xml = download_bytes(cfdi.xml_minio_key)
            except Exception as e:
                logger.warning("ERROR descargando %s: %s", cfdi.uuid, e)
                errores += 1
                continue

            rows = extract_pago20(xml)
            if not rows:
                sin_complemento += 1
                procesados += 1
                continue

            with transaction.atomic():
                if opts["force"]:
                    PagoDoctoRelacionado.objects.filter(rep_cfdi=cfdi).delete()
                for row in rows:
                    # Intentar vincular factura_cfdi por UUID
                    factura = CFDI.objects.filter(uuid=row["id_documento"]).first()
                    PagoDoctoRelacionado.objects.create(
                        rep_cfdi=cfdi,
                        factura_cfdi=factura,
                        **row,
                    )
                    filas_creadas += 1
                    if factura is not None:
                        vinculados += 1
                    else:
                        huerfanos += 1
            procesados += 1
            if procesados % 20 == 0:
                self.stdout.write(f"  ... {procesados}/{total} REPs procesados")

        self.stdout.write(self.style.SUCCESS(
            f"\nREPs procesados:        {procesados}/{total}"
        ))
        self.stdout.write(f"  sin complemento pago20: {sin_complemento}")
        self.stdout.write(f"  errores descarga/parse: {errores}")
        self.stdout.write(f"DoctoRelacionado creados: {filas_creadas}")
        self.stdout.write(f"  con factura en BD:      {vinculados}")
        self.stdout.write(f"  huerfanos (UUID no en BD): {huerfanos}")
