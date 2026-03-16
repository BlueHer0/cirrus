"""Intelligent scheduler for automated CFDI downloads.

Runs hourly via Celery Beat. For each active ScheduleConfig whose
proximo_scrape <= now, enqueues a download task with jitter.

Features:
- SAT peak avoidance: skips 11AM-3PM CST (17:00-21:00 UTC)
- Per-empresa jitter (ScheduleConfig.jitter_minutos)
- Rate limiting: 5-minute minimum gap between RFCs
- Automatic proximo_scrape recalculation
- Cross-year month range support
- Auto-init for new empresas with null proximo_scrape
"""

import logging
import random
from datetime import datetime, timedelta, timezone, time

from celery import shared_task
from django.db.models import Q

logger = logging.getLogger("core.scheduler")

# SAT peak hours (CST = UTC-6): 11:00-15:00 CST = 17:00-21:00 UTC
SAT_PEAK_START_UTC = time(17, 0)
SAT_PEAK_END_UTC = time(21, 0)

# Minimum gap between downloads of different RFCs (seconds)
RATE_LIMIT_SECONDS = 300  # 5 minutes


@shared_task(bind=True, max_retries=0)
def programar_descargas_del_dia(self):
    """Hourly task: check all ScheduleConfigs and enqueue due downloads.

    Logic:
    1. Initialize proximo_scrape for new empresas (null → next slot)
    2. Skip if we're in SAT peak hours (11AM-3PM CST)
    3. Find all active ScheduleConfigs with proximo_scrape <= now
    4. For each, enqueue descargar_cfdis with jitter delay
    5. Recalculate proximo_scrape
    """
    from core.models import ScheduleConfig

    now = datetime.now(timezone.utc)

    # 1. Initialize proximo_scrape for new empresas
    initialized = _init_missing_proximo_scrape(now)
    if initialized:
        logger.info("🆕 Initialized proximo_scrape for %d empresas", initialized)

    # 2. Check SAT peak hours
    if _is_sat_peak(now):
        logger.info("⏸️ SAT peak hours (11AM-3PM CST), skipping scheduling")
        return {"status": "skipped", "reason": "SAT peak hours", "initialized": initialized}

    # 3. Find due schedules
    due_schedules = ScheduleConfig.objects.filter(
        activo=True,
        empresa__descarga_activa=True,
        empresa__proximo_scrape__lte=now,
    ).select_related("empresa")

    if not due_schedules.exists():
        logger.debug("📋 No schedules due at %s", now.isoformat())
        return {"status": "ok", "queued": 0, "initialized": initialized}

    queued = 0
    base_delay = 0  # seconds — incremented for rate limiting

    for schedule in due_schedules:
        empresa = schedule.empresa

        # Skip if FIEL not configured
        if not empresa.fiel_cer_key or not empresa.fiel_key_key:
            logger.warning("⚠️ %s: FIEL not configured, skipping", empresa.rfc)
            continue

        # 4. Calculate jitter + rate limit delay
        jitter_secs = random.randint(0, schedule.jitter_minutos * 60)
        total_delay = base_delay + jitter_secs

        # Build download params with cross-year support
        params_list = _build_download_params(schedule, now)

        from core.tasks import descargar_cfdis
        for params in params_list:
            descargar_cfdis.apply_async(
                args=[str(empresa.id)],
                kwargs={"params": params, "triggered_by": "schedule"},
                countdown=total_delay,
            )

            logger.info(
                "📅 Queued download for %s: year=%d months=%d-%d in %ds "
                "(jitter=%ds, rate=%ds)",
                empresa.rfc, params["year"], params["month_start"],
                params["month_end"], total_delay, jitter_secs, base_delay,
            )

            # Stagger multiple param sets for same empresa by 2 minutes
            total_delay += 120

        # 5. Recalculate proximo_scrape
        nuevo_proximo = calcular_proximo_scrape(schedule, now)
        empresa.proximo_scrape = nuevo_proximo
        empresa.save(update_fields=["proximo_scrape"])

        logger.info("📆 %s: próximo scrape = %s", empresa.rfc, nuevo_proximo.isoformat())

        # Rate limiting: add gap for next RFC
        base_delay += RATE_LIMIT_SECONDS
        queued += 1

    logger.info("✅ Scheduled %d downloads", queued)
    return {"status": "ok", "queued": queued, "initialized": initialized}


