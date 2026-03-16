"""
Accounts Views — Client-facing app at /app/.

All views scoped to the logged-in user. Non-staff users only.
"""

import logging
from datetime import datetime, timezone

from django.conf import settings
from django.contrib.auth import authenticate, login as auth_login, logout as auth_logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.contrib import messages
from django.core import signing
from django.db.models import Count, Sum, Q
from django.http import HttpResponse, JsonResponse
from django.shortcuts import render, redirect, get_object_or_404
from django.views.decorators.http import require_POST

logger = logging.getLogger("accounts.views")

APP_LOGIN_URL = "/app/login/"
CONFIRM_SALT = "email-confirm"
CONFIRM_MAX_AGE = 48 * 3600  # 48 hours


# ── Helpers ───────────────────────────────────────────────────────────

def _ensure_profile(user):
    """Get or create ClienteProfile for user."""
    from accounts.models import ClienteProfile
    profile, _ = ClienteProfile.objects.get_or_create(user=user)
    return profile


def _get_empresa_or_404(request, empresa_id):
    """Get empresa scoped to current user."""
    from core.models import Empresa
    return get_object_or_404(Empresa, id=empresa_id, owner=request.user)


def _get_user_rfcs(user):
    """Get RFCs the user can access (has empresa with verified FIEL)."""
    from core.models import Empresa
    return list(
        Empresa.objects.filter(owner=user, fiel_verificada=True)
        .values_list("rfc", flat=True)
    )


def _generate_confirm_token(user):
    """Generate a signed token for email confirmation."""
    return signing.dumps({"user_id": user.id, "email": user.email}, salt=CONFIRM_SALT)


def _send_confirmation_email(user, token):
    """Send branded HTML email with confirmation link."""
    try:
        from django.core.mail import EmailMultiAlternatives
        from django.template.loader import render_to_string

        confirm_url = f"https://cirrus.nubex.me/app/confirmar/{token}/"
        subject = "Confirma tu cuenta en Cirrus"
        text_body = (
            f"Hola {user.first_name},\n\n"
            f"Confirma tu cuenta: {confirm_url}\n\n"
            f"Este enlace expira en 48 horas.\n\n"
            f"— Equipo Cirrus"
        )

        html_body = render_to_string("emails/bienvenida.html", {
            "nombre": user.first_name,
            "confirm_url": confirm_url,
        })

        msg = EmailMultiAlternatives(
            subject=subject,
            body=text_body,
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=[user.email],
        )
        msg.attach_alternative(html_body, "text/html")
        msg.send(fail_silently=True)

        from core.services.monitor import log_info
        log_info("email", f"Confirmación enviada a {user.email}")
    except Exception as e:
        from core.services.monitor import log_error
        log_error("email", f"Error enviando confirmación a {user.email}", detail=str(e))


# ── Auth ──────────────────────────────────────────────────────────────

def app_register(request):
    """Client registration with email confirmation via Django signing."""
    if request.user.is_authenticated:
        if request.user.is_staff:
            auth_logout(request)
        else:
            return redirect("app:dashboard")

    if request.method == "POST":
        email = request.POST.get("email", "").strip().lower()
        password = request.POST.get("password", "")
        password2 = request.POST.get("password2", "")
        nombre = request.POST.get("nombre", "").strip()
        empresa = request.POST.get("empresa", "").strip()
        telefono = request.POST.get("telefono", "").strip()

        errors = []
        if not email or "@" not in email:
            errors.append("Email inválido")
        if not password or len(password) < 6:
            errors.append("Password debe tener al menos 6 caracteres")
        if password != password2:
            errors.append("Los passwords no coinciden")
        if not nombre:
            errors.append("Nombre es obligatorio")
        if User.objects.filter(username=email).exists():
            errors.append("Ya existe una cuenta con ese email")

        if errors:
            for e in errors:
                messages.error(request, e)
            return render(request, "app/registro.html", {
                "form": request.POST, "year": datetime.now().year,
            })

        # Create user (inactive until email confirmed)
        parts = nombre.split(" ", 1)
        user = User.objects.create_user(
            username=email,
            email=email,
            password=password,
            first_name=parts[0],
            last_name=parts[1] if len(parts) > 1 else "",
            is_active=False,
        )

        # Create profile
        from accounts.models import ClienteProfile
        ClienteProfile.objects.create(
            user=user,
            empresa_nombre=empresa,
            telefono=telefono,
        )

        # Generate signed token and send confirmation email
        token = _generate_confirm_token(user)
        _send_confirmation_email(user, token)

        from core.services.monitor import log_info
        log_info("auth", f"Nuevo registro: {email} (pendiente confirmación)")

        return render(request, "app/registro_exitoso.html", {
            "email": email, "year": datetime.now().year,
        })

    return render(request, "app/registro.html", {"year": datetime.now().year})


