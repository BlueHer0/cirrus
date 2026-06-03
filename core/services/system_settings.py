"""System settings service.

Provides runtime-configurable system settings (SMTP credentials) stored
encrypted in the database. Editable via Django admin.

Passwords are encrypted with Fernet using settings.FIEL_ENCRYPTION_KEY.
"""

import logging
from typing import Optional

from django.conf import settings as django_settings
from django.core.mail import EmailMessage, EmailMultiAlternatives
from django.core.mail.backends.smtp import EmailBackend

from core.services.fiel_encryption import encrypt_password, decrypt_password

logger = logging.getLogger("core.system_settings")


def get_settings():
    """Return the singleton SystemSettings instance."""
    from core.models import SystemSettings
    return SystemSettings.load()


def set_noreply_password(password: str, user=None):
    """Update and encrypt the noreply password."""
    s = get_settings()
    s.noreply_password_encrypted = encrypt_password(password)
    if user:
        s.updated_by = user
    s.save()
    logger.info("noreply password updated")


def set_contacto_password(password: str, user=None):
    """Update and encrypt the contacto password."""
    s = get_settings()
    s.contacto_password_encrypted = encrypt_password(password)
    if user:
        s.updated_by = user
    s.save()
    logger.info("contacto password updated")


def _build_backend(username: str, encrypted_password: Optional[bytes]) -> EmailBackend:
    """Build a Django EmailBackend using DB-stored credentials.

    Falls back to env var EMAIL_HOST_PASSWORD if DB credentials are missing.
    """
    s = get_settings()

    if encrypted_password:
        password = decrypt_password(encrypted_password)
    else:
        password = django_settings.EMAIL_HOST_PASSWORD
        logger.warning("Using env-var fallback password for %s", username)

    return EmailBackend(
        host=s.smtp_host,
        port=s.smtp_port,
        username=username,
        password=password,
        use_ssl=s.smtp_use_ssl,
        use_tls=False,
        timeout=15,
        fail_silently=False,
    )


def get_noreply_backend() -> EmailBackend:
    s = get_settings()
    enc = bytes(s.noreply_password_encrypted) if s.noreply_password_encrypted else None
    return _build_backend(s.noreply_email, enc)


def get_contacto_backend() -> EmailBackend:
    s = get_settings()
    enc = bytes(s.contacto_password_encrypted) if s.contacto_password_encrypted else None
    return _build_backend(s.contacto_email, enc)


def _from_email(address: str, display_name: str) -> str:
    return f"{display_name} <{address}>" if display_name else address


def send_noreply(subject: str, body: str, to: list, html: str = None, reply_to: list = None) -> int:
    """Send a system email from noreply@ (confirmations, alerts, reports)."""
    s = get_settings()
    backend = get_noreply_backend()
    from_email = _from_email(s.noreply_email, s.noreply_display_name)

    if html:
        msg = EmailMultiAlternatives(subject, body, from_email, to, reply_to=reply_to, connection=backend)
        msg.attach_alternative(html, "text/html")
    else:
        msg = EmailMessage(subject, body, from_email, to, reply_to=reply_to, connection=backend)

    return msg.send(fail_silently=False)


def send_contacto(subject: str, body: str, to: list, html: str = None, reply_to: list = None) -> int:
    """Send an email from contactocirrus@ (responses to client contacts)."""
    s = get_settings()
    backend = get_contacto_backend()
    from_email = _from_email(s.contacto_email, s.contacto_display_name)

    if html:
        msg = EmailMultiAlternatives(subject, body, from_email, to, reply_to=reply_to, connection=backend)
        msg.attach_alternative(html, "text/html")
    else:
        msg = EmailMessage(subject, body, from_email, to, reply_to=reply_to, connection=backend)

    return msg.send(fail_silently=False)
