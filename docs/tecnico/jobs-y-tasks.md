# Jobs y Tasks — Documentación Técnica

## 1. Inventario de Tasks Periódicas

Configuradas en `cirrus/settings.py:126-180` (CELERY_BEAT_SCHEDULE):

| Task | Cola | Frecuencia | Timeout | Propósito |
|------|------|-----------|---------|-----------|
| `procesar_cola_descargas` | descarga | Cada 5 min | soft=600s, hard=660s | Toma el siguiente job de la cola y ejecuta la descarga |
| `generar_jobs_mes` | scheduler | Día 1 del mes, 3:00 AM | default | Genera jobs para el mes que acaba de cerrar |
| `health_check_playwright` | sistema | Cada 15 min | soft=60s, hard=90s | Verifica que Chromium puede lanzarse |
| `benchmark_hourly_report` | celery | Cada 1 hora | default | Reporte horario de métricas a Telegram |
| `supervisor_cirrus` | sistema | Cada 15 min | default | Monitoreo autónomo + acciones correctivas |
| `sync_efos_task` | sistema | Día 1 del mes, 5:00 AM | default | Sincroniza lista 69-B del SAT |
| `descargar_csf_mensual` | descarga | Día 2 del mes, 6:00 AM | default | Actualiza CSF de todas las empresas |
| `alertas_vencimiento_fiel` | sistema | Diario, 8:00 AM | default | Alerta si FIEL/CSD está por vencer |
| `sat_health_probe` | sistema | Cada 5 min | default | Probe de login al SAT (rotando nodo+RFC) |
| `sat_health_summarize` | sistema | Cada 1 hora | default | Agrega probes en resumen horario |
| `supervisor_pipelines` | sistema | Cada 5 min | default | Desbloquea pipelines, limpia abandonados |

---

## 2. Tasks Bajo Demanda

Configuradas en `cirrus/settings.py:182-199` (CELERY_TASK_ROUTES):

| Task | Cola | Max Retries | Trigger | Propósito |
|------|------|-------------|---------|-----------|
| `descargar_cfdis` | descarga | 10 | API, panel, cola de jobs | Descarga completa de CFDIs de una empresa |
| `verificar_fiel` | verificacion | 3 | Panel admin | Verifica FIEL contra SAT |
| `verificar_fiel_y_descargar_csf` | descarga | 3 | Alta de empresa | Pipeline completo: FIEL + CSF + parse + sync |
| `descargar_csf_empresa` | descarga | 3 | Manual | Refresh CSF de una empresa específica |
| `agente_sincronizacion` | scheduler | 0 | Legacy (cada 15 min) | Orquestador legacy de descargas |

---

## 3. Colas Celery

Configuradas en `cirrus/settings.py:182-199`:

| Cola | Workers | Propósito | Tasks |
|------|---------|-----------|-------|
| `descarga` | CPU/IO intensivo | Descargas SAT (Playwright) | descargar_cfdis, procesar_cola, verificar_fiel_y_csf, csf_empresa, csf_mensual |
| `verificacion` | Liviano | Verificación de FIEL | verificar_fiel |
| `scheduler` | Liviano | Generación y orquestación de jobs | generar_jobs_mes, agente_sincronizacion |
| `sistema` | Liviano | Monitoreo, alertas, health | supervisor, health_check, sat_health_*, alertas_fiel, sync_efos, supervisor_pipelines |
| `celery` | Default | Tareas generales | benchmark_hourly_report |

---

## 4. Job Scheduler

**Archivo:** `core/services/job_scheduler.py` (249 líneas)

### 4.1 `generar_jobs_iniciales(empresa)` — líneas 37-95

Se llama después de verificar la FIEL de una empresa nueva.

- Genera jobs desde `sync_desde_year/month` hasta el mes actual -1
- Orden: **mes más reciente primero** (usuario ve datos recientes antes)
- Spacing: 5 minutos entre cada job
- Prioridad según plan:

| Plan | Prioridad |
|------|-----------|
| Owner | 1 |
| Enterprise | 1 |
| Pro | 3 |
| Basico | 5 |
| Free | 9 |

### 4.2 `generar_jobs_mensuales()` — líneas 98-141

Se ejecuta el día 1 de cada mes vía Celery Beat.

- Crea 2 jobs (recibidos + emitidos) para el mes que acaba de cerrar
- Solo empresas con `sync_activa=True` y `fiel_verificada=True`
- Usa `_calcular_programacion()` para determinar fecha/hora de ejecución

### 4.3 `auditar_y_reparar_jobs(empresa)` — líneas 144-219

Se ejecuta cada noche vía `auditoria_nocturna_periodos()`.

**Lógica de detección de gaps:**
1. Calcula rango de meses según plan:
   - pro/enterprise: últimos 3 años
   - basico: últimos 2 años
   - free: último año
   - staff/admin: desde 2024
2. Para cada mes en el rango, consulta CFDIs reales en BD
3. Si 0 CFDIs encontrados → crea job nuevo o resetea existente a `en_cola`
4. Si job `completado` pero 0 CFDIs → detecta como fallo silencioso, re-encola

