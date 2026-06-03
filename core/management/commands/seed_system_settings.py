"""Seed initial SystemSettings with SMTP credentials.

Usage:
    python manage.py seed_system_settings \\
        --noreply-password 'xxx' \\
        --contacto-password 'yyy'

Passwords are read from CLI args (or env vars CIRRUS_NOREPLY_PASSWORD /
CIRRUS_CONTACTO_PASSWORD if not provided) and encrypted before storage.
"""

import os

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Seed/update SystemSettings singleton with SMTP credentials"

    def add_arguments(self, parser):
        parser.add_argument("--noreply-email", default="noreply@nubex.me")
        parser.add_argument("--contacto-email", default="contactocirrus@nubex.me")
        parser.add_argument("--smtp-host", default="chocobo.mxrouting.net")
        parser.add_argument("--smtp-port", type=int, default=465)
        parser.add_argument("--noreply-password", default=None)
        parser.add_argument("--contacto-password", default=None)

    def handle(self, *args, **opts):
        from core.services.system_settings import (
            get_settings, set_noreply_password, set_contacto_password,
        )

        s = get_settings()
        s.noreply_email = opts["noreply_email"]
        s.contacto_email = opts["contacto_email"]
        s.smtp_host = opts["smtp_host"]
        s.smtp_port = opts["smtp_port"]
        s.smtp_use_ssl = True
        s.save()

        noreply_pwd = opts["noreply_password"] or os.environ.get("CIRRUS_NOREPLY_PASSWORD")
        contacto_pwd = opts["contacto_password"] or os.environ.get("CIRRUS_CONTACTO_PASSWORD")

        if noreply_pwd:
            set_noreply_password(noreply_pwd)
            self.stdout.write(self.style.SUCCESS(f"noreply password set for {s.noreply_email}"))
        else:
            self.stdout.write(self.style.WARNING("noreply password not provided — skipped"))

        if contacto_pwd:
            set_contacto_password(contacto_pwd)
            self.stdout.write(self.style.SUCCESS(f"contacto password set for {s.contacto_email}"))
        else:
            self.stdout.write(self.style.WARNING("contacto password not provided — skipped"))

        self.stdout.write(self.style.SUCCESS(
            f"SMTP host: {s.smtp_host}:{s.smtp_port} (SSL={s.smtp_use_ssl})"
        ))
