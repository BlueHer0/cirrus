"""Backfill de campos de nómina (FechaPago, FechaInicial/FinalPago, TipoNomina).

Recorre los CFDI tipo N existentes, descarga su XML de MinIO y llena los
campos nuevos agregados en la migración 0026.

Uso:
    # Dry-run (solo contar y mostrar ejemplos)
    python manage.py backfill_nomina_fields --dry-run

    # Ejecutar en producción (solo los que aún no tienen fecha_pago_nomina)
    python manage.py backfill_nomina_fields

    # Forzar re-procesar todos
    python manage.py backfill_nomina_fields --force

    # Solo para un RFC
    python manage.py backfill_nomina_fields --rfc AIPF760625HF5
"""

import logging

from django.core.management.base import BaseCommand
from django.db import transaction


class Command(BaseCommand):
    help = "Backfill campos de nómina (FechaPago, etc.) para CFDIs tipo N existentes"

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Solo mostrar cuántos, no guardar")
        parser.add_argument("--force", action="store_true", help="Re-procesar incluso los que ya tienen fecha_pago_nomina")
        parser.add_argument("--rfc", default=None, help="Solo empresa con este RFC")
        parser.add_argument("--batch", type=int, default=50, help="Batch size para commits")

    def handle(self, *args, **opts):
        from core.models import CFDI
        from core.services.storage_minio import download_bytes
        from core.services.xml_processor import _extract_nomina

        qs = CFDI.objects.filter(tipo_comprobante="N")
        if opts["rfc"]:
            qs = qs.filter(rfc_empresa=opts["rfc"].upper())
        if not opts["force"]:
            qs = qs.filter(fecha_pago_nomina__isnull=True)

        total = qs.count()
        self.stdout.write(self.style.WARNING(
            f"{'[DRY-RUN] ' if opts['dry_run'] else ''}CFDIs a procesar: {total}"
        ))

        if total == 0:
            return

        updated = failed = no_data = 0
        batch_size = opts["batch"]
        batch = []

        for idx, cfdi in enumerate(qs.iterator(chunk_size=200), 1):
            if not cfdi.xml_minio_key:
                no_data += 1
                continue

            try:
                xml_bytes = download_bytes(cfdi.xml_minio_key)
            except Exception as e:
                failed += 1
                self.stdout.write(self.style.ERROR(
                    f"  {str(cfdi.uuid)[:8]} error descarga: {e}"
                ))
                continue

            data = _extract_nomina(xml_bytes)
            if not data.get("fecha_pago"):
                no_data += 1
                continue

            cfdi.fecha_pago_nomina = data["fecha_pago"]
            cfdi.fecha_inicial_pago = data["fecha_inicial_pago"]
            cfdi.fecha_final_pago = data["fecha_final_pago"]
            cfdi.tipo_nomina = data["tipo_nomina"]
            batch.append(cfdi)

            if not opts["dry_run"] and len(batch) >= batch_size:
                with transaction.atomic():
                    CFDI.objects.bulk_update(
                        batch,
                        ["fecha_pago_nomina", "fecha_inicial_pago", "fecha_final_pago", "tipo_nomina"],
                    )
                updated += len(batch)
                batch = []
                self.stdout.write(f"  Procesados: {idx}/{total} (actualizados: {updated})")

            if opts["dry_run"] and idx <= 10:
                self.stdout.write(
                    f"  {str(cfdi.uuid)[:8]} | timbrado={cfdi.fecha.date()} | "
                    f"FechaPago={data['fecha_pago']} | TipoNom={data['tipo_nomina']}"
                )

        # Commit last batch
        if not opts["dry_run"] and batch:
            with transaction.atomic():
                CFDI.objects.bulk_update(
                    batch,
                    ["fecha_pago_nomina", "fecha_inicial_pago", "fecha_final_pago", "tipo_nomina"],
                )
            updated += len(batch)

        self.stdout.write(self.style.SUCCESS(
            f"\n{'[DRY-RUN] ' if opts['dry_run'] else ''}Resultado: "
            f"{updated} actualizados · {no_data} sin datos de nómina · {failed} errores"
        ))
