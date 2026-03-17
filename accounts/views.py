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
    """Automatic sync dashboard — no manual download form."""
    from core.models import Empresa, DescargaLog
    from datetime import datetime

    empresas = Empresa.objects.filter(owner=request.user).order_by("rfc")

    if not empresas.exists():
        return render(request, "app/descargas.html", {
            "current_page": "descargas",
            "empresas_data": [],
            "plan": None,
            "has_empresas": False,
        })

    perfil = getattr(request.user, "perfil", None)
    plan = perfil.get_plan() if perfil else None

    MONTH_NAMES = [
        "", "Ene", "Feb", "Mar", "Abr", "May", "Jun",
        "Jul", "Ago", "Sep", "Oct", "Nov", "Dic",
    ]

    empresas_data = []
    now = datetime.now()

    for emp in empresas:
        # Total expected months (×2 for recibidos+emitidos)
        total_meses = _calc_total_meses(emp, now)
        completados = DescargaLog.objects.filter(
            empresa=emp, estado="completado"
        ).values("year", "month_start").distinct().count()
        progreso = min((completados / total_meses * 100) if total_meses > 0 else 0, 100)

        # Last download
        ultima = DescargaLog.objects.filter(
            empresa=emp, estado="completado"
        ).order_by("-completado_at").first()

        # Next download estimate
        proxima = _calc_proxima_descarga(emp, plan, now)

        # Purchasable historic year
        anno_comprable = _calc_anno_comprable(emp, plan)

        # Coverage label
        if emp.sync_desde_year and emp.sync_desde_month:
            cobertura = f"{MONTH_NAMES[emp.sync_desde_month]} {emp.sync_desde_year} → Presente"
        else:
            cobertura = "Sin configurar"

        empresas_data.append({
            "empresa": emp,
            "progreso": round(progreso, 0),
            "total_meses": total_meses,
            "completados": completados,
            "total_cfdis": emp.cfdis.count(),
            "ultima": ultima,
            "proxima": proxima,
            "anno_comprable": anno_comprable,
            "cobertura": cobertura,
        })

    # Recent download history (last 20)
    recent = DescargaLog.objects.filter(
        empresa__owner=request.user
    ).select_related("empresa").order_by("-iniciado_at")[:20]
    has_running = DescargaLog.objects.filter(
        empresa__owner=request.user, estado__in=["ejecutando", "pendiente"]
    ).exists()

    return render(request, "app/descargas.html", {
        "current_page": "descargas",
        "empresas_data": empresas_data,
        "plan": plan,
        "has_empresas": True,
        "recent_downloads": recent,
        "has_running": has_running,
    })


def _calc_total_meses(empresa, now):
    if not empresa.sync_desde_year:
        return 0
    y, m = empresa.sync_desde_year, empresa.sync_desde_month or 1
    total = 0
    while (y < now.year) or (y == now.year and m <= now.month):
        total += 1
        m += 1
        if m > 12:
            m = 1
            y += 1
    return total * 2  # recibidos + emitidos


def _calc_proxima_descarga(empresa, plan, now):
    if not empresa.sync_activa:
        return "Sync no activa"
    if not empresa.fiel_verificada:
        return "FIEL pendiente"
    slug = plan.slug if plan else "free"
    if slug == "free":
        return "Al cierre del mes"
    elif slug == "basico":
        if now.day < 15:
            return f"15/{now.month:02d}/{now.year}"
        else:
            nm = now.month + 1 if now.month < 12 else 1
            ny = now.year if now.month < 12 else now.year + 1
            return f"01/{nm:02d}/{ny}"
    elif slug == "pro":
        return "Semanal (auto)"
    elif slug in ("enterprise", "owner"):
        return "Cada ~2.5 días (auto)"
    return "Automática"


