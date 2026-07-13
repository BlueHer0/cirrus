# Cirrus — Arquitectura y Documentación

## Stack
- Django 5 + Django Ninja (API REST)
- Celery + Redis (workers, scheduling)
- PostgreSQL (metadata, CFDIs, planes, usuarios)
- MinIO S3 (XMLs, FIELs, logos)
- Playwright/Chromium (scraping SAT)
- WeasyPrint (generación PDF)
- Nginx + Gunicorn + systemd

## Servidor
- VPS2 IONOS: 8 cores, 16GB RAM, 400GB disco
- URL: https://cirrus.nubex.me
- IP: 10.20.0.2 (interna)

## Servicios systemd
- cirrus-web: Gunicorn (3 workers gthread)
- cirrus-worker: Celery (3 ForkPoolWorkers)
- cirrus-beat: Celery Beat (scheduler)

## Colas Celery
- descarga: descargar_cfdis
- verificacion: verificar_fiel
- sistema: health_check_playwright, benchmark_hourly_report
- scheduler: agente_sincronizacion, programar_descargas_del_dia

## Almacenamiento MinIO
- Bucket: cirrus
- fiel/{RFC}/{RFC}.cer y .key
- cfdis/{RFC}/{año}/{mes}/{tipo}/{UUID}.xml
- logos/{RFC}/logo.png

## Seguridad
- FIEL password: encriptado con Fernet
- FIEL archivos: MinIO bucket privado, nunca en disco permanente
- Scraping: archivos temporales en /tmp, auto-delete
- Multi-tenant: queries siempre filtran por owner=request.user
- API auth: X-API-Key header o ?api_key= query param
- Django admin oculto en /djadmin-8x7k/
- Sesiones: 8h expiry, browser close, admin/cliente separados

## Monitoreo
- Health check cada 5 min (bash script + cron)
- Playwright watchdog cada 15 min
- Telegram alerts (bot Macbotfap)
- Benchmark monitor cada 5 min → logs/benchmark.log
- Reporte horario Telegram (si hubo actividad)
- /panel/monitor/ — servicios, workers, jobs, telemetría

## Supervisor Inteligente
- Task: `supervisor_cirrus` (cada 15 min vía Celery Beat)
- Archivo: `core/services/supervisor.py`
- Funciones:
  - 🧹 Limpia descargas zombies (ejecutando > 1 hora)
  - ⚠️ Alerta empresas sin descargas (sync activa pero 0 completadas)
  - ⚠️ Detecta SAT lento (promedio 2x mayor que histórico)
  - 🔴 Monitorea espacio en disco (alerta >70%, crítico >85%)
  - 🔴 Detecta errores repetidos (3+ consecutivos por empresa)
- Acciones: limpieza automática, alertas Telegram

## Agente de Sincronización
- Task: `agente_sincronizacion` (cada 15 min)
- Auto-limpia zombies antes de evaluar
- No se bloquea por descargas ejecutando (usa sistema de slots, max 3)
- Procesa múltiples empresas por ciclo
- Bypass de restricción de plan para primeras descargas
- Verifica recibidos y emitidos por separado

## Backups
- Script: /var/www/cirrus/scripts/backup.sh
- Cron: diario 4AM UTC
- Contenido: pg_dump (gzip), metadata JSON, .env, settings.py
- Retención: 30 días
- Alerta Telegram al completar
- Directorio: /var/www/cirrus/backups/

## Vista Detalle CFDI
- URL: /app/cfdis/{uuid}/
- Parsea XML original de MinIO para datos completos (emisor, receptor, conceptos, impuestos, timbre)
- Botones: PDF (genera con WeasyPrint), XML (descarga raw), Excel (3 hojas: comprobante, conceptos, impuestos)
- Fallback: si XML no disponible, muestra datos del modelo Django

## ⚠️ Problemas conocidos de integridad de descarga (diagnóstico 2026-07-08)