def confirmar_email(request, token):
    """Confirm email via signed token."""
    try:
        data = signing.loads(token, salt=CONFIRM_SALT, max_age=CONFIRM_MAX_AGE)
        user = User.objects.get(id=data["user_id"])

        if user.is_active:
            messages.info(request, "Tu cuenta ya estaba confirmada.")
            return redirect("app:login")

        user.is_active = True
        user.save(update_fields=["is_active"])

        from core.services.monitor import log_info
        log_info("auth", f"Email confirmado: {user.email}")

        messages.success(request, "¡Cuenta confirmada! Ya puedes iniciar sesión.")
        return redirect("app:login")

    except signing.SignatureExpired:
        messages.error(request, "Este enlace expiró. Solicita uno nuevo.")
        return render(request, "app/confirmacion_expirada.html", {
            "year": datetime.now().year,
        })
    except (signing.BadSignature, User.DoesNotExist, KeyError):
        messages.error(request, "Enlace no válido.")
        return redirect("app:login")


def reenviar_confirmacion(request):
    """Resend confirmation email."""
    if request.method == "POST":
        email = request.POST.get("email", "").strip().lower()
        try:
            user = User.objects.get(email=email, is_active=False)
            token = _generate_confirm_token(user)
            _send_confirmation_email(user, token)
        except User.DoesNotExist:
            pass  # Don't reveal if email exists

        messages.success(request, "Si el correo existe, te enviamos un nuevo enlace de confirmación.")
        return redirect("app:login")

    return redirect("app:login")


def app_login(request):
    """Client login. Clears existing session if user is staff."""
    if request.user.is_authenticated:
        if request.user.is_staff:
            auth_logout(request)
        else:
            return redirect("app:dashboard")

    if request.method == "POST":
        # Clear any existing session first
        if request.user.is_authenticated:
            auth_logout(request)

        email = request.POST.get("email", "").strip().lower()
        password = request.POST.get("password", "")

        # Check if user exists but is inactive (unconfirmed)
        try:
            user_check = User.objects.get(username=email)
            if not user_check.is_active:
                messages.warning(request, "Tu cuenta aún no está confirmada. Revisa tu correo.")
                return render(request, "app/login.html", {
                    "year": datetime.now().year,
                    "show_resend": True,
                    "resend_email": email,
                })
        except User.DoesNotExist:
            pass

        user = authenticate(request, username=email, password=password)
        if user:
            auth_login(request, user)
            from core.services.monitor import log_info
            log_info("auth", f"Login: {email}")
            return redirect(request.GET.get("next", "app:dashboard"))
        else:
            messages.error(request, "Email o contraseña incorrectos")

    return render(request, "app/login.html", {"year": datetime.now().year})


def app_logout(request):
    auth_logout(request)
    return redirect("landing")


# ── Dashboard ─────────────────────────────────────────────────────────

@login_required(login_url=APP_LOGIN_URL)
def app_dashboard(request):
    if request.user.is_staff:
        messages.info(request, "Usa el panel de administración")
        return redirect("/panel/")
    from core.models import Empresa, CFDI

    profile = _ensure_profile(request.user)
    empresas = Empresa.objects.filter(owner=request.user)
    user_rfcs = _get_user_rfcs(request.user)
    total_cfdis = CFDI.objects.filter(
        Q(rfc_empresa__in=user_rfcs) | Q(uploaded_by=request.user)
    ).count()

    plan = profile.get_plan()
    return render(request, "app/dashboard.html", {
        "current_page": "dashboard",
        "profile": profile,
        "plan": plan,
        "stats": {
            "empresas": empresas.count(),
            "max_empresas": plan.max_empresas if plan else 1,
            "total_cfdis": total_cfdis,
            "conversiones_mes": profile.conversiones_este_mes,
            "max_conversiones": plan.max_conversiones_pdf if plan else 10,
            "descargas_mes": profile.descargas_este_mes,
            "max_descargas": plan.max_descargas_mes if plan else 1,
            "plan": plan.nombre if plan else "Gratis",
        },
    })


