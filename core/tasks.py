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
    from core.services.pipeline_manager import iniciar_pipeline, avanzar_paso, marcar_error
    from pathlib import Path

    try:
        empresa = Empresa.objects.get(id=empresa_id)
    except Empresa.DoesNotExist:
        return {"error": f"Empresa {empresa_id} not found"}

    pipeline = iniciar_pipeline(empresa, 'alta_empresa')
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
            marcar_error(pipeline.id, 'FIEL inválida criptográficamente', reintentable=False)
            return {"error": "FIEL invalid"}

        # Mark as verified
        empresa.fiel_verificada = True
        empresa.fiel_status = "verificada"
        empresa.fiel_verificada_at = datetime.now(timezone.utc)
        empresa.save(update_fields=["fiel_verificada", "fiel_status", "fiel_verificada_at"])
        log_info("fiel", f"FIEL verificada localmente: {empresa.rfc}")
        avanzar_paso(pipeline.id, 'FIEL válida')  # paso 1→2
        avanzar_paso(pipeline.id, f'FIEL verificada, expira {empresa.fiel_expira}')  # paso 2→3

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
            marcar_error(pipeline.id, f'CSF download: {csf_err}', reintentable=True)
            return {"verified": True, "csf": False, "error": str(csf_err)}

        if not csf_pdf_bytes:
            raise Exception("CSF PDF empty")

        # Step 4: Save PDF to MinIO
        now = datetime.now()
        csf_key = f"csf/{empresa.rfc}/{now.year}-{now.month:02d}.pdf"
        upload_bytes(csf_pdf_bytes, csf_key, content_type="application/pdf")
        empresa.csf_minio_key = csf_key
        empresa.csf_ultima_descarga = datetime.now(timezone.utc)
        avanzar_paso(pipeline.id, 'CSF descargada del SAT')  # paso 3→4

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
        avanzar_paso(pipeline.id, f'Datos extraídos: {empresa.razon_social or empresa.rfc}')  # paso 4→5

        # Step 6: Activate sync
        empresa.sync_activa = True
        empresa.sync_desde_year = 2025
        empresa.sync_desde_month = 1
        empresa.save()
        avanzar_paso(pipeline.id, 'Empresa actualizada con datos oficiales')  # paso 5→6

        # Step 7: Generate download jobs
        from core.services.job_scheduler import generar_jobs_iniciales
        jobs_count = generar_jobs_iniciales(empresa)
        log_info("csf", f"Empresa registrada con CSF: {empresa.rfc} — {empresa.nombre} ({jobs_count} jobs)")
        avanzar_paso(pipeline.id, f'{jobs_count} periodos programados')  # paso 6 → completado
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
            marcar_error(pipeline.id, f'SAT: {error_str[:200]}', reintentable=True)
            raise self.retry(exc=exc, countdown=300)

        empresa.fiel_status = "rechazada"
        empresa.save(update_fields=["fiel_status"])
        send_telegram(f"❌ CSF falló: {empresa.rfc} — {error_str[:200]}", "warning")
        marcar_error(pipeline.id, error_str[:200], reintentable=False)
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


# ── Temp FIEL Cleanup ────────────────────────────────────────────────


@shared_task(soft_time_limit=60, time_limit=90)
def limpiar_tmp_fiel():
    """Remove stale FIEL temp dirs older than 1 hour.

    Protects against FIEL files left on disk when workers die (OOM, SIGKILL).
    Runs every 30 minutes via Celery Beat.
    """
    import glob
    import os
    import time

    cutoff = time.time() - 3600  # 1 hour ago
    patterns = ["/tmp/cirrus_*", "/tmp/tmp*cirrus*"]
    removed = 0

    for pattern in patterns:
        for path in glob.glob(pattern):
            try:
                if os.path.getmtime(path) < cutoff:
                    if os.path.isdir(path):
                        import shutil
                        shutil.rmtree(path, ignore_errors=True)
                    else:
                        os.remove(path)
                    removed += 1
            except OSError:
                pass

    if removed > 0:
        logger.info("🧹 Limpieza /tmp: %d archivos/dirs FIEL eliminados", removed)
    return {"removed": removed}


# ── Job Queue Worker ──────────────────────────────────────────────────


