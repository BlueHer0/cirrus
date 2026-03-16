"""Fiscal Analysis API endpoints.

Provides summary, fiscal analysis, and IVA analysis for a given empresa/month.
Auth: Django session (client app) or API key.
"""

import calendar
import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from ninja import Router
from django.http import JsonResponse
from django.db.models import Sum, Count, Q, Avg, Max

logger = logging.getLogger("core.api.analysis")

router = Router(tags=["analysis"])

MONTH_NAMES = [
    "", "Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio",
    "Julio", "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre",
]

TIPO_LABELS = {"I": "Ingreso", "E": "Egreso", "P": "Pago", "N": "Nómina", "T": "Traslado"}

FORMA_PAGO_LABELS = {
    "01": "Efectivo", "02": "Cheque", "03": "Transferencia",
    "04": "Tarjeta de crédito", "06": "Dinero electrónico",
    "08": "Vales", "28": "Tarjeta de débito", "99": "Por definir",
}


def _check_access(request, empresa_id):
    """Return (empresa, error_response). Session or API key auth."""
    from core.models import Empresa

    try:
        empresa = Empresa.objects.get(id=empresa_id)
    except Empresa.DoesNotExist:
        return None, JsonResponse({"error": "Empresa not found"}, status=404)

    # Session auth (client app)
    if hasattr(request, "user") and request.user.is_authenticated:
        if empresa.owner_id == request.user.id or request.user.is_staff:
            return empresa, None

    # API key auth
    if hasattr(request, "api_empresas"):
        if empresa in request.api_empresas.all():
            return empresa, None

    return None, JsonResponse({"error": "Access denied"}, status=403)


def _get_base_qs(empresa, year, month):
    """Return base QuerySet of CFDIs for empresa/year/month."""
    from core.models import CFDI
    return CFDI.objects.filter(
        rfc_empresa=empresa.rfc,
        fecha__year=year,
        fecha__month=month,
    )


def _dec(val):
    """Convert Decimal/None to float for JSON."""
    if val is None:
        return 0.0
    return float(round(val, 2))


# ── ENDPOINT 1: Summary ──────────────────────────────────────────────

@router.get("/summary/", summary="Monthly summary analysis")
def analysis_summary(
    request,
    empresa_id: str,
    year: int,
    month: int,
):
    empresa, err = _check_access(request, empresa_id)
    if err:
        return err

    qs = _get_base_qs(empresa, year, month)

    # Basic counts
    total = qs.count()
    emitidos = qs.filter(tipo_relacion="emitido").count()
    recibidos = qs.filter(tipo_relacion="recibido").count()
    cancelados = 0  # estatus_sat field not yet available

    # Montos (type I = Ingreso only)
    monto_facturado = _dec(
        qs.filter(tipo_relacion="emitido", tipo_comprobante="I")
        .aggregate(s=Sum("total"))["s"]
    )
    monto_recibido = _dec(
        qs.filter(tipo_relacion="recibido", tipo_comprobante="I")
        .aggregate(s=Sum("total"))["s"]
    )
    resultado = round(monto_facturado - monto_recibido, 2)

    emitidos_i = qs.filter(tipo_relacion="emitido", tipo_comprobante="I").count()
    ticket_promedio = round(monto_facturado / emitidos_i, 2) if emitidos_i else 0
    factura_max = _dec(
        qs.filter(tipo_relacion="emitido", tipo_comprobante="I")
        .aggregate(m=Max("total"))["m"]
    )

    # Delta vs previous month
    prev_month = month - 1 if month > 1 else 12
    prev_year = year if month > 1 else year - 1
    prev_qs = _get_base_qs(empresa, prev_year, prev_month)
    prev_total = prev_qs.count()
    prev_emitidos = prev_qs.filter(tipo_relacion="emitido").count()
    prev_recibidos = prev_qs.filter(tipo_relacion="recibido").count()

    # Por tipo comprobante
    por_tipo_raw = (
        qs.values("tipo_comprobante")
        .annotate(count=Count("uuid"))
        .order_by("-count")
    )
    por_tipo = [
        {
            "tipo": r["tipo_comprobante"],
            "label": TIPO_LABELS.get(r["tipo_comprobante"], r["tipo_comprobante"]),
            "count": r["count"],
        }
        for r in por_tipo_raw
    ]

    # Por forma de pago
    por_fp_raw = (
        qs.exclude(forma_pago="")
        .values("forma_pago")
        .annotate(count=Count("uuid"))
        .order_by("-count")
    )
    fp_total = sum(r["count"] for r in por_fp_raw) or 1
    por_forma_pago = [
        {
            "codigo": r["forma_pago"],
            "label": FORMA_PAGO_LABELS.get(r["forma_pago"], r["forma_pago"]),
            "porcentaje": round(r["count"] / fp_total * 100),
        }
        for r in por_fp_raw[:6]
    ]

    # Actividad diaria
    from django.db.models.functions import ExtractDay
    daily_raw = (
        qs.annotate(dia=ExtractDay("fecha"))
        .values("dia")
        .annotate(count=Count("uuid"), monto=Sum("total"))
        .order_by("dia")
    )
    days_in_month = calendar.monthrange(year, month)[1]
    daily_map = {r["dia"]: r for r in daily_raw}
    actividad_diaria = [
        {
            "dia": d,
            "count": daily_map[d]["count"] if d in daily_map else 0,
            "monto": _dec(daily_map[d]["monto"]) if d in daily_map else 0,
        }
        for d in range(1, days_in_month + 1)
    ]

    # Top 5 clientes (emitidos → group by receptor)
    top_clientes = list(
        qs.filter(tipo_relacion="emitido", tipo_comprobante="I")
        .values("rfc_receptor")
        .annotate(monto=Sum("total"))
        .order_by("-monto")[:5]
    )
    top_clientes = [{"rfc": r["rfc_receptor"], "monto": _dec(r["monto"])} for r in top_clientes]

    # Top 5 proveedores (recibidos → group by emisor)
    top_proveedores = list(
        qs.filter(tipo_relacion="recibido", tipo_comprobante="I")
        .values("rfc_emisor")
        .annotate(monto=Sum("total"))
        .order_by("-monto")[:5]
    )
    top_proveedores = [{"rfc": r["rfc_emisor"], "monto": _dec(r["monto"])} for r in top_proveedores]

    # Alertas
    efectivo_no_deducible = qs.filter(forma_pago="01", total__gt=2000).count()
    ppd_sin_complemento = qs.filter(metodo_pago="PPD").count()

    return {
        "periodo": f"{MONTH_NAMES[month]} {year}",
        "empresa": empresa.rfc,
        "total_cfdi": total,
        "emitidos": emitidos,
        "recibidos": recibidos,
        "cancelados": cancelados,
        "delta_vs_anterior": {
            "total": total - prev_total,
            "emitidos": emitidos - prev_emitidos,
            "recibidos": recibidos - prev_recibidos,
        },
        "monto_facturado": monto_facturado,
        "monto_recibido": monto_recibido,
        "resultado_estimado": resultado,
        "ticket_promedio": ticket_promedio,
        "factura_max": factura_max,
        "por_tipo": por_tipo,
        "por_forma_pago": por_forma_pago,
        "actividad_diaria": actividad_diaria,
        "top_clientes": top_clientes,
        "top_proveedores": top_proveedores,
        "alertas": {
            "cancelados": cancelados,
            "efectivo_no_deducible": efectivo_no_deducible,
            "ppd_sin_complemento": ppd_sin_complemento,
            "listas_negras": 0,
        },
    }


