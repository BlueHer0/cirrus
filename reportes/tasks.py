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


@shared_task(bind=True, max_retries=2, soft_time_limit=120, time_limit=150)
def enviar_reporte_corte_email(self, empresa_id, corte_tipo, dest_email=None):
    """Envia el PDF v4 del reporte de un periodo segun el tipo de corte.

    corte_tipo:
      - 'cierre_mes_anterior': dia 1 a ultimo dia del mes anterior (cierre)
      - 'avance_10': dia 1 al 10 del mes en curso
      - 'avance_20': dia 1 al 20 del mes en curso

    El periodo se calcula con timezone.localdate() en America/Mexico_City
    (configurado en settings.TIME_ZONE) para evitar bordes de dia por UTC.
    """
    from datetime import date, timedelta
    import calendar as _cal
    from django.template.loader import render_to_string
    from django.core.mail import EmailMultiAlternatives
    from django.utils import timezone
    from core.models import Empresa
    from reportes.services import calcular_reporte, MONTH_NAMES

    try:
        empresa = Empresa.objects.get(id=empresa_id)
    except Empresa.DoesNotExist:
        logger.error("Empresa %s no encontrada", empresa_id)
        return f"Error: empresa {empresa_id} no encontrada"

    usuario = empresa.owner
    if dest_email is None:
        dest_email = usuario.email if usuario else None
    if not dest_email:
        return f"Skip: sin destinatario para {empresa.rfc}"

    hoy = timezone.localdate()  # zona MX por settings.TIME_ZONE

    if corte_tipo == "cierre_mes_anterior":
        primer_dia_actual = hoy.replace(day=1)
        fecha_fin = primer_dia_actual - timedelta(days=1)
        fecha_inicio = fecha_fin.replace(day=1)
        periodo_label = f"Cierre {MONTH_NAMES[fecha_fin.month]} {fecha_fin.year}"
        sub_periodo = f"{MONTH_NAMES[fecha_fin.month]} {fecha_fin.year}"
    elif corte_tipo == "avance_10":
        fecha_inicio = hoy.replace(day=1)
        fecha_fin = hoy.replace(day=10)
        periodo_label = f"Avance 1-10 {MONTH_NAMES[hoy.month]} {hoy.year}"
        sub_periodo = f"{MONTH_NAMES[hoy.month]} {hoy.year} (1-10)"
    elif corte_tipo == "avance_20":
        fecha_inicio = hoy.replace(day=1)
        fecha_fin = hoy.replace(day=20)
        periodo_label = f"Avance 1-20 {MONTH_NAMES[hoy.month]} {hoy.year}"
        sub_periodo = f"{MONTH_NAMES[hoy.month]} {hoy.year} (1-20)"
    else:
        return f"Error: corte_tipo desconocido '{corte_tipo}'"

    try:
        datos = calcular_reporte(str(empresa_id), fecha_inicio, fecha_fin, usuario)
    except Exception as e:
        logger.error("Error calculando reporte VEN corte %s: %s", corte_tipo, e)
        raise self.retry(exc=e, countdown=60)

    # Render PDF v4
    try:
        from weasyprint import HTML
        html = render_to_string("reportes/reporte_pdf.html", {
            "datos": datos,
            "empresa": empresa,
            "fecha_generacion": timezone.localtime().strftime("%d/%m/%Y %H:%M"),
        })
        pdf_bytes = HTML(string=html).write_pdf()
    except Exception as e:
        logger.error("Error generando PDF: %s", e)
        raise self.retry(exc=e, countdown=60)

    # Subject + cuerpo
    asunto = f"Cirrus · Reporte {periodo_label} — {empresa.nombre}"
    cuerpo_text = (
        f"Reporte fiscal del periodo: {sub_periodo}\n"
        f"Empresa: {empresa.nombre} ({empresa.rfc})\n"
        f"Resultado fiscal: ${datos['resultado_fiscal']:,.2f}\n"
        f"IVA neto: ${datos['iva_neto']:,.2f}"
        f" ({'a favor' if datos['iva_neto'] < 0 else 'a pagar'})\n"
        f"Health Score: {datos['health_score']} ({datos['health_score_label']})\n"
        f"Alertas activas: {len(datos['alertas_activas'])}\n\n"
        f"Adjunto: PDF de 4 paginas con el detalle completo.\n\n"
        f"Correo automatico, no responder."
    )
    cuerpo_html = (
        f"<p>Reporte fiscal del periodo: <strong>{sub_periodo}</strong></p>"
        f"<p><strong>{empresa.nombre}</strong> ({empresa.rfc})</p>"
        f"<ul>"
        f"<li>Resultado fiscal: <strong>${datos['resultado_fiscal']:,.2f}</strong></li>"
        f"<li>IVA neto: <strong>${datos['iva_neto']:,.2f}</strong> "
        f"({'a favor' if datos['iva_neto'] < 0 else 'a pagar'})</li>"
        f"<li>Health Score: <strong>{datos['health_score']}</strong> "
        f"({datos['health_score_label']})</li>"
        f"<li>Alertas activas: <strong>{len(datos['alertas_activas'])}</strong></li>"
        f"</ul>"
        f"<p>Adjunto: PDF de 4 paginas con el detalle completo.</p>"
        f"<p style='color:#94a3b8;font-size:11px;'>Correo automatico, no responder.</p>"
    )

    fname = (
        f"Cirrus_{empresa.rfc}_{corte_tipo}_"
        f"{fecha_inicio.strftime('%Y%m%d')}_{fecha_fin.strftime('%Y%m%d')}.pdf"
    )

    msg = EmailMultiAlternatives(
        subject=asunto,
        body=cuerpo_text,
        from_email=settings.EMAIL_REPORTES_FROM,
        to=[dest_email],
        connection=_get_reportes_connection(),
    )
    msg.attach_alternative(cuerpo_html, "text/html")
    msg.attach(fname, pdf_bytes, "application/pdf")

    try:
        sent = msg.send(fail_silently=False)
        logger.info(
            "📧 Reporte corte %s enviado a %s para %s (%s)",
            corte_tipo, dest_email, empresa.rfc, periodo_label,
        )
    except Exception as e:
        logger.error("Error enviando email reporte corte: %s", e)
        raise self.retry(exc=e, countdown=60)

    return (
        f"Reporte {periodo_label} enviado a {dest_email} para {empresa.rfc} "
        f"(PDF {len(pdf_bytes)} bytes, sent={sent})"
    )
