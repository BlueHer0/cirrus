# Auditoría — Sistema de API Keys

**Fecha:** 2026-04-20
**Alcance:** Multi-tenancy, almacenamiento seguro, rate limiting y ciclo de vida atado al plan.

## Hallazgos

| # | Hallazgo | Severidad | Fix |
|---|----------|-----------|-----|
| K1 | Keys guardadas en texto plano en BD (campo `APIKey.key`) | 🔴 CRÍTICO | SHA-256 en `key_hash`, campo plano eliminado (migración 0030) |
| K2 | Sin rate limiting por key | 🔴 CRÍTICO | `requests_hoy` + `limite_requests_dia` según plan; HTTP 429 al exceder |
| K3 | Plan cancelado no desactiva keys automáticamente | 🟠 ALTO | Check en `plan_vigente()` → 402 + task horaria de seguridad |
| K4 | Sin prefix identificable en keys | 🟠 ALTO | Formato `cirrus_<8hex>_<48>` + columna `key_prefix` |
| K5 | Key expuesta en `messages.success` con persistencia en session | 🟡 MEDIO | `session["new_api_key_once"]` consumida con `pop` en la siguiente vista |
| K6 | Sin audit log por endpoint | 🟡 MEDIO | No se implementó en esta sesión (pendiente fase 2) |

## Diseño final

### Modelo `APIKey`

| Campo | Qué hace |
|-------|---------|
| `key_hash` (SHA-256 hex) | Único lookup seguro; indexed + unique |
| `key_prefix` (cirrus_xxxxxxxx) | Identificación visual sin comprometer seguridad |
| `requests_hoy` / `limite_requests_dia` | Rate limit diario |
| `ultimo_reset_requests` (Date) | Reset automático si cambió el día |
| `plan_slug_al_crear` | Referencia histórica del plan que tenía el cliente al crear |
| `revocada_en` (DateTime) | Timestamp del soft delete |

### Rate limits por plan

| Plan | Requests/día |
|------|--------------|
| free | 0 (no puede usar API) |
| basico | 1000 |
| pro | 5000 |
| enterprise | 20000 |
| owner / staff | 50000 |

### Formato de key plana

```
cirrus_ab12cd34_<48 caracteres random>
  ^        ^         ^
  prefijo  id_corto  secreto
```

- El prefijo hace fácil identificar visualmente en logs (si aparece)
- El hash SHA-256 es lo que se persiste, no la key plana
- Total: ~65 caracteres

### Flujo de autenticación

```
Request → Authorization: Bearer cirrus_ab12cd34_...
   ↓
1. autenticar_key(plain) → SHA-256 → busca en key_hash
   ↓ no encontrada         → 401
   ↓ encontrada, inactiva  → 401
   ↓ plan cancelado        → 402
   ↓ rate limit excedido   → 429 + Retry-After
   ↓
2. incrementar requests_hoy (atomic F expression)
3. request.api_key, request.api_empresas, request.api_empresa_rfcs
```

## Tareas Celery asociadas

Archivo: `core/tasks_api_keys.py` (NO toca `tasks.py` principal).

| Task | Frecuencia | Qué hace |
|------|-----------|---------|
| `reset_apikey_requests_diarios` | Cron 00:05 diario | Reset `requests_hoy=0` |
| `desactivar_apikeys_plan_cancelado` | Cada hora | Soft-delete de keys cuyo owner tiene `subscription_status='canceled'` |

Red de seguridad: adicionalmente, el webhook `customer.subscription.deleted` desactiva las keys inmediatamente.

## Migraciones

| # | Archivo | Qué hace |
|---|---------|---------|
| 0029 | `apikey_hash_ratelimit_stripewebhook` | Agrega todos los campos nuevos + backfill de hash/prefix a partir del `key` existente + crea `StripeWebhookEvent` |
| 0030 | `apikey_drop_plain_key` | Elimina la columna `key` (texto plano) |

El backfill permite que las keys legadas (4 existentes) sigan funcionando sin intervención del cliente — el hash quedó computado y el cliente puede seguir usando su key plana actual si la recuerda. Si la pierde, tiene que generar una nueva.

## Archivos modificados

| Archivo | Cambio |
|---------|--------|
| `core/models.py:248-316` | `APIKey` con nuevos campos; `StripeWebhookEvent` nuevo |
| `core/migrations/0029_*.py` | Nueva |
| `core/migrations/0030_*.py` | Nueva |
| `core/services/api_keys_service.py` | **Nuevo** |
| `core/api/auth.py` | Reescrito — usa hash + rate limit + plan check |
| `core/api/router.py` | Exception handler para propagar status 402/429 |
| `core/views.py:api_keys_view` | Usa `crear_api_key()`, pop session |
| `accounts/views.py:app_api_keys` | Usa `crear_api_key()`, pop session |
| `frontend/templates/app/api_keys.html` | Muestra key UNA vez, stats uso hoy |
| `frontend/templates/panel/api_keys.html` | Muestra key UNA vez, stats uso hoy |
| `core/admin.py` | APIKeyAdmin con nuevos campos |
| `core/tasks_api_keys.py` | **Nuevo** — 2 tasks Celery |
| `cirrus/celery.py` | Registrado nuevo módulo de tasks |
| `cirrus/settings.py` | Beat schedule: reset diario + hourly cleanup |

## Verificación

```bash
# Autenticación con key legada
venv/bin/python manage.py shell -c "
from core.services.api_keys_service import autenticar_key
k = autenticar_key('<key_plana>')
print(k, k.key_prefix, k.limite_requests_dia)
"

# Rate limit funciona
curl -H "Authorization: Bearer <key>" https://cirrus.nubex.me/api/v1/cfdis/list/?limit=1
# Tras 1000 requests en un día → HTTP 429 con header Retry-After

# Plan no vigente → 402
# (simular con: profile.subscription_status='canceled')
```
