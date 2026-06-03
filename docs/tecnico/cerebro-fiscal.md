# Cerebro Fiscal — Biblioteca documental con RAG

## 1. Propósito

Pipeline automático de 4 fases que toma un archivo crudo (PDF/DOCX/imagen/texto)
y lo transforma en un documento fiscal mexicano indexado, listo para búsqueda
semántica y uso como contexto en prompts del futuro chatbot fiscal.

**El usuario solo sube el archivo.** Ningún campo manual — el LLM
(Qwen 2.5 72B local) extrae título, categoría, año de vigencia, organismo emisor,
temas clave y demás metadata automáticamente. Si el documento no es relevante
para fiscal mexicano, se rechaza con motivo.

## 2. Pipeline — 4 fases

```
recibido → convirtiendo → convertido
         → validando → (rechazado | validado)
         → embeddiendo → indexado

         → error ⟵ (cualquier fase)
```

| Fase | Qué hace | Servicio externo | Timeout |
|------|----------|------------------|---------|
| 2 (conversión) | Archivo → Markdown | **Cascada:** Docling → pdfplumber → tesseract OCR | 5 min por nivel |
| 3 (validación) | Markdown → JSON metadata | Qwen 2.5 72B en Spark | 5 min |
| 4 (embeddings) | Chunks → vectores | bge-m3 en Spark | ~50ms/chunk |

**Excepción:** archivos `.md`, `.markdown`, `.txt` no pasan por la cascada —
ya son texto nativo, se leen directamente.

### Cascada de extracción (Fase 2)

Implementada en `core/services/cerebro_fiscal.py:extraer_markdown()`.
Se corta en el primer nivel que devuelva **≥200 caracteres útiles**.

| Nivel | Método | Ideal para | Instalación |
|-------|--------|-----------|-------------|
| 1 | `_extraer_con_docling()` — POST a `http://10.20.0.5:8000/extract` | PDFs nativos modernos con layout limpio | Node 5 existente |
| 2 | `_extraer_con_pdfplumber()` — parser Python puro | PDFs con capa de texto pero sin formato que Docling entienda | `pdfplumber` (ya instalado) |
| 3 | `_extraer_con_ocr()` — pdf2image + pytesseract `lang=spa+eng psm=1 dpi=300` | **PDFs escaneados del DOF, gacetas oficiales antiguas, imágenes** | `tesseract-ocr tesseract-ocr-spa poppler-utils` (apt), `pytesseract pdf2image` (pip) |

Si los 3 niveles fallan → `DoclingNotAvailable` con mensaje claro.

**Por qué esta cascada:** el wrapper "Nubex Node 5 Vision API" (`/extract`)
solo acepta `file` como parámetro — no permite forzar OCR ni configurar
backend. PDFs escaneados (común en publicaciones fiscales mexicanas antiguas)
devuelven markdown vacío. pdfplumber rescata PDFs con texto nativo pero mal
estructurado; OCR con tesseract rescata los escaneos.

## 3. Stack

| Componente | Tecnología |
|------------|------------|
| Vector DB | pgvector 0.6 (HNSW, `vector_cosine_ops`) |
| Embeddings | bge-m3 vía Ollama local (1024 dim) |
| Clasificador / extractor | qwen2.5:72b vía Ollama local (formato JSON forzado) |
| Extracción texto | Docling en `http://10.20.0.5:8000/extract` |
| Storage | MinIO bucket `cirrus`, prefix `cerebro-fiscal/` |
| Chunking | tiktoken cl100k_base, **300 tokens + 50 overlap** |
| Task runner | Celery en worker **dedicado** `cirrus-cerebro.service` |

## 4. MinIO layout

Todos los archivos se almacenan bajo `cerebro-fiscal/`:

```
cerebro-fiscal/
├── originales/
│   └── {uuid_archivo}_{nombre_original}     ← archivo crudo subido
├── markdown/
│   └── {uuid_archivo}.md                    ← texto extraído (fase 2)
└── metadata/
    └── {uuid_archivo}.json                  ← JSON del clasificador (fase 3)
```

Cada `DocumentoFiscal` tiene un campo `uuid_archivo` único que se usa para el
naming. Facilita operaciones batch y migraciones futuras.

## 5. Modelo de datos

### `DocumentoFiscal`
Representa un archivo subido en cualquier fase del pipeline.

