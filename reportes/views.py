"""
Reportes Views — Selector, pantalla, PDF, IA, email trigger.
"""

import json
import logging
import calendar
from datetime import date, datetime
from decimal import Decimal

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, JsonResponse, HttpResponseForbidden
from django.shortcuts import render, get_object_or_404
from django.views.decorators.http import require_POST

from core.models import Empresa, CFDI
from core.services.colaboradores import get_empresas_visibles
from reportes.services import calcular_reporte, generar_resumen_ia

logger = logging.getLogger("reportes")

APP_LOGIN_URL = "/app/login/"

MONTH_NAMES = [
    "", "Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio",
    "Julio", "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre",
]


# ── Helpers ───────────────────────────────────────────────────────────


def _parse_params(request):
    """Parse report parameters from GET query string.
    
    The selector form sends: tipo_periodo, year, mes_inicio, mes_fin.
    We also accept legacy names (tipo, anio, mes) for backwards compat.
    """
    empresa_id = request.GET.get("empresa_id", "")
    tipo = request.GET.get("tipo_periodo") or request.GET.get("tipo", "mes")
    anio = int(request.GET.get("year") or request.GET.get("anio", datetime.now().year))
    mes = int(request.GET.get("mes_inicio") or request.GET.get("mes", datetime.now().month))
    mes_inicio = int(request.GET.get("mes_inicio", mes))
    mes_fin = int(request.GET.get("mes_fin", 12))

    # Normalize tipo names: selector sends 'anual', view expects 'anio'
    if tipo == "anual":
        tipo = "anio"

    return {
        "empresa_id": empresa_id,
        "tipo": tipo,
        "anio": anio,
        "mes": mes,
        "mes_inicio": mes_inicio,
        "mes_fin": mes_fin,
    }


def _periodo_from_params(params):
    """Derive fecha_inicio, fecha_fin from params."""
    anio = params["anio"]
    tipo = params["tipo"]

    if tipo == "mes":
        mes = params["mes"]
        fecha_inicio = date(anio, mes, 1)
        fecha_fin = date(anio, mes, calendar.monthrange(anio, mes)[1])
    elif tipo == "rango":
        mes_inicio = params["mes_inicio"]
        mes_fin = params["mes_fin"]
        fecha_inicio = date(anio, mes_inicio, 1)
        fecha_fin = date(anio, mes_fin, calendar.monthrange(anio, mes_fin)[1])
    else:  # anio
        fecha_inicio = date(anio, 1, 1)
        fecha_fin = date(anio, 12, 31)

    return fecha_inicio, fecha_fin


def _validate_empresa_access(request, empresa_id):
    """Get empresa or return HttpResponseForbidden."""
    empresas_visibles = get_empresas_visibles(request.user)
    empresa = empresas_visibles.filter(id=empresa_id).first()
    if not empresa:
        return None
    return empresa


# ── Views ─────────────────────────────────────────────────────────────


@login_required(login_url=APP_LOGIN_URL)
def selector_view(request):
    """Report selector — choose empresa + period."""
    empresas = get_empresas_visibles(request.user).order_by("rfc")
    now = datetime.now()

    # Dynamic year range: from oldest CFDI year to current year
    rfcs = list(empresas.values_list("rfc", flat=True))
    oldest_cfdi = (
        CFDI.objects.filter(rfc_empresa__in=rfcs)
        .order_by("fecha")
        .values_list("fecha", flat=True)
        .first()
    )
    min_year = oldest_cfdi.year if oldest_cfdi else now.year
    years = list(range(now.year, min_year - 1, -1))

    return render(request, "reportes/selector.html", {
        "current_page": "reportes",
        "empresas": empresas,
        "current_year": now.year,
        "current_month": now.month,
        "years": years,
        "months": [(i, MONTH_NAMES[i]) for i in range(1, 13)],
    })


@login_required(login_url=APP_LOGIN_URL)
def ver_view(request):
    """On-screen HTML report (dark mode)."""
    params = _parse_params(request)
    empresa = _validate_empresa_access(request, params["empresa_id"])
    if not empresa:
        return HttpResponseForbidden("No tienes acceso a esta empresa.")

    fecha_inicio, fecha_fin = _periodo_from_params(params)

    # Check if there are any CFDIs for this period
    has_data = CFDI.objects.filter(
        rfc_empresa=empresa.rfc,
        fecha__date__gte=fecha_inicio,
        fecha__date__lte=fecha_fin,
        estado_sat="vigente",
    ).exists()

    if not has_data:
        return render(request, "reportes/sin_datos.html", {
            "current_page": "reportes",
            "empresa": empresa,
            "periodo_label": f"{MONTH_NAMES[fecha_inicio.month]} {fecha_inicio.year}" if fecha_inicio.month == fecha_fin.month else f"{fecha_inicio.year}",
        })

    datos = calcular_reporte(empresa.id, fecha_inicio, fecha_fin, request.user)

    # Build PDF URL with same params
    qs_string = request.META.get("QUERY_STRING", "")
    pdf_url = f"/reportes/pdf/?{qs_string}"

    # Calendario fiscal
    now = datetime.now()
    calendario = _build_calendario_fiscal(now.year, now)

    return render(request, "reportes/reporte_pantalla.html", {
        "current_page": "reportes",
        "empresa": empresa,
        "datos": datos,
        "params": params,
        "pdf_url": pdf_url,
        "calendario": calendario,
        "anio_actual": now.year,
    })


