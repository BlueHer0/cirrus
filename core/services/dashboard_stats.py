"""Dashboard statistics service — business KPIs for the admin panel.

Pure functions that compute MRR, funnel, churn, plan distribution,
client activity aggregates, and operational health summary.

IMPORTANT: All client-oriented metrics EXCLUDE staff/superusers.
The admin panel is for the business owner, not for operational admins.
Internal Cirrus employees (staff/superuser) do not count as "clientes".

All functions return plain dicts — templates consume them directly.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from django.contrib.auth.models import User
from django.db.models import Count, Sum, Q, Avg, F, DecimalField
from django.db.models.functions import TruncMonth, Coalesce


def _month_start(dt: datetime) -> datetime:
    return dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _client_profiles_qs():
    """Return ClienteProfile queryset excluding staff and superusers."""
    from accounts.models import ClienteProfile
    return ClienteProfile.objects.exclude(
        user__is_staff=True,
    ).exclude(user__is_superuser=True)


def _client_users_qs():
    """Return User queryset for clients only (no staff/superusers)."""
    return User.objects.exclude(is_staff=True).exclude(is_superuser=True)


def business_kpis() -> dict:
    """Top-level business KPIs: MRR, active clients, new, churn, revenue, ARPU.

    Excludes staff/superuser users from all client metrics.
    """
    from accounts.models import StripePayment

    now = datetime.now(timezone.utc)
    month_start = _month_start(now)
    thirty_days_ago = now - timedelta(days=30)

    active_profiles = _client_profiles_qs().filter(
        subscription_status="active",
    ).select_related("plan_fk")

    mrr = Decimal("0")
    for p in active_profiles:
        plan = p.plan_fk
        if plan and plan.precio_mensual:
            mrr += plan.precio_mensual

    total_active = active_profiles.count()
    new_clients_30d = _client_profiles_qs().filter(
        created_at__gte=thirty_days_ago,
    ).count()

    churn_30d = _client_profiles_qs().filter(
        subscription_status="canceled",
        user__date_joined__lt=thirty_days_ago,
    ).count()

    # Revenue excluye pagos de staff
    revenue_month = StripePayment.objects.filter(
        status__in=["succeeded", "paid"],
        created_at__gte=month_start,
    ).exclude(
        user__is_staff=True,
    ).exclude(user__is_superuser=True).aggregate(
        total=Coalesce(Sum("amount"), Decimal("0"), output_field=DecimalField()),
    )["total"]

    arpu = (mrr / total_active) if total_active > 0 else Decimal("0")

    return {
        "mrr": mrr,
        "clientes_activos": total_active,
        "nuevos_30d": new_clients_30d,
        "churn_30d": churn_30d,
        "revenue_mes": revenue_month,
        "arpu": arpu,
    }


def plan_distribution() -> list:
    """Clientes por plan + MRR aportado por plan (sin staff)."""
    from core.models import Plan

    planes = Plan.objects.filter(activo=True).order_by("orden", "precio_mensual")
    rows = []
    total_clientes = _client_profiles_qs().count()

    for plan in planes:
        count = _client_profiles_qs().filter(plan_fk=plan).count()
        active_count = _client_profiles_qs().filter(
            plan_fk=plan, subscription_status="active",
        ).count()
        mrr_plan = (plan.precio_mensual or Decimal("0")) * active_count
        pct = round((count / total_clientes * 100) if total_clientes else 0, 1)
        rows.append({
            "plan": plan,
            "nombre": plan.nombre,
            "slug": plan.slug,
            "total": count,
            "activos": active_count,
            "mrr": mrr_plan,
            "pct": pct,
            "precio": plan.precio_mensual,
        })

    sin_plan = _client_profiles_qs().filter(plan_fk__isnull=True).count()
    if sin_plan:
        rows.append({
            "plan": None,
            "nombre": "Sin plan asignado",
            "slug": "none",
            "total": sin_plan,
            "activos": 0,
            "mrr": Decimal("0"),
            "pct": round((sin_plan / total_clientes * 100) if total_clientes else 0, 1),
            "precio": Decimal("0"),
        })

    return rows


def growth_series(months: int = 6) -> dict:
    """Nuevos clientes por mes (últimos N meses), sin staff."""
    now = datetime.now(timezone.utc)
    start = _month_start(now) - timedelta(days=months * 32)
    start = _month_start(start)

    new_per_month = (
        _client_profiles_qs()
        .filter(created_at__gte=start)
        .annotate(mes=TruncMonth("created_at"))
        .values("mes")
        .annotate(n=Count("id"))
        .order_by("mes")
    )

    labels = []
    new_values = []
    for row in new_per_month:
        labels.append(row["mes"].strftime("%b %y"))
        new_values.append(row["n"])

    max_new = max(new_values) if new_values else 1

    return {
        "labels": labels,
        "new_clients": new_values,
        "max_new": max_new,
    }


def funnel_conversion() -> list:
    """Funnel from public converter lead to paying customer (excluding staff)."""
    from core.models import ConversionLead

    # Leads no dependen de User staff
    leads = ConversionLead.objects.count()

    clients_only = _client_users_qs()

    registered = clients_only.filter(perfil__isnull=False).count()
    confirmed = clients_only.filter(is_active=True, perfil__isnull=False).count()
    with_empresa = clients_only.filter(
        perfil__isnull=False, empresas__isnull=False,
    ).distinct().count()
    fiel_verified = clients_only.filter(
        empresas__fiel_verificada=True,
    ).distinct().count()
    first_download = clients_only.filter(
        empresas__ultimo_scrape__isnull=False,
    ).distinct().count()
    paying = _client_profiles_qs().filter(subscription_status="active").count()

    stages = [
        ("Leads (conversor público)", leads, None),
        ("Registrados", registered, leads),
        ("Email confirmado", confirmed, registered),
        ("Con empresa", with_empresa, confirmed),
        ("FIEL verificada", fiel_verified, with_empresa),
        ("Primera descarga OK", first_download, fiel_verified),
        ("Cliente pagado", paying, first_download),
    ]

    max_count = max((s[1] for s in stages), default=1) or 1
    rows = []
    for label, count, prev in stages:
        pct_total = round((count / max_count * 100) if max_count else 0, 1)
        pct_step = None
        if prev is not None and prev > 0:
            pct_step = round((count / prev * 100), 1)
        rows.append({
            "label": label,
            "count": count,
            "pct_total": pct_total,
            "pct_step": pct_step,
        })
    return rows


def operational_health() -> dict:
    """Compact operational summary (infraestructura, no por cliente)."""
    from core.models import (
        Empresa, CFDI, DescargaLog, SATHealthSummary, DescargaJob,
    )

    now = datetime.now(timezone.utc)
    last_24h = now - timedelta(hours=24)
    last_30d = now - timedelta(days=30)

    # Empresas del sistema (incluyendo las de staff — es infra, no cliente)
    empresas_total = Empresa.objects.count()
    empresas_con_fiel = Empresa.objects.filter(fiel_verificada=True).count()

    cfdis_30d = CFDI.objects.filter(descargado_at__gte=last_30d).count()

    fallos_24h = DescargaLog.objects.filter(
        estado="error", iniciado_at__gte=last_24h,
    ).count()

    sat_summary = SATHealthSummary.objects.filter(
        hour__gte=last_24h,
    ).aggregate(avg=Avg("availability_pct"))
    sat_avg = sat_summary["avg"] or 0

    if sat_avg >= 70:
        sat_status = "🟢"
    elif sat_avg >= 30:
        sat_status = "🟡"
    else:
        sat_status = "🔴"

    jobs_en_cola = DescargaJob.objects.filter(estado="en_cola").count()
    jobs_ejecutando = DescargaJob.objects.filter(estado="ejecutando").count()
    jobs_error = DescargaJob.objects.filter(estado="error").count()

    return {
        "empresas_total": empresas_total,
        "empresas_con_fiel": empresas_con_fiel,
        "fiel_ratio": round((empresas_con_fiel / empresas_total * 100) if empresas_total else 0),
        "cfdis_30d": cfdis_30d,
        "fallos_24h": fallos_24h,
        "sat_avg": round(sat_avg),
        "sat_status": sat_status,
        "jobs_en_cola": jobs_en_cola,
        "jobs_ejecutando": jobs_ejecutando,
        "jobs_error": jobs_error,
    }


def system_aggregate_stats() -> dict:
    """Agregados del sistema para widgets informativos del dashboard.

    Solo números, sin identificación de clientes. Pensado para mostrar
    al dueño como estadística operativa agregada.
    """
    from core.models import APIKey, CFDI, DescargaJob

    return {
        "api_keys_total": APIKey.objects.count(),
        "api_keys_activas": APIKey.objects.filter(activa=True).count(),
        "downloads_activos": DescargaJob.objects.filter(
            estado__in=["en_cola", "ejecutando"],
        ).count(),
        "cfdis_sistema": CFDI.objects.count(),
    }


def attention_required() -> list:
    """Lista accionable (sin exponer datos fiscales de clientes)."""
    from core.models import Empresa, DescargaLog

    now = datetime.now(timezone.utc)
    items = []

    past_due = _client_profiles_qs().filter(
        subscription_status="past_due",
    ).count()
    if past_due:
        items.append({
            "icon": "⚠️",
            "level": "warning",
            "text": f"{past_due} cliente(s) con pago pendiente (past_due)",
            "link": "panel:clientes",
        })

    # FIEL por vencer en <30d — solo empresas de clientes (no staff)
    fiel_por_vencer = Empresa.objects.filter(
        fiel_expira__isnull=False,
        fiel_expira__gt=now,
        fiel_expira__lt=now + timedelta(days=30),
    ).exclude(
        owner__is_staff=True,
    ).exclude(owner__is_superuser=True).count()
    if fiel_por_vencer:
        items.append({
            "icon": "⚠️",
            "level": "warning",
            "text": f"{fiel_por_vencer} FIEL(s) de clientes vencen en <30 días",
            "link": "panel:clientes",
        })

    # Clientes pagados inactivos
    cutoff_30d = now - timedelta(days=30)
    inactivos = Empresa.objects.filter(
        owner__perfil__subscription_status="active",
        ultimo_scrape__lt=cutoff_30d,
    ).exclude(
        owner__is_staff=True,
    ).exclude(owner__is_superuser=True).values("owner").distinct().count()
    if inactivos:
        items.append({
            "icon": "⚠️",
            "level": "warning",
            "text": f"{inactivos} cliente(s) pagado(s) sin actividad hace 30+ días",
            "link": "panel:clientes",
        })

    # Empresas de clientes con 3+ errores consecutivos
    empresas_fallando = 0
    for emp in Empresa.objects.filter(sync_activa=True).exclude(
        owner__is_staff=True,
    ).exclude(owner__is_superuser=True):
        recent = DescargaLog.objects.filter(empresa=emp).order_by("-iniciado_at")[:5]
        consec = 0
        for log in recent:
            if log.estado == "error":
                consec += 1
            else:
                break
        if consec >= 3:
            empresas_fallando += 1
    if empresas_fallando:
        items.append({
            "icon": "🔴",
            "level": "critical",
            "text": f"{empresas_fallando} cliente(s) con descargas fallando repetidas",
            "link": "panel:clientes",
        })

    return items


def clientes_list_data(filters: dict = None, limit: int = None) -> list:
    """Lista de clientes con métricas COMERCIALES (sin datos fiscales).

    NO incluye: RFCs, nombres de empresas, CFDIs, montos facturados.
    SÍ incluye: email, plan, MRR, número de empresas, última actividad.

    Excluye staff/superusers.
    """
    from core.models import Empresa

    filters = filters or {}
    now = datetime.now(timezone.utc)

    qs = _client_profiles_qs().select_related("user", "plan_fk").order_by("-created_at")

    q = (filters.get("q") or "").strip()
    if q:
        qs = qs.filter(
            Q(user__email__icontains=q)
            | Q(user__username__icontains=q)
            | Q(empresa_nombre__icontains=q),
        )

    plan_slug = filters.get("plan")
    if plan_slug:
        qs = qs.filter(plan_fk__slug=plan_slug)

    estado = filters.get("estado")
    if estado:
        qs = qs.filter(subscription_status=estado)

    if limit:
        qs = qs[:limit]

    rows = []
    for profile in qs:
        user = profile.user
        empresas = Empresa.objects.filter(owner=user)
        empresa_count = empresas.count()

        # Última actividad del cliente (último scrape de cualquier empresa suya)
        # Es un indicador de engagement, NO un detalle fiscal.
        last_activity = empresas.filter(
            ultimo_scrape__isnull=False,
        ).order_by("-ultimo_scrape").values_list("ultimo_scrape", flat=True).first()

        plan = profile.plan_fk
        mrr_aporte = plan.precio_mensual if (plan and profile.subscription_status == "active") else Decimal("0")

        rows.append({
            "profile": profile,
            "user": user,
            "email": user.email or user.username,
            "plan_nombre": plan.nombre if plan else (profile.plan_legacy or "free").title(),
            "plan_slug": plan.slug if plan else (profile.plan_legacy or "free"),
            "mrr": mrr_aporte,
            "empresas_count": empresa_count,
            "last_activity": last_activity,
            "subscription_status": profile.subscription_status,
            "created_at": profile.created_at,
        })

    # Orden: MRR desc, luego actividad más reciente
    rows.sort(key=lambda r: (r["mrr"], r["last_activity"] or datetime(2000, 1, 1, tzinfo=timezone.utc)), reverse=True)
    return rows
