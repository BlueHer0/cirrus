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


# ── Plan de Producción ────────────────────────────────────────────────

@staff_required
def plan_produccion(request):
    """Vista interactiva del plan de producción de Cirrus."""
    return render(request, "panel/plan_produccion.html", {
        "current_page": "plan",
    })


# ── Stripe Events (auditoría de webhook) ──────────────────────────────

@staff_required
def stripe_events_view(request):
    """Panel de auditoría de eventos Stripe. Soporta reintentar eventos en error."""
    from datetime import timedelta, datetime as _dt, timezone as _tz
    from core.models import StripeWebhookEvent

    if request.method == "POST":
        action = request.POST.get("action", "")

        if action == "reprocess":
            ev_id = request.POST.get("event_id", "")
            try:
                wh = StripeWebhookEvent.objects.get(id=ev_id)
            except StripeWebhookEvent.DoesNotExist:
                messages.error(request, "Evento no encontrado")
                return redirect("panel:stripe_events")

            # Reconstruir "event dict" con event_type + data
            from core.services.stripe_service import handle_webhook_event
            reconstructed = {
                "type": wh.event_type,
                "data": {"object": wh.payload},
            }
            wh.intentos += 1
            try:
                res = handle_webhook_event(reconstructed)
                estado = (res or {}).get("status", "procesado")
                if estado == "procesado":
                    wh.estado = "procesado"
                    wh.error_detalle = None
                    from django.utils import timezone
                    wh.procesado_en = timezone.now()
                elif estado == "ignorado":
                    wh.estado = "ignorado"
                else:
                    wh.estado = "error"
                    wh.error_detalle = (res or {}).get("error", "")[:1500]
                wh.save()
                messages.success(request, f"Re-procesado: estado={estado}")
            except Exception as e:
                wh.estado = "error"
                wh.error_detalle = f"{type(e).__name__}: {str(e)[:1500]}"
                wh.save()
                messages.error(request, f"Error reintentando: {e}")
            return redirect("panel:stripe_events")

    # Filtros
    estado_filter = request.GET.get("estado", "")
    type_filter = request.GET.get("type", "")
    qs = StripeWebhookEvent.objects.all().order_by("-recibido_en")
    if estado_filter:
        qs = qs.filter(estado=estado_filter)
    if type_filter:
        qs = qs.filter(event_type__icontains=type_filter)

    now = _dt.now(_tz.utc)
    stats = {
        "total": StripeWebhookEvent.objects.count(),
        "procesados_24h": StripeWebhookEvent.objects.filter(
            estado="procesado", recibido_en__gte=now - timedelta(hours=24),
        ).count(),
        "errores": StripeWebhookEvent.objects.filter(estado="error").count(),
        "ignorados": StripeWebhookEvent.objects.filter(estado="ignorado").count(),
    }

    # Tipos únicos para el dropdown
    types = StripeWebhookEvent.objects.values_list(
        "event_type", flat=True,
    ).distinct().order_by("event_type")

    return render(request, "panel/stripe_events.html", {
        "current_page": "stripe_events",
        "events": qs[:200],
        "stats": stats,
        "types": types,
        "filters": {"estado": estado_filter, "type": type_filter},
    })


# ── Cerebro Fiscal ────────────────────────────────────────────────────