# ── ENDPOINT 2: Fiscal Analysis ──────────────────────────────────────

@router.get("/fiscal/", summary="Fiscal analysis (ISR/deductions)")
def analysis_fiscal(
    request,
    empresa_id: str,
    year: int,
    month: int,
):
    empresa, err = _check_access(request, empresa_id)
    if err:
        return err

    qs = _get_base_qs(empresa, year, month)

    # Ingresos = emitidos tipo I
    ingresos = _dec(
        qs.filter(tipo_relacion="emitido", tipo_comprobante="I")
        .aggregate(s=Sum("total"))["s"]
    )

    # Gastos recibidos tipo I
    gastos_total_qs = qs.filter(tipo_relacion="recibido", tipo_comprobante__in=["I", "E"])

    # Non-deductible: efectivo > 2000 OR cancelado
    no_deducible_efectivo = _dec(
        gastos_total_qs.filter(forma_pago="01", total__gt=2000)
        .aggregate(s=Sum("total"))["s"]
    )
    no_deducible_cancelado = 0  # estatus_sat field not yet available

    gastos_no_deducibles = round(no_deducible_efectivo + no_deducible_cancelado, 2)
    gastos_total = _dec(gastos_total_qs.aggregate(s=Sum("total"))["s"])
    gastos_deducibles = round(gastos_total - gastos_no_deducibles, 2)

    utilidad_fiscal = round(ingresos - gastos_deducibles, 2)
    isr_provisional = round(utilidad_fiscal * 0.30, 2) if utilidad_fiscal > 0 else 0

    # Retenciones from received CFDIs
    ret_agg = qs.filter(tipo_relacion="recibido").aggregate(
        isr=Sum("isr_retenido"), iva=Sum("iva_retenido"),
    )
    retenciones_isr = _dec(ret_agg["isr"])
    retenciones_iva = _dec(ret_agg["iva"])

    # Deducibilidad breakdown
    total_gastos = gastos_total or 1
    deducible_pct = round(gastos_deducibles / total_gastos * 100) if total_gastos > 0 else 100

    motivos = []
    if no_deducible_efectivo > 0:
        cnt = gastos_total_qs.filter(forma_pago="01", total__gt=2000).count()
        motivos.append({"motivo": "Pago en efectivo > $2,000", "count": cnt, "monto": no_deducible_efectivo})
    # cancelado motivos disabled until estatus_sat field is added

    # Delta vs previous month
    prev_month = month - 1 if month > 1 else 12
    prev_year = year if month > 1 else year - 1
    prev_qs = _get_base_qs(empresa, prev_year, prev_month)
    prev_ingresos = _dec(
        prev_qs.filter(tipo_relacion="emitido", tipo_comprobante="I")
        .aggregate(s=Sum("total"))["s"]
    )
    prev_gastos = _dec(
        prev_qs.filter(tipo_relacion="recibido", tipo_comprobante__in=["I", "E"])
        .aggregate(s=Sum("total"))["s"]
    )
    delta_ingresos = round((ingresos - prev_ingresos) / prev_ingresos * 100) if prev_ingresos else 0
    delta_gastos = round((gastos_total - prev_gastos) / prev_gastos * 100) if prev_gastos else 0

    return {
        "ingresos": ingresos,
        "gastos_deducibles": gastos_deducibles,
        "gastos_no_deducibles": gastos_no_deducibles,
        "utilidad_fiscal": utilidad_fiscal,
        "isr_provisional": isr_provisional,
        "retenciones_isr": retenciones_isr,
        "retenciones_iva": retenciones_iva,
        "deducibilidad": {
            "deducible_pct": deducible_pct,
            "no_deducible_pct": 100 - deducible_pct,
            "motivos": motivos,
        },
        "delta_ingresos": delta_ingresos,
        "delta_gastos": delta_gastos,
    }


