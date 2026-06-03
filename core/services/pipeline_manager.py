"""
Pipeline Manager — Controla el estado de todos los pipelines y decide cuándo proceder.

Uso desde las tasks de Celery:
    from core.services.pipeline_manager import iniciar_pipeline, avanzar_paso, marcar_error

    pipeline = iniciar_pipeline(empresa, 'alta_empresa')
    avanzar_paso(pipeline.id, 'FIEL válida')
    marcar_error(pipeline.id, 'SAT timeout', reintentable=True)
"""

import logging
from datetime import timedelta

from django.utils import timezone

logger = logging.getLogger("core.pipeline")

# ============================================================
# DEFINICIONES DE PIPELINES
# ============================================================

PIPELINE_STEPS = {
    'alta_empresa': [
        {'paso': 1, 'nombre': 'Validando FIEL', 'mensaje_cliente': 'Validando tu FIEL criptográficamente...'},
        {'paso': 2, 'nombre': 'Verificando FIEL', 'mensaje_cliente': 'Verificando certificado contra el SAT...'},
        {'paso': 3, 'nombre': 'Descargando CSF', 'mensaje_cliente': 'Descargando Constancia de Situación Fiscal del SAT...'},
        {'paso': 4, 'nombre': 'Parseando CSF', 'mensaje_cliente': 'Analizando tu CSF para extraer datos de la empresa...'},
        {'paso': 5, 'nombre': 'Capturando datos', 'mensaje_cliente': 'Registrando datos oficiales de tu empresa...'},
        {'paso': 6, 'nombre': 'Programando descargas', 'mensaje_cliente': 'Calculando periodos de descarga según tu plan...'},
    ],
    'descarga_cfdis': [
        {'paso': 1, 'nombre': 'Preparando descarga', 'mensaje_cliente': 'Preparando descarga de CFDIs...'},
        {'paso': 2, 'nombre': 'Login SAT', 'mensaje_cliente': 'Iniciando sesión en el portal del SAT...'},
        {'paso': 3, 'nombre': 'Descargando CFDIs', 'mensaje_cliente': 'Descargando tus comprobantes fiscales...'},
        {'paso': 4, 'nombre': 'Procesando XMLs', 'mensaje_cliente': 'Procesando y almacenando tus CFDIs...'},
    ],
    'csf_mensual': [
        {'paso': 1, 'nombre': 'Preparando FIEL', 'mensaje_cliente': 'Preparando credenciales FIEL...'},
        {'paso': 2, 'nombre': 'Descargando CSF', 'mensaje_cliente': 'Descargando CSF actualizada del SAT...'},
        {'paso': 3, 'nombre': 'Parseando CSF', 'mensaje_cliente': 'Extrayendo datos actualizados...'},
        {'paso': 4, 'nombre': 'Actualizando datos', 'mensaje_cliente': 'Actualizando información de la empresa...'},
    ],
}


def iniciar_pipeline(empresa, pipeline_type):
    """Crea un nuevo pipeline. Si ya hay uno activo del mismo tipo, lo retorna."""
    from core.models import PipelineState

    activo = PipelineState.objects.filter(
        empresa=empresa,
        pipeline_type=pipeline_type,
        estado__in=['pendiente', 'en_proceso', 'esperando_sat', 'reintentando'],
    ).first()

    if activo:
        return activo

    steps = PIPELINE_STEPS.get(pipeline_type, [])
    pasos_detalle = [
        {
            'paso': s['paso'],
            'nombre': s['nombre'],
            'status': 'pendiente',
            'mensaje': '',
            'timestamp': None,
            'intento': 0,
        }
        for s in steps
    ]

    if pasos_detalle:
        pasos_detalle[0]['status'] = 'en_proceso'
        pasos_detalle[0]['timestamp'] = timezone.now().isoformat()

    pipeline = PipelineState.objects.create(
        empresa=empresa,
        pipeline_type=pipeline_type,
        estado='en_proceso',
        paso_actual=1,
        total_pasos=len(steps),
        paso_nombre=steps[0]['nombre'] if steps else '',
        pasos_detalle=pasos_detalle,
        mensaje_cliente=steps[0]['mensaje_cliente'] if steps else 'Iniciando...',
    )

    logger.info("Pipeline creado: %s %s (%d pasos)", empresa.rfc, pipeline_type, len(steps))
    return pipeline


def avanzar_paso(pipeline_id, mensaje_extra=''):
    """Marca paso actual como completado y avanza al siguiente."""
    from core.models import PipelineState

    try:
        pipeline = PipelineState.objects.get(id=pipeline_id)
    except PipelineState.DoesNotExist:
        logger.warning("Pipeline %s not found for avanzar_paso", pipeline_id)
        return None

    pasos = pipeline.pasos_detalle
    steps_def = PIPELINE_STEPS.get(pipeline.pipeline_type, [])

    # Marcar paso actual como completado
    idx = pipeline.paso_actual - 1
    if idx < len(pasos):
        pasos[idx]['status'] = 'completado'
        pasos[idx]['timestamp'] = timezone.now().isoformat()
        pasos[idx]['mensaje'] = mensaje_extra or 'OK'

    # ¿Era el último?
    if pipeline.paso_actual >= pipeline.total_pasos:
        pipeline.estado = 'completado'
        pipeline.completado_at = timezone.now()
        pipeline.bloqueado_por_sat = False  # limpiar flag al completar
        pipeline.mensaje_cliente = '¡Proceso completado exitosamente!'
        pipeline.pasos_detalle = pasos
        pipeline.save()
        logger.info("Pipeline completado: %s %s", pipeline.empresa.rfc, pipeline.pipeline_type)
        return pipeline

    # Avanzar al siguiente
    next_paso = pipeline.paso_actual + 1
    next_idx = next_paso - 1

    if next_idx < len(pasos):
        pasos[next_idx]['status'] = 'en_proceso'
        pasos[next_idx]['timestamp'] = timezone.now().isoformat()

    pipeline.paso_actual = next_paso
    pipeline.paso_nombre = steps_def[next_idx]['nombre'] if next_idx < len(steps_def) else ''
    pipeline.mensaje_cliente = steps_def[next_idx]['mensaje_cliente'] if next_idx < len(steps_def) else 'Procesando...'
    pipeline.intento_actual = 1  # reset reintentos para nuevo paso
    pipeline.pasos_detalle = pasos
    pipeline.estado = 'en_proceso'
    pipeline.save()

    logger.info(
        "Pipeline avanzó: %s %s → paso %d/%d (%s)",
        pipeline.empresa.rfc, pipeline.pipeline_type,
        next_paso, pipeline.total_pasos, pipeline.paso_nombre,
    )
    return pipeline


