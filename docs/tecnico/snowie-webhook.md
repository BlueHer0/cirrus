# Webhook Snowie — Captura de Leads

## Resumen

Endpoint público que recibe leads capturados por agentes de Snowie.ai y los persiste en Cirrus. Cada lead dispara dos acciones asíncronas vía Celery:

1. **Notificación al admin** vía Telegram (con datos resumidos del lead)
2. **Email de bienvenida** al lead (si tiene email) desde `noreply@nubex.me`

El endpoint **siempre responde 200** si el lead se guardó, aunque las tareas async fallen — Snowie nunca recibe error por problemas internos.

---

## Endpoint

```
POST https://cirrus.nubex.me/api/v1/webhooks/snowie/
Content-Type: application/json
[X-Snowie-Signature: <HMAC-SHA256 hex>]   ← opcional pero recomendado
```

**Sin autenticación JWT.** La validación se hace por:
1. `agente_id` en el body debe coincidir con `SystemSettings.snowie_agente_id`
2. (Opcional) HMAC-SHA256 del body con shared secret en header `X-Snowie-Signature`

---

## Schema del payload

```json
{
  "session_id":      "snw_abc123",       // REQUERIDO — único, idempotente
  "agente_id":       "cirrus_main",      // REQUERIDO — debe coincidir con config
  "nombre":          "Juan Pérez",       // opcional
  "email":           "juan@empresa.mx",  // opcional (sin esto, no se manda email)
  "telefono":        "5512345678",       // opcional
  "rfc_empresa":     "ABC123456XYZ",     // opcional, se normaliza a UPPER
  "plan_interesado": "pro",              // opcional (free/basico/pro/enterprise)
  "summary":         "Cliente quiere descargar CFDIs de 3 RFCs..." // opcional
}
```

Cualquier campo extra en el body se guarda en `payload_raw` (JSONField) para auditoría.

---

## Respuestas

| HTTP | Body | Significado |
|------|------|-------------|
| 200 | `{"status": "ok", "id": "<uuid>", "message": "Lead saved"}` | Lead nuevo guardado |
| 200 | `{"status": "duplicate", "id": "<uuid>", "message": "session_id already processed"}` | Idempotente — ya existía |
| 400 | `{"status": "error", "message": "Invalid JSON body"}` | Body no es JSON válido |
| 400 | `{"status": "error", "message": "Missing required field: session_id"}` | Faltó session_id |
| 401 | `{"status": "error", "message": "Unknown agente_id"}` | agente_id no coincide |
| 401 | `{"status": "error", "message": "Invalid signature"}` | HMAC inválido (solo si hay secret configurado) |
| 500 | `{"status": "error", "message": "Internal error saving lead"}` | Error de BD (raro) |

---

## Validación HMAC

Si `SystemSettings.snowie_webhook_secret_encrypted` está configurado, el endpoint **exige** el header `X-Snowie-Signature`. Si no hay secret, solo valida `agente_id`.

### Cómo calcular la firma (lado Snowie)

```python
import hmac, hashlib

secret = "aio5CcPxavKAZqX6ChYaKOvnfT5v_RWQZ21hUY3Q2pc"  # configurado en ambos lados
body = '{"session_id":"snw_abc","agente_id":"cirrus_main",...}'

signature = hmac.new(
    secret.encode("utf-8"),
    body.encode("utf-8"),
    hashlib.sha256,
).hexdigest()

# Header: X-Snowie-Signature: 7bd5977541ef875a99adaacdef69eb0a8c72f713eb7a2bf5f36e387555e515ec
```

El servidor acepta tanto `<hex>` como `sha256=<hex>` en el header.

### Cómo calcular con curl + openssl

```bash
SECRET='tu_secret_aqui'
BODY='{"session_id":"snw_001","agente_id":"cirrus_main","email":"test@x.com"}'
SIG=$(echo -n "$BODY" | openssl dgst -sha256 -hmac "$SECRET" | awk '{print $2}')

curl -X POST https://cirrus.nubex.me/api/v1/webhooks/snowie/ \
  -H "Content-Type: application/json" \
  -H "X-Snowie-Signature: $SIG" \
  -d "$BODY"
```

---

## Idempotencia

El campo `session_id` tiene constraint `UNIQUE` en BD. Si Snowie reenvía el mismo `session_id` (por ejemplo retry tras timeout), Cirrus retorna `200 {"status": "duplicate"}` sin reprocesar. **No se duplican notificaciones ni emails.**

Snowie puede reintentar sin riesgo.

---

## Configuración inicial

### En Cirrus

1. Ir a `/admin/core/systemsettings/1/change/`
2. Sección **"Snowie (captura de leads)"**:
   - **`snowie_agente_id`**: el identificador que Snowie va a mandar (ej. `cirrus_main`)
   - **`snowie_webhook_secret`**: pegar el secret HMAC que vayas a usar (se guarda encriptado)

O vía shell:
```python
from core.models import SystemSettings
from core.services.fiel_encryption import encrypt_password
import secrets

s = SystemSettings.load()
s.snowie_agente_id = "cirrus_main"
s.snowie_webhook_secret_encrypted = encrypt_password(secrets.token_urlsafe(32))
s.save()
```

### En Snowie.ai

1. Dashboard → **Webhooks** / **Integrations** / **Outbound notifications**
2. Crear nuevo webhook:
   - **URL**: `https://cirrus.nubex.me/api/v1/webhooks/snowie/`
   - **Method**: POST
   - **Content-Type**: `application/json`
