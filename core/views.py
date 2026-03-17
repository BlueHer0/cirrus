"""Cirrus Admin Panel Views.

Session-authenticated Django views for the admin panel.
Uses Django templates with glassmorphism + Tailwind v4.
"""

import secrets
import logging
from datetime import datetime, timezone

from django.contrib.auth import authenticate, login as auth_login, logout as auth_logout
from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib import messages
from django.db.models import Count, Sum, Q
from django.http import HttpResponse
from django.shortcuts import render, redirect, get_object_or_404
from django.views.decorators.http import require_POST

logger = logging.getLogger("core.views")


def staff_required(view_func):
    """Decorator that requires is_staff=True.
    Non-staff users get logged out and sent to admin login."""
    from functools import wraps
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect("/panel/login/")
        if not request.user.is_staff:
            # Logout the non-staff user, don't redirect to /app/
            auth_logout(request)
            messages.warning(request, "Necesitas una cuenta de administrador para acceder al panel.")
            return redirect("/panel/login/")
        return view_func(request, *args, **kwargs)
    return wrapper


# ── Auth ──────────────────────────────────────────────────────────────

def login_view(request):
    """Admin panel login. Always shows login form to non-staff users."""
    if request.user.is_authenticated:
        if request.user.is_staff:
            return redirect("panel:dashboard")
        # Non-staff user — logout and show admin login form
        auth_logout(request)

    if request.method == "POST":
        # Clear any existing session first
        if request.user.is_authenticated:
            auth_logout(request)

        user = authenticate(
            request,
            username=request.POST.get("username"),
            password=request.POST.get("password"),
        )
        if user and user.is_staff:
            auth_login(request, user)
            return redirect(request.GET.get("next", "panel:dashboard"))
        elif user and not user.is_staff:
            messages.error(request, "Esta cuenta no tiene permisos de administrador.")
        else:
            return render(request, "panel/login.html", {
                "form": {"errors": True},
                "year": datetime.now().year,
            })

    return render(request, "panel/login.html", {"year": datetime.now().year})


def logout_view(request):
    auth_logout(request)
    return redirect("landing")


# ── Dashboard ─────────────────────────────────────────────────────────

@staff_required
def dashboard(request):
    from core.models import Empresa, CFDI, DescargaLog

    now = datetime.now(timezone.utc)
    empresas_qs = Empresa.objects.annotate(cfdi_count=Count("cfdis"))

    stats = {
        "total_empresas": empresas_qs.count(),
        "empresas_activas": empresas_qs.filter(descarga_activa=True).count(),
        "total_cfdis": CFDI.objects.count(),
        "cfdis_este_mes": CFDI.objects.filter(
            fecha__year=now.year, fecha__month=now.month,
        ).count(),
        "monto_total": CFDI.objects.aggregate(s=Sum("total"))["s"] or 0,
        "descargas_hoy": DescargaLog.objects.filter(
            iniciado_at__date=now.date(),
        ).count(),
        "descargas_error": DescargaLog.objects.filter(
            estado="error", iniciado_at__date=now.date(),
        ).count(),
    }

    return render(request, "panel/dashboard.html", {
        "current_page": "dashboard",
        "stats": stats,
        "empresas": empresas_qs.order_by("nombre"),
    })


# ── Empresas ──────────────────────────────────────────────────────────

@staff_required
def empresas_list(request):
    from core.models import Empresa

    if request.method == "POST":
        rfc = request.POST.get("rfc", "").strip().upper()
        nombre = request.POST.get("nombre", "").strip()
        notas = request.POST.get("notas", "").strip()

        if not rfc or not nombre:
            messages.error(request, "RFC y Nombre son obligatorios")
        elif len(rfc) > 13:
            messages.error(request, "RFC no puede tener más de 13 caracteres")
        elif Empresa.objects.filter(rfc=rfc).exists():
            messages.error(request, f"Ya existe una empresa con RFC {rfc}")
        else:
            empresa = Empresa.objects.create(
                rfc=rfc, nombre=nombre, notas=notas, owner=request.user,
            )
            messages.success(request, f"Empresa {rfc} creada exitosamente")
            return redirect("panel:empresa_detalle", empresa_id=empresa.id)

    empresas = Empresa.objects.annotate(cfdi_count=Count("cfdis")).order_by("nombre")
    return render(request, "panel/empresas_list.html", {
        "current_page": "empresas",
        "empresas": empresas,
    })