# ── Empresas ──────────────────────────────────────────────────────────

@login_required(login_url=APP_LOGIN_URL)
def app_empresas_list(request):
    from core.models import Empresa

    profile = _ensure_profile(request.user)

    if request.method == "POST":
        rfc = request.POST.get("rfc", "").strip().upper()
        nombre = request.POST.get("nombre", "").strip()
        notas = request.POST.get("notas", "").strip()

        empresas_count = Empresa.objects.filter(owner=request.user).count()
        plan = profile.get_plan()
        max_emp = plan.max_empresas if plan else 1
        if empresas_count >= max_emp:
            messages.error(request, f"Tu plan permite máximo {max_emp} empresa(s). Mejora tu plan para agregar más.")
        elif not rfc or not nombre:
            messages.error(request, "RFC y Nombre son obligatorios")
        elif len(rfc) > 13:
            messages.error(request, "RFC no puede tener más de 13 caracteres")
        elif Empresa.objects.filter(rfc=rfc).exists():
            messages.error(request, f"Ya existe una empresa con RFC {rfc}")
        else:
            empresa = Empresa.objects.create(
                rfc=rfc, nombre=nombre, notas=notas, owner=request.user,
            )
            messages.success(request, f"Empresa {rfc} agregada")
            return redirect("app:empresa_detail", empresa_id=empresa.id)

    empresas = Empresa.objects.filter(owner=request.user).annotate(
        cfdi_count=Count("cfdis"),
    ).order_by("nombre")

    return render(request, "app/empresas_list.html", {
        "current_page": "empresas",
        "empresas": empresas,
        "profile": profile,
    })


@login_required(login_url=APP_LOGIN_URL)
def app_empresa_detail(request, empresa_id):
    from core.models import CFDI, DescargaLog

    empresa = _get_empresa_or_404(request, empresa_id)

    if request.method == "POST":
        action = request.POST.get("action", "")
        if action == "edit":
            empresa.nombre = request.POST.get("nombre", empresa.nombre).strip()
            empresa.notas = request.POST.get("notas", "").strip()
            empresa.save(update_fields=["nombre", "notas", "updated_at"])
            messages.success(request, "Empresa actualizada")
            return redirect("app:empresa_detail", empresa_id=empresa_id)

    cfdis_qs = empresa.cfdis.all()
    stats = cfdis_qs.aggregate(
        total=Sum("total"),
        recibidos=Count("uuid", filter=Q(tipo_relacion="recibido")),
        emitidos=Count("uuid", filter=Q(tipo_relacion="emitido")),
    )

    # FIEL status
    fiel_status = "not_configured"
    if empresa.fiel_cer_key and empresa.fiel_key_key:
        if empresa.fiel_verificada:
            fiel_status = "verified"
        else:
            fiel_status = "pending"

    now = datetime.now()
    years = [2026, 2025]
    months = [
        (1, "Enero"), (2, "Febrero"), (3, "Marzo"), (4, "Abril"),
        (5, "Mayo"), (6, "Junio"), (7, "Julio"), (8, "Agosto"),
        (9, "Septiembre"), (10, "Octubre"), (11, "Noviembre"), (12, "Diciembre"),
    ]

    # Recent downloads
    downloads_qs = DescargaLog.objects.filter(empresa=empresa).order_by("-iniciado_at")
    has_running = downloads_qs.filter(estado="ejecutando").exists()
    recent_downloads = downloads_qs[:10]

    return render(request, "app/empresa_detail.html", {
        "current_page": "empresas",
        "empresa": empresa,
        "fiel_status": fiel_status,
        "cfdis": cfdis_qs.order_by("-fecha")[:20],
        "cfdi_count": cfdis_qs.count(),
        "recibidos": stats["recibidos"] or 0,
        "emitidos": stats["emitidos"] or 0,
        "monto_total": stats["total"] or 0,
        "years": years,
        "months": months,
        "current_year": now.year,
        "current_month": now.month,
        "recent_downloads": recent_downloads,
        "has_running": has_running,
    })


