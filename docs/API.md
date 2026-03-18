# Cirrus API REST — Documentación

## Base URL
```
https://cirrus.nubex.me/api/v1/
```

## Autenticación

Todas las peticiones autenticadas requieren **API Key** en uno de dos formatos:

```bash
# Header (recomendado)
Authorization: Bearer <tu-api-key>

# O query param
?api_key=<tu-api-key>
```

Genera tu API Key en: **https://cirrus.nubex.me/app/api-keys/**

Cada API Key tiene acceso limitado a las empresas asignadas. Solo puedes
consultar CFDIs de empresas vinculadas a tu key.

---

## Endpoints

### Sistema

| Método | Ruta | Auth | Descripción |
|--------|------|------|-------------|
| GET | `/health/` | No | Health check |

### Empresas

| Método | Ruta | Auth | Descripción |
|--------|------|------|-------------|
| GET | `/empresas/` | Sí | Lista empresas accesibles |
| GET | `/empresas/{rfc}/` | Sí | Detalle de empresa por RFC |
| GET | `/empresas/{rfc}/stats/` | Sí | Estadísticas de CFDIs |
| GET | `/empresas/{rfc}/descargas/` | Sí | Historial de descargas |
| POST | `/empresas/{rfc}/descargar/` | Sí* | Trigger descarga del SAT |
| POST | `/empresas/{rfc}/verificar-fiel/` | Sí* | Verificar FIEL |

*Requiere permiso `puede_trigger_descarga` en la API Key.

### CFDIs — Consulta

| Método | Ruta | Auth | Descripción |
|--------|------|------|-------------|
| GET | `/cfdis/list/` | Sí | Lista CFDIs con filtros y paginación |
| GET | `/cfdis/detail/{uuid}/` | Sí | Detalle completo (con conceptos, timbre) |
| GET | `/cfdis/detail/{uuid}/xml/` | Sí | Descarga XML original |
| GET | `/cfdis/{uuid}/pdf/` | Sí | Genera y descarga PDF |
| GET | `/cfdis/{uuid}/excel/` | Sí | Genera Excel detallado |

### CFDIs — Exportación

| Método | Ruta | Auth | Descripción |
|--------|------|------|-------------|
| GET | `/cfdis/export/json/` | Sí | Exporta CFDIs en JSON completo |
| GET | `/cfdis/export/excel/` | Sí | Exporta CFDIs a Excel (.xlsx) |
| GET | `/cfdis/export/csv/` | Sí | Exporta CFDIs a CSV |

### CFDIs — Conversión (público)

| Método | Ruta | Auth | Descripción |
|--------|------|------|-------------|
| POST | `/cfdis/convert/pdf/` | No | XML → PDF (stateless) |
| POST | `/cfdis/convert/excel/` | No | XML → Excel (stateless) |
| POST | `/cfdis/convert/send/` | No | Convierte + envía por email |

### Análisis

| Método | Ruta | Auth | Descripción |
|--------|------|------|-------------|
| GET | `/analysis/summary/` | Sí | Resumen fiscal del periodo |
| GET | `/analysis/fiscal/` | Sí | Análisis fiscal detallado |
| GET | `/analysis/iva/` | Sí | Cálculo de IVA |

---

## Filtros para `/cfdis/list/` y exportaciones

| Param | Tipo | Descripción |
|-------|------|-------------|
| `rfc` | string | RFC de la empresa |
| `year` | int | Año (ej: 2026) |
| `month` | int | Mes (1-12) |
| `tipo` | string | `emitido` o `recibido` |
| `tipo_comprobante` | string | I, E, T, N, P |
| `fecha_desde` | string | YYYY-MM-DD |
| `fecha_hasta` | string | YYYY-MM-DD |
| `limit` | int | Max resultados (default 100, max 500 list / 5000 export) |
| `offset` | int | Offset para paginación |

---

## Ejemplos

### Listar CFDIs recibidos de enero 2026
```bash
curl -H "Authorization: Bearer cirrus_xxxxx" \
  "https://cirrus.nubex.me/api/v1/cfdis/list/?rfc=LUF250407A86&year=2026&month=1&tipo=recibido"
```

### Detalle completo de un CFDI
```bash
curl -H "Authorization: Bearer cirrus_xxxxx" \
  "https://cirrus.nubex.me/api/v1/cfdis/detail/UUID-AQUI/"
```

### Descargar XML original
```bash
curl -H "Authorization: Bearer cirrus_xxxxx" \
  "https://cirrus.nubex.me/api/v1/cfdis/detail/UUID-AQUI/xml/" -o factura.xml
```

### Descargar PDF
```bash
curl -H "Authorization: Bearer cirrus_xxxxx" \
  "https://cirrus.nubex.me/api/v1/cfdis/UUID-AQUI/pdf/" -o factura.pdf
```

### Exportar todos los CFDIs de 2025 en JSON
```bash
curl -H "Authorization: Bearer cirrus_xxxxx" \
  "https://cirrus.nubex.me/api/v1/cfdis/export/json/?rfc=LUF250407A86&year=2025"
```

### Convertir XML a PDF (público, sin auth)
```bash
curl -F "xml_file=@factura.xml" \
  "https://cirrus.nubex.me/api/v1/cfdis/convert/pdf/" -o factura.pdf
```

---

## Respuestas

### Lista de CFDIs
```json
{
  "count": 128,
  "limit": 100,
  "offset": 0,
  "results": [
    {
      "uuid": "e7a17bab-...",
      "rfc_emisor": "ABC123456XYZ",
      "nombre_emisor": "Empresa SA",
      "rfc_receptor": "DEF789012ABC",
      "nombre_receptor": "Cliente SA",
      "fecha": "2026-01-15",
      "tipo_comprobante": "I",
      "tipo_relacion": "recibido",
      "subtotal": 10000.0,
      "iva": 1600.0,
      "total": 11600.0,
      "moneda": "MXN",
      "forma_pago": "03",
      "metodo_pago": "PUE",
      "estado_sat": "vigente",
      "folio": "123",
      "serie": "A"
    }
  ]
}
```

### Detalle de CFDI
```json
{
  "uuid": "e7a17bab-...",
  "emisor": {
    "rfc": "ABC123456XYZ",
    "nombre": "Empresa SA",
    "regimen_fiscal": "601",
    "regimen_fiscal_desc": "General de Ley PM"
  },
  "receptor": {
    "rfc": "DEF789012ABC",
    "nombre": "Cliente SA",
    "uso_cfdi": "G03",
    "uso_cfdi_desc": "Gastos en general"
  },
  "conceptos": [
    {
      "clave_prod_serv": "84111506",
      "descripcion": "Servicios de contabilidad",
      "cantidad": "1",
      "valor_unitario": "10000.00",
      "importe": "10000.00"
    }
  ],
  "timbre": {
    "uuid": "e7a17bab-...",
    "fecha_timbrado": "2026-01-15T10:30:00",
    "rfc_prov_certif": "SAT970701NN3"
  }
}
```

---

## Límites

- **Plan Profesional y Enterprise:** API incluida
- **Plan Gratis y Básico:** API no disponible
- **Rate limit:** 100 requests/minuto
- **Max resultados por página:** 500 (list) / 5,000 (export)

---

## Errores

```json
{"error": "CFDI not found"}           // 404
{"error": "API key not accessible"}    // 403
{"detail": "Unauthorized"}             // 401 (key inválida)
```
