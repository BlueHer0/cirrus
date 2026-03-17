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