| Campo | Notas |
|-------|-------|
| `uuid_archivo` | UUIDField unique — se usa para nombrar en MinIO |
| `nombre_archivo_original` | `safe_name` del upload |
| `archivo_original_key` / `archivo_md_key` / `archivo_json_key` | Keys en MinIO |
| `hash_sha256` | Unique — dedup de uploads duplicados |
| **Metadata extraída por Qwen** (todos blank inicialmente) | |
| `titulo`, `descripcion`, `categoria`, `año_vigencia` | |
| `fecha_publicacion`, `fecha_ultima_revision` | DateField |
| `organismo_emisor` | SAT / SCJN / CINIF / IMSS / SHCP / DOF / otro |
| `temas_clave` | JSONField(list) |
| `aplica_a` | JSONField(list) — persona_fisica, persona_moral, etc. |
| **Pipeline state** | |
| `estado` | 9 valores (ver más abajo) |
| `motivo_rechazo` | cuando `estado=rechazado` |
| `error_detalle` | cuando `estado=error` |
| `intentos_conversion`, `intentos_validacion`, `intentos_embedding` | contadores |
| `chunks_count` | |
| `job_id_celery` | para tracking |

### Estados válidos

| Estado | Significado | Fase |
|--------|-------------|------|
| `recibido` | archivo en MinIO, encolado | pre-fase 2 |
| `convirtiendo` | llamada activa a Docling | fase 2 |
| `convertido` | markdown ya en MinIO | post-fase 2 |
| `validando` | llamada activa a Qwen | fase 3 |
| `rechazado` | no es fiscal — `motivo_rechazo` explica | fase 3 (terminal) |
| `validado` | metadata completa, pre-embeddings | post-fase 3 |
| `requiere_decision` | versión anterior detectada — espera acción admin | post-fase 3 (pausa) |
| `embeddiendo` | generando embeddings | fase 4 |
| `indexado` | todos los chunks persistidos con embedding | fase 4 (terminal OK) |
| `archivado` | reemplazado por nueva versión — excluido de RAG | post-versionado (terminal) |
| `error` | excepción inesperada — `error_detalle` describe | terminal ERROR |

Campos adicionales: `metadata_extra` (JSON) persiste referencias
auxiliares; cuando hay una versión anterior detectada guarda
`version_anterior_id`, `version_anterior_titulo`, `version_anterior_creado`.

### `ChunkFiscal`
Sin cambios respecto a la versión anterior: contenido, embedding(1024), posicion_chunk, tokens, metadata JSON.

Índice HNSW sobre `embedding vector_cosine_ops` con `m=16, ef_construction=64`.

## 6. Task principal

**Archivo:** `core/cerebro_tasks.py`
**Función:** `procesar_documento_fiscal(documento_id)`

```python
@shared_task(
    bind=True,
    max_retries=5,
    default_retry_delay=300,
    soft_time_limit=1800,
    time_limit=2100,
    acks_late=True,
)
```

### Manejo de errores por tipo

| Excepción | Comportamiento |
|-----------|----------------|
| `DoclingNotAvailable` | `intentos_conversion++`, retry cada 5 min, tras 3 intentos → estado=error |
| `SparkNotAvailable` | retry cada 10 min (hasta max_retries) |
| `ClassifierInvalidJSON` | 2 retries internos con mismo prompt; si falla → estado=error |
| `Exception` inesperada | estado=error, `error_detalle` con traceback |

Siempre hay cleanup del tmpdir `/tmp/cirrus_cerebro_{uuid}/` en `finally`.

### Cleanup de rechazados

Cuando Qwen determina `valido=false`, el documento se marca como `rechazado` y
**todos los archivos asociados** (original + md) se borran de MinIO. Solo queda
la fila en BD con el `motivo_rechazo` para auditoría.

## 7. Worker dedicado

**Archivo:** `systemd/cirrus-cerebro.service`

- `concurrency=1` — evita múltiples Qwen72b en paralelo (cada uno consume GPU RAM)
- `-Q cerebro` — solo maneja esta cola
- `TimeoutStopSec=1800` — permite que un Qwen largo termine al reiniciar
- `MemoryMax=2G` — suficiente (LLM corre en Spark remoto, no localmente)

**Deploy:**
```bash
sudo cp /var/www/cirrus/systemd/cirrus-cerebro.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable cirrus-cerebro
sudo systemctl start cirrus-cerebro
```

**Routing** (`cirrus/settings.py` → `CELERY_TASK_ROUTES`):
```python
"core.cerebro_tasks.procesar_documento_fiscal": {"queue": "cerebro"},
```

## 8. API Python

### `core.services.cerebro_fiscal`