@shared_task(soft_time_limit=600, time_limit=660)
def procesar_cola_descargas():
    """Process the next download job from the ordered queue.

    Runs every 5 minutes. Rules:
    1. Only processes 1 job per cycle
    2. Max 3 concurrent jobs
    3. Order: prioridad ASC, programado_para ASC
    4. NEVER creates jobs — only executes them
    5. On failure: increment intentos, reschedule with backoff
    """
    from core.models import DescargaJob, DescargaLog
    from core.services.monitor import log_info, log_error
    from core.services.alerts import send_telegram
    from core.services.pipeline_manager import iniciar_pipeline, avanzar_paso, marcar_error
    from django.db.models import F

    now = datetime.now(timezone.utc)

    # Clean zombies (ejecutando > 1 hour)
    zombies = DescargaJob.objects.filter(
        estado="ejecutando",
        iniciado_at__lt=now - timedelta(hours=1),
    )
    zombie_count = zombies.count()
    if zombie_count > 0:
        for z in zombies:
            z.estado = "error" if z.intentos >= z.max_intentos else "en_cola"
            z.ultimo_error = "Zombie — más de 1 hora ejecutando"
            if z.estado == "en_cola":
                z.programado_para = now + timedelta(minutes=10)
            z.save(update_fields=["estado", "ultimo_error", "programado_para"])
        log_info("system", f"Cola: {zombie_count} zombies limpiados")

    # Count real running jobs
    ejecutando = DescargaJob.objects.filter(
        estado="ejecutando",
        iniciado_at__gte=now - timedelta(hours=1),
    ).count()

    if ejecutando >= 3:
        return f"Workers llenos ({ejecutando} ejecutando)"

    # Pick next job
    job = DescargaJob.objects.filter(
        estado="en_cola",
        programado_para__lte=now,
        intentos__lt=F("max_intentos"),
    ).select_related("empresa").order_by("prioridad", "programado_para").first()

    if not job:
        return "Cola vacía"

    # Mark as executing
    job.estado = "ejecutando"
    job.iniciado_at = now
    job.intentos += 1
    job.save(update_fields=["estado", "iniciado_at", "intentos"])

    empresa = job.empresa
    logger.info(
        "📥 Job: %s %d-%02d %s (intento %d, prio %d)",
        empresa.rfc, job.year, job.month, job.tipo, job.intentos, job.prioridad,
    )

    try:
        from core.services.scrapper import ejecutar_descarga

        # Create a lightweight DescargaLog as bridge to existing scrapper
        dl = DescargaLog.objects.create(
            empresa=empresa,
            estado="ejecutando",
            year=job.year,
            month_start=job.month,
            month_end=job.month,
            tipos=[job.tipo],
            triggered_by="schedule",
            iniciado_at=now,
        )

        pipeline = iniciar_pipeline(empresa, 'descarga_cfdis')
        avanzar_paso(pipeline.id, f'{empresa.rfc} {job.year}-{job.month:02d} {job.tipo}')  # paso 1→2

        result = ejecutar_descarga(empresa, dl)

        # Success
        fin = datetime.now(timezone.utc)
        job.estado = "completado"
        job.completado_at = fin
        job.duracion_segundos = int((fin - job.iniciado_at).total_seconds())
        job.cfdis_nuevos = getattr(result, "total_cfdis", 0)
        job.cfdis_descargados = getattr(result, "total_files", 0)
        job.save()

        # Verificar que realmente se descargaron CFDIs
        from core.models import CFDI
        cfdi_count = CFDI.objects.filter(
            rfc_empresa=job.empresa.rfc,
            fecha__year=job.year,
            fecha__month=job.month
        ).count()

        if cfdi_count == 0 and job.intentos < 3:
            # Completó pero sin datos — puede ser mes sin actividad
            # o fallo silencioso del SAT. Reintentar hasta 3 veces.
            job.estado = 'en_cola'
            job.intentos += 1
            from django.utils import timezone as dj_timezone
            job.programado_para = dj_timezone.now() + timedelta(hours=6)
            job.save()
            logger.warning(f"Job {job.empresa.rfc} {job.year}-{job.month:02d} completó con 0 CFDIs, reintentando ({job.intentos}/3)")
        elif cfdi_count == 0 and job.intentos >= 3:
            # Validación secundaria: verificar si hay CFDIs en meses adyacentes.
            # Si los hay, este mes vacío es sospechoso (posible fallo silencioso SAT).
            prev_m = 12 if job.month == 1 else job.month - 1
            next_m = 1 if job.month == 12 else job.month + 1
            adjacent_count = CFDI.objects.filter(
                rfc_empresa=job.empresa.rfc,
                fecha__year=job.year,
                fecha__month__in=[prev_m, next_m],
            ).count()

            if adjacent_count > 0:
                # RFC activo en meses vecinos — mes vacío es sospechoso
                job.estado = 'en_cola'
                job.programado_para = dj_timezone.now() + timedelta(hours=48)
                job.max_intentos = max(job.max_intentos, job.intentos + 2)
                job.save()
                logger.warning(
                    f"Job {job.empresa.rfc} {job.year}-{job.month:02d}: 0 CFDIs pero "
                    f"{adjacent_count} en meses adyacentes — re-encolando (sospecha fallo SAT)"
                )
            else:
                # Sin actividad en meses vecinos tampoco — genuinamente vacío
                job.estado = 'completado_vacio'
                job.save()
                logger.info(f"Job {job.empresa.rfc} {job.year}-{job.month:02d} confirmado sin CFDIs tras 3 intentos")


        # Update DescargaLog too
        dl.estado = "completado"
        dl.cfdis_nuevos = job.cfdis_nuevos
        dl.cfdis_descargados = job.cfdis_descargados
        dl.completado_at = fin
        dl.duracion_segundos = job.duracion_segundos
        dl.save()

        # Update empresa timestamp
        empresa.ultimo_scrape = fin
        empresa.save(update_fields=["ultimo_scrape"])

        log_info(
            "download",
            f"✅ {empresa.rfc} {job.year}-{job.month:02d} {job.tipo}: "
            f"{job.cfdis_nuevos} nuevos en {job.duracion_segundos}s",
        )
        avanzar_paso(pipeline.id, f'Login SAT OK')  # paso 2→3
        avanzar_paso(pipeline.id, f'{job.cfdis_nuevos} CFDIs descargados')  # paso 3→4
        avanzar_paso(pipeline.id, f'{job.cfdis_descargados} archivos procesados en {job.duracion_segundos}s')  # paso 4→completado
        return f"OK: {job}"

    except Exception as e:
        error_str = str(e)[:500]
        job.ultimo_error = error_str

        if job.intentos >= job.max_intentos:
            job.estado = "error"
            send_telegram(
                f"🔴 Job agotó reintentos: {empresa.rfc} "
                f"{job.year}-{job.month:02d} {job.tipo}\n"
                f"Error: {error_str[:200]}",
                "critical",
            )
        else:
            job.estado = "en_cola"
            delays = [300, 900, 1800, 3600, 7200]  # 5m, 15m, 30m, 1h, 2h
            delay = delays[min(job.intentos - 1, len(delays) - 1)]
            job.programado_para = now + timedelta(seconds=delay)

        job.save()
        log_error("download", f"Job failed: {job} — {error_str[:200]}")
        marcar_error(
            pipeline.id if 'pipeline' in dir() else None,
            error_str[:200],
            reintentable=(job.intentos < job.max_intentos),
        ) if 'pipeline' in dir() else None
        return f"Error: {job}"