@staff_required
def empresa_detalle(request, empresa_id):
    from core.models import Empresa, CFDI

    empresa = get_object_or_404(Empresa, id=empresa_id)

    if request.method == "POST":
        action = request.POST.get("action", "")
        if action == "edit":
            empresa.nombre = request.POST.get("nombre", empresa.nombre).strip()
            empresa.notas = request.POST.get("notas", "").strip()
            empresa.descarga_activa = "descarga_activa" in request.POST
            empresa.save(update_fields=["nombre", "notas", "descarga_activa", "updated_at"])
            messages.success(request, "Empresa actualizada")
            return redirect("panel:empresa_detalle", empresa_id=empresa_id)
        elif action == "delete":
            rfc = empresa.rfc
            empresa.delete()
            messages.warning(request, f"Empresa {rfc} eliminada")
            return redirect("panel:empresas")

    cfdis_qs = empresa.cfdis.all()
    stats = cfdis_qs.aggregate(
        total=Sum("total"),
        recibidos=Count("uuid", filter=Q(tipo_relacion="recibido")),
        emitidos=Count("uuid", filter=Q(tipo_relacion="emitido")),
    )

    now = datetime.now()
    years = list(range(now.year, now.year - 5, -1))
    months = [
        (1, "Enero"), (2, "Febrero"), (3, "Marzo"), (4, "Abril"),
        (5, "Mayo"), (6, "Junio"), (7, "Julio"), (8, "Agosto"),
        (9, "Septiembre"), (10, "Octubre"), (11, "Noviembre"), (12, "Diciembre"),
    ]

    return render(request, "panel/empresa_detalle.html", {
        "current_page": "empresas",
        "empresa": empresa,
        "cfdis": cfdis_qs.order_by("-fecha")[:20],
        "cfdi_count": cfdis_qs.count(),
        "recibidos": stats["recibidos"] or 0,
        "emitidos": stats["emitidos"] or 0,
        "monto_total": stats["total"] or 0,
        "years": years,
        "months": months,
        "current_year": now.year,
        "current_month": now.month,
    })


@staff_required
def empresa_fiel(request, empresa_id):
    from core.models import Empresa
    from core.services.fiel_encryption import upload_and_encrypt_fiel

    empresa = get_object_or_404(Empresa, id=empresa_id)

    if request.method == "POST":
        cer_file = request.FILES.get("cer_file")
        key_file = request.FILES.get("key_file")
        password = request.POST.get("password", "")

        if not cer_file or not key_file or not password:
            messages.error(request, "Todos los campos son obligatorios")
            return redirect("panel:empresa_fiel", empresa_id=empresa_id)

        try:
            upload_and_encrypt_fiel(
                empresa=empresa,
                cer_bytes=cer_file.read(),
                key_bytes=key_file.read(),
                password=password,
            )
            # Auto-trigger verification
            from core.tasks import verificar_fiel
            verificar_fiel.delay(str(empresa.id))

            messages.success(request, f"FIEL subida para {empresa.rfc}. Verificación en progreso...")
            return redirect("panel:empresa_detalle", empresa_id=empresa_id)
        except Exception as e:
            logger.error("FIEL upload failed for %s: %s", empresa.rfc, e)
            messages.error(request, f"Error subiendo FIEL: {e}")

    return render(request, "panel/empresa_fiel.html", {
        "current_page": "empresas",
        "empresa": empresa,
    })


@staff_required
@require_POST
def empresa_verificar(request, empresa_id):
    from core.models import Empresa
    from core.tasks import verificar_fiel

    empresa = get_object_or_404(Empresa, id=empresa_id)
    verificar_fiel.delay(str(empresa.id))
    messages.info(request, f"Verificación de FIEL iniciada para {empresa.rfc}")
    return redirect("panel:empresa_detalle", empresa_id=empresa_id)


@staff_required
@require_POST
def empresa_descargar(request, empresa_id):
    from core.models import Empresa
    from core.tasks import descargar_cfdis

    empresa = get_object_or_404(Empresa, id=empresa_id)
    now = datetime.now()

    year = int(request.POST.get("year", now.year))
    month_start = int(request.POST.get("month_start", now.month))
    month_end = int(request.POST.get("month_end", now.month))
    tipos = request.POST.getlist("tipos")
    if not tipos:
        tipos = ["recibidos", "emitidos"]

    descargar_cfdis.delay(str(empresa.id), params={
        "year": year,
        "month_start": month_start,
        "month_end": month_end,
        "tipos": tipos,
    }, triggered_by="manual")
    label = f"{year}/{month_start:02d}" if month_start == month_end else f"{year}/{month_start:02d}–{month_end:02d}"
    messages.success(request, f"Descarga iniciada para {empresa.rfc} ({label})")
    return redirect("panel:empresa_detalle", empresa_id=empresa_id)