| Función | Propósito |
|---------|-----------|
| `chunk_text(text)` | Divide en chunks de 300 tokens con overlap 50, respeta párrafos |
| `generar_embedding(texto)` | 1 llamada a bge-m3 en Spark → vector 1024 |
| `generar_embeddings_batch(textos)` | Secuencial (Ollama no hace batch nativo) |
| `extraer_markdown_docling(bytes, filename, ct)` | POST a `/extract`, devuelve markdown |
| `clasificar_con_qwen(md)` | POST a `/api/generate` con `format=json`, devuelve dict |
| `buscar_contexto(query, top_k=5, categoria=None)` | RAG — top chunks por distancia coseno |
| `spark_disponible()` | Health check a `/api/tags` + presencia bge-m3 |
| `esta_configurado()` | alias de `spark_disponible()` |

### Excepciones definidas

```python
class SparkNotAvailable(Exception): ...
class DoclingNotAvailable(Exception): ...
class ClassifierInvalidJSON(Exception): ...
```

## 9. Panel admin

### Lista `/panel/cerebro/`

- Drag & drop múltiple (max 10 archivos, 50MB c/u)
- Sin campos de texto — solo el archivo
- Stats: total, indexados, procesando, rechazados, con error, chunks totales
- Dedup automático por SHA-256
- Badges de estado por fase con colores + animación `pulse` en fases activas
- Acciones por fila: 📄 Detalle / 🔄 Re-procesar / 🗑 Eliminar (hard delete MinIO + BD)

### Detalle `/panel/cerebro/<uuid>/`

- **Timeline** de 7 fases (recibido → indexado) con dot verde/naranja/gris según estado
- **Metadata extraída** por Qwen: título, categoría, organismo, años, aplica_a, temas_clave, descripción
- **Archivos en MinIO** (original / markdown / json) con tamaños
- **Datos de sistema**: UUID, SHA-256, fechas, contadores de intentos, chunks
- **Preview** de los primeros 5 chunks con contenido completo (monospace, scrollable)
- Botón **🔄 Re-procesar**

## 10. Variables de entorno

| Variable | Default | Notas |
|----------|---------|-------|
| `OLLAMA_BASE_URL` | `http://10.20.0.6:11434` | Spark DGX |
| `OLLAMA_EMBEDDING_MODEL` | `bge-m3` | 1024 dim |
| `OLLAMA_CLASSIFIER_MODEL` | `qwen2.5:72b` | Extractor de metadata |
| `OLLAMA_CLASSIFIER_TIMEOUT` | `300` | segundos |
| `OLLAMA_TIMEOUT` | `60` | Para embeddings |
| `DOCLING_URL` | `http://10.20.0.5:8000/extract` | Conversión PDF→MD |
| `CEREBRO_EMBEDDING_DIMS` | `1024` | |
| `CEREBRO_CHUNK_TOKENS` | `300` | |
| `CEREBRO_CHUNK_OVERLAP` | `50` | |
| `CEREBRO_MINIO_PREFIX` | `cerebro-fiscal` | |

## 11. Prompt del clasificador

Incluido textualmente en `core/services/cerebro_fiscal.py:_PROMPT_CLASIFICADOR`.
Se trunca el input a los **primeros 8000 caracteres** del markdown. Con
`options.temperature=0` y `format="json"` Ollama fuerza JSON válido.

Esquema de respuesta cuando `valido=true`:
```json
{
  "valido": true,
  "titulo": "Ley del Impuesto sobre la Renta 2025",
  "categoria": "ley",
  "descripcion": "Resumen 2-3 líneas del contenido...",
  "año_vigencia": 2025,
  "fecha_publicacion": "2024-12-31",
  "fecha_ultima_revision": null,
  "organismo_emisor": "SAT",
  "temas_clave": ["isr", "deducciones", "personas morales"],
  "aplica_a": ["persona_fisica", "persona_moral"],
  "motivo_rechazo": null
}
```

## 12. Costos y tiempos

Todo **local**. Cero costo variable. Tiempos observados:

| Operación | Tiempo |
|-----------|--------|
| Docling (PDF 50 páginas) | 5-15 s |
| Qwen clasificación 8k chars | 45-120 s |
| bge-m3 1 chunk (300 tokens) | ~50 ms |
| Total por documento típico (~150 chunks) | 60-180 s |

Con `concurrency=1` en el worker, varios documentos se procesan secuencialmente.
Si la cola crece, se puede escalar a `concurrency=2` (limitado por GPU del Spark).

