# Auditoría — Billing y Stripe

**Fecha:** 2026-04-20
**Alcance:** Webhook, idempotencia, ciclo de vida de la suscripción, onboarding.

## Hallazgos

| # | Hallazgo | Severidad | Fix |
|---|----------|-----------|-----|
| B1 | Sin idempotencia ni log persistente de eventos Stripe | 🔴 CRÍTICO | Modelo `StripeWebhookEvent` + `get_or_create(stripe_event_id)` |
| B2 | Excepciones silenciadas → eventos perdidos sin reintentar | 🔴 CRÍTICO | `handle_webhook_event` devuelve dict con status; webhook retorna 200/500 según tipo de error |
| B3 | `stripe` NO estaba en `requirements.txt` | 🟠 ALTO | Agregado `stripe>=14.0,<15.0` |
| B4 | Plan vencido no apaga API keys | 🟠 ALTO | `customer.subscription.deleted` desactiva keys + task horaria de red |
| B5 | Emails con `fail_silently=True` — pagos fallidos sin alerta | 🟠 ALTO | Cambiados a `fail_silently=False` con try/except + `log_error()` |
| B6 | No asigna plan "free" al confirmar email | 🟡 MEDIO | Implementado en `confirmar_email` con fallback silencioso |
| B7 | Sin verificación de email antes de FIEL | 🟡 MEDIO | No aplicable: ya exige `is_active=True` para login (requerido para subir FIEL) |
| B8 | Sin período de gracia explícito | 🟡 MEDIO | `past_due` sigue concediendo acceso (gracia) hasta deletion o cancel |

## Política del webhook — cuándo 200 vs 500

| Situación | Respuesta | Por qué |
|-----------|-----------|---------|
| Firma HMAC inválida | 400 | Stripe no reintenta 4xx |
| Payload malformado | 400 | Datos incoherentes — no reintentar |
| Evento duplicado (ya procesado) | 200 | Idempotencia |
| Tipo no manejado | 200 (estado `ignorado`) | Stripe no reintenta |
| Error de datos (User/Plan/Customer no existe) | **200** (estado `error`) | **Evitar loops infinitos** con datos malos |
| Error inesperado (BD caída, bug, red) | **500** | Stripe reintenta |

Esto evita el peor caso histórico: un evento con `User.DoesNotExist` en un loop infinito hasta que el admin lo note.

## Eventos manejados

| Evento | Acción |
|--------|--------|
| `checkout.session.completed` | Activa `plan_fk`, crea `StripePayment`, actualiza rate limit API keys, email recibo |
| `invoice.paid` | Registra pago de renovación; si estaba `past_due` vuelve a `active` |
| `invoice.payment_failed` | Marca `past_due` + email al cliente + Telegram warning |
| `customer.subscription.updated` | Actualiza `cancel_at_period_end` y `current_period_end` |
| `customer.subscription.deleted` | Degrada a plan `free`, marca `canceled`, **desactiva todas las API keys del user** |
| (otros) | `ignorado`, 200 |

## Modelo `StripeWebhookEvent`

```python
class StripeWebhookEvent:
    stripe_event_id   # UNIQUE — idempotencia
    event_type        # 'invoice.paid', etc.
    customer_id       # si aplica
    estado            # recibido | procesado | error | ignorado
    payload           # JSONField con event['data']['object']
    error_detalle     # str en caso de error
    intentos          # contador
    recibido_en       # auto_now_add
    procesado_en      # set al pasar a 'procesado'
```

## Panel de auditoría

URL: `/panel/stripe-events/` (staff-only).

- Lista últimos 200 eventos con estado y timestamps
- Filtros por estado y tipo
- Estadísticas: total, procesados 24h, errores, ignorados
- Acción **🔄 Re-procesar** para eventos en error (tras corregir causa raíz)

## Mejoras en emails

Antes:
```python
msg.send(fail_silently=True)  # si falla, nadie se entera
```

Ahora:
```python
try:
    msg.send(fail_silently=False)
except Exception as e:
    log_error("email", "No se pudo enviar...", detail=str(e))
```

`log_error` dispara Telegram al admin si `TELEGRAM_ALERTS_ENABLED=True`.

## Archivos modificados

| Archivo | Cambio |
|---------|--------|
| `core/models.py` | + `StripeWebhookEvent` |
| `core/migrations/0029_*.py` | Crea tabla `stripewebhookevent` |
| `cirrus/urls.py:stripe_webhook` | Reescrito: idempotencia + log + 200/500 correcto |
| `core/services/stripe_service.py:handle_webhook_event` | Reescrito: split en handlers por evento, devuelve dict, desactiva API keys al cancelar, actualiza rate limits al cambio de plan |
| `core/views.py:stripe_events_view` | **Nueva** view `/panel/stripe-events/` |
| `core/urls.py` | + ruta `stripe-events/` |
| `frontend/templates/panel/stripe_events.html` | **Nuevo** template |
| `frontend/templates/panel/base_admin.html` | + item 💳 Stripe Events en sidebar |
| `accounts/views.py:_send_confirmation_email` | `fail_silently=False` |
| `accounts/views.py:confirmar_email` | Asigna plan "free" al activar cuenta |
| `core/admin.py` | + `StripeWebhookEventAdmin` |
| `requirements.txt` | + `stripe>=14.0,<15.0`, `pgvector>=0.4`, `tiktoken>=0.10` |

## Pendientes no implementados en esta sesión

- **Período de gracia formal** (B8): actualmente `past_due` concede acceso sin límite temporal. Sería mejor: 7 días de gracia → después degradar a `free`. Requiere task Celery que revise fecha.
- **Audit log por endpoint de API key** (K6): útil post-incidente para saber qué consultó un atacante. Requiere modelo nuevo `APIKeyAudit`.
- **Reconciliación masiva** con `stripe.Event.list()` para detectar eventos que Stripe reporta haber enviado pero nunca llegaron.