@staff_required
@require_POST
def empresa_logo(request, empresa_id):
    from core.models import Empresa
    from core.services.storage_minio import upload_logo

    empresa = get_object_or_404(Empresa, id=empresa_id)
    logo_file = request.FILES.get("logo")
    if not logo_file:
        messages.error(request, "Selecciona un archivo de logo")
        return redirect("panel:empresa_detalle", empresa_id=empresa_id)

    try:
        ext = logo_file.name.rsplit(".", 1)[-1].lower() if "." in logo_file.name else "png"
        minio_key = upload_logo(empresa.rfc, logo_file.read(), ext)
        empresa.logo_minio_key = minio_key
        empresa.save(update_fields=["logo_minio_key"])
        messages.success(request, "Logo actualizado")
    except Exception as e:
        logger.error("Logo upload failed: %s", e)
        messages.error(request, f"Error subiendo logo: {e}")

    return redirect("panel:empresa_detalle", empresa_id=empresa_id)


# ── CFDIs ─────────────────────────────────────────────────────────────

@staff_required
def cfdis_list(request):
    from core.models import CFDI, Empresa

    qs = CFDI.objects.select_related("empresa").all()
    empresas = Empresa.objects.order_by("rfc")

    # Filters
    filters = {}
    rfc = request.GET.get("rfc", "")
    if rfc:
        qs = qs.filter(empresa__rfc=rfc)
        filters["rfc"] = rfc

    year = request.GET.get("year", "")
    if year:
        year = int(year)
        qs = qs.filter(fecha__year=year)
        filters["year"] = year

    month = request.GET.get("month", "")
    if month:
        month = int(month)
        qs = qs.filter(fecha__month=month)
        filters["month"] = month

    tipo = request.GET.get("tipo", "")
    if tipo:
        qs = qs.filter(tipo_relacion=tipo)
        filters["tipo"] = tipo

    tipo_comp = request.GET.get("tipo_comp", "")
    if tipo_comp:
        qs = qs.filter(tipo_comprobante=tipo_comp)
        filters["tipo_comp"] = tipo_comp

    estado = request.GET.get("estado", "")
    if estado:
        qs = qs.filter(estado_sat=estado)
        filters["estado"] = estado

    total_cfdis = qs.count()
    page_size = 100
    page = int(request.GET.get("page", 1))
    offset = (page - 1) * page_size
    cfdis = qs.order_by("-fecha")[offset:offset + page_size]

    # Year/month options
    now = datetime.now()
    years = list(range(now.year, now.year - 5, -1))
    months = [
        (1, "Enero"), (2, "Febrero"), (3, "Marzo"), (4, "Abril"),
        (5, "Mayo"), (6, "Junio"), (7, "Julio"), (8, "Agosto"),
        (9, "Septiembre"), (10, "Octubre"), (11, "Noviembre"), (12, "Diciembre"),
    ]

    return render(request, "panel/cfdis_list.html", {
        "current_page": "cfdis",
        "cfdis": cfdis,
        "total_cfdis": total_cfdis,
        "empresas": empresas,
        "filters": filters,
        "years": years,
        "months": months,
        "page": page,
        "has_prev": page > 1,
        "has_next": offset + page_size < total_cfdis,
    })


@staff_required
def cfdi_detail(request, cfdi_uuid):
    from core.models import CFDI

    cfdi = get_object_or_404(CFDI.objects.select_related("empresa"), uuid=cfdi_uuid)

    # Try to get XML preview
    xml_preview = ""
    try:
        from core.services.storage_minio import download_bytes
        xml_bytes = download_bytes(cfdi.xml_minio_key)
        xml_preview = xml_bytes.decode("utf-8")
    except Exception as e:
        logger.warning("Could not load XML preview for %s: %s", cfdi_uuid, e)

    return render(request, "panel/cfdi_detail.html", {
        "current_page": "cfdis",
        "cfdi": cfdi,
        "xml_preview": xml_preview,
    })