3. **Trigger**: cuando un agente captura un lead o termina sesión
4. **Body mapping** — Snowie debe enviar:
   - `session_id` ← session ID único
   - `agente_id` ← string fijo `"cirrus_main"` (debe coincidir con el configurado)
   - `nombre`, `email`, `telefono`, `rfc_empresa`, `plan_interesado`, `summary` ← campos capturados
5. **(Opcional) Signature header**:
   - Header: `X-Snowie-Signature`
   - Algorithm: HMAC-SHA256
   - Secret: el mismo que pegaste en SystemSettings
   - Encoding: hex
   - Body: raw JSON

---

## Modelo de datos

```python
class SnowieLead(models.Model):
    id              = UUIDField(primary_key=True)
    session_id      = CharField(max_length=200, unique=True)
    agente_id       = CharField(max_length=100)
    nombre          = CharField(nullable)
    email           = EmailField(nullable)
    telefono        = CharField(nullable)
    rfc_empresa     = CharField(nullable)
    plan_interesado = CharField(nullable)
    summary         = TextField(nullable)
    payload_raw     = JSONField  # POST original completo
    estado          = CharField(['nuevo', 'contactado', 'convertido', 'descartado'])
    notificado_telegram = Boolean
    email_enviado       = Boolean
    creado_en       = DateTime
    actualizado_en  = DateTime
```

**Tabla:** `core_snowielead` (migración `0025`)

---

## Vista en el panel admin

`/panel/crm/` → sección **"❄️ Leads Snowie"** abajo de los leads del conversor público.

Funcionalidades:
- Stats: total, nuevos sin atender, convertidos, última semana
- Filtro por estado
- Tabla con fecha, nombre, email, teléfono, plan, estado
- Cambiar estado en línea (select que envía POST a `/panel/crm/snowie/<id>/estado/`)
- Los leads en estado `nuevo` se destacan con fondo azul
- Summary del lead se muestra en una segunda fila bajo cada registro

También accesible desde el admin de Django: `/admin/core/snowielead/`.

---

## Tareas Celery asociadas

Definidas en `core/tasks_snowie.py` (registradas en `cirrus/celery.py` vía `app.conf.include`).

| Task | Trigger | Descripción |
|------|---------|-------------|
| `notificar_telegram_snowie_lead` | Cada nuevo lead | Envía mensaje al admin con datos del lead. Marca `notificado_telegram=True` al éxito |
| `enviar_email_bienvenida_snowie` | Cada nuevo lead con email | Manda email HTML+texto desde `noreply@`. Marca `email_enviado=True` al éxito |

Ambas tienen `max_retries=3` con backoff. **Si fallan al encolarse**, el endpoint igual responde 200 — el lead ya está guardado y siempre se puede reprocesar manualmente desde el admin.

---

## Troubleshooting

### "Invalid signature" pero el secret está bien
- Verifica que el body que firmas sea **exactamente** el que mandas (mismo JSON, mismas comas, sin espacios extra)
- Snowie debe firmar el body **raw**, no el body re-serializado por algún middleware
- El header debe ser exactamente `X-Snowie-Signature` (case-insensitive en HTTP pero respetar Header convention)

### "Unknown agente_id"
- El campo `agente_id` en el body debe ser **idéntico** al `SystemSettings.snowie_agente_id`
- Case-sensitive

### El lead se guarda pero no llega Telegram
- Verifica `/panel/telegram/` → Estado debe ser "Habilitado" + `telegram_send_warning` activo
- Revisa el log de TelegramAlert ahí mismo para ver el error específico
- Posible causa: el bot no tiene el chat_id del admin (necesita `/start` previo)

### El email de bienvenida no llega
- Verifica que `SystemSettings.noreply_password_encrypted` tenga la contraseña SMTP correcta
- Revisa logs en `/var/www/cirrus/logs/cirrus.log` con `grep "enviar_email_bienvenida"`

---

## Ejemplo completo end-to-end

```bash
SECRET='tu_secret_aqui'
URL='https://cirrus.nubex.me/api/v1/webhooks/snowie/'

BODY='{
  "session_id": "snw_test_001",
  "agente_id": "cirrus_main",
  "nombre": "María González",
  "email": "maria@empresa.mx",
  "telefono": "5587654321",
  "rfc_empresa": "GOM900101ABC",
  "plan_interesado": "enterprise",
  "summary": "Tiene 8 RFCs distintos y procesa ~5,000 CFDIs/mes. Necesita API REST para integrar con su ERP."
}'

# Compactar JSON (sin espacios extra) para que el HMAC coincida
BODY_COMPACT=$(echo -n "$BODY" | python3 -c "import sys,json; print(json.dumps(json.loads(sys.stdin.read()), separators=(',',':'), ensure_ascii=False), end='')")

SIG=$(echo -n "$BODY_COMPACT" | openssl dgst -sha256 -hmac "$SECRET" | awk '{print $2}')

curl -X POST "$URL" \
  -H "Content-Type: application/json" \
  -H "X-Snowie-Signature: $SIG" \
  -d "$BODY_COMPACT"
```

Respuesta esperada:
```json
{"status": "ok", "id": "ffc8c4cb-283e-425a-8dcd-40a38dceac08", "message": "Lead saved"}
```

---

## Documentos relacionados

- [Modelo de datos](modelo-datos.md) — tabla SnowieLead
- [Panel admin](panel-admin.md) — sección Leads Snowie en CRM
- [Jobs y tasks](jobs-y-tasks.md) — celery tasks