@shared_task
def generar_jobs_mes():
    """Generate download jobs for the month that just closed. Day 1 of each month."""
    from core.services.job_scheduler import generar_jobs_mensuales
    count = generar_jobs_mensuales()
    return f"Generated {count} monthly jobs"


@shared_task(queue="sistema")
def refetch_meses_recientes_vacios():
    """Re-encola jobs marcados completado/completado_vacio con 0 CFDIs para los
    meses recientes (últimos 3 meses incluyendo el actual).

    Soluciona el bug donde un job corrido al inicio del mes encuentra 0 CFDIs
    (los emisores aún no han timbrado) y queda marcado como "hecho" para siempre,
    ignorando uploads posteriores. Re-fetch máximo 1 vez cada 5 días por job
    para no saturar al SAT.

    Corre diario; cada mes vacío se re-consulta ~6 veces antes de salir de la
    ventana de 3 meses — suficiente para capturar timbres tardíos del SAT.
    """
    from core.models import DescargaJob
    from django.db.models import Q
    from django.utils import timezone as djtz
    from datetime import date

    hoy = date.today()
    meses = []
    y, m = hoy.year, hoy.month
    for _ in range(3):
        meses.append((y, m))
        m -= 1
        if m == 0:
            m, y = 12, y - 1

    qcond = Q()
    for y, m in meses:
        qcond |= Q(year=y, month=m)

    desde = djtz.now() - timedelta(days=5)
    qs = DescargaJob.objects.filter(
        empresa__sync_activa=True,
        empresa__fiel_verificada=True,
        estado__in=["completado", "completado_vacio"],
        cfdis_descargados=0,
        completado_at__lte=desde,
    ).filter(qcond)

    n = qs.update(
        estado="en_cola",
        intentos=0,
        programado_para=djtz.now(),
        completado_at=None,
        ultimo_error="auto-refetch: posible timbre tardío del SAT",
    )
    logger.info("refetch_meses_recientes_vacios: %d jobs re-encolados", n)
    return f"re-fetched {n} jobs vacíos de meses recientes"


