"""Scrapper service — bridges Cirrus (Django/Celery) with sat_scrapper_core.

Handles:
- Building ScrapeConfig from Empresa + DescargaLog parameters
- Running SATEngine within a temp dir (FIEL files from MinIO)
- Callbacks to update DescargaLog progress in real-time
- Post-download processing (XMLs → MinIO + PostgreSQL)
- Telemetry for each phase (via StepTimer)
"""

import asyncio
import logging
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from sat_scrapper_core import SATEngine, ScrapeConfig, DownloadResult

from .fiel_encryption import get_fiel_for_scraping
from .xml_processor import process_downloaded_xmls
from .telemetry import StepTimer

logger = logging.getLogger("core.scrapper")


def ejecutar_descarga(empresa, descarga_log) -> DownloadResult:
    """Run a full CFDI download for an empresa.

    This is the main entry point called from Celery tasks.
    It handles the full lifecycle:
    1. Download FIEL from MinIO to temp dir
    2. Build ScrapeConfig from DescargaLog fields
    3. Run SATEngine.download_all()
    4. Process downloaded XMLs → MinIO + PostgreSQL
    5. Clean up temp files

    Args:
        empresa: Empresa model instance (with FIEL configured)
        descarga_log: DescargaLog model instance (with year/month_start/month_end/tipos)

    Returns:
        DownloadResult from the engine
    """
    download_dir = None
    fiel_ctx = None

    try:
        # Phase 1: Download FIEL from MinIO
        with StepTimer(descarga_log, "fiel_download", "minio") as step:
            fiel_ctx = get_fiel_for_scraping(empresa)
            step.metadata = {"rfc": empresa.rfc}

        # Phase 2: Read params and build config
        year = descarga_log.year
        month_start = descarga_log.month_start
        month_end = descarga_log.month_end
        tipos = descarga_log.tipos or ["recibidos", "emitidos"]

        download_dir = tempfile.mkdtemp(prefix="cirrus_download_")

        def on_progress(msg):
            _update_log_progress(descarga_log, msg)

        def on_error(exc, context):
            _update_log_error(descarga_log, context)

        config = ScrapeConfig(
            cer_path=fiel_ctx["cer_path"],
            key_path=fiel_ctx["key_path"],
            password=fiel_ctx["password"],
            year=year,
            month_start=month_start,
            month_end=month_end,
            tipos=tipos,
            download_dir=download_dir,
            headless=True,
            take_screenshots=False,
            screenshot_dir="/tmp/cirrus_screenshots",
            on_progress=on_progress,
            on_error=on_error,
        )

        # Phase 3: Run the RPA engine (browser + SAT login + download)
        with StepTimer(descarga_log, "engine_run", "sat") as step:
            result = asyncio.run(_run_engine(config))
            step.metadata = {
                "total_files": result.total_files,
                "total_cfdis": result.total_cfdis,
                "errors_count": len(result.errors),
            }

        # Regla 'paquete equivocado': validar que el contenido descargado
        # corresponde al periodo/empresa solicitados ANTES de tocar la BD.
        # El grid de Recuperar Descargas del SAT lista paquetes residuales de
        # hasta 3 días y entregar uno ajeno marca el mes como "completado" con
        # datos de otro periodo (1,282 CFDIs afectados, auditoría 2026-07-08).
        if result.total_files > 0:
            with StepTimer(descarga_log, "package_validation", "cirrus") as step:
                val = validar_paquete_descargado(
                    download_dir, empresa, year, month_start, month_end,
                )
                step.metadata = val
                if not val["ok"]:
                    raise Exception(
                        f"Paquete DESCARTADO por validación: solo {val['pct_en_rango']:.0%} "
                        f"de {val['total']} XMLs caen en {year}-{month_start:02d}..{month_end:02d} "
                        f"(umbral 95%). Fechas ajenas ej.: {val['ejemplos_fuera'][:3]}. "
                        f"RFC ajeno: {val['rfc_ajeno']}. Probable paquete de otra "
                        f"solicitud — job a error para retry, NO se guardó nada."
                    )

        # Regla 'fallo ≠ vacío' a nivel de resultado del engine.
        # El try/except de engine.download_all (por diseño multi-mes) traga las
        # excepciones del navigator y las registra en result.errors. Si el
        # navigator falló (ej. UI del SAT cambió, filtros muertos, timeout sin
        # tabla ni mensaje "sin resultados") y el engine no descargó nada, ese
        # caso debe propagarse como excepción real — no como "completado_vacio".
        # Vacío legítimo (SAT dijo explícitamente "sin resultados") deja
        # result.errors=[] y total_files=0, así que NO dispara este guard.
        if result.total_files == 0 and result.errors:
            preview = "; ".join(str(e)[:200] for e in result.errors[:3])
            raise Exception(
                f"Scraper falló sin descargar archivos "
                f"({len(result.errors)} errores). Primeros: {preview}"
            )

        # Phase 4: Process downloaded XMLs → MinIO + PostgreSQL
        from django.db import transaction
        with StepTimer(descarga_log, "xml_process", "cirrus") as step:
            with transaction.atomic():
                processed_count = process_downloaded_xmls(download_dir, empresa)
            step.metadata = {"processed": processed_count}

        logger.info(
            "🎉 Descarga completa para %s: %d archivos, %d CFDIs procesados, %d errores",
            empresa.rfc, result.total_files, processed_count, len(result.errors),
        )

        return result

    finally:
        # Phase 5: Cleanup
        with StepTimer(descarga_log, "cleanup", "cirrus"):
            if fiel_ctx:
                try:
                    fiel_ctx["temp_dir"].cleanup()
                except Exception:
                    pass
            if download_dir:
                shutil.rmtree(download_dir, ignore_errors=True)