@login_required(login_url=APP_LOGIN_URL)
def app_empresa_fiel(request, empresa_id):
    empresa = _get_empresa_or_404(request, empresa_id)

    if request.method == "POST":
        from core.services.fiel_encryption import upload_fiel

        cer_file = request.FILES.get("cer_file")
        key_file = request.FILES.get("key_file")
        password = request.POST.get("password", "")

        if not cer_file or not key_file or not password:
            messages.error(request, "Todos los campos son obligatorios")
        else:
            try:
                upload_fiel(
                    empresa=empresa,
                    cer_data=cer_file.read(),
                    key_data=key_file.read(),
                    password=password,
                )
                # Set status and auto-trigger verification
                empresa.fiel_status = "verificando"
                empresa.save(update_fields=["fiel_status"])

                from core.tasks import verificar_fiel
                verificar_fiel.delay(str(empresa.id))

                from core.services.monitor import log_info
                log_info("fiel", f"FIEL subida para {empresa.rfc}, verificación iniciada",
                         user_email=request.user.email)

                messages.success(request, f"FIEL recibida para {empresa.rfc}. Estamos verificando contra el SAT...")
                return redirect("app:empresa_detail", empresa_id=empresa_id)
            except Exception as e:
                from core.services.monitor import log_error
                log_error("fiel", f"Error subiendo FIEL para {empresa.rfc}: {e}",
                          user_email=request.user.email)
                messages.error(request, f"Error: {e}")

    return render(request, "app/empresa_fiel.html", {
        "current_page": "empresas",
        "empresa": empresa,
    })


@login_required(login_url=APP_LOGIN_URL)
@require_POST
def app_empresa_verificar(request, empresa_id):
    empresa = _get_empresa_or_404(request, empresa_id)
    from core.tasks import verificar_fiel
    verificar_fiel.delay(str(empresa.id))
    messages.info(request, f"Verificación iniciada para {empresa.rfc}")
    return redirect("app:empresa_detail", empresa_id=empresa_id)


@login_required(login_url=APP_LOGIN_URL)
@require_POST
def app_empresa_descargar(request, empresa_id):
    empresa = _get_empresa_or_404(request, empresa_id)
    from core.tasks import descargar_cfdis

    # Validate FIEL is verified
    if not empresa.fiel_verificada:
        messages.error(request, "Verifica tu FIEL primero antes de descargar.")
        return redirect("app:empresa_detail", empresa_id=empresa_id)

    now = datetime.now()
    year = int(request.POST.get("year", now.year))
    month_start = int(request.POST.get("month_start", now.month))
    month_end = int(request.POST.get("month_end", now.month))
    tipos = request.POST.getlist("tipos") or ["recibidos", "emitidos"]

    # Validate dates
    if month_end < month_start:
        messages.error(request, "El mes fin debe ser igual o posterior al mes inicio.")
        return redirect("app:empresa_detail", empresa_id=empresa_id)
    if year > now.year or (year == now.year and month_end > now.month):
        messages.error(request, "No puedes descargar periodos futuros.")
        return redirect("app:empresa_detail", empresa_id=empresa_id)
    if year < 2025:
        messages.error(request, "Solo se permiten descargas desde enero 2025.")
        return redirect("app:empresa_detail", empresa_id=empresa_id)

    descargar_cfdis.delay(str(empresa.id), params={
        "year": year,
        "month_start": month_start,
        "month_end": month_end,
        "tipos": tipos,
    }, triggered_by="manual")

    profile = _ensure_profile(request.user)
    profile.descargas_este_mes += 1
    profile.save(update_fields=["descargas_este_mes"])

    from core.services.monitor import log_info
    log_info("download", f"Descarga iniciada: {empresa.rfc} {year}/{month_start}-{month_end}",
             user_email=request.user.email)

    messages.success(request, f"Descarga iniciada para {empresa.rfc}. Puedes ver el progreso abajo.")
    return redirect("app:descargas")


# ── Descargas Module ─────────────────────────────────────────────────