@shared_task(soft_time_limit=300, time_limit=360)
def auditoria_nocturna_periodos():
    """Ejecuta el auditor de huecos en todas las empresas activas cada noche.
    
    Asegura que no se omitan meses ni queden periodos de por vida en estado 'error'.
    """
    from core.models import Empresa
    from core.services.job_scheduler import auditar_y_reparar_jobs
    from core.services.monitor import log_info

    empresas = Empresa.objects.filter(sync_activa=True, fiel_verificada=True)
    count_empresas = empresas.count()
    total_reparados = 0

    log_info("system", f"Iniciando auditoría nocturna de periodos en {count_empresas} empresas...")

    for emp in empresas:
        reparados = auditar_y_reparar_jobs(emp)
        if reparados > 0:
            total_reparados += reparados

    if total_reparados > 0:
        log_info("system", f"Auditoría finalizada. Se repararon/reprogramaron {total_reparados} huecos.")
    else:
        log_info("system", "Auditoría finalizada. Historial perfecto, sin huecos.")

    return f"Audited {count_empresas} empresas, repaired {total_reparados} gaps"



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
    """Trigger the new AI Executive Report email when download completes."""
    from reportes.tasks import enviar_reporte_mensual_email

    user = empresa.owner
    if not user or not user.email:
        return

    # Trigger the full report process asynchronously
    try:
        enviar_reporte_mensual_email.delay(
            str(empresa.id),
            descarga_log.year,
            descarga_log.month_start
        )
        logger.info("📧 AI Report generation triggered for %s", user.email)
    except Exception as e:
        logger.warning("Failed to trigger report generation: %s", e)


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
def alertas_vencimiento_fiel():
    """Revisa FIELs y CSDs por vencer y envía alertas."""
    from core.models import Empresa
    from django.core.mail import send_mail
    from core.services.alerts import send_telegram

    now = datetime.now().date()
    alerta_dias = [90, 60, 30, 15, 7, 3, 1]

    for empresa in Empresa.objects.filter(fiel_verificada=True, fiel_expira__isnull=False):
        dias_restantes = (empresa.fiel_expira.date() - now).days

        # Alertas a 90, 60, 30, 15, 7, 3, 1 días
        if dias_restantes in alerta_dias:
            send_mail(
                f'⚠️ Tu FIEL de {empresa.rfc} vence en {dias_restantes} días — Cirrus',
                f'La e.firma (FIEL) de {empresa.nombre} ({empresa.rfc}) '
                f'vence el {empresa.fiel_expira.strftime("%d/%m/%Y")}.\n\n'
                f'Te quedan {dias_restantes} días para renovarla en el portal del SAT.\n\n'
                f'Si la FIEL vence, Cirrus no podrá descargar tus CFDIs ni tu '
                f'Constancia de Situación Fiscal.\n\n'
                f'Renueva tu FIEL en: https://www.sat.gob.mx\n\n'
                f'— Equipo Cirrus',
                'Cirrus <cirrus@nubex.me>',
                [empresa.owner.email],
                fail_silently=True,
            )
            send_telegram(
                f"⚠️ FIEL vence en {dias_restantes}d: {empresa.rfc} ({empresa.owner.email})",
                "warning",
            )

        # Si ya venció
        if dias_restantes <= 0:
            empresa.fiel_status = 'expirada'
            empresa.sync_activa = False
            empresa.save(update_fields=['fiel_status', 'sync_activa'])
            send_telegram(
                f"🔴 FIEL EXPIRADA: {empresa.rfc} — sync desactivada",
                "critical",
            )

        # Alertas CSD
        if empresa.csd_expira:
            dias_csd = (empresa.csd_expira - now).days
            if dias_csd in alerta_dias:
                send_mail(
                    f'⚠️ Tu Sello Digital de {empresa.rfc} vence en {dias_csd} días',
                    f'El CSD de {empresa.nombre} vence el {empresa.csd_expira.strftime("%d/%m/%Y")}.\n'
                    f'Renuévalo en el portal del SAT para poder seguir facturando.\n\n'
                    f'— Equipo Cirrus',
                    'Cirrus <cirrus@nubex.me>',
                    [empresa.owner.email],
                    fail_silently=True,
                )
                send_telegram(
                    f"⚠️ CSD vence en {dias_csd}d: {empresa.rfc} ({empresa.owner.email})",
                    "warning",
                )

    logger.info("✅ Alertas de vencimiento FIEL/CSD ejecutadas")
    return "OK"


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


# ══════════════════════════════════════════════════════════════════════════
#  SAT HEALTH MONITOR — Probes distribuidos de disponibilidad del SAT
# ══════════════════════════════════════════════════════════════════════════

# Configuración de nodos
SAT_HEALTH_NODES = [
    {'id': 'vps2',  'ip': '10.20.0.2',   'url': 'http://10.20.0.2:8300'},
    {'id': 'vpsx',  'ip': '10.20.0.100', 'url': 'http://10.20.0.100:8300'},
    {'id': 'spark', 'ip': '10.20.0.6',   'url': 'http://10.20.0.6:8300'},
]

# RFCs en orden de rotación (empresas con FIEL válida)
SAT_HEALTH_RFCS = [
    'AIPF760625HF5',
    'VEN191127M21',
    'LUF250407A86',
    'AFE090605PQ0',
]