def marcar_error(pipeline_id, error_msg, reintentable=True):
    """Marca el paso actual con error. Consulta SAT Health para backoff inteligente."""
    from core.models import PipelineState

    try:
        pipeline = PipelineState.objects.get(id=pipeline_id)
    except PipelineState.DoesNotExist:
        logger.warning("Pipeline %s not found for marcar_error", pipeline_id)
        return None

    pasos = pipeline.pasos_detalle
    idx = pipeline.paso_actual - 1

    pipeline.errores_acumulados += 1
    pipeline.ultimo_error = error_msg[:500]

    if idx < len(pasos):
        pasos[idx]['status'] = 'error' if not reintentable else 'reintentando'
        pasos[idx]['mensaje'] = error_msg[:200]
        pasos[idx]['intento'] = pipeline.intento_actual

    if not reintentable or pipeline.intento_actual >= pipeline.max_intentos:
        pipeline.estado = 'error'
        pipeline.mensaje_cliente = f'Error en {pipeline.paso_nombre}. Nuestro equipo fue notificado.'
        pipeline.pasos_detalle = pasos
        pipeline.save()
        logger.error(
            "Pipeline error definitivo: %s %s — %s",
            pipeline.empresa.rfc, pipeline.pipeline_type, error_msg[:200],
        )
        return pipeline

    # Calcular backoff con SAT Health awareness
    sat_pct = _get_sat_health_pct()

    if sat_pct is not None and sat_pct < 30:
        backoff_minutes = [30, 60, 120, 240, 480]
        pipeline.bloqueado_por_sat = True
        pipeline.mensaje_cliente = (
            f'El SAT reporta baja disponibilidad ({sat_pct:.0f}%). '
            f'Reintentaremos automáticamente cuando se recupere.'
        )
    elif sat_pct is not None and sat_pct < 70:
        backoff_minutes = [15, 30, 60, 120, 240]
        pipeline.bloqueado_por_sat = True
        pipeline.mensaje_cliente = 'El SAT está lento. Reintentando en unos minutos...'
    else:
        backoff_minutes = [5, 15, 30, 60, 120]
        pipeline.bloqueado_por_sat = False
        pipeline.mensaje_cliente = (
            f'Reintentando {pipeline.paso_nombre}... '
            f'(intento {pipeline.intento_actual + 1}/{pipeline.max_intentos})'
        )

    attempt_idx = min(pipeline.intento_actual - 1, len(backoff_minutes) - 1)
    wait_minutes = backoff_minutes[attempt_idx]

    pipeline.intento_actual += 1
    pipeline.proximo_intento = timezone.now() + timedelta(minutes=wait_minutes)
    pipeline.estado = 'reintentando'
    pipeline.pasos_detalle = pasos
    pipeline.save()

    logger.warning(
        "Pipeline reintento: %s %s — paso %s intento %d/%d (espera %dm) — %s",
        pipeline.empresa.rfc, pipeline.pipeline_type, pipeline.paso_nombre,
        pipeline.intento_actual, pipeline.max_intentos, wait_minutes,
        error_msg[:100],
    )
    return pipeline


def desbloquear_por_sat_health():
    """Desbloquea pipelines cuando SAT Health mejora. Llamar desde supervisor."""
    from core.models import PipelineState

    bloqueados = PipelineState.objects.filter(
        bloqueado_por_sat=True,
        estado__in=['reintentando', 'esperando_sat'],
    )

    sat_pct = _get_sat_health_pct()
    if sat_pct is None or sat_pct < 70:
        return 0

    count = 0
    for pipeline in bloqueados:
        pipeline.bloqueado_por_sat = False
        pipeline.proximo_intento = timezone.now()
        pipeline.mensaje_cliente = f'SAT recuperado. Reanudando {pipeline.paso_nombre}...'
        pipeline.save()
        count += 1

    if count:
        logger.info("SAT Health OK (%s%%) — %d pipelines desbloqueados", sat_pct, count)
    return count


def _get_sat_health_pct():
    """Obtiene el % de disponibilidad del SAT en los últimos 30 min."""
    from core.models import SATHealthProbe

    since = timezone.now() - timedelta(minutes=30)
    probes = SATHealthProbe.objects.filter(timestamp__gte=since)
    total = probes.count()
    if total == 0:
        return None
    success = probes.filter(result='success').count()
    return round((success / total) * 100, 1)