def _build_download_params(schedule, now: datetime) -> list[dict]:
    """Build download parameter dicts, handling cross-year boundaries.

    If meses_atras crosses a year boundary (e.g., meses_atras=2 in January),
    returns two param sets: one for previous year, one for current year.

    Returns:
        List of param dicts, each with year/month_start/month_end/tipos.
    """
    meses_atras = schedule.meses_atras
    tipos = ["recibidos", "emitidos"]

    if meses_atras > now.month:
        # Cross-year: e.g., meses_atras=2, now.month=1
        # → Dec of prev year + Jan of current year
        prev_month_start = 12 - (meses_atras - now.month) + 1
        params_list = []

        # Previous year portion
        if prev_month_start <= 12:
            params_list.append({
                "year": now.year - 1,
                "month_start": prev_month_start,
                "month_end": 12,
                "tipos": tipos,
            })

        # Current year portion (if we're past January)
        if now.month >= 1:
            params_list.append({
                "year": now.year,
                "month_start": 1,
                "month_end": now.month,
                "tipos": tipos,
            })

        return params_list
    else:
        # Normal: all months in the same year
        month_start = now.month - meses_atras + 1
        return [{
            "year": now.year,
            "month_start": month_start,
            "month_end": now.month,
            "tipos": tipos,
        }]


def _init_missing_proximo_scrape(now: datetime) -> int:
    """Initialize proximo_scrape for empresas with active schedule but null proximo_scrape.

    This handles new empresas that were just onboarded. Sets proximo_scrape
    to the next valid slot based on their ScheduleConfig.

    Returns:
        Number of empresas initialized.
    """
    from core.models import ScheduleConfig

    missing = ScheduleConfig.objects.filter(
        activo=True,
        empresa__descarga_activa=True,
        empresa__proximo_scrape__isnull=True,
    ).select_related("empresa")

    count = 0
    for schedule in missing:
        proximo = calcular_proximo_scrape(schedule, now)
        schedule.empresa.proximo_scrape = proximo
        schedule.empresa.save(update_fields=["proximo_scrape"])
        logger.info(
            "🆕 Initialized proximo_scrape for %s: %s",
            schedule.empresa.rfc, proximo.isoformat(),
        )
        count += 1

    return count


def _is_sat_peak(now: datetime) -> bool:
    """Check if current time is in SAT peak hours (11AM-3PM CST = 17:00-21:00 UTC)."""
    return SAT_PEAK_START_UTC <= now.time() <= SAT_PEAK_END_UTC


def calcular_proximo_scrape(schedule, now: datetime) -> datetime:
    """Calculate the next scrape datetime based on frequency.

    Public function so it can be called from tasks.py after a successful download.

    Args:
        schedule: ScheduleConfig instance
        now: Current UTC datetime

    Returns:
        Next UTC datetime for the scrape
    """
    hora = schedule.hora_preferida
    base_date = now.date()

    freq = schedule.frecuencia

    if freq == "diaria":
        next_date = base_date + timedelta(days=1)

    elif freq == "semanal":
        # Next occurrence of dia_semana (0=Monday)
        target_day = schedule.dia_semana or 0
        days_ahead = target_day - base_date.weekday()
        if days_ahead <= 0:
            days_ahead += 7
        next_date = base_date + timedelta(days=days_ahead)

    elif freq == "quincenal":
        next_date = base_date + timedelta(days=15)

    elif freq == "mensual":
        # Same day next month
        if base_date.month == 12:
            next_date = base_date.replace(year=base_date.year + 1, month=1)
        else:
            try:
                next_date = base_date.replace(month=base_date.month + 1)
            except ValueError:
                # Handle months with fewer days (e.g., Jan 31 → Feb 28)
                next_date = base_date.replace(
                    month=base_date.month + 1, day=28
                )
    else:
        next_date = base_date + timedelta(days=7)  # fallback

    # Combine date + preferred hour
    proximo = datetime.combine(next_date, hora, tzinfo=timezone.utc)

    # Add jitter to avoid all empresas firing at the same second
    jitter = timedelta(minutes=random.randint(0, schedule.jitter_minutos))
    proximo += jitter

    # If the calculated time falls in SAT peak, shift to after peak
    if _is_sat_peak(proximo):
        proximo = proximo.replace(hour=21, minute=random.randint(5, 55))

    return proximo
