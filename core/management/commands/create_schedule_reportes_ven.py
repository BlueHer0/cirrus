"""Crea idempotentemente las 3 PeriodicTask de cortes de reporte para VEN.

  - dia 6 09:00 MX -> cierre_mes_anterior
  - dia 11 09:00 MX -> avance_10
  - dia 21 09:00 MX -> avance_20

Uso:
    python manage.py create_schedule_reportes_ven
    python manage.py create_schedule_reportes_ven --dry-run
    python manage.py create_schedule_reportes_ven --disable  # desactivar
"""

import json
import logging

from django.core.management.base import BaseCommand

logger = logging.getLogger(__name__)

VEN_RFC = "VEN191127M21"
DEST_EMAIL = "farizpe@icloud.com"
HORA = 9
MINUTO = 0
TZ = "America/Mexico_City"

CORTES = [
    {
        "name": "enviar-reporte-VEN-cierre-mes-anterior",
        "dia": 6,
        "corte_tipo": "cierre_mes_anterior",
    },
    {
        "name": "enviar-reporte-VEN-avance-10",
        "dia": 11,
        "corte_tipo": "avance_10",
    },
    {
        "name": "enviar-reporte-VEN-avance-20",
        "dia": 21,
        "corte_tipo": "avance_20",
    },
]


class Command(BaseCommand):
    help = "Crea/actualiza las 3 PeriodicTask de cortes de reporte VEN"

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--disable", action="store_true",
                            help="Marca las 3 PeriodicTask como enabled=False")

    def handle(self, *args, **opts):
        from django_celery_beat.models import CrontabSchedule, PeriodicTask
        from core.models import Empresa

        try:
            empresa = Empresa.objects.get(rfc=VEN_RFC)
        except Empresa.DoesNotExist:
            self.stderr.write(self.style.ERROR(f"No existe la empresa {VEN_RFC}"))
            return

        empresa_id = str(empresa.id)
        self.stdout.write(f"Empresa: {empresa.nombre} ({VEN_RFC}) — UUID {empresa_id}")
        self.stdout.write(f"Destinatario: {DEST_EMAIL}  Hora: {HORA:02d}:{MINUTO:02d} {TZ}")

        for c in CORTES:
            kwargs = json.dumps({
                "empresa_id": empresa_id,
                "corte_tipo": c["corte_tipo"],
                "dest_email": DEST_EMAIL,
            })
            if opts["dry_run"]:
                self.stdout.write(
                    f"[DRY] name={c['name']} crontab=({MINUTO} {HORA} {c['dia']} * *) {TZ}  kwargs={kwargs}"
                )
                continue
            sched, _ = CrontabSchedule.objects.get_or_create(
                minute=str(MINUTO),
                hour=str(HORA),
                day_of_month=str(c["dia"]),
                month_of_year="*",
                day_of_week="*",
                timezone=TZ,
            )
            obj, created = PeriodicTask.objects.update_or_create(
                name=c["name"],
                defaults={
                    "task": "reportes.tasks.enviar_reporte_corte_email",
                    "crontab": sched,
                    "kwargs": kwargs,
                    "enabled": not opts["disable"],
                    "description": (
                        f"Corte {c['corte_tipo']} para {VEN_RFC} -> {DEST_EMAIL}. "
                        f"PDF v4 adjunto."
                    ),
                },
            )
            tag = "creado" if created else "actualizado"
            estado = "ENABLED" if obj.enabled else "DISABLED"
            self.stdout.write(self.style.SUCCESS(
                f"  {tag} [{estado}] {c['name']}  (dia {c['dia']}, {HORA:02d}:{MINUTO:02d} {TZ})"
            ))
