# Cirrus — Módulo EFOS (Lista Negra SAT 69-B)

## Fuente
CSV oficial del SAT: http://omawww.sat.gob.mx/cifras_sat/Documents/Listado_Completo_69-B.csv  
Se actualiza cada ~3 meses por el SAT. El CSV tiene preamble legal (2 líneas) antes del header real.

## Sincronización
- **Automática**: día 1 de cada mes a las 5AM UTC vía Celery Beat
- **Manual**: `python manage.py sync_efos`
- Alerta Telegram al completar
- Primer sync: 14,055 registros
- Parser: detecta encoding (utf-8/latin-1), skipea preamble, busca header con "RFC"

## Modelo EFOS
- `rfc`: RFC del contribuyente (unique, indexed)
- `nombre`: razón social (puede tener errores del SAT)
- `situacion`: Presunto, Definitivo, Desvirtuado, Sentencia Favorable
- `fecha_publicacion`: fecha de publicación en DOF
- `raw_data`: JSON con todos los campos originales del CSV

## Integración con análisis de riesgos
- `analysis_risks` cruza proveedores del periodo contra tabla EFOS
- FiscScore incluye componente `riesgo_proveedores` (peso 15%)
- Pesos: cumplimiento 30% + IVA 20% + proveedores 15% + deducibilidad 15% + diversificación 10% + errores 10%
- Cada proveedor en lista resta 25 puntos al componente

## Verificador público
- URL: `/verificar-rfc/`
- Sin login, herramienta gratuita para atraer tráfico SEO
- Incluye disclaimer legal y CTA hacia registro

## Situaciones posibles
| Situación | Significado |
|-----------|-------------|
| Presunto | El SAT presume operaciones inexistentes |
| Definitivo | Confirmado por el SAT |
| Desvirtuado | El contribuyente demostró lo contrario |
| Sentencia Favorable | Tribunal falló a favor del contribuyente |

## Archivos
- `core/models.py` — modelo EFOS
- `core/services/efos_sync.py` — sync, verificar_rfc, verificar_proveedores
- `core/management/commands/sync_efos.py` — management command
- `core/tasks.py` — sync_efos_task
- `core/views.py` — verificar_rfc_view (público)
- `frontend/templates/public/verificar_rfc.html` — template verificador
- `accounts/analysis_helpers.py` — FiscScore con EFOS real
- `frontend/templates/app/analysis_risks.html` — expandable EFOS table
