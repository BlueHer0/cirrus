"""Job Scheduler — Generates and manages DescargaJob queue.

Replaces the chaotic agente_sincronizacion with an ordered,
deterministic job queue.

RULES:
- unique_together prevents duplicate jobs (empresa + year + month + tipo)
- Jobs are ordered by prioridad ASC, programado_para ASC
- Only the worker picks up jobs — scheduler NEVER executes downloads
- Plan determines priority and date range
"""

import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("core.scheduler")

PRIORIDAD_MAP = {
    "owner": 1,
    "enterprise": 1,
    "pro": 3,
    "basico": 5,
    "free": 9,
}


def _get_plan_slug(empresa):
    """Get the plan slug for an empresa's owner."""
    try:
        plan = empresa.owner.perfil.get_plan()
        return plan.slug if plan else "free"
    except Exception:
        return "free"


def generar_jobs_iniciales(empresa):
    """Generate all download jobs for a new empresa.

    Called after FIEL verification. Creates jobs from sync_desde
    to current month, most recent first, 5-min spacing.
    """
    from core.models import DescargaJob

    slug = _get_plan_slug(empresa)
    prioridad = PRIORIDAD_MAP.get(slug, 9)
    now = datetime.now(timezone.utc)

    # Determine range based on plan
    start_y = empresa.sync_desde_year or 2025
    start_m = empresa.sync_desde_month or 1

    # End at previous month (current month not yet closed)
    if now.month == 1:
        end_y, end_m = now.year - 1, 12
    else:
        end_y, end_m = now.year, now.month - 1

    # Build list of months, most recent first
    meses = []
    y, m = start_y, start_m
    while (y < end_y) or (y == end_y and m <= end_m):
        meses.append((y, m))
        m += 1
        if m > 12:
            m = 1
            y += 1

    meses.reverse()  # Most recent first

    delay_minutes = 0
    jobs_created = 0

    for year, month in meses:
        for tipo in ["recibidos", "emitidos"]:
            _, created = DescargaJob.objects.get_or_create(
                empresa=empresa,
                year=year,
                month=month,
                tipo=tipo,
                defaults={
                    "estado": "en_cola",
                    "prioridad": prioridad,
                    "programado_para": now + timedelta(minutes=delay_minutes),
                },
            )
            if created:
                jobs_created += 1
                delay_minutes += 5  # 5 min spacing

    logger.info(
        "Jobs created for %s: %d (plan=%s, priority=%d)",
        empresa.rfc, jobs_created, slug, prioridad,
    )
    return jobs_created


def generar_jobs_mensuales():
    """Generate jobs for the month that just closed.

    Runs on day 1 of each month. For each active empresa,
    creates 2 jobs (recibidos + emitidos) for the previous month.
    """
    from core.models import Empresa, DescargaJob

    now = datetime.now(timezone.utc)

    # Month that just closed
    if now.month == 1:
        closed_y, closed_m = now.year - 1, 12
    else:
        closed_y, closed_m = now.year, now.month - 1

    created_count = 0

    for empresa in Empresa.objects.filter(sync_activa=True, fiel_verificada=True):
        slug = _get_plan_slug(empresa)
        prioridad = PRIORIDAD_MAP.get(slug, 9)
        programado = _calcular_programacion(empresa, slug, now)

        for tipo in ["recibidos", "emitidos"]:
            _, created = DescargaJob.objects.get_or_create(
                empresa=empresa,
                year=closed_y,
                month=closed_m,
                tipo=tipo,
                defaults={
                    "estado": "en_cola",
                    "prioridad": prioridad,
                    "programado_para": programado,
                },
            )
            if created:
                created_count += 1
                logger.info(
                    "Monthly job: %s %d-%02d %s → %s",
                    empresa.rfc, closed_y, closed_m, tipo,
                    programado.strftime("%d/%m %H:%M"),
                )

    return created_count


def _calcular_programacion(empresa, slug, now):
    """Calculate when to schedule a download based on plan."""
    # Base: 4-10 UTC (10PM-4AM CST) — SAT is 18x faster
    hora_base = 4
    # Distribute by RFC hash to avoid thundering herd
    offset_horas = hash(empresa.rfc) % 6
    hora = hora_base + offset_horas

    if slug == "free":
        dia = 2
    elif slug == "basico":
        dia = 10
    elif slug == "pro":
        dia = 2
    elif slug in ("enterprise", "owner"):
        rfc_offset = hash(empresa.rfc) % 3
        dia = 1 + rfc_offset
    else:
        dia = 2

    try:
        fecha = datetime(now.year, now.month, dia, hora, 0, tzinfo=timezone.utc)
    except ValueError:
        fecha = datetime(now.year, now.month, 28, hora, 0, tzinfo=timezone.utc)

    return fecha
