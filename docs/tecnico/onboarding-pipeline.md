# Pipeline de Onboarding de Cliente — CIRRUS

- **Fecha auditoría:** 2026-05-24
- **Alcance:** registro → pago → alta empresa + FIEL → primera descarga → vista CFDIs
- **Superficie cliente:** app `accounts/` montada en `/app/` (NO confundir con el
  panel staff `core/urls.py` en `/panel/`, que es admin interno `@staff_required`).

---

## 1. Flujo completo (end-to-end)

```
1. REGISTRO            /app/registro/        accounts.views.app_register
   └─ User inactivo + ClienteProfile + email de confirmación (token firmado 48h)
2. CONFIRMAR EMAIL     /app/confirmar/<tok>/ accounts.views.confirmar_email
   └─ is_active=True + asigna Plan 'free'
3. LOGIN               /app/login/           accounts.views.app_login
   └─ rate-limit 5/15min, redirige staff→/panel, cliente→/app
4. (OPCIONAL) PAGO     /app/mejorar-plan/    → crear_checkout → Stripe Checkout
   └─ webhook checkout.session.completed → activa plan_fk
5. ALTA EMPRESA+FIEL   /app/empresas/nueva/  accounts.views.app_crear_empresa
   └─ valida FIEL local → sube a MinIO → tarea verificar_fiel_y_descargar_csf
       └─ valida FIEL → descarga+parsea CSF → sync_activa=True → generar_jobs_iniciales
6. PRIMERA DESCARGA    (automática, cola DescargaJob) worker procesa cola
   └─ progreso visible en /app/descargas/ y /app/empresas/<id>/
7. VISTA CFDIs         /app/cfdis/           accounts.views.app_cfdis_list
   └─ scoping por RFC verificado + límite por plan (puede_ver_cfdi)
```

---

## 2. Respuestas del mapeo (Fase 1)

### Registro
- **Campos:** email, password (×2, min 6), nombre, empresa (opcional), teléfono (opcional).
  `accounts/views.py:108-114`.
- **¿Valida email antes de activar?** Sí. Usuario se crea `is_active=False`
  (`views.py:143`); se activa solo al abrir el link firmado (`confirmar_email`,
  `views.py:181-182`). Token `django.core.signing`, expira 48h (`views.py:25,174`).
- **Plan al registrarse:** ninguno hasta confirmar; al confirmar se asigna `free`
  (`views.py:186-196`). Fallback silencioso si no existe el Plan en BD.
- **Email de confirmación:** sí, HTML `emails/bienvenida.html` vía
  `DEFAULT_FROM_EMAIL` = `Cirrus <cirrus@nubex.me>` (NO usa `noreply@`; ver Hallazgo BAJO-1).

### Stripe / Pago
- **Flujo completo:** sí. `crear_checkout` (`views.py:2175`) → `create_checkout_session`
  → redirect a Stripe → success `/app/pago-exitoso/`, cancel `/app/mejorar-plan/`.
- **Activación automática del plan:** sí, webhook `checkout.session.completed` →
  `_handle_checkout_completed` setea `profile.plan_fk` + `subscription_status='active'`
  (`stripe_service.py:161-223`).
- **Si el pago falla:** `invoice.payment_failed` → `subscription_status='past_due'`
  (`stripe_service.py:307-319`). `subscription.deleted` → baja a `free`
  (`stripe_service.py:375-391`).
- **Webhook valida firma:** SÍ. `stripe.Webhook.construct_event(payload, sig, STRIPE_WEBHOOK_SECRET)`
  (`cirrus/urls.py:44-52`), `@csrf_exempt @require_POST`, idempotencia vía
  `StripeWebhookEvent` (`cirrus/urls.py:60-76`). Semántica 200/400/500 correcta.

### Alta de empresa y FIEL
- **¿Agregar empresa antes de pagar?** Sí — el plan `free` permite 1 empresa; hay gate
  `PlanEnforcer.puede_crear_empresa()` (`views.py:434-439`). Es por diseño (free funciona
  sin pagar).
- **Verificación FIEL contra SAT:** `verificar_fiel_y_descargar_csf` (`tasks.py:340+`):
  valida FIEL local (cripto) → si OK marca `fiel_verificada` → descarga CSF del SAT →
  parsea con Docling → activa sync → genera jobs. Si CSF falla pero FIEL OK, activa sync
  igual (`tasks.py:387-399`).
- **Mensaje si FIEL incorrecta:** en alta (`app_crear_empresa`) la validación local
  muestra el error inline: `"FIEL inválida: {e}"` (`views.py:459`). En rechazo asíncrono,
  badge "❌ FIEL rechazada" en el detalle + email (ver Hallazgo MEDIO-1).

### Primera descarga
- **Periodos:** desde `sync_desde` (default 2025-01) hasta el mes anterior, 2 jobs/mes
  (recibidos+emitidos), más reciente primero, espaciado 5 min
  (`job_scheduler.generar_jobs_iniciales`).
- **¿El plan define histórico?** El piso global es 2025 para todos. Años anteriores se
  compran (`_calc_anno_comprable`: basico→2024, pro→2023, enterprise→2022). El audit
  nocturno `auditar_y_reparar_jobs` rellena huecos según ventana de plan.
- **Progreso visible:** sí, `/app/descargas/` (tabla por empresa con % y conteos) y
  `/app/empresas/<id>/` (`recent_downloads`, `has_running`).
- **Duración:** registrada en `DescargaJob.duracion_segundos` / `DescargaLog`. Sin SLA en UI.

