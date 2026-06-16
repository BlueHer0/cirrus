"""
Reportes Services — Cálculo de reportes fiscales en tiempo real.

Todas las funciones operan sobre datos en BD, nada se persiste.
"""

import logging
from datetime import date, timedelta
from decimal import Decimal

from django.db.models import Sum, Count, Q, F, Max

from core.models import Empresa, CFDI, EFOS

# EFOS segmentado por situacion (campo del SAT 69-B).
# Solo Definitivo y Presunto cuentan como riesgo fiscal real.
EFOS_SITUACIONES_RIESGO = {"Definitivo", "Presunto"}
EFOS_SITUACIONES_LIMPIAS = {"Desvirtuado", "Sentencia Favorable"}

logger = logging.getLogger("reportes.services")

MONTH_NAMES = [
    "", "Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio",
    "Julio", "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre",
]

MONTH_SHORT = [
    "", "Ene", "Feb", "Mar", "Abr", "May", "Jun",
    "Jul", "Ago", "Sep", "Oct", "Nov", "Dic",
]

FORMA_PAGO_LABELS = {
    "01": "Efectivo",
    "02": "Cheque",
    "03": "Transferencia",
    "04": "Tarjeta crédito",
    "05": "Monedero electrónico",
    "06": "Dinero electrónico",
    "08": "Vales de despensa",
    "12": "Dación en pago",
    "13": "Pago por subrogación",
    "14": "Pago por consignación",
    "15": "Condonación",
    "17": "Compensación",
    "23": "Novación",
    "24": "Confusión",
    "25": "Remisión de deuda",
    "26": "Prescripción o caducidad",
    "27": "A satisfacción del acreedor",
    "28": "Tarjeta débito",
    "29": "Tarjeta de servicios",
    "30": "Aplicación de anticipos",
    "31": "Intermediario pagos",
    "99": "Por definir",
}


def _safe(val):
    """Convert None to Decimal(0)."""
    return val if val is not None else Decimal("0")