@staff_required
def cerebro_fiscal_view(request):
    """Biblioteca documental fiscal con RAG.

    - GET: lista de documentos + drag&drop de archivos
    - POST upload: múltiples archivos, solo requiere el file. El LLM extrae el resto.
    - POST reprocess: re-encola un documento
    - POST delete: borra documento + chunks + archivos MinIO
    """
    import hashlib
    import uuid as _uuid
    from django.conf import settings
    from core.models import DocumentoFiscal, ChunkFiscal
    from core.services.storage_minio import upload_bytes, delete_object
    from core.services.cerebro_fiscal import esta_configurado

    if request.method == "POST":
        action = request.POST.get("action", "upload")

        # ── Re-procesar documento existente ─────────────────────────
        if action == "reprocess":
            doc_id = request.POST.get("documento_id", "")
            try:
                doc = DocumentoFiscal.objects.get(id=doc_id)
                from core.cerebro_tasks import procesar_documento_fiscal

                # Reset al estado "recibido" — el task decide por dónde empezar
                # basado en qué archivos tiene ya en MinIO
                doc.error_detalle = None
                doc.motivo_rechazo = None
                _next_state = "recibido"
                doc.estado = _next_state
                doc.save(update_fields=[
                    "estado", "error_detalle", "motivo_rechazo", "actualizado_en",
                ])
                procesar_documento_fiscal.apply_async(
                    args=[str(doc.id)], queue="cerebro", countdown=2,
                )
                messages.success(
                    request,
                    f"Re-procesamiento encolado: {doc.titulo or doc.nombre_archivo_original}",
                )
            except DocumentoFiscal.DoesNotExist:
                messages.error(request, "Documento no encontrado")
            return redirect("panel:cerebro_fiscal")

        # ── Eliminar documento (hard delete MinIO + BD) ─────────────
        if action == "delete":
            doc_id = request.POST.get("documento_id", "")
            try:
                doc = DocumentoFiscal.objects.get(id=doc_id)
                label = doc.titulo or doc.nombre_archivo_original
                # MinIO: borrar todos los archivos asociados
                for key_attr in ("archivo_original_key", "archivo_md_key", "archivo_json_key"):
                    key = getattr(doc, key_attr, "")
                    if key:
                        try:
                            delete_object(key)
                        except Exception:
                            pass
                doc.delete()  # CASCADE borra chunks
                messages.warning(request, f"Eliminado: {label}")
            except DocumentoFiscal.DoesNotExist:
                messages.error(request, "Documento no encontrado")
            return redirect("panel:cerebro_fiscal")

        # ── Upload de archivos (múltiples) ──────────────────────────
        archivos = request.FILES.getlist("archivos")
        if not archivos:
            # soporte también para field singular 'archivo'
            single = request.FILES.get("archivo")
            if single:
                archivos = [single]

        if not archivos:
            messages.error(request, "Debes adjuntar al menos un archivo")
            return redirect("panel:cerebro_fiscal")

        if len(archivos) > 10:
            messages.error(request, "Máximo 10 archivos por subida")
            return redirect("panel:cerebro_fiscal")

        from core.cerebro_tasks import procesar_documento_fiscal

        prefix = settings.CEREBRO_MINIO_PREFIX.rstrip("/")
        ok_count = 0
        dup_count = 0
        errores = []

        for archivo in archivos:
            try:
                if archivo.size > 50 * 1024 * 1024:
                    errores.append(f"{archivo.name}: >50MB rechazado")
                    continue

                file_bytes = archivo.read()
                sha256 = hashlib.sha256(file_bytes).hexdigest()

                # Dedup
                existing = DocumentoFiscal.objects.filter(hash_sha256=sha256).first()
                if existing:
                    dup_count += 1
                    continue

                uuid_archivo = _uuid.uuid4()
                safe_name = archivo.name.replace(" ", "_").replace("/", "_")[-150:]
                minio_key = f"{prefix}/originales/{uuid_archivo}_{safe_name}"
                upload_bytes(
                    file_bytes, minio_key,
                    content_type=archivo.content_type or "application/octet-stream",
                )

                doc = DocumentoFiscal.objects.create(
                    uuid_archivo=uuid_archivo,
                    nombre_archivo_original=safe_name,
                    archivo_original_key=minio_key,
                    archivo_tamano_bytes=len(file_bytes),
                    archivo_content_type=archivo.content_type or "",
                    hash_sha256=sha256,
                    estado="recibido",
                    subido_por=request.user,
                )

                procesar_documento_fiscal.apply_async(
                    args=[str(doc.id)], queue="cerebro", countdown=2,
                )
                ok_count += 1

            except Exception as e:
                errores.append(f"{archivo.name}: {e}")

        # Mensajes al usuario
        if ok_count:
            if esta_configurado():
                messages.success(
                    request,
                    f"{ok_count} archivo(s) recibidos y en cola de procesamiento",
                )
            else:
                messages.warning(
                    request,
                    f"{ok_count} archivo(s) recibidos, pero el Spark DGX no está "
                    f"disponible ahora. Se procesarán cuando vuelva.",
                )
        if dup_count:
            messages.warning(request, f"{dup_count} duplicado(s) ignorado(s) (mismo SHA-256)")
        if errores:
            for e in errores[:5]:
                messages.error(request, e)

        return redirect("panel:cerebro_fiscal")

    # ── GET: listado + stats ────────────────────────────────────────
    # Filtro opcional por estado (?estado=indexado|requiere_decision|archivado|…)
    estado_filter = request.GET.get("estado", "").strip()
    base_qs = DocumentoFiscal.objects.all().order_by("-creado_en")
    documentos = base_qs
    if estado_filter:
        documentos = documentos.filter(estado=estado_filter)

    stats = {
        "total_docs": base_qs.count(),
        "indexados": base_qs.filter(estado="indexado").count(),
        "procesando": base_qs.filter(
            estado__in=["recibido", "convirtiendo", "convertido",
                        "validando", "validado", "embeddiendo"],
        ).count(),
        "rechazados": base_qs.filter(estado="rechazado").count(),
        "con_error": base_qs.filter(estado="error").count(),
        "archivados": base_qs.filter(estado="archivado").count(),
        "requiere_decision": base_qs.filter(estado="requiere_decision").count(),
        "total_chunks": ChunkFiscal.objects.count(),
    }

    return render(request, "panel/cerebro_fiscal.html", {
        "current_page": "cerebro",
        "documentos": documentos,
        "stats": stats,
        "spark_disponible": esta_configurado(),
        "estado_filter": estado_filter,
        "estado_choices": DocumentoFiscal.ESTADO_CHOICES,
    })