def _calc_anno_comprable(empresa, plan):
    if not plan:
        return None
    slug = plan.slug
    min_years = {"free": None, "basico": 2025, "pro": 2024, "enterprise": 2023, "owner": None}
    min_year = min_years.get(slug)
    if not min_year:
        return None
    sync_year = empresa.sync_desde_year or 2026
    if sync_year <= min_year and (min_year - 1) >= 2023:
        return {"year": sync_year - 1, "precio": 500}
    return None


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

    # Calculate FiscScore if filters active
    fiscscore_ctx = None
    if empresa_id and empresa_id != "__none__" and filters.get("year") and filters.get("month"):
        try:
            from accounts.analysis_helpers import calcular_fiscscore
            emp_obj = Empresa.objects.filter(id=empresa_id).first()
            if emp_obj:
                fs = calcular_fiscscore(emp_obj, filters["year"], filters["month"])
                # Map color to rgba backgrounds
                color_map = {
                    "#34d399": ("16,185,129,0.15", "16,185,129,0.25"),
                    "#fbbf24": ("251,191,36,0.15", "251,191,36,0.25"),
                    "#f97316": ("249,115,22,0.15", "249,115,22,0.25"),
                    "#f87171": ("248,113,113,0.15", "248,113,113,0.25"),
                }
                bg, border = color_map.get(fs["color"], ("99,102,241,0.15", "99,102,241,0.25"))
                fiscscore_ctx = {
                    "score": fs["score"], "color": fs["color"],
                    "color_bg": bg, "color_border": border,
                }
        except Exception:
            pass

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
        "fiscscore": fiscscore_ctx,
    })


# ── CFDI Detail ───────────────────────────────────────────────────────

