from django.core.management.base import BaseCommand
from core.services.efos_sync import sync_efos


class Command(BaseCommand):
    help = "Sincroniza la lista 69-B del SAT (EFOS)"

    def handle(self, *args, **options):
        result = sync_efos()
        if result:
            self.stdout.write(
                self.style.SUCCESS(
                    f"EFOS sync OK: {result['total']} total, "
                    f"{result['nuevos']} nuevos, {result['actualizados']} actualizados, "
                    f"{result['errores']} errores parse"
                )
            )
        else:
            self.stdout.write(self.style.ERROR("EFOS sync falló"))