@staff_required
def cfdi_download_pdf(request, cfdi_uuid):
    from core.models import CFDI
    from core.services.storage_minio import download_bytes
    from sat_scrapper_core.cfdi_pdf.render import render_cfdi_pdf

    cfdi = get_object_or_404(CFDI, uuid=cfdi_uuid)
    try:
        xml_bytes = download_bytes(cfdi.xml_minio_key)
        pdf_bytes = render_cfdi_pdf(xml_bytes)
        response = HttpResponse(pdf_bytes, content_type="application/pdf")
        response["Content-Disposition"] = f'inline; filename="{cfdi_uuid}.pdf"'
        return response
    except Exception as e:
        logger.error("PDF generation failed for %s: %s", cfdi_uuid, e)
        messages.error(request, f"Error generando PDF: {e}")
        return redirect("panel:cfdi_detail", cfdi_uuid=cfdi_uuid)


@staff_required
def cfdi_download_xml(request, cfdi_uuid):
    from core.models import CFDI
    from core.services.storage_minio import download_bytes

    cfdi = get_object_or_404(CFDI, uuid=cfdi_uuid)
    try:
        xml_bytes = download_bytes(cfdi.xml_minio_key)
        response = HttpResponse(xml_bytes, content_type="application/xml")
        response["Content-Disposition"] = f'attachment; filename="{cfdi_uuid}.xml"'
        return response
    except Exception as e:
        logger.error("XML download failed for %s: %s", cfdi_uuid, e)
        messages.error(request, f"Error descargando XML: {e}")
        return redirect("panel:cfdi_detail", cfdi_uuid=cfdi_uuid)