@login_required(login_url=APP_LOGIN_URL)
def app_descargas(request):
    """Download manager — form + history for all user empresas."""
    from core.models import Empresa, DescargaLog

    empresas = Empresa.objects.filter(owner=request.user).order_by("rfc")
    empresas_fiel = empresas.filter(fiel_verificada=True)

    # Download history for all user empresas
    downloads_qs = DescargaLog.objects.filter(
        empresa__owner=request.user
    ).select_related("empresa").order_by("-iniciado_at")
    has_running = downloads_qs.filter(estado="ejecutando").exists()
    recent_downloads = downloads_qs[:30]

    now = datetime.now()
    months = [
        (1, "Enero"), (2, "Febrero"), (3, "Marzo"), (4, "Abril"),
        (5, "Mayo"), (6, "Junio"), (7, "Julio"), (8, "Agosto"),
        (9, "Septiembre"), (10, "Octubre"), (11, "Noviembre"), (12, "Diciembre"),
    ]

    return render(request, "app/descargas.html", {
        "current_page": "descargas",
        "empresas_fiel": empresas_fiel,
        "has_empresas": empresas.exists(),
        "has_fiel": empresas_fiel.exists(),
        "recent_downloads": recent_downloads,
        "has_running": has_running,
        "years": [2026, 2025],
        "months": months,
        "current_year": now.year,
        "current_month": now.month,
    })


# ── CFDIs ─────────────────────────────────────────────────────────────

@login_required(login_url=APP_LOGIN_URL)
def app_cfdis_list(request):
    from core.models import CFDI, Empresa

    empresas = Empresa.objects.filter(owner=request.user).order_by("rfc")
    user_rfcs = _get_user_rfcs(request.user)
    qs = CFDI.objects.filter(
        Q(rfc_empresa__in=user_rfcs) | Q(uploaded_by=request.user)
    ).select_related("empresa")

    filters = {}

    # Empresa filter (by UUID)
    empresa_id = request.GET.get("empresa", "")
    if empresa_id:
        if empresa_id == "__none__":
            qs = qs.filter(empresa__isnull=True)
            filters["empresa"] = "__none__"
        else:
            qs = qs.filter(empresa__id=empresa_id)
            filters["empresa"] = empresa_id

    # Legacy RFC filter support
    rfc = request.GET.get("rfc", "")
    if rfc and not empresa_id:
        if rfc == "__none__":
            qs = qs.filter(empresa__isnull=True)
            filters["empresa"] = "__none__"
        else:
            qs = qs.filter(empresa__rfc=rfc)
            filters["rfc"] = rfc

    year = request.GET.get("year", "")
    if year:
        qs = qs.filter(fecha__year=int(year))
        filters["year"] = int(year)

    month = request.GET.get("month", "")
    if month:
        qs = qs.filter(fecha__month=int(month))
        filters["month"] = int(month)

    tipo = request.GET.get("tipo", "")
    if tipo:
        qs = qs.filter(tipo_relacion=tipo)
        filters["tipo"] = tipo

    tipo_comp = request.GET.get("tipo_comp", "")
    if tipo_comp:
        qs = qs.filter(tipo_comprobante=tipo_comp)
        filters["tipo_comp"] = tipo_comp

    rfc_contra = request.GET.get("rfc_contra", "").strip()
    if rfc_contra:
        qs = qs.filter(Q(rfc_emisor__icontains=rfc_contra) | Q(rfc_receptor__icontains=rfc_contra))
        filters["rfc_contra"] = rfc_contra

    monto_min = request.GET.get("monto_min", "")
    if monto_min:
        qs = qs.filter(total__gte=float(monto_min))
        filters["monto_min"] = monto_min

    monto_max = request.GET.get("monto_max", "")
    if monto_max:
        qs = qs.filter(total__lte=float(monto_max))
        filters["monto_max"] = monto_max

    search = request.GET.get("q", "").strip()
    if search:
        qs = qs.filter(
            Q(nombre_emisor__icontains=search) |
            Q(nombre_receptor__icontains=search) |
            Q(rfc_emisor__icontains=search) |
            Q(rfc_receptor__icontains=search)
        )
        filters["q"] = search

    total = qs.count()
    page = int(request.GET.get("page", 1))
    page_size = 50
    offset = (page - 1) * page_size
    total_pages = (total + page_size - 1) // page_size

    # Count unassigned
    unassigned_count = CFDI.objects.filter(
        uploaded_by=request.user, empresa__isnull=True
    ).count()

    # Analysis toolbar context
    MONTH_NAMES = [
        "", "Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio",
        "Julio", "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre",
    ]
    selected_empresa_rfc = ""
    if empresa_id and empresa_id != "__none__":
        emp = Empresa.objects.filter(id=empresa_id).first()
        selected_empresa_rfc = emp.rfc if emp else ""
    if filters.get("month"):
        filters["month_name"] = MONTH_NAMES[filters["month"]]

    now = datetime.now()
    return render(request, "app/cfdis_list.html", {
        "current_page": "cfdis",
        "cfdis": qs.order_by("-fecha")[offset:offset + page_size],
        "total_cfdis": total,
        "empresas": empresas,
        "filters": filters,
        "selected_empresa_rfc": selected_empresa_rfc,
        "unassigned_count": unassigned_count,
        "years": [2026, 2025],
        "months": [
            (1, "Enero"), (2, "Febrero"), (3, "Marzo"), (4, "Abril"),
            (5, "Mayo"), (6, "Junio"), (7, "Julio"), (8, "Agosto"),
            (9, "Septiembre"), (10, "Octubre"), (11, "Noviembre"), (12, "Diciembre"),
        ],
        "page": page,
        "total_pages": total_pages,
        "has_prev": page > 1,
        "has_next": offset + page_size < total,
    })