### Vista de CFDIs
- **¿El cliente ve sus CFDIs?** Sí, `app_cfdis_list` filtra
  `Q(rfc_empresa__in=user_rfcs) | Q(uploaded_by=user)` (`views.py:898-900`) — aislamiento
  multi-tenant correcto vía `get_empresas_visibles`.
- **Límite de plan:** sí, `PlanEnforcer.puede_ver_cfdi()` aplica `max_cfdis_visibles`
  (`views.py:970-977`; free=30).

---

## 3. Hallazgos (Fase 2)

| # | Sev | Archivo:línea | Problema | Estado |
|---|-----|---------------|----------|--------|
| **F0** | 🔴 CRÍTICO | `core/services/job_scheduler.py:47,106,246,248` | `datetime.now(timezone.utc)` con `timezone`=`django.utils.timezone`; `.utc` fue **removido en Django 5.0** → `generar_jobs_iniciales` crasheaba SIEMPRE → **toda alta de empresa quedaba sin jobs → 0 CFDIs**. | ✅ CORREGIDO |
| **F1** | 🔴 CRÍTICO (staff) | `core/views.py:737,751` | Panel staff llamaba `upload_and_encrypt_fiel` (inexistente) con kwargs erróneos (`cer_bytes`/`key_bytes`) → 500 al subir FIEL. La app cliente usa el `upload_fiel` correcto, así que el cliente no se ve afectado. | ✅ CORREGIDO |
| **F2** | 🟠 ALTO | `core/services/stripe_service.py:226-261` | Compra de año histórico extendía `sync_desde_year` pero **no generaba jobs**; el audit nocturno calcula su ventana desde el PLAN (no desde `sync_desde`) y el año comprable cae fuera de esa ventana → **el año pagado nunca se descargaba**. | ✅ CORREGIDO |
| MEDIO-1 | 🟡 MEDIO | `app/empresa_detail.html` + `Empresa` model | Rechazo FIEL asíncrono solo muestra badge; el motivo va por email. No hay campo `fiel_error` en el modelo. (Alta directa sí muestra error inline.) | Pendiente |
| BAJO-1 | 🔵 BAJO | `accounts/views.py:85` | Email de confirmación usa `DEFAULT_FROM_EMAIL` (cirrus@) en vez de la cuenta `noreply@` configurada en `SystemSettings`. | Pendiente |
| BAJO-2 | 🔵 BAJO | `accounts/views.py:441-490` | `app_crear_empresa` no es atómico: si MinIO o el enqueue fallan tras crear la Empresa, queda en `verificando`. Reintento es idempotente (reusa por rfc+owner). | Pendiente |

> Nota: hallazgos previos de un primer barrido sobre `core/views.py` (panel staff)
> resultaron ser **falsos positivos** para el onboarding de cliente: la superficie real
> del cliente (`accounts/`) sí tiene aislamiento por RFC, gate de plan y límite de CFDIs.

---

## 4. Fixes aplicados (Fase 3)

### F0 — `timezone.utc` removido en Django 5 (CRÍTICO)
`job_scheduler.py` importa tanto `from datetime import ... timezone as dt_timezone`
como `from django.utils import timezone`; la segunda **sombrea** a la primera, y
`django.utils.timezone.utc` ya no existe en Django 5.2. Se reemplazaron las 4
ocurrencias de `timezone.utc` por `dt_timezone.utc` (alias stdlib ya importado),
dejando intactos los `timezone.now()` de Django. **Por qué:** sin esto, ningún
cliente nuevo generaba jobs de descarga; es la causa raíz de "no veo mis CFDIs".

### F1 — Import FIEL roto en panel staff (CRÍTICO staff)
`core/views.py:737,751`: `upload_and_encrypt_fiel(...cer_bytes=...)` →
`upload_fiel(...cer_data=...)`. **Por qué:** función/kwargs no existían → 500.

### F2 — Histórico pagado sin descarga (ALTO)
`stripe_service.py` `_handle_checkout_completed`, rama histórico: tras extender
`sync_desde_year` se llama `generar_jobs_iniciales(emp)` (idempotente vía
get_or_create). **Por qué:** el audit nocturno no cubre el año comprado; sin esto el
cliente pagaba y no recibía nada.

---

## 5. Prueba end-to-end (Fase 4)

Simulación completa en BD dentro de una transacción **revertida** (sin residuo, sin
Stripe/SAT/celery reales). Harness: `scripts/_e2e_onboarding_test.py`.

Resultado: **19/19 PASS**. Cubre: registro inactivo, token de confirmación, activación
+ plan free, login (ok/rechazo), gate de plan, `generar_jobs_iniciales` (32 jobs),
scoping de CFDIs + aislamiento entre usuarios, límite de plan (30), y los fixes F1/F2.

Reejecutar:
```bash
venv/bin/python manage.py shell < scripts/_e2e_onboarding_test.py
```

---

## 6. Pendientes recomendados

1. **MEDIO-1:** agregar campo `fiel_error_motivo` a `Empresa` y mostrarlo en
   `empresa_detail.html` cuando `fiel_status='rechazada'`.
2. **BAJO-1:** enrutar emails de cliente por la cuenta `noreply@` de `SystemSettings`.
3. **BAJO-2:** envolver `app_crear_empresa` en `transaction.atomic` + limpieza en error.
4. **Higiene:** los archivos `core/tests/test_scheduler.py` usan `timezone.utc` con
   import stdlib (válido); revisar que la suite corra en Django 5.2.
