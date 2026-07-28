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
from datetime import datetime, timedelta, timezone as dt_timezone, date
from django.utils import timezone

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


def _safe_programado(year, month, propuesto):
    """Devuelve un programado_para que respete: NUNCA correr antes del día 5
    del mes siguiente al mes de datos.

    Motivo: el SAT recibe timbres tardíos durante varios días después del cierre
    de mes; correr un job al instante de cerrar el mes devuelve 0 CFDIs y deja
    el job en estado completado para siempre, ignorando uploads posteriores
    (bug detectado may 2026). El task refetch_meses_recientes_vacios complementa
    para meses que ya estaban marcados completados con 0.
    """
    if month == 12:
        ny, nm = year + 1, 1
    else:
        ny, nm = year, month + 1
    no_antes = datetime(ny, nm, 5, 4, 0, tzinfo=dt_timezone.utc)
    return max(propuesto, no_antes)


def generar_jobs_iniciales(empresa):
    """Generate all download jobs for a new empresa.

    Called after FIEL verification. Creates jobs from sync_desde
    to current month, most recent first, 5-min spacing.
    """
    from core.models import DescargaJob

    slug = _get_plan_slug(empresa)
    prioridad = PRIORIDAD_MAP.get(slug, 9)
    now = datetime.now(dt_timezone.utc)

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
            programado = _safe_programado(
                year, month, now + timedelta(minutes=delay_minutes),
            )
            _, created = DescargaJob.objects.get_or_create(
                empresa=empresa,
                year=year,
                month=month,
                tipo=tipo,
                defaults={
                    "estado": "en_cola",
                    "prioridad": prioridad,
                    "programado_para": programado,
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

    now = datetime.now(dt_timezone.utc)

    # Month that just closed
    if now.month == 1:
        closed_y, closed_m = now.year - 1, 12
    else:
        closed_y, closed_m = now.year, now.month - 1

    created_count = 0

    for empresa in Empresa.objects.filter(sync_activa=True, fiel_verificada=True):
        slug = _get_plan_slug(empresa)
        prioridad = PRIORIDAD_MAP.get(slug, 9)
        programado = _safe_programado(
            closed_y, closed_m, _calcular_programacion(empresa, slug, now),
        )

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


def auditar_y_reparar_jobs(empresa):
    from core.models import DescargaJob, DescargaLog, CFDI
    if not hasattr(empresa, 'sync_activa') or not empresa.sync_activa or not empresa.fiel_verificada:
        return 0

    jobs_creados = 0
    hoy = date.today()
    
    # Fecha inicio según plan del usuario
    from accounts.models import ClienteProfile
    try:
        perfil = ClienteProfile.objects.get(user=empresa.owner)
        plan_obj = perfil.get_plan()
        plan = plan_obj.slug if plan_obj else 'free'
    except:
        plan = 'free'

    anio_actual = hoy.year
    if plan in ['pro', 'enterprise', 'owner']:
        inicio = date(anio_actual - 2, 1, 1)  # 3 años
    elif plan in ['basico', 'basic']:
        inicio = date(anio_actual - 1, 1, 1)  # 2 años
    else:
        inicio = date(anio_actual, 1, 1)       # solo año actual

    # OVERRIDE: si es el owner admin, siempre desde 2024
    if empresa.owner.is_staff or empresa.owner.is_superuser:
        inicio = date(2024, 1, 1)
    
    # Hasta el mes anterior al actual
    if hoy.month == 1:
        fin = date(hoy.year - 1, 12, 1)
    else:
        fin = date(hoy.year, hoy.month - 1, 1)
    
    from datetime import timedelta as _td
    RECHECK_COOLDOWN = _td(days=7)  # reintento semanal, no diario (evita churn RPA)
    ahora = timezone.now()

    def _mes_verificado_vacio_por_compulsa(year, month, tipo):
        """True si una compulsa SAT (metadata oficial) ya confirmó que ese
        mes/tipo NO tiene CFDIs en el SAT → mes resuelto, no re-scrapear."""
        from core.models import CompulsaSAT
        clave = f"{year}-{month:02d}"
        for c in CompulsaSAT.objects.filter(
            empresa=empresa, tipo=tipo, estado='completada',
        ).order_by('-creado_at')[:6]:
            r = (c.resultado or {}).get(clave)
            if r is not None:
                return r.get('sat_vigentes', -1) == 0
        return False

    def _reencolar(job, motivo):
        prog_safe = _safe_programado(job.year, job.month, timezone.now())
        job.estado = 'en_cola'
        job.intentos = 0
        job.programado_para = prog_safe
        job.ultimo_error = motivo
        job.save()

    mes = inicio
    while mes <= fin:
        for tipo in ['recibidos', 'emitidos']:
            tipo_rel = 'recibido' if tipo == 'recibidos' else 'emitido'
            count = CFDI.objects.filter(
                rfc_empresa=empresa.rfc,
                tipo_relacion=tipo_rel,
                fecha__year=mes.year,
                fecha__month=mes.month,
            ).count()

            prog_safe = _safe_programado(mes.year, mes.month, timezone.now())
            job, created = DescargaJob.objects.get_or_create(
                empresa=empresa,
                year=mes.year,
                month=mes.month,
                tipo=tipo,
                defaults={
                    'estado': 'en_cola',
                    'prioridad': 5,
                    'programado_para': prog_safe,
                    'intentos': 0,
                },
            )
            if created:
                jobs_creados += 1
                continue

            if job.estado not in ['error', 'completado', 'completado_vacio']:
                continue  # en_cola/ejecutando: en proceso, no tocar

            # Cooldown: si se consultó hace poco, esperar (antes era churn diario)
            if job.completado_at and (ahora - job.completado_at) < RECHECK_COOLDOWN:
                continue

            if count == 0:
                # Mes sin CFDIs de este tipo en BD.
                # Si la compulsa oficial ya dijo "SAT también tiene 0" → resuelto.
                if _mes_verificado_vacio_por_compulsa(mes.year, mes.month, tipo):
                    continue
                if job.cfdis_descargados > 0:
                    # Fix punto ciego 'paquete equivocado' (2026-07): el job
                    # reporta descargas pero NINGUN CFDI de este mes/tipo esta
                    # en BD → el paquete que lo marco completado era de OTRO
                    # periodo. Antes este caso era invisible (el refetch solo
                    # miraba cfdis_descargados==0).
                    _reencolar(job, 'auditor: completado con paquete de otro periodo (desc>0, 0 reales)')
                else:
                    _reencolar(job, 'auditor: mes sin CFDIs, sin compulsa que confirme vacio')
                jobs_creados += 1

        # Siguiente mes
        if mes.month == 12:
            mes = date(mes.year + 1, 1, 1)
        else:
            mes = date(mes.year, mes.month + 1, 1)

    return jobs_creados



def _calcular_programacion(empresa, slug, now):
    """Calculate when to schedule a download based on plan."""
    # Ventana 00:00-07:00 UTC (18:00-01:00 CST) — disponibilidad SAT 82-91%
    # según análisis mar-may 2026 (ver inteligencia/2026-05-analisis-monitoreo-sat.md).
    # Se evita la zona 09-13 UTC (03-07 CST) donde la disponibilidad cae a ~52%.
    hora_base = 0
    # Distribute by RFC hash to avoid thundering herd (0-7 UTC)
    offset_horas = hash(empresa.rfc) % 8
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
        fecha = datetime(now.year, now.month, dia, hora, 0, tzinfo=dt_timezone.utc)
    except ValueError:
        fecha = datetime(now.year, now.month, 28, hora, 0, tzinfo=dt_timezone.utc)

    return fecha