@staff_required
def cfdi_download_excel(request, cfdi_uuid):
    from core.models import CFDI
    from core.services.storage_minio import download_bytes
    from core.services.excel_export import export_cfdi_detail_excel

    cfdi = get_object_or_404(CFDI, uuid=cfdi_uuid)
    try:
        xml_bytes = download_bytes(cfdi.xml_minio_key)
        excel_bytes = export_cfdi_detail_excel(xml_bytes)
        response = HttpResponse(
            excel_bytes,
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        response["Content-Disposition"] = f'attachment; filename="{cfdi_uuid}.xlsx"'
        return response
    except Exception as e:
        logger.error("Excel export failed for %s: %s", cfdi_uuid, e)
        messages.error(request, f"Error exportando Excel: {e}")
        return redirect("panel:cfdi_detail", cfdi_uuid=cfdi_uuid)


# ── Descargas ─────────────────────────────────────────────────────────

MONTH_NAMES = {
    1: "Enero", 2: "Febrero", 3: "Marzo", 4: "Abril",
    5: "Mayo", 6: "Junio", 7: "Julio", 8: "Agosto",
    9: "Septiembre", 10: "Octubre", 11: "Noviembre", 12: "Diciembre",
}


@staff_required
def descargas_list(request):
    from core.models import DescargaLog, Empresa, CFDI

    now = datetime.now()

    # Build sync progress for each empresa with sync_activa
    sync_empresas = []
    for empresa in Empresa.objects.filter(
        fiel_verificada=True,
    ).order_by("nombre"):
        if not empresa.sync_desde_year or not empresa.sync_desde_month:
            continue

        # Calculate total months needed
        y, m = empresa.sync_desde_year, empresa.sync_desde_month
        total_months = 0
        while (y < now.year) or (y == now.year and m <= now.month):
            total_months += 1
            m += 1
            if m > 12:
                m = 1
                y += 1

        # Count completed months (need both recibidos+emitidos)
        completados_qs = (
            DescargaLog.objects.filter(empresa=empresa, estado="completado")
            .values("year", "month_start")
            .annotate(cnt=Count("id"))
            .filter(cnt__gte=2)
        )
        meses_completados = completados_qs.count()

        # Last completed month
        last_completed = (
            DescargaLog.objects.filter(empresa=empresa, estado="completado")
            .order_by("-year", "-month_start")
            .first()
        )

        # Active/pending download
        active_download = (
            DescargaLog.objects.filter(empresa=empresa, estado="ejecutando")
            .first()
        )
        pending_download = (
            DescargaLog.objects.filter(empresa=empresa, estado="pendiente")
            .order_by("-year", "-month_start")
            .first()
        )

        # Total CFDIs for this empresa
        total_cfdis = CFDI.objects.filter(rfc_empresa=empresa.rfc).count()

        progress_pct = round(meses_completados / total_months * 100) if total_months else 0

        sync_empresas.append({
            "empresa": empresa,
            "total_months": total_months,
            "meses_completados": meses_completados,
            "progress_pct": progress_pct,
            "total_cfdis": total_cfdis,
            "last_completed": last_completed,
            "last_completed_label": (
                f"{MONTH_NAMES.get(last_completed.month_start, '?')} {last_completed.year}"
                if last_completed else None
            ),
            "last_completed_count": (
                DescargaLog.objects.filter(
                    empresa=empresa, estado="completado",
                    year=last_completed.year, month_start=last_completed.month_start,
                ).aggregate(s=Sum("cfdis_nuevos"))["s"] or 0
            ) if last_completed else 0,
            "active": active_download,
            "pending": pending_download,
            "sync_desde_label": f"{MONTH_NAMES.get(empresa.sync_desde_month, '?')} {empresa.sync_desde_year}",
        })

    # Historial detallado (all logs)
    descargas = DescargaLog.objects.select_related("empresa").order_by("-iniciado_at")[:100]

    return render(request, "panel/descargas.html", {
        "current_page": "descargas",
        "sync_empresas": sync_empresas,
        "descargas": descargas,
    })


@staff_required
@require_POST
def empresa_toggle_sync(request, empresa_id):
    from core.models import Empresa

    empresa = get_object_or_404(Empresa, id=empresa_id)
    empresa.sync_activa = not empresa.sync_activa
    if empresa.sync_activa:
        empresa.sync_completada = False
    empresa.save(update_fields=["sync_activa", "sync_completada"])

    action = "reanudada" if empresa.sync_activa else "pausada"
    messages.success(request, f"Sincronización {action} para {empresa.rfc}")
    return redirect("panel:descargas")


@staff_required
def empresa_sync_config(request, empresa_id):
    from core.models import Empresa

    empresa = get_object_or_404(Empresa, id=empresa_id)

    if request.method == "POST":
        option = request.POST.get("sync_option", "")
        now = datetime.now()

        if option == "this_month":
            empresa.sync_desde_year = now.year
            empresa.sync_desde_month = now.month
        elif option == "this_year":
            empresa.sync_desde_year = now.year
            empresa.sync_desde_month = 1
        elif option == "last_year":
            empresa.sync_desde_year = now.year - 1
            empresa.sync_desde_month = 1
        elif option == "custom":
            empresa.sync_desde_year = int(request.POST.get("custom_year", now.year))
            empresa.sync_desde_month = int(request.POST.get("custom_month", 1))

        empresa.sync_activa = True
        empresa.sync_completada = False
        empresa.save(update_fields=[
            "sync_desde_year", "sync_desde_month",
            "sync_activa", "sync_completada",
        ])
        messages.success(request, f"Sincronización configurada para {empresa.rfc}")
        return redirect("panel:descargas")

    now = datetime.now()
    years = list(range(now.year, now.year - 5, -1))

    return render(request, "panel/empresa_sync.html", {
        "current_page": "empresas",
        "empresa": empresa,
        "years": years,
        "months": list(MONTH_NAMES.items()),
        "current_year": now.year,
        "current_month": now.month,
    })


@staff_required
def descarga_telemetria(request, descarga_id):
    from core.models import DescargaLog, DescargaTelemetria
    from core.services.telemetry import get_telemetry_summary
    from django.db.models import Avg, Count
    from django.db.models.functions import ExtractHour

    descarga = get_object_or_404(DescargaLog, id=descarga_id)
    summary = get_telemetry_summary(descarga)

    # Historical averages (last 20 completed downloads)
    recent_ids = (
        DescargaLog.objects.filter(estado="completado")
        .order_by("-completado_at")
        .values_list("id", flat=True)[:20]
    )
    avg_by_phase = (
        DescargaTelemetria.objects.filter(descarga_log_id__in=list(recent_ids))
        .values("fase")
        .annotate(avg_ms=Avg("duracion_ms"), count=Count("id"))
        .order_by("fase")
    )
    historical = {r["fase"]: round(r["avg_ms"] or 0) for r in avg_by_phase}

    # Best/worst hour (from completed downloads with telemetry)
    hora_stats = (
        DescargaLog.objects.filter(estado="completado", duracion_segundos__gt=0)
        .annotate(hora=ExtractHour("iniciado_at"))
        .values("hora")
        .annotate(avg_dur=Avg("duracion_segundos"), count=Count("id"))
        .order_by("hora")
    )
    hora_data = list(hora_stats)
    best_hour = min(hora_data, key=lambda x: x["avg_dur"]) if hora_data else None
    worst_hour = max(hora_data, key=lambda x: x["avg_dur"]) if hora_data else None

    return render(request, "panel/telemetria.html", {
        "current_page": "descargas",
        "descarga": descarga,
        "summary": summary,
        "historical": historical,
        "best_hour": best_hour,
        "worst_hour": worst_hour,
        "hora_data": hora_data,
    })


# ── API Keys ──────────────────────────────────────────────────────────

@staff_required
def api_keys_view(request):
    from core.models import APIKey, Empresa

    if request.method == "POST":
        nombre = request.POST.get("nombre", "").strip()
        if not nombre:
            messages.error(request, "El nombre es obligatorio")
        else:
            key = APIKey.objects.create(
                nombre=nombre,
                key=secrets.token_hex(32),
                owner=request.user,
                puede_leer="puede_leer" in request.POST,
                puede_trigger_descarga="puede_trigger_descarga" in request.POST,
            )
            # Assign empresas
            empresa_ids = request.POST.getlist("empresas")
            if empresa_ids:
                key.empresas.set(empresa_ids)

            messages.success(request, f"API Key creada: {key.key}")
            return redirect("panel:api_keys")

    api_keys = APIKey.objects.prefetch_related("empresas").order_by("-created_at")
    empresas = Empresa.objects.order_by("rfc")

    return render(request, "panel/api_keys.html", {
        "current_page": "api_keys",
        "api_keys": api_keys,
        "empresas": empresas,
    })


@staff_required
@require_POST
def api_key_revoke(request, key_id):
    from core.models import APIKey

    key = get_object_or_404(APIKey, id=key_id)
    key.activa = False
    key.save(update_fields=["activa"])
    messages.warning(request, f"API Key '{key.nombre}' revocada")
    return redirect("panel:api_keys")


# ── Public ────────────────────────────────────────────────────────────

def _get_logo_b64():
    """Cache the base64 logo data URI."""
    if not hasattr(_get_logo_b64, "_cache"):
        import base64
        from pathlib import Path
        logo_path = Path(__file__).resolve().parent.parent / "sat_scrapper_core" / "cfdi_pdf" / "templates" / "cirrus_logo.png"
        if logo_path.exists():
            try:
                from PIL import Image
                import io
                img = Image.open(logo_path)
                img.thumbnail((300, 120), Image.LANCZOS)
                buf = io.BytesIO()
                img.save(buf, "PNG", optimize=True)
                _get_logo_b64._cache = f"data:image/png;base64,{base64.b64encode(buf.getvalue()).decode()}"
            except Exception:
                raw = logo_path.read_bytes()
                _get_logo_b64._cache = f"data:image/png;base64,{base64.b64encode(raw).decode()}"
        else:
            _get_logo_b64._cache = ""
    return _get_logo_b64._cache


def landing_view(request):
    """Public landing page with stats, converter, and pricing."""
    from core.models import Empresa, CFDI, Plan

    stats = {
        "total_empresas": Empresa.objects.count(),
        "total_cfdis": CFDI.objects.count(),
    }
    planes = Plan.objects.filter(activo=True).order_by("orden")
    return render(request, "public/landing.html", {
        "stats": stats,
        "planes": planes,
        "year": datetime.now().year,
        "logo_b64": _get_logo_b64(),
    })


# ── CRM ───────────────────────────────────────────────────────────────

@staff_required
def crm_list(request):
    from core.models import ConversionLead
    from datetime import timedelta

    now = datetime.now(timezone.utc)
    week_ago = now - timedelta(days=7)

    qs = ConversionLead.objects.all()

    # Filters
    contactado = request.GET.get("contactado", "")
    if contactado == "si":
        qs = qs.filter(contactado=True)
    elif contactado == "no":
        qs = qs.filter(contactado=False)

    cliente = request.GET.get("cliente", "")
    if cliente == "si":
        qs = qs.filter(es_cliente=True)
    elif cliente == "no":
        qs = qs.filter(es_cliente=False)

    search = request.GET.get("q", "").strip()
    if search:
        qs = qs.filter(email__icontains=search)

    # Stats
    all_leads = ConversionLead.objects.all()
    stats = {
        "total_leads": all_leads.count(),
        "leads_semana": all_leads.filter(primera_conversion__gte=week_ago).count(),
        "total_conversiones": sum(all_leads.values_list("conversiones", flat=True)),
        "no_contactados": all_leads.filter(contactado=False).count(),
    }

    return render(request, "panel/crm.html", {
        "current_page": "crm",
        "leads": qs[:200],
        "stats": stats,
        "filters": {
            "contactado": contactado,
            "cliente": cliente,
            "q": search,
        },
    })


@staff_required
def crm_detail(request, lead_id):
    from core.models import ConversionLead

    lead = get_object_or_404(ConversionLead, id=lead_id)

    if request.method == "POST":
        lead.contactado = "contactado" in request.POST
        lead.es_cliente = "es_cliente" in request.POST
        lead.notas = request.POST.get("notas", "")
        lead.save(update_fields=["contactado", "es_cliente", "notas"])
        messages.success(request, f"Lead {lead.email} actualizado")
        return redirect("panel:crm_detail", lead_id=lead_id)

    logs = lead.logs.all()[:50]

    return render(request, "panel/crm_detail.html", {
        "current_page": "crm",
        "lead": lead,
        "logs": logs,
    })


# ── Monitor ──────────────────────────────────────────────────────────

@staff_required
def monitor_view(request):
    from core.models import SystemLog, DescargaLog, Empresa
    from datetime import timedelta
    import subprocess

    qs = SystemLog.objects.all()

    # Filters
    level = request.GET.get("level", "")
    category = request.GET.get("category", "")
    if level:
        qs = qs.filter(level=level)
    if category:
        qs = qs.filter(category=category)

    now = datetime.now(timezone.utc)
    last_hour = now - timedelta(hours=1)
    last_24h = now - timedelta(hours=24)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    # Log stats
    stats = {
        "errors_1h": SystemLog.objects.filter(level__in=["error", "critical"], created_at__gte=last_hour).count(),
        "warnings_1h": SystemLog.objects.filter(level="warning", created_at__gte=last_hour).count(),
        "emails_today": SystemLog.objects.filter(category="email", created_at__gte=today_start).count(),
        "conversions_today": SystemLog.objects.filter(category="conversion", created_at__gte=today_start).count(),
        "total": qs.count(),
    }

    # Worker status
    workers = {}
    for svc in ["cirrus-web", "cirrus-worker", "cirrus-beat"]:
        try:
            result = subprocess.run(
                ["systemctl", "is-active", svc],
                capture_output=True, text=True, timeout=5,
            )
            workers[svc] = result.stdout.strip() == "active"
        except Exception:
            workers[svc] = False

    # Jobs 24h
    downloads_24h = DescargaLog.objects.filter(iniciado_at__gte=last_24h)
    jobs_total = downloads_24h.count()
    jobs_ok = downloads_24h.filter(estado="completado").count()
    jobs_fail = downloads_24h.filter(estado="error").count()
    from django.db.models import Avg, Max
    jobs_agg = downloads_24h.filter(duracion_segundos__isnull=False).aggregate(
        avg_dur=Avg("duracion_segundos"),
        max_dur=Max("duracion_segundos"),
    )
    jobs_stats = {
        "total": jobs_total,
        "ok": jobs_ok,
        "fail": jobs_fail,
        "pct_ok": round(jobs_ok / jobs_total * 100) if jobs_total else 0,
        "avg_duration": int(jobs_agg["avg_dur"] or 0),
        "max_duration": int(jobs_agg["max_dur"] or 0),
    }

    # Playwright health (last check from SystemLog)
    pw_last = SystemLog.objects.filter(
        category="system", message__icontains="Playwright health"
    ).order_by("-created_at").first()
    playwright_health = {
        "last_check": pw_last.created_at if pw_last else None,
        "ok": pw_last.level == "info" if pw_last else None,
        "message": pw_last.message if pw_last else "Sin datos",
    }

    # Download history (last 20)
    recent_downloads = DescargaLog.objects.select_related("empresa").order_by("-iniciado_at")[:20]

    ctx = {
        "current_page": "monitor",
        "logs": qs.order_by("-created_at")[:100],
        "stats": stats,
        "filters": {"level": level, "category": category},
        "levels": SystemLog.LEVEL_CHOICES,
        "categories": SystemLog.CATEGORY_CHOICES,
        "workers": workers,
        "jobs_stats": jobs_stats,
        "playwright_health": playwright_health,
        "recent_downloads": recent_downloads,
    }

    # ── Worker Status (fast, non-blocking approach) ──
    import os

    celery_workers = []
    reserved_count = 0

    # 1. Get ForkPoolWorker PIDs from ps aux (instant)
    worker_pids = []
    try:
        ps_result = subprocess.run(
            ["ps", "aux"],
            capture_output=True, text=True, timeout=5,
        )
        for line in ps_result.stdout.strip().split("\n"):
            if "ForkPoolWorker" in line and "grep" not in line:
                parts = line.split()
                worker_pids.append(int(parts[1]))
    except Exception:
        pass

    # 2. Get active downloads from DB (instant) — cap to 3 workers
    running_downloads = list(
        DescargaLog.objects.filter(estado="ejecutando")
        .select_related("empresa")
        .order_by("-iniciado_at")[:3]
    )

    # 3. Build worker cards: pair running downloads with PIDs
    used_pids = set()
    for i, dl in enumerate(running_downloads):
        pid = worker_pids[i] if i < len(worker_pids) else "?"
        if pid != "?":
            used_pids.add(pid)
        elapsed = ""
        if dl.iniciado_at:
            secs = int((now - dl.iniciado_at).total_seconds())
            if secs < 60:
                elapsed = f"hace {secs}s"
            elif secs < 3600:
                elapsed = f"hace {secs // 60}min"
            else:
                elapsed = f"hace {secs // 3600}h {(secs % 3600) // 60}min"

        tipos = dl.tipos or []
        tipo_str = tipos[0][:3] if tipos else ""
        period = f"{dl.year}-{dl.month_start:02d}" if dl.year and dl.month_start else ""
        if tipo_str:
            period += f" {tipo_str}"

        celery_workers.append({
            "pid": pid,
            "task_name": "descargar_cfdis",
            "rfc": dl.empresa.rfc,
            "period": period,
            "elapsed": elapsed,
            "active": True,
        })

    # 4. Fill idle worker slots (capped to a total of 3)
    remaining = 3 - len(celery_workers)
    idle_pids = [p for p in worker_pids if p not in used_pids]
    for idx in range(remaining):
        pid = idle_pids[idx] if idx < len(idle_pids) else "—"
        celery_workers.append({
            "pid": pid,
            "task_name": "",
            "rfc": "",
            "period": "",
            "elapsed": "",
            "active": False,
        })

    # 5. Pending downloads from DB
    reserved_count = DescargaLog.objects.filter(estado="pendiente").count()

    # 6. Worker memory
    worker_memory = ""
    try:
        mem_result = subprocess.run(
            ["systemctl", "status", "cirrus-worker", "--no-pager"],
            capture_output=True, text=True, timeout=5,
        )
        for line in mem_result.stdout.split("\n"):
            if "Memory" in line:
                worker_memory = line.strip()
                break
    except Exception:
        pass

    ctx["celery_workers"] = sorted(celery_workers, key=lambda w: str(w["pid"]))
    ctx["reserved_count"] = reserved_count
    ctx["worker_memory"] = worker_memory

    return render(request, "panel/monitor.html", ctx)


# ── Public RFC Verifier ──────────────────────────────────────────────────

def verificar_rfc_view(request):
    """Public EFOS 69-B RFC verifier. No login required."""
    from core.models import EFOS

    resultado = None
    ultima_sync = EFOS.objects.order_by("-updated_at").values_list("updated_at", flat=True).first()
    total_registros = EFOS.objects.count()

    if request.method == "POST" or request.GET.get("rfc"):
        rfc = (request.POST.get("rfc") or request.GET.get("rfc", "")).strip().upper()
        if rfc:
            from core.services.efos_sync import verificar_rfc_efos
            resultado = verificar_rfc_efos(rfc)
            resultado["rfc_buscado"] = rfc
            logger.info("EFOS verification: %s → %s", rfc, "EN LISTA" if resultado["en_lista"] else "LIMPIO")

    return render(request, "public/verificar_rfc.html", {
        "resultado": resultado,
        "ultima_sync": ultima_sync,
        "total_registros": total_registros,
    })