## 13. Comandos útiles

```bash
# Health check manual del Spark y bge-m3
curl http://10.20.0.6:11434/api/tags | jq '.models[].name'

# Test directo de clasificación (texto de prueba)
venv/bin/python manage.py shell -c "
from core.services.cerebro_fiscal import clasificar_con_qwen
r = clasificar_con_qwen('La Ley del ISR establece que las personas morales...')
import json; print(json.dumps(r, indent=2, ensure_ascii=False))
"

# Re-procesar todos los documentos en estado 'error' o 'recibido'
venv/bin/python manage.py shell -c "
from core.models import DocumentoFiscal
from core.cerebro_tasks import procesar_documento_fiscal
for d in DocumentoFiscal.objects.filter(estado__in=['error','recibido']):
    procesar_documento_fiscal.apply_async(args=[str(d.id)], queue='cerebro', countdown=2)
    print(f'Re-encolado: {d.nombre_archivo_original}')
"

# Ver logs del worker dedicado
sudo journalctl -u cirrus-cerebro -f

# Estado del worker
sudo systemctl status cirrus-cerebro
```

## 14. Versionado de documentos

### Problema
Cuando se sube una actualización de un documento ya indexado (ej. RMF 2025
sobre RMF 2024), sin intervención ambas versiones quedarían en el índice y
el RAG podría mezclar normas vigentes con derogadas.

### Detección (post-Fase 3)
Tras extraer título + categoría con Qwen, `_fase_validacion` llama a
`detectar_version_anterior(titulo_nuevo, categoria, organismo, excluir_id)`.
Heurística:
- misma `categoria`
- mismo `organismo_emisor` (si viene)
- ≥ 60 % de intersección de palabras (longitud > 2 chars) entre el título
  nuevo y el de un documento ya `indexado`

Si encuentra coincidencia, el documento se marca **`requiere_decision`** y
guarda la referencia en `metadata_extra.version_anterior_id`. El pipeline
NO avanza a embeddings hasta que el admin resuelva.

### Resolución manual — `POST /panel/cerebro/<uuid>/resolver/`

Vista: `cerebro_resolver_version` (staff_required). Acciones aceptadas
vía campo `accion`:

| Acción | Efecto |
|--------|--------|
| `reemplazar` | Versión anterior → `estado='archivado'` + borra sus `ChunkFiscal`; nuevo doc se re-encola con `fase_inicio='embeddings'` |
| `mantener` | Nuevo doc continúa a fase 4 normalmente; ambas quedan indexadas (la referencia histórica queda en `metadata_extra`) |
| `cancelar` | Hard-delete del nuevo doc (MinIO + BD) |

`reemplazar` y la archivación de la anterior se ejecutan dentro de
`transaction.atomic()` con `select_for_update()` sobre el doc anterior
para evitar condiciones de carrera.

### Reanudación del pipeline
`procesar_documento_fiscal(documento_id, fase_inicio="embeddings")` salta
conversión y validación, descarga el markdown desde `archivo_md_key` y va
directo a `_fase_embeddings`. Usado por `resolver` con acciones
`reemplazar` y `mantener`.

### Exclusión de archivados en RAG
`buscar_contexto` filtra `documento__estado="indexado"`, por lo que
`archivado`, `requiere_decision`, `rechazado`, `error` y los estados
intermedios quedan automáticamente fuera de las búsquedas semánticas.

### UI
- Lista `/panel/cerebro/` — dropdown de filtro por estado; banda superior
  amarilla cuando hay `requiere_decision > 0`; filas archivadas se
  muestran en gris; filas en decisión con fondo ámbar tenue.
- Detalle `/panel/cerebro/<uuid>/` — banner ámbar con pulso y 3 botones
  cuando `estado='requiere_decision'`; banda informativa cuando
  `estado='archivado'`.

## 15. Limitaciones conocidas

- **Ollama `/api/embeddings`** no soporta batch nativo → llamadas secuenciales. Para documentos > 500 chunks el tiempo puede ser significativo.
- **Docling** puede fallar en PDFs con mucho OCR — el fallback de error se marca con `intentos_conversion` y se puede reintentar tras corregir la causa.
- **Archivo MinIO** cuando cliente elimina documento: se intentan borrar las 3 keys (original, md, json). Si el bucket no permite DeleteObject, se queda basura que limpieza manual resuelve.
- **Qwen JSON inválido** tras 2 reintentos se marca como error. En práctica con `format="json"` forzado esto es extremadamente raro.