@staff_required
def cerebro_resolver_version(request, documento_id):
    """Resuelve la decisión del admin cuando se detectó una versión anterior.

    POST con `accion` ∈ {'reemplazar', 'mantener', 'cancelar'}.
    Solo aplica si el doc está en estado 'requiere_decision'.
    """
    from django.db import transaction as _tx
    from core.models import DocumentoFiscal, ChunkFiscal
    from core.services.storage_minio import delete_object
    from core.cerebro_tasks import procesar_documento_fiscal

    if request.method != "POST":
        return redirect("panel:cerebro_detalle", documento_id=documento_id)

    doc = get_object_or_404(DocumentoFiscal, id=documento_id)
    accion = request.POST.get("accion", "")

    if doc.estado != "requiere_decision":
        messages.error(request, "Este documento no está en estado 'requiere_decision'.")
        return redirect("panel:cerebro_detalle", documento_id=doc.id)

    anterior_id = (doc.metadata_extra or {}).get("version_anterior_id")

    if accion == "reemplazar":
        if not anterior_id:
            messages.error(request, "Falta referencia a la versión anterior.")
            return redirect("panel:cerebro_detalle", documento_id=doc.id)
        try:
            with _tx.atomic():
                anterior = DocumentoFiscal.objects.select_for_update().get(id=anterior_id)
                ChunkFiscal.objects.filter(documento=anterior).delete()
                anterior.estado = "archivado"
                anterior.save(update_fields=["estado", "actualizado_en"])
                # Reanudar pipeline del nuevo desde fase 4
                doc.estado = "validado"
                doc.save(update_fields=["estado", "actualizado_en"])
            procesar_documento_fiscal.apply_async(
                args=[str(doc.id)],
                kwargs={"fase_inicio": "embeddings"},
                queue="cerebro",
                countdown=2,
            )
            messages.success(
                request,
                f"Versión anterior archivada. Generando embeddings del nuevo documento…",
            )
        except DocumentoFiscal.DoesNotExist:
            messages.error(request, "La versión anterior ya no existe.")
        return redirect("panel:cerebro_detalle", documento_id=doc.id)

    if accion == "mantener":
        # Continuar pipeline normal — ambas conviven
        doc.estado = "validado"
        # Dejamos metadata_extra con la referencia histórica por auditoría
        doc.save(update_fields=["estado", "actualizado_en"])
        procesar_documento_fiscal.apply_async(
            args=[str(doc.id)],
            kwargs={"fase_inicio": "embeddings"},
            queue="cerebro",
            countdown=2,
        )
        messages.success(request, "Ambas versiones se mantendrán indexadas.")
        return redirect("panel:cerebro_detalle", documento_id=doc.id)

    if accion == "cancelar":
        label = doc.titulo or doc.nombre_archivo_original
        for key_attr in ("archivo_original_key", "archivo_md_key", "archivo_json_key"):
            key = getattr(doc, key_attr, "")
            if key:
                try:
                    delete_object(key)
                except Exception:
                    pass
        doc.delete()
        messages.warning(request, f"Documento eliminado: {label}")
        return redirect("panel:cerebro_fiscal")

    messages.error(request, f"Acción desconocida: {accion!r}")
    return redirect("panel:cerebro_detalle", documento_id=doc.id)


