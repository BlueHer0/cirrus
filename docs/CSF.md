# Constancia de Situación Fiscal (CSF) — Documentación

## Flujo de Alta de Empresa

```
Cliente sube FIEL (.cer, .key, password)
        │
        ▼
Validación local cripto (inmediata)
        │
        ▼ (si falla → error, pide corregir)
Extraer RFC del .cer
        │
        ▼
Crear empresa con datos mínimos
Subir FIEL a MinIO (cifrada)
        │
        ▼ (Celery task async)
Verificar FIEL localmente
        │
        ▼
Login SAT con Playwright → Descargar CSF PDF
        │
        ▼
Guardar PDF en MinIO: csf/{RFC}/{año}-{mes}.pdf
        │
        ▼
Parsear PDF (Docling ó pdfplumber)
        │
        ▼
Actualizar empresa con datos oficiales
Activar sync de CFDIs
Notificar al cliente (email)
```

## Archivos

| Archivo | Descripción |
|---------|-------------|
| `core/services/csf_scraper.py` | Scraper Playwright: login SAT + descarga CSF PDF |
| `core/services/csf_parser.py` | Parser: Docling API (primary) + pdfplumber (fallback) |
| `core/tasks.py` | Tasks: `verificar_fiel_y_descargar_csf`, `descargar_csf_mensual`, `descargar_csf_empresa` |
| `accounts/views.py` | View: `app_crear_empresa` (FIEL-only) |
| `frontend/templates/app/empresa_nueva.html` | Template: formulario FIEL-only |
| `frontend/templates/app/empresa_detail.html` | Template: sección "Datos Oficiales (SAT)" |

## Campos del Modelo Empresa (CSF)

| Campo | Tipo | Fuente |
|-------|------|--------|
| `razon_social` | CharField(500) | CSF |
| `regimen_capital` | CharField(200) | CSF |
| `nombre_comercial` | CharField(500) | CSF |
| `regimen_fiscal` | CharField(200) | CSF |
| `codigo_postal` | CharField(5) | CSF |
| `direccion_calle` | CharField(300) | CSF |
| `direccion_num_ext` | CharField(50) | CSF |
| `direccion_num_int` | CharField(50) | CSF |
| `direccion_colonia` | CharField(200) | CSF |
| `direccion_localidad` | CharField(200) | CSF |
| `direccion_municipio` | CharField(200) | CSF |
| `direccion_estado` | CharField(100) | CSF |
| `actividades_economicas` | JSONField | CSF |
| `fecha_inicio_operaciones` | DateField | CSF |
| `estatus_padron` | CharField(50) | CSF |
| `csf_minio_key` | CharField(500) | Sistema |
| `csf_ultima_descarga` | DateTimeField | Sistema |

## Celery Tasks

### `verificar_fiel_y_descargar_csf(empresa_id)`
- **Queue**: descarga
- **Retries**: 5 (5 min entre reintentos)
- **Timeout**: 10 min
- **Flujo**: Verificar FIEL → Descargar CSF → Parsear → Actualizar empresa → Activar sync
- **Si CSF falla pero FIEL OK**: activa sync de todas formas

### `descargar_csf_mensual()`
- **Schedule**: Día 2 del mes, 6:00 UTC
- **Queue**: descarga
- **Flujo**: Para cada empresa activa, encola `descargar_csf_empresa`

### `descargar_csf_empresa(empresa_id)`
- **Retries**: 3
- **Timeout**: 10 min
- **Flujo**: Descargar CSF → actualizar datos fiscales

## Storage en MinIO

```
csf/
  {RFC}/
    2026-03.pdf
    2026-02.pdf
    ...
```

## Notas

- El scraper de CSF **necesitará ajustes iterativos** contra el portal real del SAT
- Los selectores CSS pueden variar
- Parser fallback con pdfplumber usa regex; funciona con el formato conocido
- Docling (nodo5) URL pendiente de confirmar