@login_required(login_url=APP_LOGIN_URL)
def app_cfdi_pdf(request, cfdi_uuid):
    from core.models import CFDI
    from core.services.storage_minio import download_bytes
    from sat_scrapper_core.cfdi_pdf.render import render_cfdi_pdf

    cfdi = get_object_or_404(CFDI, uuid=cfdi_uuid)
    if cfdi.empresa and cfdi.empresa.owner != request.user:
        if cfdi.uploaded_by != request.user:
            return HttpResponse("Forbidden", status=403)
    elif not cfdi.empresa and cfdi.uploaded_by != request.user:
        return HttpResponse("Forbidden", status=403)

    xml_bytes = download_bytes(cfdi.xml_minio_key)
    pdf_bytes = render_cfdi_pdf(xml_bytes)
    response = HttpResponse(pdf_bytes, content_type="application/pdf")
    response["Content-Disposition"] = f'inline; filename="{cfdi_uuid}.pdf"'
    return response


@login_required(login_url=APP_LOGIN_URL)
def app_cfdi_xml(request, cfdi_uuid):
    from core.models import CFDI
    from core.services.storage_minio import download_bytes

    cfdi = get_object_or_404(CFDI, uuid=cfdi_uuid)
    if cfdi.empresa and cfdi.empresa.owner != request.user:
        if cfdi.uploaded_by != request.user:
            return HttpResponse("Forbidden", status=403)
    elif not cfdi.empresa and cfdi.uploaded_by != request.user:
        return HttpResponse("Forbidden", status=403)

    xml_bytes = download_bytes(cfdi.xml_minio_key)
    response = HttpResponse(xml_bytes, content_type="application/xml")
    response["Content-Disposition"] = f'attachment; filename="{cfdi_uuid}.xml"'
    return response


# ── Upload XMLs ───────────────────────────────────────────────────────