@staff_required
def cerebro_detalle_view(request, documento_id):
    """Detalle de un DocumentoFiscal: timeline, metadata, chunks, archivos."""
    from core.models import DocumentoFiscal, ChunkFiscal

    doc = get_object_or_404(DocumentoFiscal, id=documento_id)

    if request.method == "POST":
        action = request.POST.get("action", "")
        if action == "reprocess":
            from core.cerebro_tasks import procesar_documento_fiscal
            doc.error_detalle = None
            doc.motivo_rechazo = None
            doc.estado = "recibido"
            doc.save(update_fields=[
                "estado", "error_detalle", "motivo_rechazo", "actualizado_en",
            ])
            procesar_documento_fiscal.apply_async(
                args=[str(doc.id)], queue="cerebro", countdown=2,
            )
            messages.success(request, "Re-procesamiento encolado")
            return redirect("panel:cerebro_detalle", documento_id=doc.id)

    # Timeline — cada estado con su estado de completado
    PIPELINE_STATES = [
        ("recibido", "Recibido"),
        ("convirtiendo", "Convirtiendo"),
        ("convertido", "Convertido"),
        ("validando", "Validando"),
        ("validado", "Validado"),
        ("embeddiendo", "Embeddiendo"),
        ("indexado", "Indexado"),
    ]
    state_order = {s[0]: i for i, s in enumerate(PIPELINE_STATES)}
    current_idx = state_order.get(doc.estado, -1)
    is_rechazado = doc.estado == "rechazado"
    is_error = doc.estado == "error"

    timeline = []
    for slug, label in PIPELINE_STATES:
        idx = state_order[slug]
        if is_rechazado and idx > state_order.get("validando", 0):
            status = "skipped"
        elif idx < current_idx:
            status = "done"
        elif idx == current_idx:
            status = "current"
        else:
            status = "pending"
        timeline.append({"slug": slug, "label": label, "status": status})

    # Primeros 5 chunks
    chunks_preview = ChunkFiscal.objects.filter(documento=doc).order_by("posicion_chunk")[:5]

    # Versión anterior referida (si aplica)
    version_anterior = None
    meta_extra = doc.metadata_extra or {}
    if meta_extra.get("version_anterior_id"):
        from core.models import DocumentoFiscal as _DF
        try:
            version_anterior = _DF.objects.get(id=meta_extra["version_anterior_id"])
        except _DF.DoesNotExist:
            version_anterior = None

    return render(request, "panel/cerebro_detalle.html", {
        "current_page": "cerebro",
        "doc": doc,
        "timeline": timeline,
        "chunks_preview": chunks_preview,
        "is_rechazado": is_rechazado,
        "is_error": is_error,
        "is_requiere_decision": doc.estado == "requiere_decision",
        "is_archivado": doc.estado == "archivado",
        "version_anterior": version_anterior,
    })


# ── Dashboard ─────────────────────────────────────────────────────────

@staff_required
def dashboard(request):
    """Business-focused admin dashboard: MRR, funnel, plan distribution, KPIs.

    Excluye staff/superusers de todas las métricas de cliente.
    Incluye agregados del sistema (API keys, downloads, CFDIs) solo como
    números — sin identificar clientes.
    """
    from core.services.dashboard_stats import (
        business_kpis, plan_distribution, growth_series, funnel_conversion,
        operational_health, attention_required, clientes_list_data,
        system_aggregate_stats,
    )

    return render(request, "panel/dashboard.html", {
        "current_page": "dashboard",
        "kpis": business_kpis(),
        "planes": plan_distribution(),
        "growth": growth_series(months=6),
        "funnel": funnel_conversion(),
        "health": operational_health(),
        "system_stats": system_aggregate_stats(),
        "attention": attention_required(),
        "top_clientes": clientes_list_data(limit=10),
    })


# ── Clientes ──────────────────────────────────────────────────────────

@staff_required
def clientes_list(request):
    """List of clients (users with ClienteProfile) with aggregated metrics."""
    from core.models import Plan
    from core.services.dashboard_stats import clientes_list_data

    filters = {
        "q": request.GET.get("q", ""),
        "plan": request.GET.get("plan", ""),
        "estado": request.GET.get("estado", ""),
    }
    clientes = clientes_list_data(filters=filters)
    planes = Plan.objects.filter(activo=True).order_by("orden")

    return render(request, "panel/clientes_list.html", {
        "current_page": "clientes",
        "clientes": clientes,
        "filters": filters,
        "planes": planes,
        "total": len(clientes),
    })


