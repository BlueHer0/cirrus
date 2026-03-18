"""Celery tasks for Cirrus.

Task inventory:
- descargar_cfdis: Full RPA download pipeline for an empresa
- verificar_fiel: Test FIEL login against SAT portal
- health_check_playwright: Verify Playwright can launch Chromium
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from celery import shared_task

logger = logging.getLogger("core.tasks")


@shared_task(
    bind=True,
    max_retries=10,
    soft_time_limit=1800,
    time_limit=2100,
    acks_late=True,
    reject_on_worker_lost=True,
)
def descargar_cfdis(self, empresa_id: str, params: dict | None = None,
                    triggered_by: str = "api", descarga_log_id: str = ""):
    """Celery task: download CFDIs from SAT for an empresa.

    Aggressive retry policy:
    - Up to 10 retries with escalating delays (2min → 1hr)
    - acks_late: task only ACKed after completion (survives worker restart)
    - reject_on_worker_lost: re-queued if worker crashes
    - Log estado stays 'ejecutando' during retries — client only sees 'En proceso'
    - Only set estado='error' after all 10 retries exhausted
    """
    from core.models import Empresa, DescargaLog
    from core.services.scrapper import ejecutar_descarga
    from core.services.alerts import send_telegram

    try:
        empresa = Empresa.objects.get(id=empresa_id)
    except Empresa.DoesNotExist:
        logger.error("Empresa %s not found", empresa_id)
        return {"error": f"Empresa {empresa_id} not found"}

    # Default params
    now = datetime.now()
    year = (params or {}).get("year", now.year)
    month_start = (params or {}).get("month_start", 1)
    month_end = (params or {}).get("month_end", 12)
    tipos = (params or {}).get("tipos", ["recibidos", "emitidos"])

    # Reuse existing DescargaLog on retries, or create new one
    log = None
    if descarga_log_id:
        try:
            log = DescargaLog.objects.get(id=descarga_log_id)
        except DescargaLog.DoesNotExist:
            pass

    if log is None:
        log = DescargaLog.objects.create(
            empresa=empresa,
            estado="ejecutando",
            year=year,
            month_start=month_start,
            month_end=month_end,
            tipos=tipos,
            celery_task_id=self.request.id or "",
            triggered_by=triggered_by,
            iniciado_at=datetime.now(timezone.utc),
        )
        # Removed: "descarga iniciada" telegram — too noisy
        logger.info("📥 Descarga iniciada: %s (%s-%s a %s)", empresa.rfc, year, month_start, month_end)
    else:
        # Update for retry
        log.estado = "ejecutando"
        log.celery_task_id = self.request.id or ""
        log.progreso = f"Reintento {self.request.retries}/10 en curso..."
        log.save(update_fields=["estado", "celery_task_id", "progreso"])

    retry_num = self.request.retries

    # ── Download dedup: skip if same RFC+period already downloaded ──
    if retry_num == 0:
        existing = DescargaLog.objects.filter(
            empresa__rfc=empresa.rfc,
            year=year,
            month_start__lte=month_start,
            month_end__gte=month_end,
            estado="completado",
        ).exclude(id=log.id).exists()

        if existing:
            from core.models import CFDI
            cfdi_count = CFDI.objects.filter(rfc_empresa=empresa.rfc).count()
            log.estado = "completado"
            log.progreso = ""
            log.cfdis_nuevos = 0
            log.cfdis_descargados = cfdi_count
            log.completado_at = datetime.now(timezone.utc)
            log.duracion_segundos = 0
            log.save()
            # Removed: "descarga omitida" telegram — too noisy
            logger.info("⏭️ Download skipped for %s — data already exists", empresa.rfc)
            return {"skipped": True, "existing_cfdis": cfdi_count}

    try:
        result = ejecutar_descarga(empresa, log)

        # ── Success ──
        log.estado = "completado"
        log.cfdis_descargados = result.total_files
        log.cfdis_nuevos = result.total_cfdis
        log.errores = result.errors
        log.completado_at = datetime.now(timezone.utc)
        log.progreso = ""
        if log.iniciado_at:
            log.duracion_segundos = int(
                (log.completado_at - log.iniciado_at).total_seconds()
            )
        log.save(update_fields=[
            "estado", "cfdis_descargados", "cfdis_nuevos",
            "errores", "completado_at", "duracion_segundos", "progreso",
        ])

        empresa.ultimo_scrape = datetime.now(timezone.utc)
        empresa.save(update_fields=["ultimo_scrape"])
        _update_proximo_scrape(empresa)

        send_telegram(
            f"Descarga OK: *{empresa.rfc}* — {result.total_cfdis} CFDIs nuevos "
            f"({log.duracion_segundos}s)\n"
            f"{_telemetry_for_telegram(log)}",
            "success",
        )

        # Email client
        _send_client_email_success(empresa, log)

        logger.info("✅ Task descargar_cfdis complete for %s", empresa.rfc)
        return result.summary()

    except Exception as exc:
        logger.error(
            "❌ Task descargar_cfdis failed for %s (attempt %d/10): %s",
            empresa.rfc, retry_num + 1, exc,
        )

        # Smart retry delays based on error type
        error_str = str(exc).lower()
        if "login fallido" in error_str or "sesión activa" in error_str:
            # SAT login failure — wait 30 min (SAT saturated)
            delay = 1800
        elif "timeout" in error_str or "connection" in error_str:
            # Network issue — wait 15 min
            delay = 900
        else:
            # Other errors — escalating delays
            delays = [300, 600, 1200, 1800, 3600, 3600, 3600, 3600, 3600, 3600]
            delay = delays[min(retry_num, len(delays) - 1)]

        # Append error to log but do NOT set estado='error'
        errors = log.errores or []
        errors.append(f"Intento {retry_num + 1}: {str(exc)[:200]}")
        log.errores = errors
        log.progreso = f"Reintentando en {delay // 60} min (intento {retry_num + 1}/10)"
        log.save(update_fields=["errores", "progreso"])

        if retry_num >= self.max_retries:
            # ── Definitive failure ──
            log.estado = "error"
            log.progreso = "No se pudo completar después de 10 intentos"
            log.completado_at = datetime.now(timezone.utc)
            if log.iniciado_at:
                log.duracion_segundos = int(
                    (log.completado_at - log.iniciado_at).total_seconds()
                )
            log.save(update_fields=[
                "estado", "progreso", "completado_at", "duracion_segundos",
            ])

            send_telegram(
                f"🔴 FALLÓ DEFINITIVAMENTE: *{empresa.rfc}*\n"
                f"10 intentos agotados\n`{str(exc)[:300]}`",
                "critical",
            )

            _send_client_email_failure(empresa, log)
            return {"error": "max retries exhausted"}
        else:
            logger.warning("⚠️ Reintento %d/10: %s — %s (delay=%dmin)", retry_num + 1, empresa.rfc, str(exc)[:200], delay // 60)
            raise self.retry(
                exc=exc,
                countdown=delay,
                kwargs={
                    "empresa_id": empresa_id,
                    "params": params,
                    "triggered_by": triggered_by,
                    "descarga_log_id": str(log.id),
                },
            )


@shared_task(bind=True, soft_time_limit=300, time_limit=360, max_retries=5)
def verificar_fiel(self, empresa_id: str):
    """Celery task: verify FIEL credentials.

    Tries SAT portal login first. Falls back to local crypto validation.
    Updates fiel_status and auto-activates sync on success.
    """
    from django.conf import settings as django_settings
    from django.core.mail import send_mail
    from core.models import Empresa
    from core.services.fiel_encryption import verify_fiel_sat, get_fiel_for_scraping, validate_fiel_local
    from core.services.alerts import send_telegram

    try:
        empresa = Empresa.objects.get(id=empresa_id)
    except Empresa.DoesNotExist:
        return {"error": f"Empresa {empresa_id} not found"}

    # Mark as verifying
    empresa.fiel_status = "verificando"
    empresa.save(update_fields=["fiel_status"])

    def _on_success(method):
        """Handle successful verification."""
        empresa.fiel_verificada = True
        empresa.fiel_status = "verificada"
        empresa.fiel_verificada_at = datetime.now(timezone.utc)
        # Auto-activate sync
        if not empresa.sync_activa:
            empresa.sync_desde_year = 2025
            empresa.sync_desde_month = 1
            empresa.sync_activa = True
            empresa.sync_completada = False
        empresa.save()
        send_telegram(f"✅ FIEL verificada: *{empresa.rfc}* ({method})", "success")
        # Email to client
        try:
            send_mail(
                "Tu FIEL fue verificada — Cirrus",
                f"¡Buenas noticias! La FIEL de {empresa.rfc} fue verificada exitosamente.\n\n"
                f"Ya estamos descargando tus CFDIs automáticamente.\n"
                f"Consulta tu panel: https://cirrus.nubex.me/app/\n\nEquipo Cirrus",
                django_settings.DEFAULT_FROM_EMAIL,
                [empresa.owner.email],
                fail_silently=True,
            )
        except Exception:
            pass
        logger.info("✅ FIEL verified for %s (%s)", empresa.rfc, method)

    def _on_reject(reason):
        """Handle rejected FIEL."""
        empresa.fiel_verificada = False
        empresa.fiel_status = "rechazada"
        empresa.save(update_fields=["fiel_verificada", "fiel_status"])
        send_telegram(f"❌ FIEL rechazada: *{empresa.rfc}* — {reason}", "warning")
        try:
            send_mail(
                "Problema con tu FIEL — Cirrus",
                f"El SAT rechazó las credenciales FIEL de {empresa.rfc}.\n\n"
                f"Posibles causas:\n"
                f"- La contraseña es incorrecta\n"
                f"- La FIEL está revocada\n"
                f"- Los archivos .cer y .key no coinciden\n\n"
                f"Intenta subir tu FIEL de nuevo: https://cirrus.nubex.me/app/\n\nEquipo Cirrus",
                django_settings.DEFAULT_FROM_EMAIL,
                [empresa.owner.email],
                fail_silently=True,
            )
        except Exception:
            pass
        logger.warning("❌ FIEL rejected for %s: %s", empresa.rfc, reason)

    # Try SAT portal verification (browser-based)
    try:
        result = asyncio.run(verify_fiel_sat(empresa))
        logger.info("FIEL verification for %s: %s", empresa.rfc, result)
        if result.get("verified"):
            _on_success("SAT login OK")
            return result
        else:
            _on_reject(result.get("error", "SAT login failed"))
            return result
    except Exception as e:
        error_str = str(e).lower()
        if "timeout" in error_str or "connection" in error_str or "navegación" in error_str:
            # SAT unavailable — retry
            logger.warning("SAT unavailable for %s: %s, retrying...", empresa.rfc, e)
            raise self.retry(exc=e, countdown=300)
        elif "captcha" in error_str:
            # Bot detected — wait 1 hour
            logger.warning("CAPTCHA detected for %s, retrying in 1h", empresa.rfc)
            raise self.retry(exc=e, countdown=3600)
        else:
            logger.warning("SAT verification failed for %s (%s), trying local validation", empresa.rfc, e)

    # Fallback: local crypto validation
    if not empresa.fiel_cer_key or not empresa.fiel_key_key:
        _on_reject("FIEL not configured")
        return {"error": "FIEL not configured"}

    fiel_ctx = get_fiel_for_scraping(empresa)
    try:
        from pathlib import Path
        cer_data = Path(fiel_ctx["cer_path"]).read_bytes()
        key_data = Path(fiel_ctx["key_path"]).read_bytes()
        info = validate_fiel_local(cer_data, key_data, fiel_ctx["password"])

        if info.get("is_valid"):
            _on_success("validación local")
            return {"verified": True, "rfc": info.get("rfc"), "method": "local"}
        else:
            _on_reject("certificado inválido")
            return {"verified": False, "error": "FIEL invalid"}
    except Exception as e:
        _on_reject(str(e)[:200])
        return {"verified": False, "error": str(e)}
    finally:
        fiel_ctx["temp_dir"].cleanup()


@shared_task(bind=True, max_retries=5, soft_time_limit=600, time_limit=660)
def verificar_fiel_y_descargar_csf(self, empresa_id):
    """Verify FIEL + download CSF + parse + update empresa with official data.

    Full flow for new empresa registration:
    1. Verify FIEL against SAT (or local)
    2. Download Constancia de Situación Fiscal PDF
    3. Parse with Docling/pdfplumber
    4. Update empresa with official data
    5. Activate CFDI sync
    """
    from core.models import Empresa
    from core.services.fiel_encryption import get_fiel_for_scraping, validate_fiel_local
    from core.services.csf_scraper import descargar_csf
    from core.services.csf_parser import parsear_csf_con_docling
    from core.services.storage_minio import upload_bytes
    from core.services.monitor import log_info, log_error
    from core.services.alerts import send_telegram
    from pathlib import Path

    try:
        empresa = Empresa.objects.get(id=empresa_id)
    except Empresa.DoesNotExist:
        return {"error": f"Empresa {empresa_id} not found"}

    fiel_ctx = None
    try:
        # Step 1: Get FIEL files
        fiel_ctx = get_fiel_for_scraping(empresa)

        # Step 2: Verify FIEL locally first
        cer_data = Path(fiel_ctx["cer_path"]).read_bytes()
        key_data = Path(fiel_ctx["key_path"]).read_bytes()
        info = validate_fiel_local(cer_data, key_data, fiel_ctx["password"])

        if not info.get("is_valid"):
            empresa.fiel_status = "rechazada"
            empresa.save(update_fields=["fiel_status"])
            send_telegram(f"❌ FIEL inválida: {empresa.rfc}", "warning")
            return {"error": "FIEL invalid"}

        # Mark as verified
        empresa.fiel_verificada = True
        empresa.fiel_status = "verificada"
        empresa.fiel_verificada_at = datetime.now(timezone.utc)
        empresa.save(update_fields=["fiel_verificada", "fiel_status", "fiel_verificada_at"])
        log_info("fiel", f"FIEL verificada localmente: {empresa.rfc}")

        # Step 3: Download CSF from SAT
        try:
            csf_pdf_bytes = descargar_csf(
                cer_path=fiel_ctx["cer_path"],
                key_path=fiel_ctx["key_path"],
                password=fiel_ctx["password"],
            )
        except Exception as csf_err:
            # CSF download failed but FIEL is OK — activate sync anyway
            log_error("csf", f"CSF download failed for {empresa.rfc}: {csf_err}")
            empresa.sync_activa = True
            empresa.sync_desde_year = 2025
            empresa.sync_desde_month = 1
            empresa.save(update_fields=["sync_activa", "sync_desde_year", "sync_desde_month"])
            send_telegram(
                f"⚠️ FIEL OK pero CSF falló: {empresa.rfc}\n{str(csf_err)[:200]}",
                "warning",
            )
            return {"verified": True, "csf": False, "error": str(csf_err)}

        if not csf_pdf_bytes:
            raise Exception("CSF PDF empty")

        # Step 4: Save PDF to MinIO
        now = datetime.now()
        csf_key = f"csf/{empresa.rfc}/{now.year}-{now.month:02d}.pdf"
        upload_bytes(csf_pdf_bytes, csf_key, content_type="application/pdf")
        empresa.csf_minio_key = csf_key
        empresa.csf_ultima_descarga = datetime.now(timezone.utc)

        # Step 5: Parse CSF
        datos = parsear_csf_con_docling(csf_pdf_bytes)

        if datos:
            empresa.nombre = datos.get("razon_social", empresa.nombre)
            empresa.razon_social = datos.get("razon_social", "")
            empresa.regimen_fiscal = datos.get("regimen_fiscal", "")
            empresa.regimen_capital = datos.get("regimen_capital", "")
            empresa.nombre_comercial = datos.get("nombre_comercial", "")
            empresa.codigo_postal = datos.get("codigo_postal", "")
            empresa.direccion_calle = datos.get("calle", "")
            empresa.direccion_num_ext = datos.get("num_exterior", "")
            empresa.direccion_num_int = datos.get("num_interior", "")
            empresa.direccion_colonia = datos.get("colonia", "")
            empresa.direccion_localidad = datos.get("localidad", "")
            empresa.direccion_municipio = datos.get("municipio", "")
            empresa.direccion_estado = datos.get("estado", "")
            empresa.actividades_economicas = datos.get("actividades", [])
            # Parse fecha if string
            fecha_str = datos.get("fecha_inicio")
            if fecha_str:
                try:
                    from dateutil.parser import parse as parse_date
                    empresa.fecha_inicio_operaciones = parse_date(
                        fecha_str, dayfirst=True
                    ).date()
                except Exception:
                    pass
            empresa.estatus_padron = datos.get("estatus_padron", "")

        # Step 6: Activate sync
        empresa.sync_activa = True
        empresa.sync_desde_year = 2025
        empresa.sync_desde_month = 1
        empresa.save()

        log_info("csf", f"Empresa registrada con CSF: {empresa.rfc} — {empresa.nombre}")
        send_telegram(
            f"✅ Empresa registrada con CSF: {empresa.rfc}\n{empresa.nombre}",
            "success",
        )

        # Notify client
        from django.core.mail import send_mail
        from django.conf import settings as dj_settings

        send_mail(
            f"Tu empresa {empresa.rfc} está lista — Cirrus",
            f"¡Tu empresa fue registrada exitosamente!\n\n"
            f"RFC: {empresa.rfc}\n"
            f"Nombre: {empresa.nombre}\n\n"
            f"Ya estamos descargando tus CFDIs automáticamente.\n\n"
            f"— Equipo Cirrus",
            dj_settings.DEFAULT_FROM_EMAIL,
            [empresa.owner.email],
            fail_silently=True,
        )

        return {"verified": True, "csf": True, "nombre": empresa.nombre}

    except Exception as exc:
        error_str = str(exc)
        log_error("csf", f"Error CSF {empresa.rfc}: {error_str}")

        if "login" in error_str.lower() or "timeout" in error_str.lower():
            raise self.retry(exc=exc, countdown=300)

        empresa.fiel_status = "rechazada"
        empresa.save(update_fields=["fiel_status"])
        send_telegram(f"❌ CSF falló: {empresa.rfc} — {error_str[:200]}", "warning")
        return {"error": error_str}

    finally:
        if fiel_ctx:
            fiel_ctx["temp_dir"].cleanup()


@shared_task
def descargar_csf_mensual():
    """Download CSF for all active empresas. Runs on day 2 of each month."""
    from core.models import Empresa
    from core.services.monitor import log_info

    empresas = Empresa.objects.filter(fiel_verificada=True, sync_activa=True)
    count = empresas.count()
    log_info("csf", f"CSF mensual: {count} empresas a procesar")

    for emp in empresas:
        descargar_csf_empresa.delay(str(emp.id))

    return f"Enqueued {count} CSF downloads"


@shared_task(bind=True, max_retries=3, soft_time_limit=600, time_limit=660)
def descargar_csf_empresa(self, empresa_id):
    """Download & parse CSF for a single empresa (refresh)."""
    from core.models import Empresa
    from core.services.fiel_encryption import get_fiel_for_scraping
    from core.services.csf_scraper import descargar_csf
    from core.services.csf_parser import parsear_csf_con_docling
    from core.services.storage_minio import upload_bytes
    from core.services.monitor import log_info, log_error

    empresa = Empresa.objects.get(id=empresa_id)
    fiel_ctx = None

    try:
        fiel_ctx = get_fiel_for_scraping(empresa)

        csf_pdf_bytes = descargar_csf(
            cer_path=fiel_ctx["cer_path"],
            key_path=fiel_ctx["key_path"],
            password=fiel_ctx["password"],
        )

        if not csf_pdf_bytes:
            raise Exception("CSF PDF empty")

        now = datetime.now()
        csf_key = f"csf/{empresa.rfc}/{now.year}-{now.month:02d}.pdf"
        upload_bytes(csf_pdf_bytes, csf_key, content_type="application/pdf")

        datos = parsear_csf_con_docling(csf_pdf_bytes)
        if datos:
            empresa.razon_social = datos.get("razon_social", empresa.razon_social)
            empresa.regimen_fiscal = datos.get("regimen_fiscal", empresa.regimen_fiscal)
            empresa.estatus_padron = datos.get("estatus_padron", empresa.estatus_padron)

        empresa.csf_minio_key = csf_key
        empresa.csf_ultima_descarga = datetime.now(timezone.utc)
        empresa.save()

        log_info("csf", f"CSF actualizada: {empresa.rfc}")

    except Exception as exc:
        log_error("csf", f"CSF refresh failed {empresa.rfc}: {exc}")
        raise self.retry(exc=exc, countdown=600)

    finally:
        if fiel_ctx:
            fiel_ctx["temp_dir"].cleanup()


@shared_task(soft_time_limit=60, time_limit=90)
def health_check_playwright():
    """Verify that Playwright can launch Chromium. Runs every 15 minutes."""
    from core.services.monitor import log_info, log_critical
    from core.services.alerts import send_telegram

    async def _test():
        from playwright.async_api import async_playwright
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-setuid-sandbox"],
            )
            page = await browser.new_page()
            await page.goto("https://www.google.com", timeout=30000)
            title = await page.title()
            await browser.close()
            return title

    try:
        result = asyncio.run(_test())
        log_info("system", f"Playwright health OK: {result}")
        return {"ok": True, "title": result}
    except Exception as e:
        log_critical("system", f"Playwright health FAILED: {e}", detail=str(e))
        send_telegram(
            f"🔴 CRÍTICO: Playwright no puede lanzar browser\n`{str(e)[:300]}`",
            "critical",
        )
        return {"ok": False, "error": str(e)}


@shared_task(soft_time_limit=60, time_limit=90)
def agente_sincronizacion():
    """Sync agent: checks empresas needing downloads, queues next months.

    Runs every 15 minutes via Celery Beat.
    - Auto-cleans zombie downloads (ejecutando > 1 hour)
    - Night window: only downloads 10PM-4AM CST (04:00-10:00 UTC)
    - 10-min spacing between downloads of same RFC
    - Rotates RFCs (least recently downloaded first)
    - Respects worker capacity (max 3 concurrent)
    - Processes multiple empresas per cycle
    - Bypasses time/plan restrictions for first-time downloads
    """
    from core.models import Empresa, DescargaLog
    from core.services.monitor import log_info
    from django.db.models import F

    now = datetime.now()
    now_utc = datetime.now(timezone.utc)

    # PASO 0: Limpiar zombies (ejecutando > 1 hora)
    cutoff = now_utc - timedelta(hours=1)
    zombies = DescargaLog.objects.filter(
        estado="ejecutando", iniciado_at__lt=cutoff
    )
    zombie_count = zombies.count()
    if zombie_count > 0:
        zombies.update(estado="error", progreso="Zombie auto-limpiado por agente")
        log_info("system", f"Agente: {zombie_count} zombies limpiados")

    # PASO 0.5: Verificar ventana horaria
    if not _es_hora_optima():
        # Exception: check if any empresa has 0 downloads (first-time)
        tiene_primera = Empresa.objects.filter(
            sync_activa=True, fiel_verificada=True, sync_completada=False,
        ).exclude(
            id__in=DescargaLog.objects.filter(estado="completado").values("empresa_id")
        ).exists()
        if not tiene_primera:
            return "Fuera de horario óptimo, esperando madrugada"

    # PASO 1: Contar ejecuciones reales (< 1 hora)
    ejecutando = DescargaLog.objects.filter(
        estado="ejecutando", iniciado_at__gte=cutoff
    ).count()

    if ejecutando >= 3:
        return f"Workers llenos ({ejecutando} ejecutando)"

    slots = 3 - ejecutando

    # PASO 2: Empresas por prioridad de plan + RFC rotation (ultimo_scrape)
    empresas = Empresa.objects.filter(
        sync_activa=True,
        fiel_verificada=True,
        sync_completada=False,
    ).select_related("owner").order_by(F("ultimo_scrape").asc(nulls_first=True))

    def _plan_priority(emp):
        try:
            plan = emp.owner.perfil.get_plan()
            slug = plan.slug if plan else "free"
        except Exception:
            slug = "free"
        return {"owner": 0, "enterprise": 1, "pro": 2, "basico": 3, "free": 4}.get(slug, 4)

    sorted_empresas = sorted(empresas, key=_plan_priority)

    encoladas = 0

    for empresa in sorted_empresas:
        if encoladas >= slots:
            break

        try:
            plan = empresa.owner.perfil.get_plan()
        except Exception:
            plan = None

        # First-time bypass: if 0 completed downloads, allow anytime
        es_primera = not DescargaLog.objects.filter(
            empresa=empresa, estado="completado"
        ).exists()

        if not es_primera:
            # Night window check (already checked globally, but first-time bypass)
            if not _es_hora_optima():
                continue
            # Plan-based day check
            if not _decidir_si_descargar(empresa, plan, now):
                continue
            # 10-min spacing: skip if last download finished < 10 min ago
            ultima = DescargaLog.objects.filter(
                empresa=empresa, estado="completado"
            ).order_by("-completado_at").first()
            if ultima and ultima.completado_at:
                diff = (now_utc - ultima.completado_at).total_seconds()
                if diff < 600:  # 10 minutes
                    continue

        siguiente = _encontrar_siguiente_pendiente(empresa, now)
        if not siguiente:
            empresa.sync_completada = True
            empresa.save(update_fields=["sync_completada"])
            log_info("download", f"Sync completa: {empresa.rfc} — todos los meses descargados")
            continue

        year, month = siguiente

        # Don't queue if already ejecutando for this period
        ya_ejecutando = DescargaLog.objects.filter(
            empresa=empresa, year=year, month_start=month,
            estado="ejecutando",
        ).exists()
        if ya_ejecutando:
            continue

        # Queue only what's missing (recibidos and/or emitidos)
        queued_any = False
        for tipo in ["recibidos", "emitidos"]:
            ya_completado = DescargaLog.objects.filter(
                empresa=empresa, year=year, month_start=month,
                month_end=month, estado="completado",
            ).filter(tipos__contains=[tipo]).exists()

            if not ya_completado:
                descargar_cfdis.delay(
                    str(empresa.id),
                    params={
                        "year": year,
                        "month_start": month,
                        "month_end": month,
                        "tipos": [tipo],
                    },
                    triggered_by="schedule",
                )
                queued_any = True

        if queued_any:
            log_info("download", f"Agente encoló: {empresa.rfc} {year}-{month:02d}")
            encoladas += 1

    if encoladas == 0:
        return "Nada pendiente"
    return f"Encoladas: {encoladas} empresas"


def _es_hora_optima():
    """Solo descargar en ventana óptima: 10PM-4AM CST (04:00-10:00 UTC).
    También 22:00-23:59 UTC = 4-6PM CST (buenos resultados en telemetría).
    """
    hora_utc = datetime.now(timezone.utc).hour
    return (4 <= hora_utc <= 10) or hora_utc >= 22


def _decidir_si_descargar(empresa, plan, now):
    """Check if plan allows downloading today, using RFC hash for distribution."""
    slug = plan.slug if plan else "free"

    if slug == "free":
        return now.day <= 5
    elif slug == "basico":
        # Two windows: around day 10 and day 20
        ventana1 = abs(now.day - 10) <= 2  # days 8-12
        ventana2 = abs(now.day - 20) <= 2  # days 18-22
        return ventana1 or ventana2
    elif slug == "pro":
        # Weekly, fixed day per RFC
        dia_semana = hash(empresa.rfc) % 5  # mon(0) to fri(4)
        return now.weekday() == dia_semana
    elif slug in ("enterprise", "owner"):
        # Every 3 days, staggered by RFC
        dia_inicio = hash(empresa.rfc) % 3
        return (now.day - dia_inicio) % 3 == 0
    return False


def _encontrar_siguiente_pendiente(empresa, now):
    """Find next month needing download (most recent first).

    Checks recibidos and emitidos separately via JSON tipos field.
    """
    from core.models import DescargaLog

    if not empresa.sync_desde_year or not empresa.sync_desde_month:
        return None

    y = empresa.sync_desde_year
    m = empresa.sync_desde_month

    meses = []
    while (y < now.year) or (y == now.year and m <= now.month):
        meses.append((y, m))
        m += 1
        if m > 12:
            m = 1
            y += 1

    meses.reverse()

    for year, month in meses:
        rec = DescargaLog.objects.filter(
            empresa=empresa, year=year, month_start=month,
            estado="completado",
        ).filter(tipos__contains=["recibidos"]).exists()
        emi = DescargaLog.objects.filter(
            empresa=empresa, year=year, month_start=month,
            estado="completado",
        ).filter(tipos__contains=["emitidos"]).exists()
        if not rec or not emi:
            return (year, month)

    return None


def _update_proximo_scrape(empresa):
    """Recalculate and save proximo_scrape after a successful download."""
    try:
        schedule = empresa.schedule  # OneToOneField reverse
    except Exception:
        return

    if not schedule.activo:
        return

    from core.services.scheduler import calcular_proximo_scrape

    now = datetime.now(timezone.utc)
    nuevo_proximo = calcular_proximo_scrape(schedule, now)
    empresa.proximo_scrape = nuevo_proximo
    empresa.save(update_fields=["proximo_scrape"])

    logger.info(
        "📆 Updated proximo_scrape for %s: %s",
        empresa.rfc, nuevo_proximo.isoformat(),
    )


def _telemetry_for_telegram(descarga_log):
    """Format telemetry breakdown for Telegram alert."""
    try:
        from core.services.telemetry import format_telegram_telemetry
        return format_telegram_telemetry(descarga_log)
    except Exception:
        return ""


def _send_client_email_success(empresa, descarga_log):
    """Send email to client when download completes successfully."""
    try:
        from django.core.mail import send_mail
        from django.conf import settings

        user = empresa.owner
        if not user or not user.email:
            return

        periodo = f"{descarga_log.year}/{descarga_log.month_start:02d}-{descarga_log.month_end:02d}"
        send_mail(
            subject=f"Tus CFDIs están listos — {empresa.rfc}",
            message=(
                f"Hola {user.first_name or user.email},\n\n"
                f"Descargamos {descarga_log.cfdis_nuevos} CFDIs de {empresa.rfc} "
                f"del periodo {periodo}.\n\n"
                f"Ya puedes verlos en tu panel:\n"
                f"https://cirrus.nubex.me/app/cfdis/\n\n"
                f"Saludos,\nEquipo Cirrus"
            ),
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[user.email],
            fail_silently=True,
        )
        logger.info("📧 Email sent to %s: download complete", user.email)
    except Exception as e:
        logger.warning("Failed to send success email: %s", e)


def _send_client_email_failure(empresa, descarga_log):
    """Send friendly email to client when download fails definitively."""
    try:
        from django.core.mail import send_mail
        from django.conf import settings

        user = empresa.owner
        if not user or not user.email:
            return

        send_mail(
            subject=f"Actualización sobre tu descarga — {empresa.rfc}",
            message=(
                f"Hola {user.first_name or user.email},\n\n"
                f"Tuvimos dificultades descargando tus CFDIs del SAT para {empresa.rfc}.\n\n"
                f"Nuestro equipo fue notificado y estamos trabajando en resolverlo.\n"
                f"Puedes intentar de nuevo más tarde desde tu panel:\n"
                f"https://cirrus.nubex.me/app/descargas/\n\n"
                f"Disculpa las molestias.\n"
                f"Equipo Cirrus"
            ),
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[user.email],
            fail_silently=True,
        )
        logger.info("📧 Email sent to %s: download failed", user.email)
    except Exception as e:
        logger.warning("Failed to send failure email: %s", e)


@shared_task
def benchmark_hourly_report():
    """Send hourly benchmark summary to Telegram."""
    from core.models import DescargaLog, CFDI, Empresa
    from core.services.alerts import send_telegram
    from datetime import timedelta

    now = datetime.now(timezone.utc)
    last_hour = now - timedelta(hours=1)

    completados = DescargaLog.objects.filter(
        estado="completado", completado_at__gte=last_hour).count()
    errores = DescargaLog.objects.filter(
        estado="error", completado_at__gte=last_hour).count()
    omitidos = DescargaLog.objects.filter(
        estado="completado", cfdis_nuevos=0, completado_at__gte=last_hour).count()
    pendientes = DescargaLog.objects.filter(estado="pendiente").count()
    ejecutando = DescargaLog.objects.filter(estado="ejecutando").count()
    total_cfdis = CFDI.objects.count()
    empresas_count = Empresa.objects.filter(fiel_verificada=True).count()

    # Next scheduled download
    proximo = ""
    next_emp = Empresa.objects.filter(
        sync_activa=True, sync_completada=False, fiel_verificada=True
    ).order_by("ultimo_scrape").first()
    if next_emp:
        proximo = f"\nSiguiente: {next_emp.rfc}"

    # Suppress report if nothing happened
    if completados == 0 and errores == 0 and ejecutando == 0:
        return "Nada que reportar"

    send_telegram(
        f"📊 *Reporte horario CIRRUS*\n"
        f"Última hora: {completados} completadas, {errores} errores, {omitidos} omitidas\n"
        f"Total acumulado: {total_cfdis:,} CFDIs en {empresas_count} empresas\n"
        f"Workers: {ejecutando} activos, {pendientes} en cola"
        f"{proximo}",
        "info"
    )


@shared_task(queue="sistema")
def sync_efos_task():
    """Sincroniza la lista 69-B del SAT. Corre mensual."""
    from core.services.efos_sync import sync_efos
    return sync_efos()


@shared_task(queue="sistema")
def supervisor_cirrus():
    """Agente supervisor. Corre cada 15 min."""
    from core.services.supervisor import CirrusSupervisor
    sup = CirrusSupervisor()
    return sup.ejecutar()