def calcular_reporte(empresa_id, fecha_inicio, fecha_fin, usuario):
    """
    Calcula todos los datos para un reporte fiscal ejecutivo.

    Args:
        empresa_id: UUID de la empresa
        fecha_inicio: date — primer día del periodo
        fecha_fin: date — último día del periodo
        usuario: User — para validación de propiedad

    Returns:
        dict con todos los datos del reporte
    """
    from core.services.colaboradores import get_empresas_visibles

    empresa = get_empresas_visibles(usuario).get(id=empresa_id)
    rfc = empresa.rfc

    # ── Periodo label ───────────────────────────────────────────────
    if fecha_inicio.month == fecha_fin.month and fecha_inicio.year == fecha_fin.year:
        periodo_label = f"{MONTH_NAMES[fecha_inicio.month]} {fecha_inicio.year}"
    elif fecha_inicio.month == 1 and fecha_fin.month == 12 and fecha_inicio.year == fecha_fin.year:
        periodo_label = f"{fecha_inicio.year}"
    else:
        periodo_label = (
            f"{MONTH_SHORT[fecha_inicio.month]} – "
            f"{MONTH_SHORT[fecha_fin.month]} {fecha_fin.year}"
        )

    # ── Base queryset ───────────────────────────────────────────────
    cfdis = CFDI.objects.filter(
        rfc_empresa=rfc,
        fecha__date__gte=fecha_inicio,
        fecha__date__lte=fecha_fin,
        estado_sat="vigente",
    )

    cfdi_count = cfdis.count()

    # ── CFDI sets ───────────────────────────────────────────────────
    # INGRESOS: CFDIs tipo I donde la empresa es el EMISOR (ventas facturadas).
    # Nota: tipo 'E' (notas de credito/debito) y tipo 'N' (nomina) no entran
    # en este conjunto; el criterio de ajustes lo evaluara la contadora.
    ingresos_qs = CFDI.objects.filter(
        rfc_empresa=rfc,
        tipo_comprobante='I',
        rfc_emisor=rfc,
        fecha__date__gte=fecha_inicio,
        fecha__date__lte=fecha_fin,
        estado_sat='vigente'
    )

    # GASTOS: CFDIs tipo I donde la empresa es el RECEPTOR (compras recibidas).
    # Antes filtraba tipo 'E' lo cual solo capturaba notas de credito; las
    # compras reales son tipo 'I' con la empresa como receptor.
    gastos_qs = CFDI.objects.filter(
        rfc_empresa=rfc,
        tipo_comprobante='I',
        rfc_receptor=rfc,
        fecha__date__gte=fecha_inicio,
        fecha__date__lte=fecha_fin,
        estado_sat='vigente'
    )

    # PAGOS tipo P
    pagos_qs = CFDI.objects.filter(
        rfc_empresa=rfc,
        tipo_comprobante='P',
        fecha__date__gte=fecha_inicio,
        fecha__date__lte=fecha_fin,
        estado_sat='vigente'
    )

    # NOTAS DE CREDITO/DEBITO tipo 'E' — ajuste a ingresos/gastos (criterio 1).
    # Notas E recibidas (rfc_receptor=empresa) restan de gastos y de IVA acreditable.
    # Notas E emitidas (rfc_emisor=empresa)  restan de ingresos y de IVA trasladado.
    notas_e_recibidas_qs = CFDI.objects.filter(
        rfc_empresa=rfc, tipo_comprobante='E', rfc_receptor=rfc,
        fecha__date__gte=fecha_inicio, fecha__date__lte=fecha_fin,
        estado_sat='vigente',
    )
    notas_e_emitidas_qs = CFDI.objects.filter(
        rfc_empresa=rfc, tipo_comprobante='E', rfc_emisor=rfc,
        fecha__date__gte=fecha_inicio, fecha__date__lte=fecha_fin,
        estado_sat='vigente',
    )
    notas_e_recibidas_total = _safe(notas_e_recibidas_qs.aggregate(s=Sum("total"))["s"])
    notas_e_recibidas_iva = _safe(notas_e_recibidas_qs.aggregate(s=Sum("iva"))["s"])
    notas_e_emitidas_total = _safe(notas_e_emitidas_qs.aggregate(s=Sum("total"))["s"])
    notas_e_emitidas_iva = _safe(notas_e_emitidas_qs.aggregate(s=Sum("iva"))["s"])

    # ── KPIs ────────────────────────────────────────────────────────
    # Ingresos y gastos NETOS (despues de notas E como ajuste, criterio 1).
    total_ingresos_bruto = _safe(ingresos_qs.aggregate(s=Sum("total"))["s"])
    total_gastos_bruto = _safe(gastos_qs.aggregate(s=Sum("total"))["s"])
    total_ingresos = total_ingresos_bruto - notas_e_emitidas_total
    total_gastos = total_gastos_bruto - notas_e_recibidas_total

    # Riesgo de no deducibilidad: efectivo > $2,000 — INFORMATIVO, no resta auto
    # (criterio 2). Se expone como lista para revision manual; el resultado fiscal
    # ya NO descuenta este monto automaticamente.
    gastos_riesgo_efectivo_qs = gastos_qs.filter(forma_pago="01", total__gt=2000)
    total_riesgo_no_deducible = _safe(gastos_riesgo_efectivo_qs.aggregate(s=Sum("total"))["s"])
    iva_riesgo_no_deducible = _safe(gastos_riesgo_efectivo_qs.aggregate(s=Sum("iva"))["s"])
    # Aliases retrocompatibles (mismos valores, ahora INFORMATIVOS).
    total_no_deducible = total_riesgo_no_deducible
    iva_no_acreditable = iva_riesgo_no_deducible

    # Resultado fiscal: ingresos netos - gastos netos (sin restar el riesgo
    # de efectivo>$2K, que ahora es informativo).
    total_gastos_deducibles = total_gastos
    resultado_fiscal = total_ingresos - total_gastos_deducibles

    # ── IVA ─────────────────────────────────────────────────────────
    # IVA trasladado = IVA cobrado en ventas - IVA de notas E emitidas (devoluciones).
    iva_trasladado_bruto = _safe(ingresos_qs.aggregate(s=Sum("iva"))["s"])
    iva_trasladado = iva_trasladado_bruto - notas_e_emitidas_iva
    # IVA acreditable = IVA pagado en compras - IVA de notas E recibidas.
    # Criterio 2: NO se excluye el IVA de gastos efectivo>$2K — todo IVA legitimo
    # entra al acreditable; el riesgo de efectivo se reporta aparte como aviso.
    iva_acreditable_bruto = _safe(gastos_qs.aggregate(s=Sum("iva"))["s"])
    iva_acreditable = iva_acreditable_bruto - notas_e_recibidas_iva
    iva_retenido_total = _safe(gastos_qs.aggregate(s=Sum("iva_retenido"))["s"])
    iva_neto = iva_trasladado - iva_acreditable  # positivo=a pagar, negativo=a favor
    isr_retenido = _safe(gastos_qs.aggregate(s=Sum("isr_retenido"))["s"])
    isr_provisional = max(resultado_fiscal * Decimal("0.30"), Decimal("0"))

    # Reserva fiscal mínima
    reserva_fiscal_minima = isr_provisional + isr_retenido
    if iva_neto > 0:
        reserva_fiscal_minima += iva_neto

    # ── % de gastos en riesgo de no deducibilidad (efectivo > $2K) ─
    # Informativo. No descuenta automatico del resultado fiscal.
    if total_gastos > 0:
        pct_no_deducible = float(total_riesgo_no_deducible / total_gastos * 100)
        pct_deducible = 100 - pct_no_deducible
    else:
        pct_deducible = 100.0
        pct_no_deducible = 0.0

    # ── PPD sin REP ─────────────────────────────────────────────────
    ppd_gastos = gastos_qs.filter(metodo_pago="PPD")
    ppd_sin_rep = []
    for cfdi in ppd_gastos.iterator():
        tiene_rep = CFDI.objects.filter(
            rfc_empresa=rfc,
            tipo_comprobante="P",
            rfc_emisor=cfdi.rfc_emisor,
            fecha__date__gte=fecha_inicio,
            fecha__date__lte=fecha_fin + timedelta(days=60),
            estado_sat="vigente",
        ).exists()
        if not tiene_rep:
            ppd_sin_rep.append({
                "uuid": str(cfdi.uuid),
                "fecha": cfdi.fecha.date(),
                "rfc_emisor": cfdi.rfc_emisor,
                "nombre_emisor": cfdi.nombre_emisor or cfdi.rfc_emisor,
                "total": cfdi.total,
            })
    ppd_sin_rep_monto = sum(x["total"] for x in ppd_sin_rep)

    # ── EFOS segmentado por situacion (criterio 3) ──────────────────
    # Definitivo / Presunto = riesgo real. Desvirtuado / Sentencia Favorable = limpio.
    rfcs_proveedores = list(
        gastos_qs.values_list("rfc_emisor", flat=True).distinct()
    )
    efos_encontrados = EFOS.objects.filter(rfc__in=rfcs_proveedores)
    efos_lista_full = list(
        efos_encontrados.values("rfc", "nombre", "situacion")
    )
    efos_definitivo_lista = [e for e in efos_lista_full if e["situacion"] == "Definitivo"]
    efos_presunto_lista = [e for e in efos_lista_full if e["situacion"] == "Presunto"]
    efos_desvirtuado_lista = [e for e in efos_lista_full if e["situacion"] == "Desvirtuado"]
    efos_sentencia_lista = [e for e in efos_lista_full if e["situacion"] == "Sentencia Favorable"]
    efos_definitivo_count = len(efos_definitivo_lista)
    efos_presunto_count = len(efos_presunto_lista)
    efos_desvirtuado_count = len(efos_desvirtuado_lista)
    efos_sentencia_count = len(efos_sentencia_lista)
    efos_riesgo_count = efos_definitivo_count + efos_presunto_count
    # Compat retrocompatible: efos_count y efos_lista ahora solo cuentan los de RIESGO.
    efos_count = efos_riesgo_count
    efos_lista = efos_definitivo_lista + efos_presunto_lista

    # ── Formas de pago ──────────────────────────────────────────────
    forma_pago_dist = (
        gastos_qs.values("forma_pago")
        .annotate(total_fp=Sum("total"), count=Count("uuid"))
        .order_by("-total_fp")
    )
    formas_pago = []
    for row in forma_pago_dist:
        fp = row["forma_pago"] or ""
        pct = float(row["total_fp"] / total_gastos * 100) if total_gastos > 0 else 0
        formas_pago.append({
            "codigo": fp,
            "label": FORMA_PAGO_LABELS.get(fp, fp or "Sin definir"),
            "total": row["total_fp"],
            "count": row["count"],
            "pct": round(pct, 1),
        })

    # Porcentaje de "por definir"
    gastos_sin_fp = gastos_qs.filter(
        Q(forma_pago="99") | Q(forma_pago="") | Q(forma_pago__isnull=True)
    ).aggregate(s=Sum("total"))["s"] or Decimal("0")
    pct_por_definir = float(gastos_sin_fp / total_gastos * 100) if total_gastos > 0 else 0

    # ── Top proveedores ─────────────────────────────────────────────
    top_proveedores = list(
        gastos_qs.values("rfc_emisor", "nombre_emisor")
        .annotate(total_proveedor=Sum("total"))
        .order_by("-total_proveedor")[:5]
    )
    for p in top_proveedores:
        p["pct"] = round(
            float(p["total_proveedor"] / total_gastos * 100), 1
        ) if total_gastos > 0 else 0

    # ── Tendencia IVA 6 meses ───────────────────────────────────────
    # Usa el mismo criterio fiscal que el bloque principal:
    # IVA trasladado = ventas (tipo I, emisor=empresa) - notas E emitidas.
    # IVA acreditable = compras (tipo I, receptor=empresa) - notas E recibidas.
    tendencia_iva = []
    for i in range(5, -1, -1):
        m_date = date(fecha_fin.year, fecha_fin.month, 1) - timedelta(days=i * 28)
        m_inicio = date(m_date.year, m_date.month, 1)
        if m_date.month == 12:
            m_fin_d = date(m_date.year + 1, 1, 1) - timedelta(days=1)
        else:
            m_fin_d = date(m_date.year, m_date.month + 1, 1) - timedelta(days=1)
        per = _calc_periodo_simple(rfc, m_inicio, m_fin_d)
        tendencia_iva.append({
            "mes": f"{MONTH_SHORT[m_inicio.month]} {str(m_inicio.year)[2:]}",
            "iva_neto": per["iva_trasladado"] - per["iva_acreditable"],
            "ingresos": per["total_ingresos"],
            "gastos": per["total_gastos"],
            "iva_trasladado": per["iva_trasladado"],
            "iva_acreditable": per["iva_acreditable"],
        })

    # ── Health Score ─────────────────────────────────────────────────
    score = 100
    if resultado_fiscal < 0:
        score -= 15
    score -= min(len(ppd_sin_rep) * 10, 20)
    if total_no_deducible > 0:
        score -= 15
    if efos_count > 0:
        score -= 10
    if pct_por_definir > 20:
        score -= 5
    score = max(0, min(100, score))

    # Health score delta (vs mes anterior)
    health_score_delta = None
    try:
        prev_inicio = date(fecha_inicio.year, fecha_inicio.month - 1, 1) if fecha_inicio.month > 1 else date(fecha_inicio.year - 1, 12, 1)
        if prev_inicio.month == 12:
            prev_fin = date(prev_inicio.year, 12, 31)
        else:
            prev_fin = date(prev_inicio.year, prev_inicio.month + 1, 1) - timedelta(days=1)

        prev_cfdis = CFDI.objects.filter(
            rfc_empresa=rfc,
            fecha__date__gte=prev_inicio,
            fecha__date__lte=prev_fin,
            estado_sat="vigente",
        )
        if prev_cfdis.exists():
            prev_score = 100
            prev_ing = _safe(prev_cfdis.filter(tipo_comprobante="I").aggregate(s=Sum("total"))["s"])
            prev_gas = _safe(prev_cfdis.filter(tipo_comprobante="E").aggregate(s=Sum("total"))["s"])
            prev_no_ded = _safe(prev_cfdis.filter(tipo_comprobante="E", forma_pago="01", total__gt=2000).aggregate(s=Sum("total"))["s"])
            prev_ded = prev_gas - prev_no_ded
            prev_res = prev_ing - prev_ded
            if prev_res < 0:
                prev_score -= 15
            if prev_no_ded > 0:
                prev_score -= 15
            prev_rfcs_prov = list(prev_cfdis.filter(tipo_comprobante="E").values_list("rfc_emisor", flat=True).distinct())
            if EFOS.objects.filter(rfc__in=prev_rfcs_prov).exists():
                prev_score -= 10
            prev_score = max(0, min(100, prev_score))
            health_score_delta = score - prev_score
    except Exception:
        pass

    # ── CFDIs en RIESGO de no deducibilidad (tabla informativa) ─────
    # Criterio 2: gastos efectivo>$2K NO se descuentan auto del resultado,
    # solo se marcan aqui para revision manual. Incluye su IVA.
    cfdis_riesgo_no_deducible = []
    for c in gastos_riesgo_efectivo_qs:
        cfdis_riesgo_no_deducible.append({
            "uuid": str(c.uuid),
            "fecha": c.fecha.date(),
            "rfc_emisor": c.rfc_emisor,
            "nombre_emisor": c.nombre_emisor or c.rfc_emisor,
            "forma_pago_label": FORMA_PAGO_LABELS.get(c.forma_pago, c.forma_pago),
            "motivo": "Efectivo > $2,000",
            "total": c.total,
            "iva": c.iva or Decimal("0"),
        })
    # Alias retrocompatible para templates existentes.
    cfdi_no_deducibles = cfdis_riesgo_no_deducible

    # ── Acciones dinámicas ──────────────────────────────────────────
    acciones = []
    if ppd_sin_rep:
        acciones.append({
            "num": "01",
            "prioridad": "alta",
            "titulo": f"Solicitar REPs faltantes ({len(ppd_sin_rep)} facturas)",
            "subtitulo": f"${ppd_sin_rep_monto:,.0f} MXN en riesgo de no ser deducibles",
            "cta": "Ver facturas PPD",
        })
    if total_no_deducible > 0:
        acciones.append({
            "num": "02",
            "prioridad": "alta",
            "titulo": "Revisar gastos pagados en efectivo",
            "subtitulo": f"${total_no_deducible:,.0f} no deducibles por efectivo > $2,000",
            "cta": "Ver detalle",
        })
    if pct_por_definir > 20:
        acciones.append({
            "num": str(len(acciones) + 1).zfill(2),
            "prioridad": "media",
            "titulo": "Solicitar forma de pago a proveedores",
            "subtitulo": f"{pct_por_definir:.0f}% de gastos sin forma de pago definida",
            "cta": "Ver proveedores",
        })
    if efos_count > 0:
        acciones.append({
            "num": str(len(acciones) + 1).zfill(2),
            "prioridad": "alta",
            "titulo": f"Revisar proveedores en lista 69-B del SAT ({efos_count})",
            "subtitulo": "Estas facturas podrían ser no deducibles",
            "cta": "Ver proveedores EFOS",
        })
    if iva_neto < -5000:
        acciones.append({
            "num": str(len(acciones) + 1).zfill(2),
            "prioridad": "baja",
            "titulo": "Solicitar devolución de IVA a favor",
            "subtitulo": f"${abs(iva_neto):,.0f} MXN recuperables ante el SAT",
            "cta": "Ver guía",
        })

    # ── Aggregates extra para vistas web ─────────────────────────────
    ingresos_count = ingresos_qs.count()
    gastos_count = gastos_qs.count()
    total_emitidos_count = CFDI.objects.filter(
        rfc_empresa=rfc, rfc_emisor=rfc,
        fecha__date__gte=fecha_inicio, fecha__date__lte=fecha_fin,
        estado_sat='vigente',
    ).count()
    total_recibidos_count = CFDI.objects.filter(
        rfc_empresa=rfc, rfc_receptor=rfc,
        fecha__date__gte=fecha_inicio, fecha__date__lte=fecha_fin,
        estado_sat='vigente',
    ).count()
    factura_max = _safe(ingresos_qs.aggregate(m=Max("total"))["m"])
    ticket_promedio = (total_ingresos / ingresos_count) if ingresos_count > 0 else Decimal("0")

    # IVA por tasa (de ingresos / ventas)
    ing_ivable = ingresos_qs.filter(subtotal__gt=0)
    iva_por_tasa_16 = _safe(
        ing_ivable.filter(iva__gt=F("subtotal") * Decimal("0.10"))
        .aggregate(s=Sum("iva"))["s"]
    )
    iva_por_tasa_8 = _safe(
        ing_ivable.filter(iva__gt=0, iva__lte=F("subtotal") * Decimal("0.10"))
        .aggregate(s=Sum("iva"))["s"]
    )
    ventas_tasa_0 = _safe(ingresos_qs.filter(iva=0).aggregate(s=Sum("total"))["s"])

    # Top clientes (a partir de ingresos = ventas)
    top_clientes = list(
        ingresos_qs.values("rfc_receptor", "nombre_receptor")
        .annotate(total_cliente=Sum("total"))
        .order_by("-total_cliente")[:5]
    )

    # Distribuciones para Resumen Rapido (base = todos los CFDIs vigentes del periodo)
    dist_por_tipo = list(
        cfdis.values("tipo_comprobante").annotate(n=Count("uuid")).order_by("-n")
    )
    dist_por_forma_pago = list(
        cfdis.exclude(forma_pago="").values("forma_pago")
        .annotate(n=Count("uuid")).order_by("-n")[:6]
    )

    # Actividad diaria del periodo (count + monto por dia)
    from django.db.models.functions import ExtractDay
    actividad_dia_raw = list(
        cfdis.annotate(dia=ExtractDay("fecha")).values("dia")
        .annotate(n=Count("uuid"), monto=Sum("total")).order_by("dia")
    )

    return {
        # Empresa
        "empresa_nombre": empresa.nombre,
        "empresa_rfc": empresa.rfc,
        "empresa_regimen": getattr(empresa, "regimen_fiscal", "") or "",
        "opinion_sat": "Sin datos",
        "opinion_sat_fecha": None,
        "periodo_label": periodo_label,

        # CFDIs
        "cfdi_count": cfdi_count,
        "ingresos_count": ingresos_count,
        "gastos_count": gastos_count,
        "pagos_count": pagos_qs.count(),
        "total_emitidos_count": total_emitidos_count,
        "total_recibidos_count": total_recibidos_count,
        "factura_max": factura_max,
        "ticket_promedio": ticket_promedio,

        # KPIs
        "total_ingresos": total_ingresos,
        "total_ingresos_bruto": total_ingresos_bruto,
        "total_gastos": total_gastos,
        "total_gastos_bruto": total_gastos_bruto,
        "total_gastos_deducibles": total_gastos_deducibles,
        # total_no_deducible / pct_no_deducible / iva_no_acreditable son INFORMATIVOS:
        # senalan riesgo de no deducibilidad por efectivo>$2K, no descuentan auto.
        "total_no_deducible": total_no_deducible,
        "total_riesgo_no_deducible": total_riesgo_no_deducible,
        "iva_riesgo_no_deducible": iva_riesgo_no_deducible,
        "resultado_fiscal": resultado_fiscal,

        # Notas E (criterio 1: ajuste a ingresos/gastos)
        "notas_e_recibidas_count": notas_e_recibidas_qs.count(),
        "notas_e_recibidas_total": notas_e_recibidas_total,
        "notas_e_recibidas_iva": notas_e_recibidas_iva,
        "notas_e_emitidas_count": notas_e_emitidas_qs.count(),
        "notas_e_emitidas_total": notas_e_emitidas_total,
        "notas_e_emitidas_iva": notas_e_emitidas_iva,

        # IVA
        "iva_trasladado": iva_trasladado,
        "iva_trasladado_bruto": iva_trasladado_bruto,
        "iva_acreditable": iva_acreditable,
        "iva_acreditable_bruto": iva_acreditable_bruto,
        "iva_retenido": iva_retenido_total,
        "iva_no_acreditable": iva_no_acreditable,
        "iva_neto": iva_neto,
        "iva_por_tasa_16": iva_por_tasa_16,
        "iva_por_tasa_8": iva_por_tasa_8,
        "ventas_tasa_0": ventas_tasa_0,
        "isr_retenido": isr_retenido,
        "isr_provisional": isr_provisional,
        "reserva_fiscal_minima": reserva_fiscal_minima,

        # Deducibilidad
        "pct_deducible": round(pct_deducible, 1),
        "pct_no_deducible": round(pct_no_deducible, 1),

        # PPD sin REP
        "ppd_sin_rep": ppd_sin_rep,
        "ppd_sin_rep_monto": ppd_sin_rep_monto,

        # EFOS segmentado (criterio 3)
        "efos_count": efos_count,  # solo riesgo (definitivo+presunto)
        "efos_riesgo_count": efos_riesgo_count,
        "efos_lista": efos_lista,  # solo riesgo
        "efos_definitivo_count": efos_definitivo_count,
        "efos_presunto_count": efos_presunto_count,
        "efos_desvirtuado_count": efos_desvirtuado_count,
        "efos_sentencia_count": efos_sentencia_count,
        "efos_definitivo_lista": efos_definitivo_lista,
        "efos_presunto_lista": efos_presunto_lista,
        "efos_desvirtuado_lista": efos_desvirtuado_lista,
        "efos_sentencia_lista": efos_sentencia_lista,

        # Formas de pago
        "formas_pago": formas_pago,
        "pct_por_definir": round(pct_por_definir, 1),

        # Top proveedores / clientes
        "top_proveedores": top_proveedores,
        "top_clientes": top_clientes,

        # Distribuciones (base = todos los CFDI del periodo)
        "dist_por_tipo": dist_por_tipo,
        "dist_por_forma_pago": dist_por_forma_pago,
        "actividad_dia_raw": actividad_dia_raw,

        # Tendencia (IVA + ingresos + gastos por mes)
        "tendencia_iva": tendencia_iva,

        # Health Score
        "health_score": score,
        "health_score_delta": health_score_delta,

        # Tablas
        "cfdi_no_deducibles": cfdi_no_deducibles,
        "cfdis_riesgo_no_deducible": cfdis_riesgo_no_deducible,

        # Acciones
        "acciones": acciones,

        # IA (se llena después, si se solicita)
        "resumen_ia": None,
    }


