"""Management command to initialize proximo_scrape for all empresas.

Usage:
    python manage.py init_schedules           # seed proximo_scrape
    python manage.py init_schedules --dry-run # preview without saving
    python manage.py init_schedules --force   # overwrite existing proximo_scrape
"""

from datetime import datetime, timezone

from django.core.management.base import BaseCommand

from core.models import ScheduleConfig
from core.services.scheduler import calcular_proximo_scrape


class Command(BaseCommand):
    help = "Initialize proximo_scrape for empresas with active ScheduleConfig"

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Preview changes without saving",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Overwrite existing proximo_scrape values",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        force = options["force"]
        now = datetime.now(timezone.utc)

        schedules = ScheduleConfig.objects.filter(
            activo=True,
            empresa__descarga_activa=True,
        ).select_related("empresa")

        if not force:
            schedules = schedules.filter(empresa__proximo_scrape__isnull=True)

        if not schedules.exists():
            self.stdout.write(self.style.SUCCESS("✅ No empresas need initialization"))
            return

        count = 0
        for schedule in schedules:
            empresa = schedule.empresa
            proximo = calcular_proximo_scrape(schedule, now)

            action = "SET" if empresa.proximo_scrape is None else "OVERWRITE"
            self.stdout.write(
                f"  {action} {empresa.rfc}: "
                f"freq={schedule.frecuencia}, "
                f"hora={schedule.hora_preferida}, "
                f"proximo_scrape → {proximo.isoformat()}"
            )

            if not dry_run:
                empresa.proximo_scrape = proximo
                empresa.save(update_fields=["proximo_scrape"])
                count += 1

        if dry_run:
            self.stdout.write(self.style.WARNING(
                f"\n🔍 DRY RUN: {schedules.count()} empresas would be updated"
            ))
        else:
            self.stdout.write(self.style.SUCCESS(
                f"\n✅ Initialized proximo_scrape for {count} empresas"
            ))