@shared_task(queue='sistema', name='core.tasks.sat_health_probe')
def sat_health_probe():
    """
    Orquestador: selecciona nodo + FIEL en round-robin y envía probe.
    Se ejecuta cada 5 minutos via Celery Beat.
    """
    import base64
    import uuid as uuid_mod

    import httpx
    from django.core.cache import cache
    from django.utils import timezone as tz

    from core.models import SATHealthProbe, Empresa
    from core.services.fiel_encryption import decrypt_password
    from core.services.storage_minio import download_bytes, upload_bytes
    from core.services.alerts import send_telegram

    # Obtener índice actual de rotación
    rotation_index = cache.get('sat_health_rotation_index', 0)

    # Seleccionar nodo y FIEL
    node = SAT_HEALTH_NODES[rotation_index % len(SAT_HEALTH_NODES)]
    rfc = SAT_HEALTH_RFCS[rotation_index % len(SAT_HEALTH_RFCS)]

    # Avanzar rotación
    next_index = (rotation_index + 1) % max(len(SAT_HEALTH_NODES), len(SAT_HEALTH_RFCS))
    cache.set('sat_health_rotation_index', next_index, timeout=None)

    # Obtener empresa
    try:
        empresa = Empresa.objects.get(rfc=rfc)
    except Empresa.DoesNotExist:
        return f"RFC {rfc} no encontrado, saltando probe"

    # Leer archivos FIEL de MinIO
    if not empresa.fiel_cer_key or not empresa.fiel_key_key:
        return f"FIEL no configurada para {rfc}"

    try:
        cer_data = download_bytes(empresa.fiel_cer_key)
        cer_b64 = base64.b64encode(cer_data).decode()

        key_data = download_bytes(empresa.fiel_key_key)
        key_b64 = base64.b64encode(key_data).decode()
    except Exception as e:
        return f"Error leyendo FIEL de {rfc} en MinIO: {e}"

    # Desencriptar password
    try:
        fiel_password = decrypt_password(empresa.fiel_password_encrypted)
    except Exception as e:
        return f"Error desencriptando password de {rfc}: {e}"

    # Generar probe_id
    probe_id = str(uuid_mod.uuid4())

    # Auth token (from Django settings or env)
    from django.conf import settings
    sat_health_token = getattr(settings, 'SAT_HEALTH_TOKEN', '')
    headers = {}
    if sat_health_token:
        headers['Authorization'] = f'Bearer {sat_health_token}'

    # Enviar probe al worker
    try:
        with httpx.Client(timeout=120) as client:
            response = client.post(
                f"{node['url']}/probe",
                json={
                    'probe_id': probe_id,
                    'rfc': rfc,
                    'cer_b64': cer_b64,
                    'key_b64': key_b64,
                    'fiel_password': fiel_password,
                    'timeout_seconds': 90,
                },
                headers=headers,
            )

            if response.status_code == 200:
                data = response.json()

                # Si hay screenshot, guardar en MinIO
                screenshot_path = ''
                if data.get('screenshot_b64'):
                    screenshot_bytes = base64.b64decode(data['screenshot_b64'])
                    ts = tz.now().strftime('%Y%m%d_%H%M%S')
                    screenshot_path = f"sat_health/screenshots/{rfc}/{ts}_{node['id']}.png"
                    upload_bytes(
                        screenshot_bytes,
                        screenshot_path,
                        content_type='image/png',
                    )

                # Guardar probe en BD
                SATHealthProbe.objects.create(
                    id=probe_id,
                    node_id=data['node_id'],
                    node_ip=data['node_ip'],
                    rfc_used=rfc,
                    empresa=empresa,
                    result=data['result'],
                    last_phase_reached=data['last_phase_reached'],
                    error_message=data.get('error_message', ''),
                    http_status=data.get('http_status'),
                    time_dns_ms=data.get('time_dns_ms'),
                    time_page_load_ms=data.get('time_page_load_ms'),
                    time_form_visible_ms=data.get('time_form_visible_ms'),
                    time_fiel_upload_ms=data.get('time_fiel_upload_ms'),
                    time_login_submit_ms=data.get('time_login_submit_ms'),
                    time_session_active_ms=data.get('time_session_active_ms'),
                    time_total_ms=data.get('time_total_ms', 0),
                    screenshot_path=screenshot_path,
                    user_agent=data.get('user_agent', ''),
                )

                # Alertar en cambio de estado
                _check_state_change(rfc, data['result'])

                return (
                    f"Probe OK: {node['id']} → {rfc} = {data['result']} "
                    f"({data.get('time_total_ms', 0)}ms)"
                )

            else:
                SATHealthProbe.objects.create(
                    id=probe_id,
                    node_id=node['id'],
                    node_ip=node['ip'],
                    rfc_used=rfc,
                    empresa=empresa,
                    result='browser_error',
                    last_phase_reached='dns',
                    error_message=f"Worker respondió HTTP {response.status_code}: {response.text[:200]}",
                    time_total_ms=0,
                )
                return f"Worker {node['id']} respondió {response.status_code}"

    except httpx.TimeoutException:
        SATHealthProbe.objects.create(
            id=probe_id,
            node_id=node['id'],
            node_ip=node['ip'],
            rfc_used=rfc,
            empresa=empresa,
            result='network_error',
            last_phase_reached='dns',
            error_message=f"Timeout conectando a worker {node['id']} ({node['url']})",
            time_total_ms=120000,
        )
        return f"Timeout conectando a worker {node['id']}"

    except Exception as e:
        SATHealthProbe.objects.create(
            id=probe_id,
            node_id=node['id'],
            node_ip=node['ip'],
            rfc_used=rfc,
            empresa=empresa,
            result='network_error',
            last_phase_reached='dns',
            error_message=f"Error conectando a worker: {str(e)[:300]}",
            time_total_ms=0,
        )
        return f"Error: {e}"


def _check_state_change(rfc, current_result):
    """
    Envía alerta a Telegram si el estado del SAT cambió.
    Solo alerta en transiciones ok→fallo o fallo→ok.
    """
    from django.core.cache import cache
    from core.services.alerts import send_telegram

    cache_key = f'sat_health_last_state_{rfc}'
    last_state = cache.get(cache_key, 'unknown')

    is_ok = current_result == 'success'
    was_ok = last_state == 'success'

    if last_state != 'unknown':
        if was_ok and not is_ok:
            send_telegram(
                f"🔴 SAT Health: Login {rfc} FALLÓ\n"
                f"Error: {current_result}\n"
                f"El SAT puede estar experimentando problemas.",
                level='warning',
            )
        elif not was_ok and is_ok:
            send_telegram(
                f"🟢 SAT Health: Login {rfc} RECUPERADO\n"
                f"El SAT está respondiendo correctamente.",
                level='info',
            )

    cache.set(cache_key, current_result, timeout=3600)


