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

        # Phase 4: Process downloaded XMLs → MinIO + PostgreSQL
        with StepTimer(descarga_log, "xml_process", "cirrus") as step:
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
