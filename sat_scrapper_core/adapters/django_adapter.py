"""
Adaptador Django para sat-scrapper-core.

Permite integrar la descarga de CFDIs en cualquier proyecto Django
sin modificar la librería core. Usa threading para background tasks.

Uso en tu proyecto Django:
    # views.py
    from sat_scrapper_core.adapters.django_adapter import launch_sat_download, get_download_status

    def trigger_download(request):
        task_id = launch_sat_download(
            cer_path=settings.SAT_CER_PATH,
            key_path=settings.SAT_KEY_PATH,
            password=settings.SAT_PASSWORD,
            year=2025,
            on_cfdi_downloaded=lambda xml_bytes, meta: MiModeloCfdi.objects.create(**meta),
        )
        return JsonResponse({'task_id': task_id})

    def check_status(request, task_id):
        status = get_download_status(task_id)
        return JsonResponse(status)
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import uuid
from pathlib import Path
from typing import Callable

from ..config import ScrapeConfig
from ..engine import SATEngine

logger = logging.getLogger("sat_scrapper_core.django")

# Directorio para archivos de estado (configurable)
_STATUS_DIR = Path("/tmp/sat_scrapper_status")
_STATUS_DIR.mkdir(parents=True, exist_ok=True)


def set_status(task_id: str, status: str, payload: dict | None = None):
    """Guarda el estado de una tarea en un archivo JSON."""
    status_file = _STATUS_DIR / f"scraper_{task_id}.json"
    data = {"status": status, "task_id": task_id}
    if payload:
        data.update(payload)
    status_file.write_text(json.dumps(data, ensure_ascii=False, default=str))


def get_download_status(task_id: str) -> dict:
    """Lee el estado de una tarea."""
    status_file = _STATUS_DIR / f"scraper_{task_id}.json"
    if not status_file.exists():
        return {"status": "not_found", "task_id": task_id}
    try:
        return json.loads(status_file.read_text())
    except Exception:
        return {"status": "error", "detail": "Error leyendo status"}


def launch_sat_download(
    cer_path: str,
    key_path: str,
    password: str,
    year: int = 2025,
    month_start: int = 1,
    month_end: int = 12,
    tipos: list[str] | None = None,
    engine: str = "auto",
    download_dir: str = "./downloads",
    on_cfdi_downloaded: Callable | None = None,
    on_month_completed: Callable | None = None,
    on_error: Callable | None = None,
    task_id: str | None = None,
) -> str:
    """
    Lanza la descarga de CFDIs en un hilo daemon en segundo plano.

    Returns:
        task_id para consultar el estado vía get_download_status()
    """
    task_id = task_id or str(uuid.uuid4())
    set_status(task_id, "pending")

    thread = threading.Thread(
        target=_orchestrator_thread,
        kwargs={
            "cer_path": cer_path,
            "key_path": key_path,
            "password": password,
            "year": year,
            "month_start": month_start,
            "month_end": month_end,
            "tipos": tipos or ["recibidos", "emitidos"],
            "engine": engine,
            "download_dir": download_dir,
            "on_cfdi_downloaded": on_cfdi_downloaded,
            "on_month_completed": on_month_completed,
            "on_error": on_error,
            "task_id": task_id,
        },
        daemon=True,
    )
    thread.start()
    return task_id


def _orchestrator_thread(
    cer_path: str,
    key_path: str,
    password: str,
    year: int,
    month_start: int,
    month_end: int,
    tipos: list[str],
    engine: str,
    download_dir: str,
    on_cfdi_downloaded: Callable | None,
    on_month_completed: Callable | None,
    on_error: Callable | None,
    task_id: str,
):
    """Hilo daemon que orquesta la descarga y reporta progreso."""
    try:
        set_status(task_id, "running", {"step": "Iniciando scraping..."})

        config = ScrapeConfig(
            cer_path=cer_path,
            key_path=key_path,
            password=password,
            year=year,
            month_start=month_start,
            month_end=month_end,
            tipos=tipos,
            engine=engine,
            download_dir=download_dir,
            on_cfdi_downloaded=on_cfdi_downloaded,
            on_month_completed=on_month_completed,
            on_error=on_error,
            on_progress=lambda msg: set_status(task_id, "running", {"step": msg}),
        )

        async def _run():
            async with SATEngine(config) as eng:
                return await eng.download_all()

        result = asyncio.run(_run())

        set_status(task_id, "completed", {
            "summary": result.summary(),
            "folios": result.folios,
            "errors": result.errors,
        })
        logger.info("✅ Tarea %s completada", task_id)

    except Exception as e:
        logger.exception("Error en tarea %s", task_id)
        set_status(task_id, "error", {"detail": str(e)})