def _calc_periodo_simple(rfc, fecha_inicio, fecha_fin):
    """Resumen fiscal minimo de un periodo con los mismos criterios que calcular_reporte.

    Usado por la tendencia 6-meses y por las vistas web que necesitan
    comparar periodos sin pagar el costo completo de calcular_reporte
    (PPD-sin-REP, EFOS, acciones, etc.). Aplica criterio 1 (notas E como ajuste)
    pero NO criterio 2/3 (no calcula riesgos ni EFOS).
    """
    base = dict(rfc_empresa=rfc, fecha__date__gte=fecha_inicio,
                fecha__date__lte=fecha_fin, estado_sat='vigente')
    ingresos = CFDI.objects.filter(tipo_comprobante='I', rfc_emisor=rfc, **base)
    gastos = CFDI.objects.filter(tipo_comprobante='I', rfc_receptor=rfc, **base)
    notas_emi = CFDI.objects.filter(tipo_comprobante='E', rfc_emisor=rfc, **base)
    notas_rec = CFDI.objects.filter(tipo_comprobante='E', rfc_receptor=rfc, **base)
    ti_b = _safe(ingresos.aggregate(s=Sum("total"))["s"])
    tg_b = _safe(gastos.aggregate(s=Sum("total"))["s"])
    iv_t_b = _safe(ingresos.aggregate(s=Sum("iva"))["s"])
    iv_a_b = _safe(gastos.aggregate(s=Sum("iva"))["s"])
    ne_emi_t = _safe(notas_emi.aggregate(s=Sum("total"))["s"])
    ne_rec_t = _safe(notas_rec.aggregate(s=Sum("total"))["s"])
    ne_emi_iv = _safe(notas_emi.aggregate(s=Sum("iva"))["s"])
    ne_rec_iv = _safe(notas_rec.aggregate(s=Sum("iva"))["s"])
    return {
        "total_ingresos": ti_b - ne_emi_t,
        "total_gastos": tg_b - ne_rec_t,
        "iva_trasladado": iv_t_b - ne_emi_iv,
        "iva_acreditable": iv_a_b - ne_rec_iv,
        "ingresos_count": ingresos.count(),
        "gastos_count": gastos.count(),
    }


