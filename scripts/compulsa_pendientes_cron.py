#!/usr/bin/env python3
"""Retoma compulsas SAT pendientes ('solicitada') — cron cada 30 min con flock.

El SAT procesa las solicitudes de metadata de forma asíncrona (a veces >48h).
Este cron garantiza que en cuanto el SAT libere el paquete, se descargue,
parsee y diffee SIN depender de sesiones interactivas ni loops en memoria.
Al completarse una compulsa, _actuar_sobre_hallazgos re-encola los meses con
faltantes al RPA y avisa por Telegram (flujo ya probado offline).
"""
import os
import sys

sys.path.insert(0, "/var/www/cirrus")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "cirrus.settings")
import django

django.setup()
from django.utils import timezone

from core.models import CompulsaSAT
from core.services.compulsa_sat import procesar_compulsa

pendientes = list(CompulsaSAT.objects.filter(estado="solicitada").order_by("id"))
if not pendientes:
    sys.exit(0)

for c in pendientes:
    try:
        done = procesar_compulsa(c)
        c.refresh_from_db()
        print(
            f"[{timezone.now():%Y-%m-%d %H:%M}] compulsa {c.id} {c.empresa.rfc} "
            f"{c.tipo}: done={done} estado={c.estado} "
            f"SAT={c.sat_vigentes} faltantes={c.faltantes_count}",
            flush=True,
        )
    except Exception as e:
        print(f"[{timezone.now():%Y-%m-%d %H:%M}] compulsa {c.id} ERROR: {e}", flush=True)