@shared_task(queue='sistema', name='core.tasks.sat_health_summarize')
def sat_health_summarize():
    """
    Genera resumen horario de disponibilidad. Se ejecuta cada hora.
    """
    from collections import Counter

    from django.db.models import Avg, Min, Max
    from django.utils import timezone as tz

    from core.models import SATHealthProbe, SATHealthSummary
    from core.services.alerts import send_telegram

    now = tz.now()
    hour_start = now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
    hour_end = hour_start + timedelta(hours=1)

    probes = SATHealthProbe.objects.filter(
        timestamp__gte=hour_start,
        timestamp__lt=hour_end,
    )

    total = probes.count()
    if total == 0:
        return "No probes en la última hora"

    successful = probes.filter(result='success').count()
    failed = total - successful

    # Agregar por nodo
    results_by_node = {}
    for node in SAT_HEALTH_NODES:
        node_probes = probes.filter(node_id=node['id'])
        results_by_node[node['id']] = {
            'success': node_probes.filter(result='success').count(),
            'failed': node_probes.exclude(result='success').count(),
        }

    # Error más común
    errors = probes.exclude(result='success').values_list('result', flat=True)
    most_common = Counter(errors).most_common(1)
    most_common_error = most_common[0][0] if most_common else ''

    # Tiempos
    time_stats = probes.filter(result='success').aggregate(
        avg_total=Avg('time_total_ms'),
        min_total=Min('time_total_ms'),
        max_total=Max('time_total_ms'),
        avg_login=Avg('time_login_submit_ms'),
    )

    summary, _created = SATHealthSummary.objects.update_or_create(
        hour=hour_start,
        defaults={
            'total_probes': total,
            'successful_probes': successful,
            'failed_probes': failed,
            'availability_pct': round((successful / total) * 100, 1),
            'avg_total_time_ms': int(time_stats['avg_total']) if time_stats['avg_total'] else None,
            'avg_login_time_ms': int(time_stats['avg_login']) if time_stats['avg_login'] else None,
            'min_total_time_ms': time_stats['min_total'],
            'max_total_time_ms': time_stats['max_total'],
            'most_common_error': most_common_error,
            'results_by_node': results_by_node,
        },
    )

    # Alerta horaria a Telegram
    emoji = "🟢" if (successful / total) >= 0.8 else "🟡" if (successful / total) >= 0.5 else "🔴"

    send_telegram(
        f"{emoji} SAT Health {hour_start:%H:00}-{hour_end:%H:00}\n"
        f"Disponibilidad: {summary.availability_pct:.0f}%\n"
        f"Probes: {successful}/{total} exitosos\n"
        f"{'Error más común: ' + most_common_error if most_common_error else ''}\n"
        f"Tiempo promedio: {summary.avg_total_time_ms or 'N/A'}ms",
        level='info',
    )

    return f"Resumen {hour_start:%H:00}: {summary.availability_pct:.0f}% up"


@shared_task(soft_time_limit=60, time_limit=90)
def supervisor_pipelines():
    """Pipeline supervisor — runs every 5 min.

    1. Desbloquea pipelines cuando SAT Health mejora
    2. Re-dispara tasks para pipelines con reintento vencido
    3. Limpia pipelines abandonados (>2h sin update)
    4. Auto re-parse de empresas con CSF pero sin datos (Gap #1)
    """
    from core.models import PipelineState, Empresa
    from core.services.pipeline_manager import desbloquear_por_sat_health
    from core.services.monitor import log_info

    now = datetime.now(timezone.utc)

    # 1. Desbloquear por SAT Health
    desbloqueados = desbloquear_por_sat_health()

    # 2. Pipelines con reintento vencido — re-disparar task
    vencidos = PipelineState.objects.filter(
        estado='reintentando',
        proximo_intento__lte=now,
    ).select_related('empresa')

    re_dispatched = 0
    for p in vencidos:
        if p.pipeline_type == 'alta_empresa':
            verificar_fiel_y_descargar_csf.delay(str(p.empresa.id))
            re_dispatched += 1
        # descarga_cfdis: procesar_cola_descargas handles its own queue

    # 3. Pipelines abandonados (activos sin update en 2h)
    cutoff = now - timedelta(hours=2)
    abandonados = PipelineState.objects.filter(
        estado__in=['en_proceso', 'reintentando'],
        actualizado__lt=cutoff,
    )
    abandon_count = abandonados.count()
    for p in abandonados:
        p.estado = 'error'
        p.mensaje_cliente = 'Proceso interrumpido. Nuestro equipo fue notificado.'
        p.ultimo_error = 'Pipeline abandonado — sin actividad por más de 2 horas'
        p.save(update_fields=['estado', 'mensaje_cliente', 'ultimo_error'])

    # 4. Auto re-parse de empresas con CSF pero sin datos
    from core.models import Empresa
    empresas_sin_datos = Empresa.objects.filter(
        fiel_verificada=True,
    ).exclude(csf_minio_key='').filter(razon_social='')

    reparse_count = 0
    for emp in empresas_sin_datos[:5]:  # max 5 per cycle
        try:
            from core.services.storage_minio import download_bytes
            from core.services.csf_parser import parsear_csf_con_docling

            pdf = download_bytes(emp.csf_minio_key)
            datos = parsear_csf_con_docling(pdf)
            if datos and datos.get('razon_social'):
                emp.nombre = datos.get('razon_social', emp.nombre)
                emp.razon_social = datos.get('razon_social', '')
                emp.regimen_fiscal = datos.get('regimen_fiscal', '')
                emp.regimen_capital = datos.get('regimen_capital', '')
                emp.codigo_postal = datos.get('codigo_postal', '')
                emp.save()
                reparse_count += 1
                log_info("csf", f"Auto re-parse OK: {emp.rfc} → {emp.razon_social}")
        except Exception as e:
            logger.warning("Auto re-parse failed for %s: %s", emp.rfc, e)

    summary = (
        f"Pipelines: {desbloqueados} desbloqueados, "
        f"{re_dispatched} re-dispatched, "
        f"{abandon_count} abandonados, "
        f"{reparse_count} re-parsed"
    )
    if any([desbloqueados, re_dispatched, abandon_count, reparse_count]):
        log_info("system", f"Supervisor pipelines: {summary}")
    return summary


