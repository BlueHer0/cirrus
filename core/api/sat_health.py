"""SAT Health Monitor — API endpoints for dashboard/charts.

Django Ninja router with endpoints for:
- Current SAT status
- Hourly availability time series
- Day×hour heatmap
- Individual probe list with filters
- Worker node health check
"""

from ninja import Router
from django.utils import timezone

from core.models import SATHealthProbe, SATHealthSummary

router = Router(tags=["SAT Health"])


@router.get("/current")
def sat_health_current(request):
    """Estado actual del SAT basado en últimos 30 minutos."""
    since = timezone.now() - timezone.timedelta(minutes=30)
    recent = SATHealthProbe.objects.filter(timestamp__gte=since)
    total = recent.count()
    success = recent.filter(result='success').count()

    if total == 0:
        status = 'unknown'
        availability = None
    elif (success / total) >= 0.7:
        status = 'up'
        availability = round((success / total) * 100, 1)
    elif (success / total) >= 0.3:
        status = 'degraded'
        availability = round((success / total) * 100, 1)
    else:
        status = 'down'
        availability = round((success / total) * 100, 1)

    last_probe = SATHealthProbe.objects.first()

    return {
        'status': status,
        'availability_pct': availability,
        'total_probes': total,
        'successful': success,
        'last_probe': last_probe.timestamp.isoformat() if last_probe else None,
    }


@router.get("/hourly")
def sat_health_hourly(request, days: int = 7):
    """Resúmenes horarios para gráfica de disponibilidad."""
    since = timezone.now() - timezone.timedelta(days=days)
    summaries = SATHealthSummary.objects.filter(hour__gte=since).order_by('hour')

    return [{
        'hour': s.hour.isoformat(),
        'availability_pct': s.availability_pct,
        'total_probes': s.total_probes,
        'avg_time_ms': s.avg_total_time_ms,
        'most_common_error': s.most_common_error,
    } for s in summaries]


@router.get("/heatmap")
def sat_health_heatmap(request, days: int = 30):
    """
    Datos para heatmap: disponibilidad por hora del día × día de la semana.
    Ideal para identificar patrones (ej: lunes 10AM siempre falla).
    """
    since = timezone.now() - timezone.timedelta(days=days)
    summaries = SATHealthSummary.objects.filter(hour__gte=since)

    # Agregar por hora del día (0-23) y día de la semana (0=lunes, 6=domingo)
    heatmap = {}
    for s in summaries:
        dow = s.hour.weekday()  # 0=lunes
        hod = s.hour.hour       # 0-23
        key = f"{dow}_{hod}"
        if key not in heatmap:
            heatmap[key] = {'total': 0, 'success': 0}
        heatmap[key]['total'] += s.total_probes
        heatmap[key]['success'] += s.successful_probes

    result = []
    for key, data in heatmap.items():
        dow, hod = key.split('_')
        result.append({
            'day_of_week': int(dow),
            'hour_of_day': int(hod),
            'availability_pct': round((data['success'] / data['total']) * 100, 1) if data['total'] > 0 else None,
            'total_probes': data['total'],
        })

    return sorted(result, key=lambda x: (x['day_of_week'], x['hour_of_day']))


@router.get("/probes")
def sat_health_probes(request, limit: int = 50, result: str = None, node: str = None, rfc: str = None):
    """Lista de probes individuales con filtros."""
    qs = SATHealthProbe.objects.all()
    if result:
        qs = qs.filter(result=result)
    if node:
        qs = qs.filter(node_id=node)
    if rfc:
        qs = qs.filter(rfc_used=rfc)

    probes = qs[:limit]
    return [{
        'id': str(p.id),
        'timestamp': p.timestamp.isoformat(),
        'node_id': p.node_id,
        'rfc_used': p.rfc_used,
        'result': p.result,
        'last_phase': p.last_phase_reached,
        'time_total_ms': p.time_total_ms,
        'time_dns_ms': p.time_dns_ms,
        'time_page_load_ms': p.time_page_load_ms,
        'time_form_visible_ms': p.time_form_visible_ms,
        'time_fiel_upload_ms': p.time_fiel_upload_ms,
        'time_login_submit_ms': p.time_login_submit_ms,
        'time_session_active_ms': p.time_session_active_ms,
        'error_message': p.error_message,
        'has_screenshot': bool(p.screenshot_path),
    } for p in probes]


@router.get("/nodes")
def sat_health_nodes(request):
    """Estado de cada nodo worker."""
    import httpx

    nodes_config = [
        {'id': 'vps2',  'url': 'http://10.20.0.2:8300'},
        {'id': 'vpsx',  'url': 'http://10.20.0.100:8300'},
        {'id': 'spark', 'url': 'http://10.20.0.6:8300'},
    ]

    nodes_status = []
    for node in nodes_config:
        try:
            resp = httpx.get(f"{node['url']}/health", timeout=5)
            nodes_status.append({
                'id': node['id'],
                'status': 'online' if resp.status_code == 200 else 'error',
            })
        except Exception:
            nodes_status.append({
                'id': node['id'],
                'status': 'offline',
            })

    return nodes_status
