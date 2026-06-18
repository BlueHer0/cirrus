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
def enviar_reporte_corte_email(self, empresa_id, corte_tipo, dest_email=None, subject_prefix=None):
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

    # ── Subir PDF a MinIO + generar link temporal (7 días) ───────────────
    from django.core.signing import dumps as _token_dumps
    from core.services.storage_minio import upload_bytes as _upload_bytes

    _pdf_fname = (
        f"Cirrus_{empresa.rfc}_{corte_tipo}_"
        f"{fecha_inicio.strftime('%Y%m%d')}_{fecha_fin.strftime('%Y%m%d')}.pdf"
    )
    _minio_key = f"reportes/{empresa.rfc}/{fecha_inicio.strftime('%Y-%m')}/{_pdf_fname}"
    try:
        _upload_bytes(pdf_bytes, _minio_key, content_type="application/pdf")
    except Exception as _ue:
        logger.error("Error subiendo PDF a MinIO: %s", _ue)
        raise self.retry(exc=_ue, countdown=60)

    _dl_token = _token_dumps(
        {"key": _minio_key, "filename": _pdf_fname},
        salt="reporte_descarga_v1",
    )
    _site_url = getattr(settings, "SITE_URL", "https://cirrus.nubex.me")
    dl_url = f"{_site_url}/reportes/descargar/{_dl_token}/"
    logger.info("📁 PDF subido a MinIO: %s", _minio_key)

    # ── Display values for email ─────────────────────────────────────────
    import uuid as _uuid
    from html import escape as _he
    from cirrus.utils.formatters import fmt_mxn as _fmt

    en = _he(empresa.nombre)
    er = _he(empresa.rfc)
    resultado_fmt = _fmt(datos['resultado_fiscal'])
    iva_fmt = _fmt(abs(datos['iva_neto']))
    hs = datos['health_score']
    hl = _he(datos['health_score_label'])
    n_alertas = len(datos['alertas_activas'])

    lbl_resultado = 'Utilidad fiscal' if datos['resultado_fiscal'] >= 0 else 'Pérdida fiscal'
    lbl_iva = 'Saldo a favor SAT' if datos['iva_neto'] <= 0 else 'A pagar al SAT'
    lbl_alertas = ('Sin alertas activas' if n_alertas == 0
                   else '1 alerta activa' if n_alertas == 1
                   else f'{n_alertas} alertas activas')

    _hs_clr = {'verde': '#16a34a', 'ambar': '#d97706', 'rojo': '#dc2626'}
    clr_resultado = '#16a34a' if datos['resultado_fiscal'] >= 0 else '#dc2626'
    clr_iva       = '#16a34a' if datos['iva_neto'] <= 0 else '#d97706'
    clr_health    = _hs_clr.get(datos.get('health_score_color', ''), '#374151')
    clr_alertas   = ('#dc2626' if any(a.get('nivel') == 'rojo' for a in datos['alertas_activas'])
                     else '#d97706' if n_alertas > 0 else '#16a34a')

    # ── Subject ──────────────────────────────────────────────────────────
    asunto = f"Cirrus · Reporte {periodo_label} — {empresa.nombre}"
    if subject_prefix:
        asunto = f"[{subject_prefix}] {asunto}"

    # ── Message-ID único con dominio nubex.me (alineado con DKIM) ────────
    msg_id = (
        f"<cirrus.{empresa.rfc.lower()}.{corte_tipo}."
        f"{fecha_inicio.strftime('%Y%m%d')}.{_uuid.uuid4().hex[:10]}@nubex.me>"
    )

    # ── Plain text ────────────────────────────────────────────────────────
    _sep = "─" * 44
    cuerpo_text = (
        f"CIRRUS · Inteligencia Fiscal\n"
        f"{empresa.nombre} ({empresa.rfc})\n"
        f"Reporte: {periodo_label}\n"
        f"{_sep}\n"
        f"INDICADORES CLAVE — {sub_periodo}\n\n"
        f"Resultado Fiscal:  {resultado_fmt}  ({lbl_resultado})\n"
        f"IVA Neto:          {iva_fmt}  ({lbl_iva})\n"
        f"Health Score:      {hs}/100 ({datos['health_score_label']})\n"
        f"Alertas Activas:   {n_alertas}  ({lbl_alertas})\n"
        f"{_sep}\n\n"
        f"REPORTE PDF DISPONIBLE\n"
        f"{dl_url}\n"
        f"Contenido: análisis fiscal, IVA, proveedores, nómina, historial 6 meses.\n"
        f"El enlace caduca en 7 días.\n\n"
        f"Correo automático · Cirrus · cirrus.nubex.me\n"
        f"Para cancelar: cirrus-reportes@nubex.me  asunto: unsubscribe"
    )

    # ── HTML body (tabla para compatibilidad email) ───────────────────────
    _kpi_cell = (
        "background:#f8fafc;border:1px solid #e2e8f0;border-radius:6px;"
        "padding:12px 14px;vertical-align:top;"
    )
    cuerpo_html = f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Reporte {periodo_label}</title>