# ══════════════════════════════════════════════════════════════════════════
#  FIEL POR VENCER — aviso automático al CLIENTE (no al admin)
# ══════════════════════════════════════════════════════════════════════════

@shared_task(queue="sistema")
def verificar_fiel_por_vencer():
    """Aviso al DUEÑO de cada empresa cuya FIEL vence en 30/15/7 días.

    - Envía email al owner de la empresa (no al admin).
    - Registra el aviso en SystemLog.
    - NO notifica al admin por Telegram (el vencimiento de FIEL es proceso
      del cliente, no del stack).

    Corre diario a las 09:00 CST (15:00 UTC).
    """
    from core.models import Empresa
    from django.core.mail import send_mail
    from core.services.monitor import log_info, log_warning

    hoy = datetime.now(timezone.utc).date()
    umbrales = [30, 15, 7]
    avisados = 0

    qs = Empresa.objects.filter(
        fiel_verificada=True, fiel_expira__isnull=False,
    ).select_related("owner")

    for empresa in qs:
        dias = (empresa.fiel_expira.date() - hoy).days
        if dias not in umbrales:
            continue

        owner_email = getattr(empresa.owner, "email", "") or ""
        if not owner_email:
            log_warning(
                "fiel",
                f"FIEL de {empresa.rfc} vence en {dias}d pero el owner no tiene email",
            )
            continue

        urgencia = "pronto" if dias > 7 else "MUY pronto"
        send_mail(
            f"Tu e.firma (FIEL) de {empresa.rfc} vence en {dias} días",
            (
                f"Hola,\n\n"
                f"La e.firma (FIEL) de {empresa.nombre} ({empresa.rfc}) "
                f"vence {urgencia}: el {empresa.fiel_expira.strftime('%d/%m/%Y')} "
                f"(te quedan {dias} días).\n\n"
                f"Qué tienes que hacer:\n"
                f"1. Entra al portal del SAT (https://www.sat.gob.mx) con tu "
                f"e.firma actual o tu RFC y contraseña.\n"
                f"2. Renueva tu e.firma (servicio 'CERTISAT' o en un módulo del SAT).\n"
                f"3. Sube tus nuevos archivos .cer y .key a Cirrus desde tu panel.\n\n"
                f"Si tu FIEL vence, Cirrus dejará de poder descargar tus CFDIs y tu "
                f"Constancia de Situación Fiscal de forma automática.\n\n"
                f"— Equipo Cirrus"
            ),
            "Cirrus <cirrus@nubex.me>",
            [owner_email],
            fail_silently=True,
        )
        log_info(
            "fiel",
            f"Aviso de vencimiento FIEL enviado a {owner_email} "
            f"({empresa.rfc}, {dias}d)",
        )
        avisados += 1

    # ── FIEL ya vencida: marcar expirada y desactivar sync ────────────
    # (sin notificar al admin: es proceso del cliente). Reemplaza el
    # comportamiento de la antigua tarea alertas_vencimiento_fiel.
    expiradas = 0
    for empresa in Empresa.objects.filter(
        fiel_verificada=True, fiel_expira__isnull=False, sync_activa=True,
    ).select_related("owner"):
        if (empresa.fiel_expira.date() - hoy).days <= 0:
            empresa.fiel_status = "expirada"
            empresa.sync_activa = False
            empresa.save(update_fields=["fiel_status", "sync_activa"])
            log_warning(
                "fiel",
                f"FIEL EXPIRADA: {empresa.rfc} — sync desactivada",
            )
            expiradas += 1

    # ── CSD por vencer: aviso al cliente (30/15/7) ────────────────────
    for empresa in Empresa.objects.filter(csd_expira__isnull=False).select_related("owner"):
        dias_csd = (empresa.csd_expira - hoy).days
        if dias_csd not in umbrales:
            continue
        owner_email = getattr(empresa.owner, "email", "") or ""
        if not owner_email:
            continue
        send_mail(
            f"Tu Sello Digital (CSD) de {empresa.rfc} vence en {dias_csd} días",
            (
                f"Hola,\n\n"
                f"El Certificado de Sello Digital (CSD) de {empresa.nombre} "
                f"({empresa.rfc}) vence el "
                f"{empresa.csd_expira.strftime('%d/%m/%Y')} "
                f"(te quedan {dias_csd} días).\n\n"
                f"Renuévalo en el portal del SAT para poder seguir facturando.\n\n"
                f"— Equipo Cirrus"
            ),
            "Cirrus <cirrus@nubex.me>",
            [owner_email],
            fail_silently=True,
        )
        log_info("fiel", f"Aviso CSD enviado a {owner_email} ({empresa.rfc}, {dias_csd}d)")
        avisados += 1

    return f"Avisos enviados: {avisados}; FIEL expiradas: {expiradas}"


# ══════════════════════════════════════════════════════════════════════════
#  VIGILANCIA DE STACK + INCIDENTES — únicas alertas que llegan al admin
# ══════════════════════════════════════════════════════════════════════════

