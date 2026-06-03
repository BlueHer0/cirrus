"""Hard delete de usuarios de prueba — auditable y con dry-run.

Uso:
    # Ver qué se borraría sin tocar nada
    python manage.py delete_test_users --dry-run

    # Ejecutar delete real
    python manage.py delete_test_users

    # Agregar/quitar usuarios específicos
    python manage.py delete_test_users --emails foo@x.com bar@y.com --dry-run
"""

from django.core.management.base import BaseCommand
from django.contrib.auth.models import User
from django.db import transaction


DEFAULT_EMAILS = [
    "arizpef@gmail.com",
    "farizpe@nubex.me",
]


class Command(BaseCommand):
    help = "Hard delete de usuarios de prueba (con dry-run y SELECT de relaciones)"

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Solo mostrar qué se borraría, sin ejecutar",
        )
        parser.add_argument(
            "--emails", nargs="+", default=None,
            help="Lista de emails a eliminar (default: arizpef@gmail.com, farizpe@nubex.me)",
        )

    def handle(self, *args, **opts):
        from accounts.models import ClienteProfile, StripePayment, EmailConfirmation
        from core.models import (
            Empresa, CFDI, DescargaLog, DescargaJob, APIKey,
            Colaborador, PipelineState, SATHealthProbe, TelegramAlert,
        )

        emails = opts["emails"] or DEFAULT_EMAILS
        dry_run = opts["dry_run"]

        self.stdout.write(self.style.WARNING(
            f"{'[DRY-RUN] ' if dry_run else ''}Objetivo: {len(emails)} usuario(s)"
        ))

        users_to_delete = []
        for email in emails:
            try:
                u = User.objects.get(email=email)
            except User.DoesNotExist:
                self.stdout.write(self.style.ERROR(f"  {email}: NO EXISTE"))
                continue

            if u.is_staff or u.is_superuser:
                self.stdout.write(self.style.ERROR(
                    f"  {email}: staff={u.is_staff} superuser={u.is_superuser} — SKIP (protección)"
                ))
                continue

            users_to_delete.append(u)

            # SELECT de relaciones
            empresas = Empresa.objects.filter(owner=u)
            rfcs = list(empresas.values_list("rfc", flat=True))
            rels = {
                "ClienteProfile": ClienteProfile.objects.filter(user=u).count(),
                "StripePayment": StripePayment.objects.filter(user=u).count(),
                "EmailConfirmation": EmailConfirmation.objects.filter(user=u).count(),
                "Empresa (owner)": empresas.count(),
                "CFDI (rfc_empresa)": CFDI.objects.filter(rfc_empresa__in=rfcs).count() if rfcs else 0,
                "DescargaLog": DescargaLog.objects.filter(empresa__in=empresas).count() if rfcs else 0,
                "DescargaJob": DescargaJob.objects.filter(empresa__in=empresas).count() if rfcs else 0,
                "PipelineState": PipelineState.objects.filter(empresa__in=empresas).count() if rfcs else 0,
                "SATHealthProbe": SATHealthProbe.objects.filter(empresa__in=empresas).count() if rfcs else 0,
                "APIKey": APIKey.objects.filter(owner=u).count(),
                "Colaborador (principal)": Colaborador.objects.filter(cuenta_principal=u).count(),
                "Colaborador (invitado)": Colaborador.objects.filter(usuario=u).count(),
                "TelegramAlert": TelegramAlert.objects.filter(recipient_user=u).count(),
            }

            self.stdout.write(self.style.NOTICE(f"\n=== {email} (id={u.id}) ==="))
            self.stdout.write(f"  is_staff={u.is_staff} is_superuser={u.is_superuser}")
            self.stdout.write(f"  date_joined={u.date_joined}")
            for name, count in rels.items():
                color = self.style.SUCCESS if count == 0 else self.style.WARNING
                self.stdout.write(f"  {name}: {color(str(count))}")

        if not users_to_delete:
            self.stdout.write(self.style.WARNING("\nNo hay usuarios para eliminar."))
            return

        self.stdout.write(self.style.WARNING(
            f"\n{'[DRY-RUN] ' if dry_run else ''}Total a borrar: {len(users_to_delete)} usuario(s)"
        ))

        if dry_run:
            self.stdout.write(self.style.NOTICE(
                "Dry-run finalizado. Usa sin --dry-run para ejecutar."
            ))
            return

        with transaction.atomic():
            for u in users_to_delete:
                email = u.email
                u.delete()
                self.stdout.write(self.style.SUCCESS(f"  ✓ {email} eliminado (hard delete, cascade)"))

        self.stdout.write(self.style.SUCCESS(
            f"\n✓ {len(users_to_delete)} usuario(s) eliminado(s) permanentemente."
        ))