@login_required(login_url=APP_LOGIN_URL)
def app_upload_xmls(request):
    """Manual XML upload with drag & drop. Supports .xml and .zip files."""
    from core.models import CFDI, Empresa
    from core.services.storage_minio import upload_bytes
    from sat_scrapper_core.cfdi_pdf.xml_parse import parse_cfdi_xml, CFDIParseError
    import zipfile
    import io

    if request.method == "POST":
        files = request.FILES.getlist("xmls")
        if not files:
            return JsonResponse({"error": "No se recibieron archivos"}, status=400)

        # Extract XMLs from zip files
        xml_items = []  # list of (name, bytes)
        for f in files:
            if f.name.lower().endswith(".zip"):
                try:
                    zf = zipfile.ZipFile(io.BytesIO(f.read()))
                    for name in zf.namelist():
                        if name.lower().endswith(".xml") and not name.startswith("__MACOSX"):
                            xml_items.append((name, zf.read(name)))
                except zipfile.BadZipFile:
                    xml_items.append((f.name, None))  # Will be caught as error
            elif f.name.lower().endswith(".xml"):
                xml_items.append((f.name, f.read()))
            else:
                xml_items.append((f.name, None))  # Unsupported format

        empresas = {e.rfc: e for e in Empresa.objects.filter(owner=request.user)}
        results = {"uploaded": 0, "duplicated": 0, "unassigned": 0, "errors": [], "assigned": {}}

        for fname, xml_bytes in xml_items:
            if xml_bytes is None:
                results["errors"].append(f"{fname}: Formato no soportado")
                continue

            try:
                parsed = parse_cfdi_xml(xml_bytes)

                cfdi_uuid = parsed.timbre.get("UUID", "").strip()
                if not cfdi_uuid:
                    results["errors"].append(f"{fname}: Sin UUID")
                    continue

                from uuid import UUID
                try:
                    cfdi_uuid_obj = UUID(cfdi_uuid)
                except ValueError:
                    results["errors"].append(f"{fname}: UUID inválido")
                    continue

                if CFDI.objects.filter(uuid=cfdi_uuid_obj).exists():
                    results["duplicated"] += 1
                    continue

                rfc_emisor = parsed.emisor.get("Rfc", "")
                rfc_receptor = parsed.receptor.get("Rfc", "")
                empresa = None
                tipo_relacion = "recibido"

                if rfc_emisor in empresas:
                    empresa = empresas[rfc_emisor]
                    tipo_relacion = "emitido"
                elif rfc_receptor in empresas:
                    empresa = empresas[rfc_receptor]
                    tipo_relacion = "recibido"

                if empresa:
                    fecha = parsed.comprobante.get("Fecha", datetime.now())
                    minio_key = f"cfdis/{empresa.rfc}/{fecha.year}/{fecha.month:02d}/{tipo_relacion}/{cfdi_uuid}.xml"
                    results["assigned"].setdefault(empresa.rfc, 0)
                    results["assigned"][empresa.rfc] += 1
                else:
                    minio_key = f"cfdis/__uploads__/{request.user.id}/{cfdi_uuid}.xml"

                upload_bytes(xml_bytes, minio_key, content_type="application/xml")

                comp = parsed.comprobante
                CFDI.objects.create(
                    uuid=cfdi_uuid_obj,
                    empresa=empresa,
                    uploaded_by=request.user,
                    tipo_relacion=tipo_relacion,
                    version=comp.get("Version", "4.0"),
                    fecha=comp.get("Fecha", datetime.now(timezone.utc)),
                    serie=comp.get("Serie", ""),
                    folio=comp.get("Folio", ""),
                    total=comp.get("Total", 0),
                    subtotal=comp.get("SubTotal", 0),
                    moneda=comp.get("Moneda", "MXN"),
                    tipo_cambio=comp.get("TipoCambio", 1),
                    tipo_comprobante=comp.get("TipoDeComprobante", "I"),
                    forma_pago=comp.get("FormaPago", ""),
                    metodo_pago=comp.get("MetodoPago", ""),
                    rfc_emisor=rfc_emisor,
                    nombre_emisor=parsed.emisor.get("Nombre", ""),
                    regimen_fiscal_emisor=parsed.emisor.get("RegimenFiscal", ""),
                    rfc_receptor=rfc_receptor,
                    nombre_receptor=parsed.receptor.get("Nombre", ""),
                    uso_cfdi=parsed.receptor.get("UsoCFDI", ""),
                    total_impuestos_trasladados=parsed.impuestos.get("TotalImpuestosTrasladados", 0),
                    total_impuestos_retenidos=parsed.impuestos.get("TotalImpuestosRetenidos", 0),
                    xml_minio_key=minio_key,
                    xml_size_bytes=len(xml_bytes),
                    fuente="upload",
                )

                results["uploaded"] += 1
                if not empresa:
                    results["unassigned"] += 1

            except CFDIParseError as e:
                results["errors"].append(f"{fname}: {str(e)[:100]}")
            except Exception as e:
                results["errors"].append(f"{fname}: {str(e)[:100]}")

        from core.services.monitor import log_info
        log_info("upload", f"Upload XML: {results['uploaded']} subidos, {results['duplicated']} duplicados",
                 user_email=request.user.email)

        return JsonResponse(results)

    return render(request, "app/upload.html", {
        "current_page": "upload",
    })


