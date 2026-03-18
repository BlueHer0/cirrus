"""Shared helpers for analysis views."""
import calendar
from datetime import datetime
from decimal import Decimal

from django.contrib.auth.decorators import login_required
from django.db.models import Sum, Count, Max, F
from django.shortcuts import redirect, render

MONTH_NAMES_ES = [
    "", "Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio",
    "Julio", "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre",
]

APP_LOGIN_URL = "/app/login/"


def fmt(val):
    """Format number with commas."""
    if val is None:
        val = 0
    v = float(val)
    if abs(v) >= 1_000_000:
        return f"{v / 1_000_000:,.1f}M"
    return f"{v:,.0f}"


def get_empresa_and_qs(request, empresa_id, year=None, month=None):
    """Validate access and return (empresa, base_qs) or (None, None).

    year and month are optional — omitting widens the filter.
    """
    from core.models import Empresa, CFDI
    try:
        empresa = Empresa.objects.get(id=empresa_id)
    except (Empresa.DoesNotExist, ValueError):
        return None, None
    if empresa.owner_id != request.user.id and not request.user.is_staff:
        return None, None
    qs = CFDI.objects.filter(rfc_empresa=empresa.rfc)
    if year:
        qs = qs.filter(fecha__year=year)
    if month:
        qs = qs.filter(fecha__month=month)
    return empresa, qs


def prev_month(year, month):
    """Return (year, month) for the previous month."""
    m = month - 1 if month > 1 else 12
    y = year if month > 1 else year - 1
    return y, m


def _detectar_duplicados(empresa, year=None, month=None):
    """Detect suspicious duplicate invoices: same emisor+monto within 3 days."""
    from core.models import CFDI

    qs = CFDI.objects.filter(
        rfc_empresa=empresa.rfc,
        rfc_receptor=empresa.rfc, tipo_comprobante="I",
    )
    if year:
        qs = qs.filter(fecha__year=year)
    if month:
        qs = qs.filter(fecha__month=month)

    # Group by (rfc_emisor, total) with 2+ occurrences
    grupos = qs.values("rfc_emisor", "total").annotate(
        count=Count("uuid"),
    ).filter(count__gte=2)

    sospechosos = 0
    for dup in grupos:
        cfdis = qs.filter(
            rfc_emisor=dup["rfc_emisor"], total=dup["total"],
        ).order_by("fecha")
        fechas = list(cfdis.values_list("fecha", flat=True))
        for i in range(len(fechas) - 1):
            if fechas[i] and fechas[i + 1]:
                delta = abs((fechas[i + 1] - fechas[i]).days)
                if delta <= 3:
                    sospechosos += 1
                    break
    return sospechosos


def calcular_fiscscore(empresa, year=None, month=None):
    """Calculate FiscScore for an empresa+period. Returns dict.

    year and month are optional — omitting widens the analysis range.
    """
    from core.models import CFDI
    from core.services.efos_sync import verificar_proveedores_empresa

    qs = CFDI.objects.filter(rfc_empresa=empresa.rfc)
    if year:
        qs = qs.filter(fecha__year=year)
    if month:
        qs = qs.filter(fecha__month=month)

    # Components
    emitidos_i = qs.filter(tipo_relacion="emitido", tipo_comprobante="I")
    recibidos_ie = qs.filter(tipo_relacion="recibido", tipo_comprobante__in=["I", "E"])

    ingresos = float(emitidos_i.aggregate(s=Sum("total"))["s"] or 0)
    gastos_total = float(recibidos_ie.aggregate(s=Sum("total"))["s"] or 0)

    # Cancelados
    cancelados = 0

    # Efectivo no deducible
    efectivo_no_ded = recibidos_ie.filter(forma_pago="01", total__gt=2000).count()
    efectivo_monto = float(recibidos_ie.filter(forma_pago="01", total__gt=2000).aggregate(s=Sum("total"))["s"] or 0)

    # PPD sin complemento
    ppd = qs.filter(metodo_pago="PPD").count()

    # Duplicados sospechosos (real detection)
    duplicados = _detectar_duplicados(empresa, year, month)

    # EFOS — real blacklist check
    alertas_efos = verificar_proveedores_empresa(empresa, year, month)

    # Deducibilidad
    no_ded_total = float(recibidos_ie.filter(forma_pago="01", total__gt=2000).aggregate(s=Sum("total"))["s"] or 0)
    gastos_ded = gastos_total - no_ded_total
    deducibilidad = round(gastos_ded / gastos_total * 100) if gastos_total > 0 else 100

    # Concentración cliente #1
    top_cliente = emitidos_i.values("rfc_receptor").annotate(m=Sum("total")).order_by("-m").first()
    conc_cliente = round(float(top_cliente["m"]) / ingresos * 100) if top_cliente and ingresos > 0 else 0
    diversificacion = max(100 - conc_cliente, 0)

    # IVA
    iva_trasladado = float(emitidos_i.aggregate(s=Sum("iva"))["s"] or 0)
    iva_acreditable = float(recibidos_ie.aggregate(s=Sum("iva"))["s"] or 0)
    iva_por_pagar = iva_trasladado - iva_acreditable

    # Score components (each 0-100)
    cumplimiento = 100
    if cancelados > 0:
        cumplimiento -= 10
    if efectivo_no_ded > 0:
        cumplimiento -= 15
    if ppd > 0:
        cumplimiento -= 10
    cumplimiento = max(cumplimiento, 0)

    consistencia_iva = 100
    if iva_por_pagar < 0:
        consistencia_iva = 70

    errores_cfdi = max(100 - cancelados * 5, 0)

    # Riesgo proveedores (real EFOS check)
    riesgo_proveedores = 100 if len(alertas_efos) == 0 else max(0, 100 - len(alertas_efos) * 25)

    # FiscScore with real EFOS component
    score = round(
        cumplimiento * 0.30
        + consistencia_iva * 0.20
        + riesgo_proveedores * 0.15
        + deducibilidad * 0.15
        + diversificacion * 0.10
        + errores_cfdi * 0.10
    )
    score = min(max(score, 0), 100)

    if score >= 80:
        label, color = "Excelente", "#34d399"
    elif score >= 60:
        label, color = "Buena", "#fbbf24"
    elif score >= 40:
        label, color = "Regular", "#f97316"
    else:
        label, color = "Riesgo", "#f87171"

    # SVG dasharray: full circle = 2*pi*50 ≈ 314
    score_dash = round(score / 100 * 314)

    return {
        "score": score,
        "label": label,
        "color": color,
        "score_dash": score_dash,
        "cumplimiento": cumplimiento,
        "deducibilidad": deducibilidad,
        "diversificacion": diversificacion,
        "consistencia_iva": consistencia_iva,
        "riesgo_proveedores": riesgo_proveedores,
        "alertas_efos": alertas_efos,
        "alertas": {
            "cancelados": cancelados,
            "efectivo_no_ded": efectivo_no_ded,
            "efectivo_monto": efectivo_monto,
            "ppd": ppd,
            "duplicados": duplicados,
        },
    }