### Bug del "paquete equivocado" (CORREGIDO 2026-07-12/13 — ver fix abajo)
Cadena de 3 fallas en la descarga masiva por RPA:
1. `sat_navigator._select_all_and_request_download` (líneas ~417-428) extrae el
   folio de la solicitud con un regex de UUID sobre TODO el HTML — la página
   está llena de folios fiscales de CFDIs, así que captura casi siempre el
   folio del primer CFDI de la tabla, NO el de la solicitud.
2. `sat_navigator._recover_downloads` (líneas ~579-584): si el folio no aparece
   en el grid de "Recuperar Descargas" (lista residuales de hasta 3 días), hace
   fallback a la fila 0 (paquete más reciente) con solo un warning → puede
   bajar un paquete de OTRA solicitud/periodo.
3. `xml_processor.process_single_xml` no valida que la fecha del CFDI caiga en
   el rango solicitado por el job, ni que empresa.rfc sea emisor o receptor.
   Solo dedup por UUID (eso sí evita duplicados).

Consecuencia: el CFDI se guarda con sus datos correctos (fecha del XML), pero
el mes solicitado queda marcado "completado" con datos de otro periodo. El
dedup de `tasks.descargar_cfdis` (DescargaLog completado que cubre el rango →
skip) impide reintentarlo: el mes real queda en punto ciego permanente.

Auditoría BD 2026-07-08: **1,282 CFDIs** entraron por paquetes cuyo rango
solicitado está a >35 días de la fecha del CFDI (ITA 1,132; AFE 49; AIPF 45;
VEN 31; LUF 25). Ej.: corrida 2026-06-01 pidiendo 2025-07 de AFE insertó 20
CFDIs de 2026-05.

Fix implementado (2026-07-12/13, requiere reinicio de cirrus-worker):
- Folio de solicitud: se extrae del alert de confirmación (contenedor de
  `#btnAlertDCCerrar`), con fallback a "UUID del body que NO esté en la tabla
  de resultados". Sin folio → SATNavigatorError (nunca descargar a ciegas).
- `_recover_downloads`: si el folio no aparece en GridViewReporte → sigue
  polling hasta timeout (el fallback a fila 0 se eliminó). Timeout → error →
  retry de tasks.py.
- `validar_paquete_descargado` (core/services/scrapper.py): antes de procesar,
  ≥95% de los XMLs deben tener fecha en [rango solicitado ±3 días] y contener
  el RFC de la empresa; si no, el paquete se DESCARTA completo y el job queda
  en error para retry (fase `package_validation` en telemetría).

### Gap estructural de timbres tardíos (CONFIRMADO)
- La descarga es 1 vez por mes y el refetch (`refetch_meses_recientes_vacios`)
  solo re-encola meses con 0 CFDIs. Un CFDI timbrado DESPUÉS de que su mes ya
  se descargó (con datos) nunca se recupera. Casos confirmados VEN: factura
  Stripe mayo (timbrada 2-jun), 27 CFDIs de marzo (~$509k, timbrados 13-31 mar
  tras la descarga del 16-mar), 21 facturas del 29-jun + 3 nóminas jun/jul
  (timbradas horas después del job del 3-jul).
- Solución diseñada (NO implementada): ventanas de descarga múltiples
  solapadas por mes (días 1, 5, 10, 15, 20, 25 con solape de 5-10 días).
- El portal SAT SÍ devuelve búsquedas retrospectivas >3 meses (verificado
  2026-07-08: feb y mar 2026 completos en tabla), pero las re-consultas de
  producción a veces devuelven vacíos falsos; el guard 'fallo ≠ vacío' de
  `_wait_for_results_or_empty` cubre el caso de filtros muertos, no el de
  paquete equivocado.
- Alternativa robusta identificada: Web Service oficial de Descarga Masiva
  v1.5 del SAT (SOAP + FIEL, librería `python-satcfdi`) como vía primaria,
  RPA como fallback.