@staff_required
def cliente_detalle(request, user_id):
    """Vista COMERCIAL de un cliente — sin datos fiscales.

    Muestra: perfil, plan/Stripe, pagos, solo el NÚMERO de empresas (no cuáles).
    Acciones: contactar (mailto), cambiar plan, marcar churned.
    """
    from django.contrib.auth.models import User
    from accounts.models import ClienteProfile, StripePayment
    from core.models import Empresa, Plan

    user = get_object_or_404(User, id=user_id)

    # Defensa: no permitir ver staff como "cliente"
    if user.is_staff or user.is_superuser:
        messages.error(request, "Este usuario es staff del sistema, no un cliente.")
        return redirect("panel:clientes")

    profile = getattr(user, "perfil", None)
    if profile is None:
        profile = ClienteProfile.objects.create(user=user)

    # POST: acciones comerciales
    if request.method == "POST":
        action = request.POST.get("action", "")

        if action == "change_plan":
            new_plan_slug = request.POST.get("plan_slug", "").strip()
            try:
                plan = Plan.objects.get(slug=new_plan_slug, activo=True)
                profile.plan_fk = plan
                profile.plan_legacy = plan.slug
                profile.save(update_fields=["plan_fk", "plan_legacy"])
                messages.success(request, f"Plan cambiado a {plan.nombre}")
            except Plan.DoesNotExist:
                messages.error(request, f"Plan '{new_plan_slug}' no encontrado")
            return redirect("panel:cliente_detalle", user_id=user_id)

        elif action == "mark_churned":
            profile.subscription_status = "canceled"
            profile.subscription_cancel_at_period_end = True
            profile.save(update_fields=[
                "subscription_status", "subscription_cancel_at_period_end",
            ])
            messages.success(request, f"Cliente {user.email} marcado como churned")
            return redirect("panel:cliente_detalle", user_id=user_id)

        elif action == "reactivate":
            profile.subscription_status = "active"
            profile.subscription_cancel_at_period_end = False
            profile.save(update_fields=[
                "subscription_status", "subscription_cancel_at_period_end",
            ])
            messages.success(request, f"Cliente {user.email} reactivado")
            return redirect("panel:cliente_detalle", user_id=user_id)

    # Solo el NÚMERO de empresas, sin traer detalles
    empresa_count = Empresa.objects.filter(owner=user).count()

    # Última actividad (indicador de engagement, no dato fiscal)
    last_activity = Empresa.objects.filter(
        owner=user, ultimo_scrape__isnull=False,
    ).order_by("-ultimo_scrape").values_list("ultimo_scrape", flat=True).first()

    pagos = StripePayment.objects.filter(user=user).order_by("-created_at")[:20]
    total_pagado = StripePayment.objects.filter(
        user=user, status__in=["succeeded", "paid"],
    ).aggregate(s=Sum("amount"))["s"] or 0

    # Planes disponibles para el selector de cambio
    planes_disponibles = Plan.objects.filter(activo=True).order_by("orden", "precio_mensual")

    return render(request, "panel/cliente_detalle.html", {
        "current_page": "clientes",
        "cliente": user,
        "profile": profile,
        "empresa_count": empresa_count,
        "last_activity": last_activity,
        "pagos": pagos,
        "total_pagado": total_pagado,
        "planes_disponibles": planes_disponibles,
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
    from core.services.fiel_encryption import upload_fiel

    empresa = get_object_or_404(Empresa, id=empresa_id)

    if request.method == "POST":
        cer_file = request.FILES.get("cer_file")
        key_file = request.FILES.get("key_file")
        password = request.POST.get("password", "")

        if not cer_file or not key_file or not password:
            messages.error(request, "Todos los campos son obligatorios")
            return redirect("panel:empresa_fiel", empresa_id=empresa_id)

        try:
            upload_fiel(
                empresa=empresa,
                cer_data=cer_file.read(),
                key_data=key_file.read(),
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
            from core.services.api_keys_service import crear_api_key

            empresa_ids = request.POST.getlist("empresas")
            empresas = []
            if empresa_ids:
                # Scope server-side a empresas del staff (no asignar ajenas)
                empresas = list(
                    Empresa.objects.filter(id__in=empresa_ids, owner=request.user)
                )
            apikey, key_plain = crear_api_key(
                owner=request.user,
                nombre=nombre,
                empresas=empresas,
                puede_leer="puede_leer" in request.POST,
                puede_trigger_descarga="puede_trigger_descarga" in request.POST,
            )
            # Mostrar la key plana UNA sola vez vía session
            request.session["new_api_key_once"] = {
                "id": str(apikey.id),
                "plain": key_plain,
                "prefix": apikey.key_prefix,
                "nombre": apikey.nombre,
            }
            return redirect("panel:api_keys")

    # Pop de la key recién creada (si aplica) — se muestra una sola vez
    new_key_reveal = request.session.pop("new_api_key_once", None)

    # Solo API keys creadas por este usuario (staff ve las suyas, no todas)
    api_keys = APIKey.objects.filter(
        owner=request.user,
    ).prefetch_related("empresas").order_by("-created_at")

    # Solo empresas del que está creando la key — previene asignar empresas de
    # otros clientes por accidente. Seguridad multi-tenant en el formulario.
    empresas = Empresa.objects.filter(owner=request.user).order_by("rfc")

    return render(request, "panel/api_keys.html", {
        "current_page": "api_keys",
        "api_keys": api_keys,
        "empresas": empresas,
        "new_key_reveal": new_key_reveal,
    })


@staff_required
@require_POST
def api_key_revoke(request, key_id):
    from core.models import APIKey

    key = get_object_or_404(APIKey, id=key_id, owner=request.user)
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

    # ── Snowie leads (sección separada en la misma página) ──────────
    from core.models import SnowieLead
    snowie_qs = SnowieLead.objects.all()

    snowie_estado_filter = request.GET.get("snowie_estado", "")
    if snowie_estado_filter:
        snowie_qs = snowie_qs.filter(estado=snowie_estado_filter)

    snowie_stats = {
        "total": SnowieLead.objects.count(),
        "nuevos": SnowieLead.objects.filter(estado="nuevo").count(),
        "convertidos": SnowieLead.objects.filter(estado="convertido").count(),
        "ultima_semana": SnowieLead.objects.filter(creado_en__gte=week_ago).count(),
    }

    return render(request, "panel/crm.html", {
        "current_page": "crm",
        "leads": qs[:200],
        "stats": stats,
        "filters": {
            "contactado": contactado,
            "cliente": cliente,
            "q": search,
            "snowie_estado": snowie_estado_filter,
        },
        "snowie_leads": snowie_qs[:200],
        "snowie_stats": snowie_stats,
    })


@staff_required
def snowie_lead_update_estado(request, lead_id):
    """Cambiar manualmente el estado de un SnowieLead."""
    from core.models import SnowieLead

    lead = get_object_or_404(SnowieLead, id=lead_id)

    if request.method != "POST":
        return redirect("panel:crm")

    nuevo_estado = request.POST.get("estado", "").strip()
    valid = [c[0] for c in SnowieLead.ESTADO_CHOICES]
    if nuevo_estado not in valid:
        messages.error(request, f"Estado inválido: {nuevo_estado}")
        return redirect("panel:crm")

    lead.estado = nuevo_estado
    lead.save(update_fields=["estado", "actualizado_en"])
    messages.success(request, f"Lead Snowie marcado como {nuevo_estado}")
    return redirect("panel:crm")


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


# ── Telegram Config ───────────────────────────────────────────────────

@staff_required
def telegram_config(request):
    """Configurar parámetros del bot de Telegram + ver log de alertas + usuarios vinculados."""
    from datetime import timedelta
    from django.contrib.auth.models import User
    from accounts.models import ClienteProfile
    from core.models import SystemSettings, TelegramAlert
    from core.services.fiel_encryption import encrypt_password, decrypt_password
    from core.services.alerts import send_telegram

    s = SystemSettings.load()

    if request.method == "POST":
        action = request.POST.get("action", "save")

        if action == "save":
            s.telegram_enabled = "telegram_enabled" in request.POST
            s.telegram_bot_username = request.POST.get("telegram_bot_username", "").strip()
            s.telegram_admin_chat_id = request.POST.get("telegram_admin_chat_id", "").strip()
            s.telegram_solo_stack = "telegram_solo_stack" in request.POST
            s.telegram_send_info = "telegram_send_info" in request.POST
            s.telegram_send_warning = "telegram_send_warning" in request.POST
            s.telegram_send_error = "telegram_send_error" in request.POST
            s.telegram_send_critical = "telegram_send_critical" in request.POST
            new_token = request.POST.get("telegram_bot_token", "").strip()
            if new_token:
                s.telegram_bot_token_encrypted = encrypt_password(new_token)
            s.updated_by = request.user
            s.save()
            messages.success(request, "Configuración de Telegram guardada")
            return redirect("panel:telegram_config")

        elif action == "test":
            msg = request.POST.get("test_message", "Test manual desde panel admin")
            ok = send_telegram(msg, level="info", category="test")
            if ok:
                messages.success(request, f"Mensaje de prueba enviado a {s.telegram_admin_chat_id}")
            else:
                messages.error(request, "Fallo el envío. Revisa el log de alertas abajo para detalles.")
            return redirect("panel:telegram_config")

    # Stats 24h
    now = datetime.now(timezone.utc)
    last_24h = now - timedelta(hours=24)
    last_7d = now - timedelta(days=7)

    alerts_24h = TelegramAlert.objects.filter(created_at__gte=last_24h)
    alerts_7d = TelegramAlert.objects.filter(created_at__gte=last_7d)

    stats = {
        "sent_24h": alerts_24h.filter(status="sent").count(),
        "failed_24h": alerts_24h.filter(status="failed").count(),
        "skipped_24h": alerts_24h.filter(status="skipped").count(),
        "sent_7d": alerts_7d.filter(status="sent").count(),
        "failed_7d": alerts_7d.filter(status="failed").count(),
    }

    # Últimos 50 eventos
    recent_alerts = TelegramAlert.objects.all()[:50]

    # Usuarios vinculados
    linked_users = ClienteProfile.objects.filter(
        telegram_chat_id__gt="",
    ).select_related("user").order_by("-telegram_linked_at")

    # Token preview (masked)
    token_preview = ""
    if s.telegram_bot_token_encrypted:
        try:
            full = decrypt_password(bytes(s.telegram_bot_token_encrypted))
            token_preview = f"{full[:12]}...{full[-6:]}"
        except Exception:
            token_preview = "●●● (error al descifrar)"

    return render(request, "panel/telegram_config.html", {
        "current_page": "telegram",
        "settings": s,
        "token_preview": token_preview,
        "stats": stats,
        "recent_alerts": recent_alerts,
        "linked_users": linked_users,
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
    from core.models import DescargaJob
    downloads_24h = DescargaLog.objects.filter(iniciado_at__gte=last_24h)
    jobs_total = downloads_24h.count()
    jobs_ok = downloads_24h.filter(estado="completado").count()
    jobs_fail = downloads_24h.filter(estado="error").count()
    from django.db.models import Avg, Max
    jobs_agg = downloads_24h.filter(duracion_segundos__isnull=False).aggregate(
        avg_dur=Avg("duracion_segundos"),
        max_dur=Max("duracion_segundos"),
    )
    # Queue state (agregado, no por cliente)
    queue_en_cola = DescargaJob.objects.filter(estado="en_cola").count()
    queue_ejecutando = DescargaJob.objects.filter(estado="ejecutando").count()
    queue_error = DescargaJob.objects.filter(estado="error").count()
    queue_completado_vacio = DescargaJob.objects.filter(estado="completado_vacio").count()

    jobs_stats = {
        "total": jobs_total,
        "ok": jobs_ok,
        "fail": jobs_fail,
        "pct_ok": round(jobs_ok / jobs_total * 100) if jobs_total else 0,
        "avg_duration": int(jobs_agg["avg_dur"] or 0),
        "max_duration": int(jobs_agg["max_dur"] or 0),
        "queue_en_cola": queue_en_cola,
        "queue_ejecutando": queue_ejecutando,
        "queue_error": queue_error,
        "queue_completado_vacio": queue_completado_vacio,
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

    # ── Incidentes de descarga (inteligencia operativa) ───────────────
    from core.models import DescargaIncidente
    incidentes_abiertos = DescargaIncidente.objects.filter(
        resuelto=False
    ).select_related("empresa", "job").order_by("-creado_en")[:30]
    ctx["incidentes_abiertos"] = incidentes_abiertos
    ctx["incidentes_stats"] = {
        "abiertos": DescargaIncidente.objects.filter(resuelto=False).count(),
        "nuevos_24h": DescargaIncidente.objects.filter(creado_en__gte=last_24h).count(),
    }

    # ── Pipelines ─────────────────────────────────────────────────────
    from core.models import PipelineState

    pipelines_activos = PipelineState.objects.filter(
        estado__in=['en_proceso', 'reintentando', 'esperando_sat', 'pendiente']
    ).select_related('empresa').order_by('-actualizado')[:20]

    pipelines_recientes = PipelineState.objects.filter(
        actualizado__gte=last_24h
    ).select_related('empresa').order_by('-actualizado')[:20]

    pipelines_bloqueados_sat = PipelineState.objects.filter(bloqueado_por_sat=True).count()

    # Use recientes if no activos, so the section always has data
    ctx['pipelines_activos'] = pipelines_activos if pipelines_activos.exists() else pipelines_recientes
    ctx['pipelines_bloqueados_sat'] = pipelines_bloqueados_sat

    activos_count = pipelines_activos.count()
    completados_24h = PipelineState.objects.filter(
        estado='completado', actualizado__gte=last_24h
    ).count()
    errores_24h = PipelineState.objects.filter(
        estado='error', actualizado__gte=last_24h
    ).count()
    ctx['pipelines_summary'] = (
        f"{activos_count} activos · {completados_24h} completados · {errores_24h} errores (24h)"
        + (f" · {pipelines_bloqueados_sat} bloqueados SAT" if pipelines_bloqueados_sat else "")
    )

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

    # ── SAT Health Monitor ───────────────────────────────────────────────
    from core.models import SATHealthProbe, SATHealthSummary

    # Section A: Current status (last 30 min)
    since_30m = now - timedelta(minutes=30)
    sat_recent = SATHealthProbe.objects.filter(timestamp__gte=since_30m)
    sat_total = sat_recent.count()
    sat_success = sat_recent.filter(result='success').count()
    sat_availability = round((sat_success / sat_total) * 100, 1) if sat_total > 0 else 0

    if sat_total == 0:
        sat_status = 'unknown'
        sat_status_label = 'SIN DATOS'
        sat_status_emoji = '⚪'
    elif sat_availability >= 70:
        sat_status = 'up'
        sat_status_label = 'OPERATIVO'
        sat_status_emoji = '🟢'
    elif sat_availability >= 30:
        sat_status = 'degraded'
        sat_status_label = 'DEGRADADO'
        sat_status_emoji = '🟡'
    else:
        sat_status = 'down'
        sat_status_label = 'CAÍDO'
        sat_status_emoji = '🔴'

    # Last probe
    sat_last_probe = SATHealthProbe.objects.order_by('-timestamp').first()
    sat_last_probe_ago = ""
    if sat_last_probe:
        secs = int((now - sat_last_probe.timestamp).total_seconds())
        if secs < 60:
            sat_last_probe_ago = f"hace {secs}s"
        elif secs < 3600:
            sat_last_probe_ago = f"hace {secs // 60} min"
        else:
            sat_last_probe_ago = f"hace {secs // 3600}h {(secs % 3600) // 60}min"

    # Avg login time (successful probes last hour)
    since_1h = now - timedelta(hours=1)
    avg_time = SATHealthProbe.objects.filter(
        timestamp__gte=since_1h, result='success'
    ).aggregate(avg=Avg('time_total_ms'))['avg']

    ctx['sat_health'] = {
        'status': sat_status,
        'status_label': sat_status_label,
        'status_emoji': sat_status_emoji,
        'availability': sat_availability,
        'total': sat_total,
        'success': sat_success,
        'failed': sat_total - sat_success,
        'avg_time_ms': int(avg_time) if avg_time else None,
        'last_probe': sat_last_probe,
        'last_probe_ago': sat_last_probe_ago,
    }

    # Section B: Node status (health check each worker)
    sat_nodes_config = [
        {'id': 'vps2',  'ip': '10.20.0.2'},
        {'id': 'vpsx',  'ip': '10.20.0.100'},
        {'id': 'spark', 'ip': '10.20.0.6'},
    ]
    sat_nodes = []
    try:
        import httpx
        for node in sat_nodes_config:
            try:
                resp = httpx.get(f"http://{node['ip']}:8300/health", timeout=3)
                sat_nodes.append({**node, 'status': 'online' if resp.status_code == 200 else 'error'})
            except Exception:
                sat_nodes.append({**node, 'status': 'offline'})
    except ImportError:
        for node in sat_nodes_config:
            sat_nodes.append({**node, 'status': 'offline'})
    ctx['sat_nodes'] = sat_nodes

    # Section C: Recent probes (last 20)
    ctx['sat_probes'] = SATHealthProbe.objects.order_by('-timestamp')[:20]

    # Section D: Hourly summaries (last 24h)
    ctx['sat_summaries'] = SATHealthSummary.objects.order_by('-hour')[:24]

    # ── Chart data (JSON for Chart.js) ───────────────────────────────────
    import json as _json

    probes_qs = SATHealthProbe.objects.order_by('-timestamp')[:300]
    ctx['sat_probes_json'] = _json.dumps([{
        'timestamp': p.timestamp.isoformat(),
        'result': p.result,
        'time_total_ms': p.time_total_ms,
        'node_id': p.node_id,
        'rfc_used': p.rfc_used,
    } for p in probes_qs])

    summaries_qs = SATHealthSummary.objects.order_by('-hour')[:168]
    ctx['sat_summaries_json'] = _json.dumps([{
        'hour': s.hour.isoformat(),
        'availability_pct': float(s.availability_pct) if s.availability_pct else 0,
        'total_probes': s.total_probes,
        'successful_probes': s.successful_probes,
        'avg_time_ms': s.avg_total_time_ms,
        'most_common_error': s.most_common_error or '',
    } for s in summaries_qs])

    # ── Collapsed summaries ──────────────────────────────────────────────
    svc_statuses = list(workers.values())
    svc_down = sum(1 for s in svc_statuses if not s)
    ctx['svc_summary'] = f"{'Todo operativo' if svc_down == 0 else f'{svc_down} servicio(s) caído(s)'}"
    ctx['svc_dots'] = svc_statuses  # list of True/False

    ctx['stats_summary'] = (
        f"{stats['errors_1h']} errores · {stats['warnings_1h']} warnings · {stats['total']} logs"
    )

    sat_avg_str = f"{int(avg_time/1000)}s" if avg_time and avg_time >= 1000 else (f"{int(avg_time)}ms" if avg_time else "—")
    ctx['sat_summary'] = (
        f"{sat_status_emoji} {sat_status_label} — {sat_availability}% disponibilidad — {sat_avg_str} promedio"
    )

    ctx['jobs_summary'] = (
        f"{jobs_stats['total']} jobs — {jobs_stats['pct_ok']}% éxito"
        + (f" — {jobs_stats['avg_duration']}s promedio" if jobs_stats['total'] else "")
    )

    ctx['workers_summary'] = (
        f"{len(celery_workers)} workers · {reserved_count} en cola"
        + (f" · {worker_memory}" if worker_memory else "")
    )

    last_dl = recent_downloads[0] if recent_downloads else None
    ctx['descargas_summary'] = (
        f"Última: {last_dl.empresa.rfc} {last_dl.year}/{last_dl.month_start} "
        f"{'✓ OK' if last_dl.estado == 'completado' else '✗ ' + last_dl.estado} — "
        f"{last_dl.iniciado_at.strftime('%d/%m %H:%M')}"
    ) if last_dl else "Sin descargas recientes"

    pw_status = "✅ OK" if playwright_health['ok'] else ("⏳ Pendiente" if playwright_health['ok'] is None else "❌ FAIL")
    pw_time = playwright_health['last_check'].strftime('%d/%m %H:%M') if playwright_health['last_check'] else "—"
    ctx['playwright_summary'] = f"{pw_status} — {pw_time}"

    last_log = qs.order_by('-created_at').first()
    ctx['logs_summary'] = (
        f"{qs.count()} entradas — último: {last_log.created_at.strftime('%d/%m %H:%M')}"
    ) if last_log else "Sin logs"

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