@login_required(login_url=APP_LOGIN_URL)
def app_cfdi_detail(request, uuid):
    """Full CFDI detail page — parses original XML from MinIO."""
    from core.models import CFDI
    from django.shortcuts import get_object_or_404

    cfdi = get_object_or_404(CFDI, uuid=uuid)
    # Access check
    if cfdi.empresa and cfdi.empresa.owner != request.user and not request.user.is_staff:
        if cfdi.uploaded_by != request.user:
            return redirect("app:cfdis")
    elif not cfdi.empresa and cfdi.uploaded_by != request.user and not request.user.is_staff:
        return redirect("app:cfdis")

    # Parse original XML for full details
    xml_data = None
    if cfdi.xml_minio_key:
        try:
            from core.services.storage_minio import download_bytes
            from sat_scrapper_core.cfdi_pdf.xml_parse import parse_cfdi_xml
            xml_bytes = download_bytes(cfdi.xml_minio_key)
            xml_data = parse_cfdi_xml(xml_bytes)
        except Exception as e:
            import logging
            logging.getLogger("accounts.views").warning("XML parse failed for %s: %s", uuid, e)

    # Type badge color
    tipo_colors = {
        "I": "#34d399", "E": "#f87171", "T": "#60a5fa",
        "N": "#fbbf24", "P": "#a78bfa",
    }
    tipo_color = tipo_colors.get(cfdi.tipo_comprobante, "#718096")
    tipo_names = {"I": "Ingreso", "E": "Egreso", "T": "Traslado", "N": "Nómina", "P": "Pago"}
    tipo_name = tipo_names.get(cfdi.tipo_comprobante, cfdi.tipo_comprobante)

    return render(request, "app/cfdi_detail.html", {
        "current_page": "cfdis",
        "cfdi": cfdi,
        "xml": xml_data,
        "tipo_color": tipo_color,
        "tipo_name": tipo_name,
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


# ── Analysis Views ────────────────────────────────────────────────────

import calendar
from decimal import Decimal

MONTH_NAMES_ES = [
    "", "Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio",
    "Julio", "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre",
]
TIPO_LABELS = {"I": "Ingreso", "E": "Egreso", "P": "Pago", "N": "Nómina", "T": "Traslado"}
FORMA_PAGO_LABELS = {
    "01": "Efectivo", "02": "Cheque", "03": "Transferencia",
    "04": "Tarjeta créd.", "06": "Dinero elec.", "08": "Vales",
    "28": "Tarjeta déb.", "99": "Por definir",
}
BAR_CLASSES = ["bg-indigo", "bg-green", "bg-blue", "bg-amber", "bg-purple", "bg-pink", "bg-cyan", "bg-red"]


def _fmt(val):
    """Format number with commas."""
    if val is None:
        val = 0
    v = float(val)
    if abs(v) >= 1_000_000:
        return f"{v/1_000_000:,.1f}M"
    return f"{v:,.0f}"


def _analysis_base_qs(request, empresa_id, year, month):
    """Validate access and return (empresa, base_qs) or redirect."""
    from core.models import Empresa, CFDI
    try:
        empresa = Empresa.objects.get(id=empresa_id)
    except (Empresa.DoesNotExist, ValueError):
        return None, None
    if empresa.owner_id != request.user.id and not request.user.is_staff:
        return None, None
    qs = CFDI.objects.filter(rfc_empresa=empresa.rfc, fecha__year=year, fecha__month=month)
    return empresa, qs


@login_required(login_url=APP_LOGIN_URL)
def analysis_summary_view(request):
    from core.models import CFDI
    from django.db.models import Sum, Count, Max
    from django.db.models.functions import ExtractDay

    empresa_id = request.GET.get("empresa", "")
    year = int(request.GET.get("year", 2026))
    month = int(request.GET.get("month", 3))

    empresa, qs = _analysis_base_qs(request, empresa_id, year, month)
    if not empresa:
        return redirect("app:cfdis")

    total = qs.count()
    emitidos = qs.filter(tipo_relacion="emitido").count()
    recibidos = qs.filter(tipo_relacion="recibido").count()

    emitidos_i = qs.filter(tipo_relacion="emitido", tipo_comprobante="I")
    recibidos_i = qs.filter(tipo_relacion="recibido", tipo_comprobante="I")

    facturado = emitidos_i.aggregate(s=Sum("total"))["s"] or 0
    gastos = recibidos_i.aggregate(s=Sum("total"))["s"] or 0
    resultado = float(facturado) - float(gastos)
    ticket = float(facturado) / max(emitidos_i.count(), 1)
    factura_max = emitidos_i.aggregate(m=Max("total"))["m"] or 0

    # Delta vs previous month
    pm = month - 1 if month > 1 else 12
    py = year if month > 1 else year - 1
    _, prev_qs = _analysis_base_qs(request, empresa_id, py, pm)
    prev_total = prev_qs.count() if prev_qs is not None else 0
    prev_emit = prev_qs.filter(tipo_relacion="emitido").count() if prev_qs is not None else 0
    prev_recv = prev_qs.filter(tipo_relacion="recibido").count() if prev_qs is not None else 0

    # Por tipo comprobante
    por_tipo_raw = qs.values("tipo_comprobante").annotate(count=Count("uuid")).order_by("-count")
    max_tipo = max((r["count"] for r in por_tipo_raw), default=1)
    por_tipo = [
        {
            "label": TIPO_LABELS.get(r["tipo_comprobante"], r["tipo_comprobante"]),
            "count": r["count"],
            "pct": round(r["count"] / max_tipo * 100),
            "bar_class": BAR_CLASSES[i % len(BAR_CLASSES)],
        }
        for i, r in enumerate(por_tipo_raw)
    ]

    # Por forma de pago
    por_fp_raw = qs.exclude(forma_pago="").values("forma_pago").annotate(count=Count("uuid")).order_by("-count")[:6]
    fp_total = sum(r["count"] for r in por_fp_raw) or 1
    por_forma_pago = [
        {
            "label": FORMA_PAGO_LABELS.get(r["forma_pago"], r["forma_pago"]),
            "pct": round(r["count"] / fp_total * 100),
            "bar_class": BAR_CLASSES[(i + 2) % len(BAR_CLASSES)],
        }
        for i, r in enumerate(por_fp_raw)
    ]

    # Actividad diaria
    daily_raw = qs.annotate(dia=ExtractDay("fecha")).values("dia").annotate(count=Count("uuid"), monto=Sum("total")).order_by("dia")
    days = calendar.monthrange(year, month)[1]
    dmap = {r["dia"]: r for r in daily_raw}
    max_daily = max((dmap[d]["count"] for d in dmap), default=1)
    actividad_diaria = [
        {
            "dia": d,
            "count": dmap[d]["count"] if d in dmap else 0,
            "pct": round((dmap[d]["count"] / max_daily) * 100) if d in dmap else 2,
            "monto_fmt": _fmt(dmap[d]["monto"]) if d in dmap else "0",
        }
        for d in range(1, days + 1)
    ]

    # Top 5 clients + providers
    top_clientes = [
        {"rfc": r["rfc_receptor"], "monto_fmt": _fmt(r["monto"])}
        for r in emitidos_i.values("rfc_receptor").annotate(monto=Sum("total")).order_by("-monto")[:5]
    ]
    top_proveedores = [
        {"rfc": r["rfc_emisor"], "monto_fmt": _fmt(r["monto"])}
        for r in qs.filter(tipo_relacion="recibido", tipo_comprobante__in=["I", "E"]).values("rfc_emisor").annotate(monto=Sum("total")).order_by("-monto")[:5]
    ]

    # Alertas
    efectivo = qs.filter(forma_pago="01", total__gt=2000).count()
    ppd = qs.filter(metodo_pago="PPD").count()

    data = {
        "total_cfdi": total, "emitidos": emitidos, "recibidos": recibidos,
        "delta_total": total - prev_total, "delta_emitidos": emitidos - prev_emit, "delta_recibidos": recibidos - prev_recv,
        "resultado": resultado, "resultado_fmt": _fmt(resultado),
        "facturado_fmt": _fmt(facturado), "gastos_fmt": _fmt(gastos),
        "ticket_fmt": _fmt(ticket), "factura_max_fmt": _fmt(factura_max),
        "por_tipo": por_tipo, "por_forma_pago": por_forma_pago,
        "actividad_diaria": actividad_diaria, "days_in_month": days,
        "top_clientes": top_clientes, "top_proveedores": top_proveedores,
        "alertas": {"cancelados": 0, "efectivo": efectivo, "ppd": ppd, "listas_negras": 0},
    }
    return render(request, "app/analysis_summary.html", {
        "titulo": "📊 Resumen Rápido",
        "empresa": empresa, "year": year, "month": month,
        "periodo": f"{MONTH_NAMES_ES[month]} {year}",
        "data": data, "now": datetime.now(),
    })


@login_required(login_url=APP_LOGIN_URL)
def analysis_fiscal_view(request):
    from core.models import CFDI
    from django.db.models import Sum

    empresa_id = request.GET.get("empresa", "")
    year = int(request.GET.get("year", 2026))
    month = int(request.GET.get("month", 3))

    empresa, qs = _analysis_base_qs(request, empresa_id, year, month)
    if not empresa:
        return redirect("app:cfdis")

    ingresos = float(qs.filter(tipo_relacion="emitido", tipo_comprobante="I").aggregate(s=Sum("total"))["s"] or 0)
    gastos_qs = qs.filter(tipo_relacion="recibido", tipo_comprobante__in=["I", "E"])
    gastos_total = float(gastos_qs.aggregate(s=Sum("total"))["s"] or 0)

    no_ded_efectivo = float(gastos_qs.filter(forma_pago="01", total__gt=2000).aggregate(s=Sum("total"))["s"] or 0)
    no_ded = no_ded_efectivo
    gastos_ded = gastos_total - no_ded

    utilidad = ingresos - gastos_ded
    isr = utilidad * 0.30 if utilidad > 0 else 0

    ret_agg = qs.filter(tipo_relacion="recibido").aggregate(isr=Sum("isr_retenido"), iva=Sum("iva_retenido"))
    ret_isr = float(ret_agg["isr"] or 0)
    ret_iva = float(ret_agg["iva"] or 0)

    ded_pct = round(gastos_ded / gastos_total * 100) if gastos_total > 0 else 100
    no_ded_pct = 100 - ded_pct

    # Motivos
    motivos = []
    if no_ded_efectivo > 0:
        cnt = gastos_qs.filter(forma_pago="01", total__gt=2000).count()
        motivos.append({
            "motivo": "Efectivo > $2,000", "count": cnt,
            "monto_fmt": _fmt(no_ded_efectivo),
            "pct": round(no_ded_efectivo / max(no_ded, 1) * 100),
        })

    # Delta
    pm = month - 1 if month > 1 else 12
    py = year if month > 1 else year - 1
    _, prev_qs = _analysis_base_qs(request, empresa_id, py, pm)
    prev_ing = float((prev_qs.filter(tipo_relacion="emitido", tipo_comprobante="I").aggregate(s=Sum("total"))["s"] or 0)) if prev_qs is not None else 0
    prev_gas = float((prev_qs.filter(tipo_relacion="recibido", tipo_comprobante__in=["I", "E"]).aggregate(s=Sum("total"))["s"] or 0)) if prev_qs is not None else 0
    delta_ing = round((ingresos - prev_ing) / prev_ing * 100) if prev_ing else 0
    delta_gas = round((gastos_total - prev_gas) / prev_gas * 100) if prev_gas else 0

    data = {
        "ingresos_fmt": _fmt(ingresos), "gastos_ded_fmt": _fmt(gastos_ded),
        "utilidad": utilidad, "utilidad_fmt": _fmt(utilidad),
        "isr_fmt": _fmt(isr), "ret_isr_fmt": _fmt(ret_isr), "ret_iva_fmt": _fmt(ret_iva),
        "no_ded_fmt": _fmt(no_ded), "ded_pct": ded_pct, "no_ded_pct": no_ded_pct,
        "motivos": motivos, "delta_ingresos": delta_ing, "delta_gastos": delta_gas,
    }
    return render(request, "app/analysis_fiscal.html", {
        "titulo": "💰 Análisis Fiscal",
        "empresa": empresa, "year": year, "month": month,
        "periodo": f"{MONTH_NAMES_ES[month]} {year}",
        "data": data, "now": datetime.now(),
    })


@login_required(login_url=APP_LOGIN_URL)
def analysis_iva_view(request):
    from core.models import CFDI
    from django.db.models import Sum, F

    empresa_id = request.GET.get("empresa", "")
    year = int(request.GET.get("year", 2026))
    month = int(request.GET.get("month", 3))

    empresa, qs = _analysis_base_qs(request, empresa_id, year, month)
    if not empresa:
        return redirect("app:cfdis")

    iva_trasladado = float(qs.filter(tipo_relacion="emitido", tipo_comprobante="I").aggregate(s=Sum("iva"))["s"] or 0)
    recibidos_ie = qs.filter(tipo_relacion="recibido", tipo_comprobante__in=["I", "E"])
    iva_acreditable_total = float(recibidos_ie.aggregate(s=Sum("iva"))["s"] or 0)
    iva_efectivo = float(recibidos_ie.filter(forma_pago="01", total__gt=2000).aggregate(s=Sum("iva"))["s"] or 0)
    iva_acreditable = iva_acreditable_total - iva_efectivo
    iva_por_pagar = iva_trasladado - iva_acreditable
    iva_retenido = float(qs.filter(tipo_relacion="recibido").aggregate(s=Sum("iva_retenido"))["s"] or 0)
    iva_neto = iva_por_pagar - iva_retenido

    # Por tasa
    emitidos_i = qs.filter(tipo_relacion="emitido", tipo_comprobante="I", subtotal__gt=0)
    iva_16 = float(emitidos_i.filter(iva__gt=F("subtotal") * Decimal("0.10")).aggregate(s=Sum("iva"))["s"] or 0)
    iva_8 = float(emitidos_i.filter(iva__gt=0, iva__lte=F("subtotal") * Decimal("0.10")).aggregate(s=Sum("iva"))["s"] or 0)
    iva_0 = float(emitidos_i.filter(iva=0).aggregate(s=Sum("total"))["s"] or 0)
    max_tasa = max(iva_16, iva_8, iva_0, 1)
    por_tasa = [
        {"tasa": "16%", "monto_fmt": _fmt(iva_16), "pct": round(iva_16 / max_tasa * 100), "bar_class": "bg-indigo"},
        {"tasa": "8%", "monto_fmt": _fmt(iva_8), "pct": round(iva_8 / max_tasa * 100), "bar_class": "bg-green"},
        {"tasa": "0%", "monto_fmt": _fmt(iva_0), "pct": round(iva_0 / max_tasa * 100), "bar_class": "bg-blue"},
    ]

    # Tendencia 6 meses
    tendencia = []
    max_tend = 1
    for i in range(5, -1, -1):
        m = month - i
        y = year
        while m <= 0:
            m += 12
            y -= 1
        _, tqs = _analysis_base_qs(request, empresa_id, y, m)
        if tqs is not None:
            tt = float(tqs.filter(tipo_relacion="emitido", tipo_comprobante="I").aggregate(s=Sum("iva"))["s"] or 0)
            ta = float(tqs.filter(tipo_relacion="recibido", tipo_comprobante__in=["I", "E"]).aggregate(s=Sum("iva"))["s"] or 0)
        else:
            tt, ta = 0, 0
        v = tt - ta
        max_tend = max(max_tend, abs(v))
        tendencia.append({"mes": f"{MONTH_NAMES_ES[m][:3]} {y}", "valor": v, "valor_fmt": _fmt(v)})

    for t in tendencia:
        t["pct"] = round(abs(t["valor"]) / max_tend * 100) if max_tend > 0 else 0

    data = {
        "iva_trasladado_fmt": _fmt(iva_trasladado), "iva_acreditable_fmt": _fmt(iva_acreditable),
        "iva_por_pagar": iva_por_pagar, "iva_por_pagar_fmt": _fmt(iva_por_pagar),
        "iva_retenido_fmt": _fmt(iva_retenido), "iva_efectivo_fmt": _fmt(iva_efectivo),
        "iva_neto_fmt": _fmt(iva_neto),
        "por_tasa": por_tasa, "tendencia": tendencia,
    }
    return render(request, "app/analysis_iva.html", {
        "titulo": "🧾 IVA del Periodo",
        "empresa": empresa, "year": year, "month": month,
        "periodo": f"{MONTH_NAMES_ES[month]} {year}",
        "data": data, "now": datetime.now(),
    })


# ── Analysis Phase 2 Views ───────────────────────────────────────────

@login_required(login_url=APP_LOGIN_URL)
def analysis_income_view(request):
    from django.db.models import Sum

    empresa_id = request.GET.get("empresa", "")
    year = int(request.GET.get("year", 2026))
    month = int(request.GET.get("month", 3))

    empresa, qs = _analysis_base_qs(request, empresa_id, year, month)
    if not empresa:
        return redirect("app:cfdis")

    ingresos = float(qs.filter(tipo_relacion="emitido", tipo_comprobante="I").aggregate(s=Sum("total"))["s"] or 0)
    gastos = float(qs.filter(tipo_relacion="recibido", tipo_comprobante__in=["I", "E"]).aggregate(s=Sum("total"))["s"] or 0)
    utilidad = ingresos - gastos
    margen_bruto = round(utilidad / ingresos * 100) if ingresos > 0 else 0
    isr = utilidad * 0.30 if utilidad > 0 else 0
    margen_neto = round((utilidad - isr) / ingresos * 100) if ingresos > 0 else 0

    gastos_pct = round(gastos / ingresos * 100) if ingresos > 0 else 0
    utilidad_pct_abs = max(round(abs(utilidad) / max(ingresos, 1) * 100), 3)

    # 6-month trend
    tendencia = []
    max_val = 1
    for i in range(5, -1, -1):
        m = month - i
        y = year
        while m <= 0:
            m += 12
            y -= 1
        _, tqs = _analysis_base_qs(request, empresa_id, y, m)
        if tqs is not None:
            ing = float(tqs.filter(tipo_relacion="emitido", tipo_comprobante="I").aggregate(s=Sum("total"))["s"] or 0)
            gas = float(tqs.filter(tipo_relacion="recibido", tipo_comprobante__in=["I", "E"]).aggregate(s=Sum("total"))["s"] or 0)
        else:
            ing, gas = 0, 0
        max_val = max(max_val, ing, gas)
        tendencia.append({
            "mes_nombre": MONTH_NAMES_ES[m][:3], "year": y, "month": m,
            "ingresos": ing, "gastos": gas, "ing_fmt": _fmt(ing), "gas_fmt": _fmt(gas),
        })

    for t in tendencia:
        t["ing_pct"] = round(t["ingresos"] / max_val * 100) if max_val > 0 else 0
        t["gas_pct"] = round(t["gastos"] / max_val * 100) if max_val > 0 else 0

    data = {
        "ingresos_fmt": _fmt(ingresos), "gastos_fmt": _fmt(gastos),
        "utilidad_fmt": _fmt(abs(utilidad)), "es_perdida": utilidad < 0,
        "gastos_pct": gastos_pct, "utilidad_pct_abs": utilidad_pct_abs,
        "margen_bruto": margen_bruto, "isr_fmt": _fmt(isr), "margen_neto": margen_neto,
        "tendencia": tendencia,
    }
    return render(request, "app/analysis_income.html", {
        "titulo": "📈 Estado de Resultados",
        "empresa": empresa, "year": year, "month": month,
        "periodo": f"{MONTH_NAMES_ES[month]} {year}",
        "data": data, "now": datetime.now(),
    })


@login_required(login_url=APP_LOGIN_URL)
def analysis_top_rfc_view(request):
    from django.db.models import Sum, Count

    empresa_id = request.GET.get("empresa", "")
    year = int(request.GET.get("year", 2026))
    month = int(request.GET.get("month", 3))

    empresa, qs = _analysis_base_qs(request, empresa_id, year, month)
    if not empresa:
        return redirect("app:cfdis")

    # Top clients (empresa is emisor → group by receptor)
    clientes_raw = list(
        qs.filter(tipo_relacion="emitido", tipo_comprobante="I")
        .values("rfc_receptor").annotate(monto=Sum("total"), count=Count("uuid"))
        .order_by("-monto")[:10]
    )
    # Top providers (empresa is receptor → group by emisor)
    proveedores_raw = list(
        qs.filter(tipo_relacion="recibido", tipo_comprobante__in=["I", "E"])
        .values("rfc_emisor").annotate(monto=Sum("total"), count=Count("uuid"))
        .order_by("-monto")[:10]
    )

    total_ing = sum(float(c["monto"]) for c in clientes_raw) or 1
    total_gas = sum(float(p["monto"]) for p in proveedores_raw) or 1
    max_c = float(clientes_raw[0]["monto"]) if clientes_raw else 1
    max_p = float(proveedores_raw[0]["monto"]) if proveedores_raw else 1

    clientes = [
        {
            "rfc": c["rfc_receptor"], "count": c["count"],
            "monto_fmt": _fmt(c["monto"]),
            "pct": round(float(c["monto"]) / total_ing * 100),
            "bar_pct": round(float(c["monto"]) / max_c * 100),
        }
        for c in clientes_raw
    ]
    proveedores = [
        {
            "rfc": p["rfc_emisor"], "count": p["count"],
            "monto_fmt": _fmt(p["monto"]),
            "pct": round(float(p["monto"]) / total_gas * 100),
            "bar_pct": round(float(p["monto"]) / max_p * 100),
        }
        for p in proveedores_raw
    ]

    conc_c = clientes[0]["pct"] if clientes else 0
    conc_p = proveedores[0]["pct"] if proveedores else 0

    data = {
        "clientes": clientes, "proveedores": proveedores,
        "concentracion_cliente": conc_c,
        "concentracion_proveedor": conc_p,
        "riesgo_cliente": "alto" if conc_c > 50 else "medio" if conc_c > 30 else "bajo",
        "riesgo_proveedor": "alto" if conc_p > 50 else "medio" if conc_p > 30 else "bajo",
    }
    return render(request, "app/analysis_top_rfc.html", {
        "titulo": "🏢 Top RFC — Clientes y Proveedores",
        "empresa": empresa, "year": year, "month": month,
        "periodo": f"{MONTH_NAMES_ES[month]} {year}",
        "data": data, "now": datetime.now(),
    })


@login_required(login_url=APP_LOGIN_URL)
def analysis_risks_view(request):
    from accounts.analysis_helpers import calcular_fiscscore

    empresa_id = request.GET.get("empresa", "")
    year = int(request.GET.get("year", 2026))
    month = int(request.GET.get("month", 3))

    empresa, qs = _analysis_base_qs(request, empresa_id, year, month)
    if not empresa:
        return redirect("app:cfdis")

    fs = calcular_fiscscore(empresa, year, month)

    data = {
        "score": fs["score"], "score_label": fs["label"], "score_color": fs["color"],
        "score_dash": fs["score_dash"],
        "cumplimiento": fs["cumplimiento"],
        "deducibilidad": fs["deducibilidad"],
        "diversificacion": fs["diversificacion"],
        "consistencia_iva": fs["consistencia_iva"],
        "alertas": {
            **fs["alertas"],
            "efectivo_monto_fmt": _fmt(fs["alertas"]["efectivo_monto"]) if fs["alertas"]["efectivo_monto"] else "",
        },
    }
    return render(request, "app/analysis_risks.html", {
        "titulo": "⚠️ Riesgos Fiscales — FiscScore",
        "empresa": empresa, "year": year, "month": month,
        "periodo": f"{MONTH_NAMES_ES[month]} {year}",
        "data": data, "now": datetime.now(),
    })


# ── Comprar Histórico ────────────────────────────────────────────────

@login_required(login_url=APP_LOGIN_URL)
def app_comprar_historico(request):
    """Historic year purchase request."""
    from core.models import Empresa, SolicitudHistorico

    empresa_id = request.GET.get("empresa", "") or request.POST.get("empresa", "")
    year = int(request.GET.get("year", 0) or request.POST.get("year", 0))

    try:
        empresa = Empresa.objects.get(id=empresa_id, owner=request.user)
    except (Empresa.DoesNotExist, ValueError):
        return redirect("app:descargas")

    if year < 2023 or year > 2026:
        return redirect("app:descargas")

    # Check if already requested
    existing = SolicitudHistorico.objects.filter(
        empresa=empresa, usuario=request.user, year=year,
    ).exclude(estado="rechazado").first()

    if request.method == "POST" and not existing:
        sol = SolicitudHistorico.objects.create(
            empresa=empresa,
            usuario=request.user,
            year=year,
            precio=500,
        )
        # Send notification email (best effort)
        try:
            from django.core.mail import send_mail
            send_mail(
                f"[Cirrus] Solicitud histórico: {empresa.rfc} — {year}",
                f"El usuario {request.user.email} solicita el año {year} "
                f"para {empresa.rfc} ({empresa.nombre}). Precio: $500 MXN.",
                "noreply@cirrus.nubex.me",
                ["farizpe@icloud.com"],
                fail_silently=True,
            )
        except Exception:
            pass

        from django.contrib import messages
        messages.success(request,
            f"Solicitud recibida para {empresa.rfc} — año {year}. "
            "Te contactaremos para procesar el pago.")
        return redirect("app:descargas")

    return render(request, "app/comprar_historico.html", {
        "current_page": "descargas",
        "empresa": empresa,
        "year": year,
        "precio": 500,
        "existing": existing,
    })


# ── Stripe Payment Views ────────────────────────────────────────────


@login_required
def mejorar_plan(request):
    """Page to choose and upgrade plan."""
    from core.models import Plan

    planes = Plan.objects.filter(activo=True, precio_mensual__gt=0).order_by("orden")
    plan_actual = request.user.perfil.get_plan()

    return render(request, "app/mejorar_plan.html", {
        "current_page": "mejorar_plan",
        "planes": planes,
        "plan_actual": plan_actual,
        "stripe_key": settings.STRIPE_PUBLISHABLE_KEY,
        "stripe_test_mode": settings.STRIPE_TEST_MODE,
    })


@login_required
def crear_checkout(request):
    """Create Stripe Checkout session and redirect."""
    if request.method != "POST":
        return redirect("app:mejorar_plan")

    plan_slug = request.POST.get("plan")
    if not plan_slug:
        messages.error(request, "No se especificó un plan.")
        return redirect("app:mejorar_plan")

    try:
        from core.services.stripe_service import create_checkout_session

        session = create_checkout_session(
            user=request.user,
            plan_slug=plan_slug,
            success_url=request.build_absolute_uri("/app/pago-exitoso/"),
            cancel_url=request.build_absolute_uri("/app/mejorar-plan/"),
        )
        return redirect(session.url)
    except Exception as e:
        messages.error(request, f"Error al crear sesión de pago: {e}")
        return redirect("app:mejorar_plan")


@login_required
def pago_exitoso(request):
    """Payment success confirmation page."""
    return render(request, "app/pago_exitoso.html", {
        "current_page": "mejorar_plan",
    })


@login_required
def comprar_historico_checkout(request):
    """Stripe Checkout for one-time historical year purchase."""
    if request.method != "POST":
        return redirect("app:descargas")

    empresa_id = request.POST.get("empresa")
    year = request.POST.get("year")

    if not empresa_id or not year:
        messages.error(request, "Faltan datos para la compra.")
        return redirect("app:descargas")

    try:
        from core.services.stripe_service import create_onetime_checkout

        session = create_onetime_checkout(
            user=request.user,
            concept=f"Año histórico {year}",
            amount_cents=50000,
            success_url=request.build_absolute_uri("/app/pago-exitoso/"),
            cancel_url=request.build_absolute_uri("/app/descargas/"),
            metadata={
                "cirrus_user_id": str(request.user.id),
                "empresa_id": empresa_id,
                "year": year,
                "type": "historico",
            },
        )
        return redirect(session.url)
    except Exception as e:
        messages.error(request, f"Error al crear sesión de pago: {e}")
        return redirect("app:descargas")


@login_required
def cancelar_plan(request):
    """Cancel subscription at end of billing period."""
    if request.method == "POST":
        try:
            from core.services.stripe_service import cancel_subscription

            cancel_subscription(request.user)
            messages.info(
                request,
                "Tu suscripción se cancelará al final del periodo actual.",
            )
        except Exception as e:
            messages.error(request, f"Error al cancelar: {e}")
    return redirect("app:perfil")
