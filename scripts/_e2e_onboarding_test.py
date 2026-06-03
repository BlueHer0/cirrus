"""
E2E onboarding simulation — runs entirely inside a transaction that is
ROLLED BACK at the end. Zero residue in the DB. No external calls (no Stripe
API, no SAT, no celery .delay). Verifies each onboarding step's logic.
"""
import uuid as uuidlib
from datetime import datetime, timezone

from django.db import transaction
from django.contrib.auth import authenticate
from django.contrib.auth.models import User
from django.core import signing

from accounts.models import ClienteProfile, StripePayment
from accounts.views import _generate_confirm_token, _get_user_rfcs, CONFIRM_SALT, CONFIRM_MAX_AGE
from core.models import Empresa, Plan, CFDI, DescargaJob
from core.services.plan_enforcer import PlanEnforcer
from core.services.job_scheduler import generar_jobs_iniciales
from core.services.stripe_service import handle_webhook_event

EMAIL = "e2e+onboarding@example.invalid"
PWD = "TestPass123"
RFC = "EKU9003173C9"  # RFC de prueba público del SAT
ok = []
fail = []


def check(cond, label):
    (ok if cond else fail).append(label)
    print(("  PASS " if cond else "  FAIL ") + label)


class _Rollback(Exception):
    pass


try:
    with transaction.atomic():
        print("STEP 1 — Registro (cuenta inactiva + perfil)")
        if User.objects.filter(username=EMAIL).exists():
            User.objects.filter(username=EMAIL).delete()
        user = User.objects.create_user(
            username=EMAIL, email=EMAIL, password=PWD,
            first_name="Test", last_name="E2E", is_active=False,
        )
        profile = ClienteProfile.objects.create(user=user, empresa_nombre="E2E SA")
        check(not user.is_active, "usuario creado inactivo hasta confirmar email")
        check(profile.pk is not None, "ClienteProfile creado")

        print("STEP 2 — Confirmacion de email (token firmado + plan free)")
        token = _generate_confirm_token(user)
        data = signing.loads(token, salt=CONFIRM_SALT, max_age=CONFIRM_MAX_AGE)
        check(data["user_id"] == user.id, "token firmado valida y decodifica al user correcto")
        user.is_active = True
        user.save(update_fields=["is_active"])
        plan_free = Plan.objects.filter(slug="free").first()
        check(plan_free is not None, "existe Plan 'free' en BD")
        if plan_free:
            profile.plan_fk = plan_free
        profile.plan_legacy = "free"
        profile.save()
        user.refresh_from_db()
        check(user.is_active, "cuenta activada tras confirmar")

        print("STEP 3 — Login (authenticate)")
        authed = authenticate(username=EMAIL, password=PWD)
        check(authed is not None and authed.id == user.id, "login con credenciales correctas")
        check(authenticate(username=EMAIL, password="wrong") is None, "login con password incorrecto rechazado")

        print("STEP 4 — Plan gate al crear empresa")
        enf = PlanEnforcer(user)
        emp_check = enf.puede_crear_empresa()
        check(emp_check["permitido"], "plan free permite crear su 1a empresa")

        print("STEP 5 — Alta empresa + verificacion FIEL (simulada) + jobs iniciales")
        emp = Empresa.objects.create(
            rfc=RFC, nombre=f"{RFC} (pendiente CSF)", owner=user,
            fiel_status="verificada", fiel_verificada=True,
            fiel_verificada_at=datetime.now(timezone.utc),
            sync_activa=True, sync_desde_year=2025, sync_desde_month=1,
        )
        jobs = generar_jobs_iniciales(emp)
        check(jobs > 0, f"generar_jobs_iniciales creo jobs ({jobs})")
        check(DescargaJob.objects.filter(empresa=emp, year=2025).exists(), "hay jobs para 2025 (sync_desde)")

        print("STEP 6 — Vista CFDIs: scoping por RFC + limite de plan")
        # Crear 3 CFDIs del RFC del cliente
        for i in range(3):
            CFDI.objects.create(
                uuid=uuidlib.uuid4(), rfc_empresa=RFC, empresa=emp,
                tipo_relacion="recibido", version="4.0",
                fecha=datetime(2025, 3, 1, 12, 0, tzinfo=timezone.utc),
                total=100, subtotal=100, tipo_comprobante="I",
                rfc_emisor="AAA010101AAA", rfc_receptor=RFC,
                xml_minio_key=f"test/{i}.xml",
            )
        user_rfcs = _get_user_rfcs(user)
        check(RFC in user_rfcs, "el RFC verificado aparece en _get_user_rfcs del dueno")
        visibles = CFDI.objects.filter(rfc_empresa__in=user_rfcs).count()
        check(visibles == 3, f"el cliente ve sus 3 CFDIs ({visibles})")
        # Otro usuario NO debe verlos (tenant isolation)
        otro = User.objects.create_user(username="e2e+otro@example.invalid",
                                        email="e2e+otro@example.invalid", password=PWD)
        ClienteProfile.objects.create(user=otro)
        otro_rfcs = _get_user_rfcs(otro)
        check(RFC not in otro_rfcs, "otro usuario NO ve el RFC del cliente (aislamiento)")
        cv = enf.puede_ver_cfdi()
        check("limite" in cv and cv["limite"] >= 1, f"plan enforcer define limite de CFDIs visibles ({cv.get('limite')})")

        print("STEP 7 — FIX F2: compra de ano historico genera jobs")
        # Borrar jobs de 2024 si los hubiera y simular webhook historico
        DescargaJob.objects.filter(empresa=emp, year=2024).delete()
        fake_event = {
            "type": "checkout.session.completed",
            "data": {"object": {
                "metadata": {"cirrus_user_id": str(user.id),
                             "empresa_id": str(emp.id), "year": "2024", "type": "historico"},
                "amount_total": 50000, "currency": "mxn",
                "payment_intent": "pi_test_e2e",
            }},
        }
        res = handle_webhook_event(fake_event)
        emp.refresh_from_db()
        check(res.get("status") == "procesado", "webhook historico procesado")
        check(emp.sync_desde_year == 2024, f"sync_desde_year extendido a 2024 ({emp.sync_desde_year})")
        check(DescargaJob.objects.filter(empresa=emp, year=2024).exists(),
              "FIX F2: jobs para 2024 generados tras la compra")

        print("STEP 8 — FIX F1: import de upload_fiel en panel staff")
        import importlib
        from core.services import fiel_encryption
        check(hasattr(fiel_encryption, "upload_fiel"), "upload_fiel existe en fiel_encryption")
        check(not hasattr(fiel_encryption, "upload_and_encrypt_fiel"),
              "upload_and_encrypt_fiel (nombre roto) NO existe — confirma el fix")

        print("\n=== RESULTADO ===")
        print(f"PASS: {len(ok)}   FAIL: {len(fail)}")
        if fail:
            print("FALLOS:", fail)
        raise _Rollback()
except _Rollback:
    print("\n(transaccion revertida — sin residuo en la BD)")
