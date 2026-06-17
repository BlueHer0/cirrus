"""Backfill de atributos sueltos del CFDI (Bloque C).

Llena los 3 atributos que el parser actual no extraia:
- regimen_fiscal_emisor   (columna existente en core_cfdi)
- regimen_fiscal_receptor (columna nueva, migracion 0036)
- uso_cfdi                (columna existente)

Y puebla la tabla CfdiRelacionadoLink desde el nodo <cfdi:CfdiRelacionados>.

Idempotente: --force re-procesa todos sobreescribiendo. Sin --force solo
procesa CFDIs cuyos 3 campos esten vacios (los que ya tienen algun valor
se omiten).

Uso:
    python manage.py backfill_cfdi_atributos --dry-run
    python manage.py backfill_cfdi_atributos --year 2026
    python manage.py backfill_cfdi_atributos --force
"""

import logging

from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Q

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Backfill regimen_fiscal_*, uso_cfdi y CfdiRelacionadoLink desde XML en MinIO"

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--force", action="store_true",
                            help="Re-procesa todos sobrescribiendo atributos y relaciones")
        parser.add_argument("--rfc", type=str, default=None)
        parser.add_argument("--year", type=int, default=None)
        parser.add_argument("--limit", type=int, default=None)

    def handle(self, *args, **opts):
        from core.models import CFDI, CfdiRelacionadoLink
        from core.services.storage_minio import download_bytes
        from core.services.xml_processor import (
            extract_cfdi_atributos_basicos,
            extract_cfdi_relacionados,
        )

        qs = CFDI.objects.exclude(xml_minio_key="")
        if opts["rfc"]:
            qs = qs.filter(rfc_empresa=opts["rfc"])
        if opts["year"]:
            qs = qs.filter(fecha__year=opts["year"])
        if not opts["force"]:
            qs = qs.filter(
                Q(regimen_fiscal_emisor="") &
                Q(regimen_fiscal_receptor="") &
                Q(uso_cfdi="")
            )
        qs = qs.order_by("fecha")
        if opts["limit"]:
            qs = qs[: opts["limit"]]

        total = qs.count()
        self.stdout.write(f"CFDIs candidatos: {total}")
        if opts["dry_run"]:
            for c in qs[:5]:
                self.stdout.write(f"  - {c.uuid} {c.fecha.date()} tipo={c.tipo_comprobante}")
            self.stdout.write(self.style.WARNING("[DRY-RUN] no se escribe"))
            return

        procesados = 0
        atributos_llenados = 0
        relaciones_creadas = 0
        relaciones_vinculadas = 0
        relaciones_huerfanas = 0
        errores = 0

        for cfdi in qs.iterator(chunk_size=200):
            try:
                xml = download_bytes(cfdi.xml_minio_key)
            except Exception as e:
                logger.warning("ERROR descargando %s: %s", cfdi.uuid, e)
                errores += 1
                continue

            attrs = extract_cfdi_atributos_basicos(xml)
            rels = extract_cfdi_relacionados(xml)

            with transaction.atomic():
                # Atributos sueltos
                dirty = False
                for k, v in attrs.items():
                    if v and getattr(cfdi, k) != v:
                        setattr(cfdi, k, v)
                        dirty = True
                if dirty:
                    cfdi.save(update_fields=list(attrs.keys()))
                    atributos_llenados += 1

                # CfdiRelacionadoLink
                if opts["force"]:
                    CfdiRelacionadoLink.objects.filter(cfdi_origen=cfdi).delete()
                for r in rels:
                    rel_cfdi = CFDI.objects.filter(uuid=r["uuid_relacionado"]).first()
                    link, created = CfdiRelacionadoLink.objects.update_or_create(
                        cfdi_origen=cfdi,
                        uuid_relacionado=r["uuid_relacionado"],
                        defaults={
                            "tipo_relacion": r["tipo_relacion"],
                            "cfdi_relacionado": rel_cfdi,
                        },
                    )
                    if created:
                        relaciones_creadas += 1
                        if rel_cfdi is not None:
                            relaciones_vinculadas += 1
                        else:
                            relaciones_huerfanas += 1

            procesados += 1
            if procesados % 100 == 0:
                self.stdout.write(f"  ... {procesados}/{total}")

        self.stdout.write(self.style.SUCCESS(
            f"\nCFDIs procesados:        {procesados}/{total}"
        ))
        self.stdout.write(f"  con atributos llenados:   {atributos_llenados}")
        self.stdout.write(f"  CfdiRelacionadoLink creados: {relaciones_creadas}")
        self.stdout.write(f"    con CFDI relacionado en BD: {relaciones_vinculadas}")
        self.stdout.write(f"    huerfanos (UUID no en BD):  {relaciones_huerfanas}")
        self.stdout.write(f"  errores: {errores}")
