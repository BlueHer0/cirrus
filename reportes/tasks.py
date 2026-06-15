"""
Reportes Tasks — Celery tasks for automated email reports.
"""

import logging
import calendar
from datetime import date

from celery import shared_task
from django.conf import settings

logger = logging.getLogger("reportes.tasks")


def _get_reportes_connection():
    """Return SMTP connection using the dedicated reports account.

    Uses EMAIL_REPORTES_USER/EMAIL_REPORTES_PASSWORD on the same host/port/SSL
    as the system account. Falls back to the default Django connection if
    the dedicated credentials are not configured.
    """
    from django.core.mail import get_connection
    if not settings.EMAIL_REPORTES_USER or not settings.EMAIL_REPORTES_PASSWORD:
        return None  # let Django use the default connection
    return get_connection(
        backend="django.core.mail.backends.smtp.EmailBackend",
        host=settings.EMAIL_HOST,
        port=settings.EMAIL_PORT,
        username=settings.EMAIL_REPORTES_USER,
        password=settings.EMAIL_REPORTES_PASSWORD,
        use_ssl=settings.EMAIL_USE_SSL,
        timeout=settings.EMAIL_TIMEOUT,
    )


@shared_task(bind=True, max_retries=2, soft_time_limit=120, time_limit=150)
def enviar_reporte_mensual_email(self, empresa_id, anio, mes):
    """
    Genera el reporte PDF del mes, llama a IA para el resumen,
    y envía el email al usuario dueño de la empresa.

    Se lanza automáticamente cuando DescargaJob confirma que
    se completó la descarga de un mes, o manualmente via trigger endpoint.
    """
    from core.models import Empresa
    from reportes.services import calcular_reporte, generar_resumen_ia, MONTH_NAMES

    try:
        empresa = Empresa.objects.get(id=empresa_id)
    except Empresa.DoesNotExist:
        logger.error("Empresa %s not found for email report", empresa_id)
        return f"Error: Empresa {empresa_id} not found"

    usuario = empresa.owner
    if not usuario or not usuario.email:
        return f"Skip: empresa {empresa.rfc} sin usuario/email"

    fecha_inicio = date(anio, mes, 1)
    fecha_fin = date(anio, mes, calendar.monthrange(anio, mes)[1])

    # 1. Calcular datos del reporte
    try:
        datos = calcular_reporte(str(empresa_id), fecha_inicio, fecha_fin, usuario)
    except Exception as e:
        logger.error("Error calculando reporte para %s: %s", empresa.rfc, e)
        return f"Error calculating report: {e}"

    if datos["cfdi_count"] == 0:
        return f"Skip: 0 CFDIs for {empresa.rfc} {anio}-{mes:02d}"

    # 2. Generar resumen IA
    try:
        datos["resumen_ia"] = generar_resumen_ia(datos)
    except Exception as e:
        logger.warning("IA failed for email report: %s", e)
        datos["resumen_ia"] = (
            f"En {datos['periodo_label']}, tu empresa registró "
            f"${datos['total_ingresos']:,.0f} en ingresos y "
            f"${datos['total_gastos']:,.0f} en gastos."
        )

    # 5. Construir URL del reporte online
    site_url = getattr(settings, "SITE_URL", "https://cirrus.nubex.me")
    url_reporte = (
        f"{site_url}/reportes/ver/"
        f"?empresa_id={empresa_id}&tipo=mes&anio={anio}&mes={mes}"
    )

    # 6. Enviar email
    from django.core.mail import EmailMultiAlternatives

    mes_label = MONTH_NAMES[mes] + f" {anio}"
    asunto = f"Resumen Ejecutivo {mes_label} — {empresa.nombre}"

    resumen_text = datos.get("resumen_ia", "")

    cuerpo_html = f"""
    <div style="font-family: 'Helvetica Neue', Arial, sans-serif; max-width: 600px; margin: 0 auto;">
      <div style="background: #0d1117; padding: 24px 28px; border-radius: 12px 12px 0 0;">
        <div style="display: flex; align-items: center; gap: 12px;">
          <div style="width: 36px; height: 36px; border-radius: 10px;
                      background: linear-gradient(135deg, #6366f1, #8b5cf6);
                      display: flex; align-items: center; justify-content: center;">
            <span style="color: white; font-size: 18px; font-weight: 700;">☁</span>
          </div>
          <div>
            <h2 style="color: #58a6ff; margin: 0; font-size: 18px;">Cirrus · Reporte Fiscal</h2>
            <p style="color: #8b949e; margin: 4px 0 0; font-size: 13px;">
              {empresa.nombre} · {mes_label}
            </p>
          </div>
        </div>
      </div>
      <div style="background: #f7f8fa; padding: 28px; border-radius: 0 0 12px 12px;">
        <div style="background: white; border-radius: 8px; padding: 20px; border: 1px solid #e5e7eb; margin-bottom: 20px;">
          <p style="color: #374151; font-size: 15px; line-height: 1.7; margin: 0;">
            {resumen_text}
          </p>
        </div>
        <div style="display: flex; gap: 12px;">
          <a href="{url_reporte}" style="display: inline-block;
             background: linear-gradient(135deg, #6366f1, #8b5cf6);
             color: white; padding: 12px 24px;
             border-radius: 8px; text-decoration: none; font-weight: 600; font-size: 14px;">
            Ver reporte completo →
          </a>
        </div>
        <p style="color: #9ca3af; font-size: 11px; margin-top: 24px; line-height: 1.5;">
          Cirrus · Inteligencia Fiscal · cirrus.nubex.me<br>
          Análisis basado en CFDIs del SAT. No constituye opinión fiscal.
        </p>
      </div>
    </div>
    """

    email = EmailMultiAlternatives(
        subject=asunto,
        body=resumen_text,
        from_email=settings.EMAIL_REPORTES_FROM,
        to=[usuario.email],
        connection=_get_reportes_connection(),
    )
    email.attach_alternative(cuerpo_html, "text/html")

    # (PDF removido para evitar bloqueos Anti-Phishing de iCloud)

    try:
        email.send()
        logger.info("📧 Reporte enviado a %s para %s %s", usuario.email, empresa.rfc, mes_label)
    except Exception as e:
        logger.error("Error enviando email reporte: %s", e)
        raise self.retry(exc=e, countdown=60)

    return f"Reporte enviado a {usuario.email} para {empresa.rfc} {mes_label}"


