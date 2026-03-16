"""Empresa API endpoints.

CRUD for empresas + FIEL management + trigger downloads.
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from django.db.models import Q, Sum, Count
from ninja import Router, Schema
from django.http import HttpResponse, JsonResponse

from core.api.auth import api_key_auth

logger = logging.getLogger("core.api.empresas")

router = Router(tags=["empresas"], auth=api_key_auth)


# ── Schemas ───────────────────────────────────────────────────────────

class EmpresaOut(Schema):
    id: str  # UUID
    rfc: str
    nombre: str
    descarga_activa: bool
    fiel_configurada: bool
    fiel_verificada: bool
    ultimo_scrape: Optional[datetime] = None
    total_cfdis: int = 0


class DescargaTriggerIn(Schema):
    year: Optional[int] = None
    month_start: Optional[int] = 1
    month_end: Optional[int] = 12
    tipos: Optional[list[str]] = ["recibidos", "emitidos"]


class DescargaLogOut(Schema):
    id: str
    empresa_rfc: str
    estado: str
    year: int
    month_start: int
    month_end: int
    tipos: list
    cfdis_descargados: int
    cfdis_nuevos: int
    errores: list
    progreso: str
    triggered_by: str
    iniciado_at: Optional[datetime] = None
    completado_at: Optional[datetime] = None
    duracion_segundos: int


# ── Endpoints ─────────────────────────────────────────────────────────


@router.get("/", response=list[EmpresaOut], summary="List empresas")
def list_empresas(request):
    """List all empresas accessible to this API key."""
    empresas = request.api_empresas
    result = []
    for e in empresas:
        result.append(EmpresaOut(
            id=str(e.id),
            rfc=e.rfc,
            nombre=e.nombre,
            descarga_activa=e.descarga_activa,
            fiel_configurada=bool(e.fiel_cer_key and e.fiel_key_key),
            fiel_verificada=e.fiel_verificada,
            ultimo_scrape=e.ultimo_scrape,
            total_cfdis=e.cfdis.count(),
        ))
    return result


@router.get("/{rfc}/", response=EmpresaOut, summary="Get empresa by RFC")
def get_empresa(request, rfc: str):
    """Get a single empresa by RFC."""
    empresa = _get_empresa_or_404(request, rfc)
    if isinstance(empresa, HttpResponse):
        return empresa

    return EmpresaOut(
        id=str(empresa.id),
        rfc=empresa.rfc,
        nombre=empresa.nombre,
        descarga_activa=empresa.descarga_activa,
        fiel_configurada=bool(empresa.fiel_cer_key and empresa.fiel_key_key),
        fiel_verificada=empresa.fiel_verificada,
        ultimo_scrape=empresa.ultimo_scrape,
        total_cfdis=empresa.cfdis.count(),
    )


@router.post("/{rfc}/descargar/", summary="Trigger CFDI download")
def trigger_descarga(request, rfc: str, payload: DescargaTriggerIn):
    """Trigger a CFDI download for an empresa. Requires puede_trigger_descarga permission."""
    if not request.api_key.puede_trigger_descarga:
        return HttpResponse(
            '{"error": "API key does not have download trigger permission"}',
            status=403,
            content_type="application/json",
        )

    empresa = _get_empresa_or_404(request, rfc)
    if isinstance(empresa, HttpResponse):
        return empresa

    from core.tasks import descargar_cfdis

    params = {
        "year": payload.year or datetime.now().year,
        "month_start": payload.month_start,
        "month_end": payload.month_end,
        "tipos": payload.tipos,
    }

    task = descargar_cfdis.delay(str(empresa.id), params=params)

    return JsonResponse({
        "status": "queued",
        "task_id": task.id,
        "empresa": rfc,
        "params": params,
    })


@router.post("/{rfc}/verificar-fiel/", summary="Verify FIEL credentials")
def trigger_verificar_fiel(request, rfc: str):
    """Trigger FIEL verification against the SAT portal."""
    if not request.api_key.puede_trigger_descarga:
        return HttpResponse(
            '{"error": "API key does not have trigger permission"}',
            status=403,
            content_type="application/json",
        )

    empresa = _get_empresa_or_404(request, rfc)
    if isinstance(empresa, HttpResponse):
        return empresa

    from core.tasks import verificar_fiel

    task = verificar_fiel.delay(str(empresa.id))

    return JsonResponse({
        "status": "queued",
        "task_id": task.id,
        "empresa": rfc,
    })


@router.get("/{rfc}/descargas/", response=list[DescargaLogOut], summary="List downloads")
def list_descargas(request, rfc: str, limit: int = 20):
    """List recent download logs for an empresa."""
    empresa = _get_empresa_or_404(request, rfc)
    if isinstance(empresa, HttpResponse):
        return empresa

    logs = empresa.descargas.all()[:limit]
    return [
        DescargaLogOut(
            id=str(log.id),
            empresa_rfc=empresa.rfc,
            estado=log.estado,
            year=log.year,
            month_start=log.month_start,
            month_end=log.month_end,
            tipos=log.tipos,
            cfdis_descargados=log.cfdis_descargados,
            cfdis_nuevos=log.cfdis_nuevos,
            errores=log.errores,
            progreso=log.progreso,
            triggered_by=log.triggered_by,
            iniciado_at=log.iniciado_at,
            completado_at=log.completado_at,
            duracion_segundos=log.duracion_segundos,
        )
        for log in logs
    ]


@router.get("/{rfc}/stats/", summary="Get empresa statistics")
def get_empresa_stats(request, rfc: str):
    """Get CFDI statistics for an empresa."""
    empresa = _get_empresa_or_404(request, rfc)
    if isinstance(empresa, HttpResponse):
        return empresa

    cfdis = empresa.cfdis
    stats = cfdis.aggregate(
        total_count=Count("uuid"),
        total_sum=Sum("total"),
        recibidos=Count("uuid", filter=Q(tipo_relacion="recibido")),
        emitidos=Count("uuid", filter=Q(tipo_relacion="emitido")),
    )

    return JsonResponse({
        "rfc": rfc,
        "total_cfdis": stats["total_count"] or 0,
        "total_monto": float(stats["total_sum"] or 0),
        "recibidos": stats["recibidos"] or 0,
        "emitidos": stats["emitidos"] or 0,
        "fiel_configurada": bool(empresa.fiel_cer_key and empresa.fiel_key_key),
        "fiel_verificada": empresa.fiel_verificada,
        "ultimo_scrape": empresa.ultimo_scrape.isoformat() if empresa.ultimo_scrape else None,
    })


# ── Helpers ───────────────────────────────────────────────────────────

def _get_empresa_or_404(request, rfc: str):
    """Get empresa if accessible by this API key."""
    empresa = request.api_empresas.filter(rfc=rfc.upper()).first()
    if not empresa:
        return HttpResponse(
            '{"error": "Empresa not found or not accessible"}',
            status=404,
            content_type="application/json",
        )
    return empresa