### 4.4 `_calcular_programacion(empresa, slug, now)` — líneas 223-248

Distribuye descargas para evitar saturar el SAT:

| Plan | Día del mes | Hora (UTC) |
|------|-------------|------------|
| Free | Día 2 del mes | 4-10 UTC |
| Basico | Día 10 del mes | 4-10 UTC |
| Pro | Día 2 del mes | 4-10 UTC |
| Enterprise | Días 1-3 (staggered por RFC hash) | 4-10 UTC |

La hora exacta se determina con un hash del RFC para distribuir la carga.

---

## 5. Pipeline Manager

**Archivo:** `core/services/pipeline_manager.py` (255 líneas)

### Tipos de pipeline

| Tipo | Pasos | Propósito |
|------|-------|-----------|
| `alta_empresa` | 6 | FIEL validate → FIEL verify → CSF download → CSF parse → Data capture → Job generation |
| `descarga_cfdis` | 4 | Prepare → SAT login → Download → XML process |
| `csf_mensual` | 4 | FIEL prep → CSF download → CSF parse → Data update |

### Funciones principales

| Función | Líneas | Qué hace |
|---------|--------|----------|
| `iniciar_pipeline(empresa, tipo)` | 47-89 | Crea PipelineState con pasos inicializados en JSON |
| `avanzar_paso(pipeline_id, msg)` | 92-143 | Marca paso actual como completado, avanza al siguiente |
| `marcar_error(pipeline_id, error, reintentable)` | 146-215 | Registra error, calcula backoff según SAT health |
| `desbloquear_por_sat_health()` | 218-241 | Desbloquea pipelines cuando SAT >70% |
| `_get_sat_health_pct()` | 244-254 | Consulta probes últimos 30 min |

### Máquina de estados

```
pendiente → en_proceso → completado
                       → error (no reintentable o max intentos)
                       → reintentando → en_proceso (retry)
                       → esperando_sat (SAT <30%) → en_proceso (SAT recupera)
```

---

## 6. Supervisor Autónomo

**Archivo:** `core/services/supervisor.py` (163 líneas)
**Clase:** `CirrusSupervisor`
**Ejecución:** Cada 15 minutos via `supervisor_cirrus` task

### Chequeos

| Chequeo | Método | Qué detecta | Acción automática |
|---------|--------|-------------|-------------------|
| Zombies | `limpiar_zombies()` | DescargaLogs en `ejecutando` >1hr | Marca como `error` |
| Sin descargas | `verificar_empresas_sin_descargas()` | Empresas con sync activa y 0 completadas >24hr | Alerta warning |
| Huecos | `detectar_huecos_descarga()` | Meses sin DescargaJob | Crea jobs faltantes |
| SAT lento | `detectar_sat_lento()` | Última hora 2x más lento que promedio histórico | Alerta warning |
| Disco | `verificar_espacio_disco()` | Uso >70% warning, >85% critical | Alerta Telegram |
| Errores repetidos | `detectar_errores_repetidos()` | 3+ errores consecutivos por empresa | Alerta critical |

### Supervisor de Pipelines

**Archivo:** `core/tasks.py:1526-1605`
**Ejecución:** Cada 5 minutos via `supervisor_pipelines` task

| Acción | Qué hace |
|--------|----------|
| Desbloquear por SAT Health | Si SAT >70%, desbloquea pipelines con `bloqueado_por_sat=True` |
| Re-despachar | Pipelines en `reintentando` con `proximo_intento <= now` |
| Limpiar abandonados | Pipelines activos sin actualización >2hrs → marca como `error` |
| Re-parsear CSF | Empresas con `csf_minio_key` pero sin `razon_social` → re-parsea |

---

## 7. Flujo de Ejecución Temporal

### Cada 5 minutos
```
sat_health_probe → Probe login SAT en nodo rotativo
procesar_cola_descargas → Toma 1 job, ejecuta descarga
supervisor_pipelines → Desbloquea/limpia pipelines
```

### Cada 15 minutos
```
health_check_playwright → Verifica Chromium
supervisor_cirrus → 6 chequeos de salud
```

### Cada hora
```
sat_health_summarize → Resumen horario SAT
benchmark_hourly_report → Métricas a Telegram
```

### Diario
```
08:00 → alertas_vencimiento_fiel (FIEL/CSD por vencer)
Noche → auditoria_nocturna_periodos (detección de gaps)
```

### Mensual
```
Día 1, 03:00 → generar_jobs_mes (jobs del mes cerrado)
Día 1, 05:00 → sync_efos_task (lista 69-B)
Día 2, 06:00 → descargar_csf_mensual (CSF de todas las empresas)
```

---

## Documentos Relacionados

- [Descargador CFDI](descargador-cfdi.md) — Flujo detallado de descarga
- [Modelo de Datos](modelo-datos.md) — Tablas involucradas
- [Fallos y Fallbacks](fallos-y-fallbacks.md) — Manejo de errores