@shared_task(bind=True, max_retries=2, soft_time_limit=180, time_limit=240)
def generar_y_enviar_reporte_anual(self, empresa_id, anio, emails_extra=None, override_owner_email=None):
    """
    Genera el reporte PDF anual, llama a IA para el resumen,
    y envía el email a una lista de correos.
    """
    if emails_extra is None:
        emails_extra = []
        
    from core.models import Empresa
    from reportes.services import calcular_reporte, generar_resumen_ia

    try:
        empresa = Empresa.objects.get(id=empresa_id)
    except Empresa.DoesNotExist:
        logger.error("Empresa %s not found for email report", empresa_id)
        return f"Error: Empresa {empresa_id} not found"

    usuario = empresa.owner
    
    to_emails = []
    if override_owner_email:
        to_emails.append(override_owner_email)
    elif usuario and usuario.email:
        to_emails.append(usuario.email)
        
    for e in emails_extra:
        if e and e not in to_emails:
            to_emails.append(e)
            
    if not to_emails:
        return f"Skip: empresa {empresa.rfc} sin correos destino"

    fecha_inicio = date(anio, 1, 1)
    fecha_fin = date(anio, 12, 31)

    try:
        # Pasa el usuario original para validación de visibilidad de calcular_reporte()
        datos = calcular_reporte(str(empresa_id), fecha_inicio, fecha_fin, usuario)
    except Exception as e:
        logger.error("Error calculando reporte para %s: %s", empresa.rfc, e)
        return f"Error calculating report: {e}"

    if datos["cfdi_count"] == 0:
        return f"Skip: 0 CFDIs for {empresa.rfc} {anio}"

    try:
        datos["resumen_ia"] = generar_resumen_ia(datos)
    except Exception as e:
        logger.warning("IA failed for email report: %s", e)
        datos["resumen_ia"] = (
            f"En el año {anio}, tu empresa registró "
            f"${datos['total_ingresos']:,.0f} en ingresos y "
            f"${datos['total_gastos']:,.0f} en gastos."
        )

    site_url = getattr(settings, "SITE_URL", "https://cirrus.nubex.me")
    url_reporte = f"{site_url}/reportes/ver/?empresa_id={empresa_id}&tipo=anio&anio={anio}"

    from django.core.mail import EmailMultiAlternatives
    asunto = f"Resumen Ejecutivo Anual {anio} — {empresa.nombre}"
    resumen_text = datos.get("resumen_ia", "")

    cuerpo_html = f"""
    <div style="font-family: 'Helvetica Neue', Arial, sans-serif; max-width: 600px; margin: 0 auto;">
      <div style="background: #0d1117; padding: 24px 28px; border-radius: 12px 12px 0 0;">
        <div style="display: flex; align-items: center; gap: 12px;">
          <div style="width: 36px; height: 36px; border-radius: 10px;
                      background: linear-gradient(135deg, #6366f1, #8b5cf6);
                      display: flex; align-items: center; justify-content: center;">
            <span style="color: white; font-size: 18px; font-weight: 700;">☁</span>
          </div>
          <div>
            <h2 style="color: #58a6ff; margin: 0; font-size: 18px;">Cirrus · Reporte Fiscal Anual</h2>
            <p style="color: #8b949e; margin: 4px 0 0; font-size: 13px;">
              {empresa.nombre} · {anio}
            </p>
          </div>
        </div>
      </div>
      <div style="background: #f7f8fa; padding: 28px; border-radius: 0 0 12px 12px;">
        <div style="background: white; border-radius: 8px; padding: 20px; border: 1px solid #e5e7eb; margin-bottom: 20px;">
          <p style="color: #374151; font-size: 15px; line-height: 1.7; margin: 0;">
            {resumen_text}
          </p>
        </div>
        <div style="display: flex; gap: 12px;">
          <a href="{url_reporte}" style="display: inline-block;
             background: linear-gradient(135deg, #6366f1, #8b5cf6);
             color: white; padding: 12px 24px;
             border-radius: 8px; text-decoration: none; font-weight: 600; font-size: 14px;">
            Ver reporte completo →
          </a>
        </div>
        <p style="color: #9ca3af; font-size: 11px; margin-top: 24px; line-height: 1.5;">
          Cirrus · Inteligencia Fiscal · cirrus.nubex.me<br>
          Análisis basado en CFDIs del SAT. No constituye opinión fiscal.
        </p>
      </div>
    </div>
    """

    email = EmailMultiAlternatives(
        subject=asunto,
        body=resumen_text,
        from_email=settings.EMAIL_REPORTES_FROM,
        to=to_emails,
        connection=_get_reportes_connection(),
    )
    email.attach_alternative(cuerpo_html, "text/html")

    # (PDF removido para evitar bloqueos Anti-Phishing de iCloud)

    try:
        email.send()
        logger.info("📧 Reporte enviado a %s para %s %s", to_emails, empresa.rfc, anio)
    except Exception as e:
        logger.error("Error enviando email reporte: %s", e)
        if hasattr(self, 'request') and self.request and getattr(self.request, 'id', None):
            raise self.retry(exc=e, countdown=60)
        else:
            raise e

    return f"Reporte enviado a {to_emails} para {empresa.rfc} {anio}"