# ── ENDPOINT 3: IVA Analysis ─────────────────────────────────────────

@router.get("/iva/", summary="IVA analysis for period")
def analysis_iva(
    request,
    empresa_id: str,
    year: int,
    month: int,
):
    empresa, err = _check_access(request, empresa_id)
    if err:
        return err

    qs = _get_base_qs(empresa, year, month)

    # IVA trasladado = IVA of emitidos tipo I
    iva_trasladado = _dec(
        qs.filter(tipo_relacion="emitido", tipo_comprobante="I")
        .aggregate(s=Sum("iva"))["s"]
    )

    # IVA acreditable = IVA of recibidos tipo I (only deductible)
    recibidos_i = qs.filter(tipo_relacion="recibido", tipo_comprobante__in=["I", "E"])
    iva_acreditable_total = _dec(recibidos_i.aggregate(s=Sum("iva"))["s"])

    # Non-acreditable (cash > 2000)
    iva_efectivo = _dec(
        recibidos_i.filter(forma_pago="01", total__gt=2000)
        .aggregate(s=Sum("iva"))["s"]
    )
    iva_acreditable = round(iva_acreditable_total - iva_efectivo, 2)
    iva_por_pagar = round(iva_trasladado - iva_acreditable, 2)

    # Retenciones
    iva_retenido = _dec(
        qs.filter(tipo_relacion="recibido").aggregate(s=Sum("iva_retenido"))["s"]
    )

    # IVA por tasa (approximate from iva/subtotal ratio)
    from django.db.models import F, Case, When, Value, DecimalField
    from django.db.models.functions import Round

    # Group by approximate tax rate
    emitidos_i = qs.filter(tipo_relacion="emitido", tipo_comprobante="I", subtotal__gt=0)

    iva_16 = _dec(emitidos_i.filter(
        iva__gt=F("subtotal") * Decimal("0.10")
    ).aggregate(s=Sum("iva"))["s"])

    iva_8 = _dec(emitidos_i.filter(
        iva__gt=0,
        iva__lte=F("subtotal") * Decimal("0.10"),
    ).aggregate(s=Sum("iva"))["s"])

    iva_0 = _dec(emitidos_i.filter(iva=0).aggregate(s=Sum("total"))["s"])

    por_tasa = [
        {"tasa": "16%", "monto": iva_16},
        {"tasa": "8%", "monto": iva_8},
        {"tasa": "0%", "monto": iva_0},
    ]

    # Tendencia últimos 6 meses
    tendencia = []
    for i in range(5, -1, -1):
        m = month - i
        y = year
        while m <= 0:
            m += 12
            y -= 1
        t_qs = _get_base_qs(empresa, y, m)
        t_trasladado = _dec(
            t_qs.filter(tipo_relacion="emitido", tipo_comprobante="I")
            .aggregate(s=Sum("iva"))["s"]
        )
        t_acreditable = _dec(
            t_qs.filter(tipo_relacion="recibido", tipo_comprobante__in=["I", "E"])
            .aggregate(s=Sum("iva"))["s"]
        )
        tendencia.append({
            "mes": f"{MONTH_NAMES[m][:3]} {y}",
            "iva_pagar": round(t_trasladado - t_acreditable, 2),
        })

    return {
        "iva_trasladado": iva_trasladado,
        "iva_acreditable": iva_acreditable,
        "iva_por_pagar": iva_por_pagar,
        "iva_retenido": iva_retenido,
        "iva_efectivo_no_acreditable": iva_efectivo,
        "por_tasa": por_tasa,
        "tendencia": tendencia,
    }