def _ya_alertado(category: str, horas: int) -> bool:
    """True si ya se envió (status=sent) una alerta de esta categoría
    dentro de las últimas `horas` (anti-spam)."""
    from core.models import TelegramAlert
    desde = datetime.now(timezone.utc) - timedelta(hours=horas)
    return TelegramAlert.objects.filter(
        category=category, status="sent", created_at__gte=desde,
    ).exists()


def _clasificar_incidente(ultimo_error: str) -> str:
    """Mapea el ultimo_error de un job a un tipo de DescargaIncidente."""
    e = (ultimo_error or "").lower()
    if "timeout" in e or "timed out" in e:
        return "timeout"
    if "fiel" in e or "login" in e or "sesión" in e or "sesion" in e:
        return "fiel_error"
    if "sat" in e or "portal" in e or "page" in e or "captcha" in e:
        return "sat_error"
    return "otro"


@shared_task(queue="sistema")
def vigilancia_stack():
    """Vigilancia de stack: única fuente de alertas Telegram al admin.

    Chequeos (cada uno con anti-spam por categoría):
    1. SAT con tasa de éxito <60% en ventana móvil de 24h  → sat_health
    2. 3+ descargas fallidas (DescargaLog estado=error) en 24h → job_failures
    3. Detecta DescargaJobs stuck (>48h sin completar) → crea DescargaIncidente
    4. 3+ incidentes nuevos en 24h → incidentes
    5. Worker/web caído → service_down (critical)

    Corre cada hora.
    """
    from core.models import (
        SATHealthProbe, DescargaLog, DescargaJob, DescargaIncidente,
    )
    from django.db.models import Q
    from core.services.alerts import send_telegram

    now = datetime.now(timezone.utc)
    hace_24h = now - timedelta(hours=24)
    resultados = []

    # ── 1. Salud SAT (ventana móvil 24h) ──────────────────────────────
    probes_24h = SATHealthProbe.objects.filter(timestamp__gte=hace_24h)
    total_p = probes_24h.count()
    if total_p >= 20:  # muestra mínima para que sea significativo
        ok_p = probes_24h.filter(result="success").count()
        tasa = 100.0 * ok_p / total_p
        if tasa < 60 and not _ya_alertado("sat_health", 24):
            send_telegram(
                f"🚨 *SAT degradado* — tasa de éxito {tasa:.0f}% en las "
                f"últimas 24h ({ok_p}/{total_p} probes OK). Posible caída o "
                f"saturación del portal.",
                level="critical", category="sat_health",
            )
            resultados.append(f"sat_health alert ({tasa:.0f}%)")

    # ── 2. Patrón de descargas fallidas (24h) ─────────────────────────
    errores_24h = DescargaLog.objects.filter(
        estado="error", completado_at__gte=hace_24h,
    ).count()
    if errores_24h >= 3 and not _ya_alertado("job_failures", 12):
        send_telegram(
            f"⚠️ *Patrón de fallas* — {errores_24h} descargas fallidas en "
            f"las últimas 24h. Revisa /panel/monitor/",
            level="error", category="job_failures",
        )
        resultados.append(f"job_failures alert ({errores_24h})")

    # ── 3. Jobs stuck >48h → crear incidentes ─────────────────────────
    hace_48h = now - timedelta(hours=48)
    stuck = DescargaJob.objects.filter(
        estado__in=["en_cola", "ejecutando"],
        created_at__lte=hace_48h,
    ).select_related("empresa")
    nuevos_incidentes = 0
    for job in stuck:
        # dedup: no crear si ya hay incidente abierto para este job
        ya = DescargaIncidente.objects.filter(job=job, resuelto=False).exists()
        if ya:
            continue
        DescargaIncidente.objects.create(
            empresa=job.empresa,
            tipo=_clasificar_incidente(job.ultimo_error),
            descripcion=(
                f"Job {job.year}-{job.month:02d} {job.tipo} lleva >48h en "
                f"'{job.estado}' ({job.intentos} intentos). "
                f"Último error: {(job.ultimo_error or 'n/a')[:300]}"
            ),
            job=job,
        )
        nuevos_incidentes += 1
    if nuevos_incidentes:
        resultados.append(f"{nuevos_incidentes} incidentes creados")

    # ── 4. 3+ incidentes nuevos en 24h → alerta ───────────────────────
    incidentes_24h = DescargaIncidente.objects.filter(
        creado_en__gte=hace_24h,
    ).count()
    if incidentes_24h >= 3 and not _ya_alertado("incidentes", 12):
        send_telegram(
            f"⚠️ *{incidentes_24h} incidentes de descarga* en las últimas 24h. "
            f"Revisa /panel/monitor/ → Incidentes",
            level="error", category="incidentes",
        )
        resultados.append(f"incidentes alert ({incidentes_24h})")

    # ── 5. Servicios críticos: worker Celery vivo ─────────────────────
    try:
        from cirrus.celery import app as celery_app
        pong = celery_app.control.ping(timeout=3)
        if not pong and not _ya_alertado("service_down", 1):
            send_telegram(
                "🚨 *Sin workers Celery* — ningún worker respondió al ping. "
                "Revisa `systemctl status cirrus-worker`.",
                level="critical", category="service_down",
            )
            resultados.append("service_down: workers")
    except Exception as e:
        logger.warning("vigilancia_stack: no se pudo hacer ping a workers: %s", e)

    return "; ".join(resultados) if resultados else "stack OK"