@login_required(login_url=APP_LOGIN_URL)
def pdf_view(request):
    """PDF download via WeasyPrint."""
    from weasyprint import HTML

    params = _parse_params(request)
    empresa = _validate_empresa_access(request, params["empresa_id"])
    if not empresa:
        return HttpResponseForbidden("No tienes acceso a esta empresa.")

    fecha_inicio, fecha_fin = _periodo_from_params(params)

    from django.template.loader import render_to_string

    datos = calcular_reporte(empresa.id, fecha_inicio, fecha_fin, request.user)

    html_string = render_to_string("reportes/reporte_pdf.html", {
        "datos": datos,
        "empresa": empresa,
        "fecha_generacion": datetime.now().strftime("%d/%m/%Y %H:%M"),
    })

    pdf_bytes = HTML(string=html_string).write_pdf()

    filename = f"Cirrus_Reporte_{empresa.rfc}_{datos['periodo_label'].replace(' ', '_')}.pdf"
    response = HttpResponse(pdf_bytes, content_type="application/pdf")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


@login_required(login_url=APP_LOGIN_URL)
@require_POST
def generar_ia_view(request):
    """Generate AI summary via Anthropic Claude. Returns JSON."""
    try:
        body = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    empresa_id = body.get("empresa_id", "")
    fecha_inicio_str = body.get("fecha_inicio", "")
    fecha_fin_str = body.get("fecha_fin", "")

    if not empresa_id or not fecha_inicio_str or not fecha_fin_str:
        return JsonResponse({"error": "Missing parameters"}, status=400)

    empresa = _validate_empresa_access(request, empresa_id)
    if not empresa:
        return JsonResponse({"error": "Access denied"}, status=403)

    try:
        fecha_inicio = date.fromisoformat(fecha_inicio_str)
        fecha_fin = date.fromisoformat(fecha_fin_str)
    except ValueError:
        return JsonResponse({"error": "Invalid date format"}, status=400)

    datos = calcular_reporte(empresa_id, fecha_inicio, fecha_fin, request.user)
    resumen = generar_resumen_ia(datos)

    return JsonResponse({"resumen": resumen})


@login_required(login_url=APP_LOGIN_URL)
@require_POST
def trigger_email_view(request):
    """
    Trigger email report for a specific empresa+month.
    Called by the download process when a job completes.
    """
    try:
        body = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    empresa_id = body.get("empresa_id", "")
    anio = int(body.get("anio", 0))
    mes = int(body.get("mes", 0))

    if not empresa_id or not anio or not mes:
        return JsonResponse({"error": "Missing parameters"}, status=400)

    empresa = _validate_empresa_access(request, empresa_id)
    if not empresa:
        return JsonResponse({"error": "Access denied"}, status=403)

    from reportes.tasks import enviar_reporte_mensual_email
    enviar_reporte_mensual_email.delay(str(empresa_id), anio, mes)

    return JsonResponse({"ok": True, "message": f"Email queued for {empresa.rfc} {anio}-{mes:02d}"})


# ── Helpers ───────────────────────────────────────────────────────────


def _build_calendario_fiscal(anio, now):
    """Build calendar of fiscal deadlines for the year."""
    from datetime import timedelta

    eventos = []

    # Monthly declarations (17th of each month)
    for m in range(1, 13):
        try:
            d = date(anio, m, 17)
        except ValueError:
            continue
        dias_restantes = (d - now.date()).days if hasattr(now, 'date') else (d - now).days
        urgente = 0 <= dias_restantes <= 15
        eventos.append({
            "fecha": d,
            "titulo": f"Declaración mensual {MONTH_NAMES[m]}",
            "urgente": urgente,
            "pasado": dias_restantes < 0,
            "dias_restantes": dias_restantes,
        })

    # Annual PM: March 31
    d = date(anio, 3, 31)
    dias_restantes = (d - now.date()).days if hasattr(now, 'date') else (d - now).days
    eventos.append({
        "fecha": d,
        "titulo": "Declaración anual Personas Morales",
        "urgente": 0 <= dias_restantes <= 15,
        "pasado": dias_restantes < 0,
        "dias_restantes": dias_restantes,
    })

    # Annual PF: April 30
    d = date(anio, 4, 30)
    dias_restantes = (d - now.date()).days if hasattr(now, 'date') else (d - now).days
    eventos.append({
        "fecha": d,
        "titulo": "Declaración anual Personas Físicas",
        "urgente": 0 <= dias_restantes <= 15,
        "pasado": dias_restantes < 0,
        "dias_restantes": dias_restantes,
    })

    # Sort by date and filter: show upcoming + recent past
    eventos.sort(key=lambda e: e["fecha"])
    return [e for e in eventos if e["dias_restantes"] >= -30]