def validar_paquete_descargado(download_dir, empresa, year, month_start, month_end) -> dict:
    """Valida que los XMLs descargados correspondan a lo solicitado.

    Reglas (fix bug 'paquete equivocado', 2026-07-08):
    - Fecha del CFDI dentro de [1/month_start - 3 días, fin de month_end + 3 días]
      (margen por fronteras de zona horaria; el SAT filtra por fecha de emisión,
      así que un paquete legítimo no trae fechas de otros meses).
    - empresa.rfc debe ser emisor o receptor de cada XML.
    - Pasa si ≥95% de los XMLs cumplen ambas. Si no, el paquete se descarta
      completo (el caller marca el job como error para retry) — nunca se
      guarda a medias.

    Returns dict: ok, total, en_rango, pct_en_rango, rfc_ajeno, ejemplos_fuera.
    """
    import calendar
    import re as _re
    from datetime import datetime as _dt, timedelta as _td

    ini = _dt(year, month_start, 1) - _td(days=3)
    fin = _dt(year, month_end, calendar.monthrange(year, month_end)[1], 23, 59, 59) + _td(days=3)
    rfc = empresa.rfc.upper()

    xmls = list(Path(download_dir).rglob("*.xml"))
    total = len(xmls)
    en_rango = 0
    rfc_ajeno = 0
    ejemplos_fuera = []

    re_fecha = _re.compile(rb'<cfdi:Comprobante[^>]*?\sFecha="([^"]+)"')
    for x in xmls:
        try:
            data = x.read_bytes()
            m = re_fecha.search(data)
            fecha_ok = False
            fecha_str = ""
            if m:
                fecha_str = m.group(1).decode("utf-8", errors="ignore")
                try:
                    f = _dt.fromisoformat(fecha_str.replace("Z", ""))
                    f = f.replace(tzinfo=None)
                    fecha_ok = ini <= f <= fin
                except ValueError:
                    fecha_ok = False
            rfc_ok = rfc.encode() in data.upper()
            if not rfc_ok:
                rfc_ajeno += 1
            if fecha_ok and rfc_ok:
                en_rango += 1
            elif len(ejemplos_fuera) < 5:
                ejemplos_fuera.append(f"{x.name[:13]}:{fecha_str or 'sin-fecha'}")
        except Exception as e:
            if len(ejemplos_fuera) < 5:
                ejemplos_fuera.append(f"{x.name[:13]}:error:{e}")

    pct = (en_rango / total) if total else 1.0
    ok = pct >= 0.95
    if not ok:
        logger.error(
            "🚫 VALIDACIÓN DE PAQUETE FALLIDA para %s %d-%02d..%02d: %d/%d XMLs "
            "en rango (%.0f%%), %d con RFC ajeno. Ejemplos fuera: %s",
            empresa.rfc, year, month_start, month_end, en_rango, total,
            pct * 100, rfc_ajeno, ejemplos_fuera,
        )
    else:
        logger.info(
            "✅ Paquete validado para %s %d-%02d..%02d: %d/%d XMLs en rango (%.0f%%)",
            empresa.rfc, year, month_start, month_end, en_rango, total, pct * 100,
        )
    return {
        "ok": ok, "total": total, "en_rango": en_rango,
        "pct_en_rango": pct, "rfc_ajeno": rfc_ajeno,
        "ejemplos_fuera": ejemplos_fuera,
    }


async def _run_engine(config: ScrapeConfig) -> DownloadResult:
    """Run SATEngine inside an async context."""
    async with SATEngine(config) as engine:
        return await engine.download_all()


def _update_log_progress(descarga_log, message: str):
    """Update DescargaLog with progress message."""
    try:
        descarga_log.progreso = message
        descarga_log.save(update_fields=["progreso"])
    except Exception:
        pass  # Don't let logging errors break the download


def _update_log_error(descarga_log, error_msg: str):
    """Append error to DescargaLog."""
    try:
        errors = descarga_log.errores or []
        errors.append(error_msg)
        descarga_log.errores = errors
        descarga_log.save(update_fields=["errores"])
    except Exception:
        pass
