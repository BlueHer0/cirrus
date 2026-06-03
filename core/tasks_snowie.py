"""Celery tasks para el flujo de captura de leads desde Snowie.ai.

Mantenido en archivo separado de core/tasks.py para no tocar el módulo
principal de tasks. Registrado vía cirrus/celery.py app.conf.include.

Tasks:
- notificar_telegram_snowie_lead: alerta al admin cuando llega un nuevo lead
- enviar_email_bienvenida_snowie: email de bienvenida/promo al lead
"""

import logging

from celery import shared_task

logger = logging.getLogger("core.tasks_snowie")


@shared_task(
    bind=True,
    max_retries=3,
    default_retry_delay=120,
)
def notificar_telegram_snowie_lead(self, lead_id: str):
    """Envía alerta al admin con los datos del nuevo lead Snowie."""
    from core.models import SnowieLead
    from core.services.alerts import send_telegram

    try:
        lead = SnowieLead.objects.get(id=lead_id)
    except SnowieLead.DoesNotExist:
        logger.error("SnowieLead %s no encontrado", lead_id)
        return {"error": "lead_not_found"}

    parts = ["*Nuevo lead Snowie* ❄️"]
    if lead.nombre:
        parts.append(f"👤 {lead.nombre}")
    if lead.email:
        parts.append(f"✉️ {lead.email}")
    if lead.telefono:
        parts.append(f"📱 {lead.telefono}")
    if lead.rfc_empresa:
        parts.append(f"🏢 {lead.rfc_empresa}")
    if lead.plan_interesado:
        parts.append(f"📦 Plan: *{lead.plan_interesado}*")
    if lead.summary:
        snippet = lead.summary[:200] + ("..." if len(lead.summary) > 200 else "")
        parts.append(f"\n_{snippet}_")
    parts.append(f"\n🆔 `{lead.session_id}`")

    msg = "\n".join(parts)

    try:
        sent = send_telegram(msg, level="warning", category="snowie_lead")
        if sent:
            lead.notificado_telegram = True
            lead.save(update_fields=["notificado_telegram", "actualizado_en"])
            return {"ok": True}
        else:
            logger.warning("Telegram no enviado para lead %s (probablemente disabled)", lead_id)
            return {"ok": False, "reason": "telegram_skipped"}
    except Exception as e:
        logger.error("Error notificando Telegram para lead %s: %s", lead_id, e)
        raise self.retry(exc=e)


@shared_task(
    bind=True,
    max_retries=3,
    default_retry_delay=300,
)
def enviar_email_bienvenida_snowie(self, lead_id: str):
    """Envía email de bienvenida/promo al lead capturado desde Snowie."""
    from core.models import SnowieLead
    from core.services.system_settings import send_noreply

    try:
        lead = SnowieLead.objects.get(id=lead_id)
    except SnowieLead.DoesNotExist:
        logger.error("SnowieLead %s no encontrado", lead_id)
        return {"error": "lead_not_found"}

    if not lead.email:
        logger.info("Lead %s sin email, skipping bienvenida", lead_id)
        return {"ok": False, "reason": "no_email"}

    nombre = lead.nombre or "Hola"
    plan_text = ""
    if lead.plan_interesado:
        plan_text = f"\n\nVi que te interesa nuestro plan **{lead.plan_interesado}** — está pensado exactamente para empresas como la tuya."

    body_text = f"""Hola {nombre},

Gracias por tu interés en Cirrus, la plataforma fiscal mexicana que descarga automáticamente tus CFDIs del SAT y los organiza para que puedas dejar de perder horas en el portal.{plan_text}

Algunas cosas que Cirrus hace por ti:
• Descarga automática de CFDIs (recibidos y emitidos) desde el SAT
• Sin más logins manuales — solo subes tu FIEL una vez
• Organización por mes, tipo, RFC, monto
• Análisis fiscal y reportes ejecutivos
• Alertas cuando algo necesita tu atención

Si quieres, te puedo agendar una demo de 15 minutos. Solo responde este correo y coordinamos.

Saludos,
Equipo Cirrus
https://cirrus.nubex.me
"""

    body_html = f"""<html><body style="font-family:system-ui,sans-serif;max-width:600px;margin:0 auto;padding:20px;color:#1f2937;">
<h2 style="color:#6366f1;">Hola {nombre},</h2>
<p>Gracias por tu interés en <strong>Cirrus</strong>, la plataforma fiscal mexicana que descarga automáticamente tus CFDIs del SAT y los organiza para que puedas dejar de perder horas en el portal.</p>
{f'<p>Vi que te interesa nuestro plan <strong>{lead.plan_interesado}</strong> — está pensado exactamente para empresas como la tuya.</p>' if lead.plan_interesado else ''}
<h3>Algunas cosas que Cirrus hace por ti:</h3>
<ul>
  <li>Descarga automática de CFDIs (recibidos y emitidos) desde el SAT</li>
  <li>Sin más logins manuales — solo subes tu FIEL una vez</li>
  <li>Organización por mes, tipo, RFC, monto</li>
  <li>Análisis fiscal y reportes ejecutivos</li>
  <li>Alertas cuando algo necesita tu atención</li>
</ul>
<p>Si quieres, te puedo agendar una demo de 15 minutos. Solo responde este correo y coordinamos.</p>
<p>Saludos,<br><strong>Equipo Cirrus</strong><br><a href="https://cirrus.nubex.me">cirrus.nubex.me</a></p>
</body></html>"""

    try:
        send_noreply(
            subject="Gracias por tu interés en Cirrus",
            body=body_text,
            to=[lead.email],
            html=body_html,
        )
        lead.email_enviado = True
        lead.save(update_fields=["email_enviado", "actualizado_en"])
        logger.info("Email bienvenida enviado a %s (lead %s)", lead.email, lead_id)
        return {"ok": True}
    except Exception as e:
        logger.error("Error enviando email a %s: %s", lead.email, e)
        raise self.retry(exc=e)
