"""Cirrus API — Pipeline status endpoints for client polling."""

from ninja import Router
from django.shortcuts import get_object_or_404

from core.models import Empresa, PipelineState
from core.services.colaboradores import get_empresas_visibles

router = Router(tags=["pipelines"])


@router.get("/{empresa_id}/")
def get_pipeline_status(request, empresa_id: str):
    """Retorna pipelines activos/recientes para una empresa.

    Used by the client UI for real-time agent panel polling (every 5s).
    Requires auth — user must own or have collaborator access to the empresa.
    """
    empresa = get_object_or_404(Empresa, id=empresa_id)

    # Auth: verify user has access
    if not request.user.is_staff:
        visible = get_empresas_visibles(request.user)
        if not visible.filter(id=empresa.id).exists():
            return {"error": "No tienes acceso a esta empresa"}

    pipelines = PipelineState.objects.filter(
        empresa=empresa,
    ).order_by('-actualizado')[:10]

    return [
        {
            'id': str(p.id),
            'tipo': p.pipeline_type,
            'tipo_display': p.get_pipeline_type_display(),
            'estado': p.estado,
            'paso_actual': p.paso_actual,
            'total_pasos': p.total_pasos,
            'paso_nombre': p.paso_nombre,
            'progreso_pct': p.progreso_pct,
            'mensaje': p.mensaje_cliente,
            'pasos': p.pasos_detalle,
            'intento': p.intento_actual,
            'max_intentos': p.max_intentos,
            'bloqueado_por_sat': p.bloqueado_por_sat,
            'ultimo_error': p.ultimo_error[:200] if p.ultimo_error else '',
            'iniciado': p.iniciado.isoformat(),
            'actualizado': p.actualizado.isoformat(),
            'completado_at': p.completado_at.isoformat() if p.completado_at else None,
        }
        for p in pipelines
    ]