</head>
<body style="margin:0;padding:0;background:#f1f5f9;font-family:Arial,Helvetica,sans-serif;font-size:14px;color:#1e293b;">
<table role="presentation" cellspacing="0" cellpadding="0" border="0" width="100%" style="background:#f1f5f9;">
<tr><td align="center" style="padding:28px 12px;">
<table role="presentation" cellspacing="0" cellpadding="0" border="0" width="560"
       style="max-width:560px;background:#fff;border-radius:8px;border:1px solid #e2e8f0;">

<!-- HEADER -->
<tr><td style="background:#0f172a;padding:20px 26px;border-radius:8px 8px 0 0;">
  <p style="margin:0;font-size:10px;letter-spacing:2px;color:#475569;text-transform:uppercase;font-weight:700;">CIRRUS &middot; INTELIGENCIA FISCAL</p>
  <p style="margin:5px 0 0;font-size:19px;font-weight:800;color:#f8fafc;line-height:1.2;">{en}</p>
  <p style="margin:4px 0 0;font-size:12px;color:#64748b;">{er} &nbsp;&middot;&nbsp; {periodo_label}</p>
</td></tr>

<!-- INTRO -->
<tr><td style="padding:20px 26px 10px;">
  <p style="margin:0;font-size:14px;color:#374151;line-height:1.65;">
    Se adjunta el reporte fiscal de <strong>{en}</strong> para el periodo
    <strong>{sub_periodo}</strong>. A continuaci&oacute;n los indicadores clave:
  </p>
</td></tr>

<!-- KPIs — fila 1 -->
<tr><td style="padding:0 26px 0;">
<table role="presentation" cellspacing="0" cellpadding="0" border="0" width="100%">
<tr>
  <td width="49%" style="{_kpi_cell}">
    <p style="margin:0 0 2px;font-size:9px;color:#64748b;font-weight:700;text-transform:uppercase;letter-spacing:0.8px;">Resultado Fiscal</p>
    <p style="margin:0;font-size:16px;font-weight:800;font-family:'Courier New',monospace;color:{clr_resultado};">{resultado_fmt}</p>
    <p style="margin:3px 0 0;font-size:11px;color:#94a3b8;">{lbl_resultado}</p>
  </td>
  <td width="2%"></td>
  <td width="49%" style="{_kpi_cell}">
    <p style="margin:0 0 2px;font-size:9px;color:#64748b;font-weight:700;text-transform:uppercase;letter-spacing:0.8px;">IVA Neto del Periodo</p>
    <p style="margin:0;font-size:16px;font-weight:800;font-family:'Courier New',monospace;color:{clr_iva};">{iva_fmt}</p>
    <p style="margin:3px 0 0;font-size:11px;color:#94a3b8;">{lbl_iva}</p>
  </td>