# ── API Keys ──────────────────────────────────────────────────────────

@login_required(login_url=APP_LOGIN_URL)
def app_api_keys(request):
    import secrets
    from core.models import APIKey, Empresa

    if request.method == "POST":
        action = request.POST.get("action", "create")
        if action == "create":
            nombre = request.POST.get("nombre", "").strip()
            if nombre:
                key = APIKey.objects.create(
                    nombre=nombre,
                    key=secrets.token_hex(32),
                    owner=request.user,
                    puede_leer="puede_leer" in request.POST,
                    puede_trigger_descarga="puede_trigger_descarga" in request.POST,
                )
                empresa_ids = request.POST.getlist("empresas")
                if empresa_ids:
                    key.empresas.set(
                        Empresa.objects.filter(id__in=empresa_ids, owner=request.user)
                    )
                messages.success(request, f"API Key creada: {key.key}")
                return redirect("app:api_keys")
        elif action == "revoke":
            key_id = request.POST.get("key_id")
            key = get_object_or_404(APIKey, id=key_id, owner=request.user)
            key.activa = False
            key.save(update_fields=["activa"])
            messages.warning(request, f"Key '{key.nombre}' revocada")
            return redirect("app:api_keys")

    keys = APIKey.objects.filter(owner=request.user).prefetch_related("empresas").order_by("-created_at")
    empresas = Empresa.objects.filter(owner=request.user).order_by("rfc")

    return render(request, "app/api_keys.html", {
        "current_page": "api_keys",
        "api_keys": keys,
        "empresas": empresas,
    })


# ── Profile ───────────────────────────────────────────────────────────

@login_required(login_url=APP_LOGIN_URL)
def app_perfil(request):
    profile = _ensure_profile(request.user)

    if request.method == "POST":
        action = request.POST.get("action", "profile")
        if action == "profile":
            nombre = request.POST.get("nombre", "").strip()
            if nombre:
                parts = nombre.split(" ", 1)
                request.user.first_name = parts[0]
                request.user.last_name = parts[1] if len(parts) > 1 else ""
                request.user.save(update_fields=["first_name", "last_name"])

            profile.telefono = request.POST.get("telefono", "").strip()
            profile.empresa_nombre = request.POST.get("empresa_nombre", "").strip()
            profile.save(update_fields=["telefono", "empresa_nombre"])
            messages.success(request, "Perfil actualizado")
        elif action == "password":
            current = request.POST.get("current_password", "")
            new_pass = request.POST.get("new_password", "")
            new_pass2 = request.POST.get("new_password2", "")

            if not request.user.check_password(current):
                messages.error(request, "Contraseña actual incorrecta")
            elif len(new_pass) < 6:
                messages.error(request, "La nueva contraseña debe tener al menos 6 caracteres")
            elif new_pass != new_pass2:
                messages.error(request, "Las contraseñas no coinciden")
            else:
                request.user.set_password(new_pass)
                request.user.save()
                auth_login(request, request.user)
                messages.success(request, "Contraseña cambiada")

        return redirect("app:perfil")

    return render(request, "app/perfil.html", {
        "current_page": "perfil",
        "profile": profile,
    })


# ── Facturación ───────────────────────────────────────────────────────

@login_required(login_url=APP_LOGIN_URL)
def app_facturacion(request):
    profile = _ensure_profile(request.user)

    if request.method == "POST":
        profile.rfc_facturacion = request.POST.get("rfc_facturacion", "").strip().upper()
        profile.razon_social = request.POST.get("razon_social", "").strip()
        profile.regimen_fiscal = request.POST.get("regimen_fiscal", "").strip()
        profile.codigo_postal = request.POST.get("codigo_postal", "").strip()
        profile.uso_cfdi = request.POST.get("uso_cfdi", "G03").strip()
        profile.email_facturacion = request.POST.get("email_facturacion", "").strip()
        profile.save(update_fields=[
            "rfc_facturacion", "razon_social", "regimen_fiscal",
            "codigo_postal", "uso_cfdi", "email_facturacion",
        ])
        messages.success(request, "Datos de facturación actualizados")
        return redirect("app:facturacion")

    return render(request, "app/facturacion.html", {
        "current_page": "facturacion",
        "profile": profile,
    })