def generar_resumen_ia(datos):
    """
    Genera un resumen ejecutivo con Anthropic Claude.

    Returns:
        str — párrafo del resumen (o mensaje de fallback)
    """
    import os
    try:
        import anthropic
    except ImportError:
        logger.warning("anthropic no instalado")
        return "No disponible — módulo de IA no instalado."

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return "No disponible — API key de IA no configurada."

    system_prompt = (
        "Eres el asistente fiscal de Cirrus, una plataforma de inteligencia fiscal mexicana. "
        "Tu tarea es redactar UN párrafo ejecutivo de máximo 3 líneas para el dueño de la empresa. "
        "El párrafo debe: (1) mencionar el dato más importante del periodo, (2) señalar el principal "
        "riesgo o acción urgente con el monto exacto en pesos, (3) terminar con una recomendación "
        "concreta y accionable. Usa lenguaje de empresario, no de contador. Nunca uses términos como "
        "'devengar', 'póliza' o 'asiento contable'. Sé directo y específico. Tono profesional pero cercano."
    )

    res_fiscal_tipo = "pérdida" if datos["resultado_fiscal"] < 0 else "utilidad"
    iva_tipo = "saldo a favor" if datos["iva_neto"] < 0 else "a pagar"

    user_prompt = (
        f"Empresa: {datos['empresa_nombre']} (RFC: {datos['empresa_rfc']})\n"
        f"Periodo: {datos['periodo_label']}\n"
        f"Ingresos facturados: ${datos['total_ingresos']:,.0f} MXN\n"
        f"Gastos deducibles: ${datos['total_gastos_deducibles']:,.0f} MXN\n"
        f"Resultado fiscal: ${datos['resultado_fiscal']:,.0f} MXN ({res_fiscal_tipo})\n"
        f"IVA neto: ${datos['iva_neto']:,.0f} MXN ({iva_tipo})\n"
        f"PPD sin REP: {len(datos['ppd_sin_rep'])} facturas por ${datos['ppd_sin_rep_monto']:,.0f} MXN\n"
        f"Gastos no deducibles: ${datos['total_no_deducible']:,.0f} MXN\n"
        f"Proveedores en EFOS: {datos['efos_count']}\n"
        f"Health Score: {datos['health_score']}/100\n"
        f"Redacta el párrafo ejecutivo para el dueño de esta empresa."
    )

    try:
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model="claude-sonnet-4-5-20250514",
            max_tokens=200,
            temperature=0.3,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )
        return response.content[0].text.strip()
    except Exception as e:
        logger.error("Error llamando a Anthropic: %s", e)
        return "No disponible en este momento."