</tr>
<tr><td colspan="3" style="height:8px;"></td></tr>
<!-- KPIs — fila 2 -->
<tr>
  <td width="49%" style="{_kpi_cell}">
    <p style="margin:0 0 2px;font-size:9px;color:#64748b;font-weight:700;text-transform:uppercase;letter-spacing:0.8px;">Health Score Fiscal</p>
    <p style="margin:0;font-size:16px;font-weight:800;font-family:'Courier New',monospace;color:{clr_health};">{hs}/100</p>
    <p style="margin:3px 0 0;font-size:11px;color:#94a3b8;">{hl}</p>
  </td>
  <td width="2%"></td>
  <td width="49%" style="{_kpi_cell}">
    <p style="margin:0 0 2px;font-size:9px;color:#64748b;font-weight:700;text-transform:uppercase;letter-spacing:0.8px;">Alertas Activas</p>
    <p style="margin:0;font-size:16px;font-weight:800;font-family:'Courier New',monospace;color:{clr_alertas};">{n_alertas}</p>
    <p style="margin:3px 0 0;font-size:11px;color:#94a3b8;">{lbl_alertas}</p>
  </td>
</tr>
</table>
</td></tr>

<!-- DOWNLOAD LINK -->
<tr><td style="padding:16px 26px 20px;">
<table role="presentation" cellspacing="0" cellpadding="0" border="0" width="100%"
       style="background:#eff6ff;border-left:4px solid #3b82f6;border-radius:0 6px 6px 0;">
<tr><td style="padding:14px 16px;">
  <p style="margin:0 0 6px;font-size:13px;font-weight:700;color:#1d4ed8;">Reporte PDF disponible</p>
  <p style="margin:0 0 12px;font-size:12px;color:#374151;line-height:1.55;">
    An&aacute;lisis completo de 4 p&aacute;ginas: resultados, IVA, proveedores
    (Art.&nbsp;76 LISR), n&oacute;mina, historial 6 meses y acciones sugeridas.
  </p>
  <a href="{dl_url}"
     style="display:inline-block;background:#1d4ed8;color:#ffffff;text-decoration:none;
            font-size:13px;font-weight:700;padding:10px 22px;border-radius:6px;
            font-family:Arial,Helvetica,sans-serif;">
    &#x1F4C4;&nbsp; Descargar reporte PDF
  </a>
  <p style="margin:10px 0 0;font-size:11px;color:#64748b;">
    El enlace caduca en 7 d&iacute;as.
  </p>
</td></tr>
</table>
</td></tr>

<!-- FOOTER -->
<tr><td style="padding:14px 26px 20px;border-top:1px solid #e2e8f0;">
  <p style="margin:0 0 5px;font-size:11px;color:#94a3b8;line-height:1.5;">
    Correo generado autom&aacute;ticamente por <strong style="color:#64748b;">Cirrus &middot; cirrus.nubex.me</strong>.<br>
    An&aacute;lisis basado en CFDIs del SAT. No constituye opini&oacute;n fiscal contable.<br>
    Valida con tu contador antes de tomar decisiones con impacto fiscal.
  </p>
  <p style="margin:0;font-size:11px;color:#cbd5e1;">
    Para cancelar este reporte escribe a
    <a href="mailto:cirrus-reportes@nubex.me?subject=unsubscribe"
       style="color:#94a3b8;text-decoration:underline;">cirrus-reportes@nubex.me</a>
    con asunto &ldquo;unsubscribe&rdquo;.
  </p>
</td></tr>

</table>
</td></tr>
</table>
</body>
</html>"""

    msg = EmailMultiAlternatives(
        subject=asunto,
        body=cuerpo_text,
        from_email=settings.EMAIL_REPORTES_FROM,
        to=[dest_email],
        reply_to=["noreply@nubex.me"],
        headers={
            "List-Unsubscribe": "<mailto:cirrus-reportes@nubex.me?subject=unsubscribe>",
            "Message-ID": msg_id,
            "X-Mailer": "Cirrus/1.0",
        },
        connection=_get_reportes_connection(),
    )
    msg.attach_alternative(cuerpo_html, "text/html")
    # Sin adjunto — el PDF se descarga desde MinIO vía link temporal (7 días)

    try:
        sent = msg.send(fail_silently=False)
        logger.info(
            "📧 Reporte corte %s enviado a %s para %s (%s) — link: %s",
            corte_tipo, dest_email, empresa.rfc, periodo_label, dl_url,
        )
    except Exception as e:
        logger.error("Error enviando email reporte corte: %s", e)
        raise self.retry(exc=e, countdown=60)

    return (
        f"Reporte {periodo_label} enviado a {dest_email} para {empresa.rfc} "
        f"(sent={sent}, MinIO: {_minio_key})"
    )
