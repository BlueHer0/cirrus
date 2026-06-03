"""Celery tasks del Cerebro Fiscal — pipeline de 4 fases.

Pipeline:
    recibido → convirtiendo → convertido
             → validando → (rechazado | validado)
             → embeddiendo → indexado

Fases dentro de `procesar_documento_fiscal`:
    Fase 2: conversión con Docling (si no es markdown/texto nativo)
    Fase 3: validación + extracción de metadata con Qwen 2.5 72B
    Fase 4: chunking + embeddings con bge-m3

Configuración Celery:
    queue='cerebro'  → worker dedicado (cirrus-cerebro.service)
    concurrency=1    → evita sobrecargar Spark con múltiples Qwen en paralelo
    max_retries=5
    acks_late=True

Separado de core/tasks.py para no tocar el módulo principal.
Registrado en cirrus/celery.py via app.conf.include.
"""

import json as _json
import logging
import os
import re
import shutil
from datetime import datetime, timezone as dt_timezone
from pathlib import Path

import requests
from celery import shared_task
from django.conf import settings
from django.db import transaction

logger = logging.getLogger("core.cerebro_tasks")


# Extensiones que NO necesitan ir a Docling (ya son texto)
_NATIVE_TEXT_EXTS = {".md", ".markdown", ".txt"}


def _ext(filename: str) -> str:
    """Devuelve extensión en minúsculas con punto."""
    return os.path.splitext(filename or "")[1].lower()


def _set_estado(doc, estado: str, save_fields=None):
    """Helper: setea estado y guarda con actualizado_en."""
    doc.estado = estado
    fields = ["estado", "actualizado_en"]
    if save_fields:
        fields.extend(save_fields)
    doc.save(update_fields=list(set(fields)))


def _parse_date_or_none(s):
    """'2024-05-15' → date, 'null'/None/vacío → None."""
    if not s or s in ("null", "None"):
        return None
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


@shared_task(
    bind=True,
    max_retries=5,
    default_retry_delay=300,  # 5 min
    soft_time_limit=1800,     # 30 min (Qwen puede tardar 1-2 min)
    time_limit=2100,
    acks_late=True,
)
def procesar_documento_fiscal(self, documento_id: str, fase_inicio: str = "conversion"):
    """Procesa un DocumentoFiscal end-to-end.

    Args:
        documento_id: UUID del DocumentoFiscal
        fase_inicio: "conversion" (default, pipeline completo) o
                     "embeddings" (saltar a fase 4 — usado al resolver versionado
                     con acción 'reemplazar', el .md ya está en MinIO).

    Retorna dict con `status` y `estado_final`.
    """
    from core.models import DocumentoFiscal, ChunkFiscal
    from core.services.storage_minio import download_bytes, upload_bytes
    from core.services.cerebro_fiscal import (
        chunk_text, generar_embedding,
        extraer_markdown_docling, clasificar_con_qwen,
        spark_disponible,
        SparkNotAvailable, DoclingNotAvailable, ClassifierInvalidJSON,
    )

    try:
        doc = DocumentoFiscal.objects.get(id=documento_id)
    except DocumentoFiscal.DoesNotExist:
        logger.error("DocumentoFiscal %s no existe", documento_id)
        return {"status": "error", "reason": "not_found"}

    doc.job_id_celery = self.request.id or ""
    doc.save(update_fields=["job_id_celery", "actualizado_en"])

    # tmpdir
    tmpdir = Path(f"/tmp/cirrus_cerebro_{doc.uuid_archivo}")
    tmpdir.mkdir(parents=True, exist_ok=True)

    try:
        # ── Atajo: reanudar directamente desde embeddings ────────────
        if fase_inicio == "embeddings":
            if not doc.archivo_md_key:
                raise ValueError(
                    "fase_inicio=embeddings pero doc no tiene archivo_md_key"
                )
            md_bytes = download_bytes(doc.archivo_md_key)
            markdown_text = md_bytes.decode("utf-8", errors="replace")
            logger.info(
                "Cerebro: reanudando %s desde embeddings (%d chars md)",
                documento_id, len(markdown_text),
            )
            _fase_embeddings(self, doc, markdown_text)
            return {"status": "ok", "estado_final": doc.estado,
                    "chunks": doc.chunks_count, "resumed_from": "embeddings"}

        # ═══════════ FASE 2 — Conversión a Markdown ═══════════
        markdown_text, md_bytes = _fase_conversion(doc, tmpdir)
        if markdown_text is None:
            return {"status": "failed", "estado_final": doc.estado}

        # ═══════════ FASE 3 — Validación + metadata (Qwen) ═══════════
        resultado_fase3 = _fase_validacion(doc, markdown_text)
        if resultado_fase3 == "rechazado":
            # rechazado — limpia MinIO + tmp
            _cleanup_rechazado(doc, tmpdir)
            return {"status": "rechazado", "estado_final": doc.estado}
        if resultado_fase3 == "requiere_decision":
            # versión anterior detectada — esperamos acción del admin
            logger.info(
                "Cerebro: %s en requiere_decision, esperando resolución admin",
                documento_id,
            )
            return {"status": "requiere_decision", "estado_final": doc.estado}
        if resultado_fase3 != "validado":
            # error o resultado inesperado — fase_validacion ya seteó estado
            return {"status": "failed", "estado_final": doc.estado}

        # ═══════════ FASE 4 — Embeddings ═══════════
        _fase_embeddings(self, doc, markdown_text)

        return {"status": "ok", "estado_final": doc.estado, "chunks": doc.chunks_count}

    except SparkNotAvailable as e:
        # Spark caído → retry con backoff largo
        doc.error_detalle = f"Spark DGX: {e}"[:2000]
        doc.save(update_fields=["error_detalle", "actualizado_en"])
        logger.warning("Cerebro: Spark no disponible para %s, retry", documento_id)
        raise self.retry(exc=e, countdown=600)

    except DoclingNotAvailable as e:
        # Docling caído → retry con backoff
        doc.intentos_conversion += 1
        doc.error_detalle = f"Docling: {e}"[:2000]
        doc.save(update_fields=[
            "intentos_conversion", "error_detalle", "actualizado_en",
        ])
        if doc.intentos_conversion >= 3:
            _set_estado(doc, "error", save_fields=[])
            logger.error("Cerebro: Docling falló 3 veces para %s, estado=error", documento_id)
            return {"status": "error", "reason": "docling_exhausted"}
        raise self.retry(exc=e, countdown=300)

    except Exception as e:
        logger.exception("Cerebro: error inesperado procesando %s", documento_id)
        doc.error_detalle = f"{type(e).__name__}: {str(e)[:1500]}"
        _set_estado(doc, "error", save_fields=["error_detalle"])
        return {"status": "error", "reason": str(e)[:200]}

    finally:
        # Cleanup tmpdir siempre (incluso en error)
        try:
            shutil.rmtree(tmpdir, ignore_errors=True)
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════════
# Fases internas — cada una maneja su estado de MinIO y BD
# ═══════════════════════════════════════════════════════════════════


def _fase_conversion(doc, tmpdir: Path):
    """Fase 2: descarga original, convierte a MD si hace falta, sube .md a MinIO.

    Returns: (markdown_text, md_bytes) o (None, None) si falla.
    """
    from core.services.storage_minio import download_bytes, upload_bytes
    from core.services.cerebro_fiscal import extraer_markdown

    _set_estado(doc, "convirtiendo")

    # Descargar original a /tmp
    logger.info("Cerebro fase 2: descargando %s", doc.archivo_original_key)
    file_bytes = download_bytes(doc.archivo_original_key)
    local_path = tmpdir / (doc.nombre_archivo_original or f"{doc.uuid_archivo}.bin")
    local_path.write_bytes(file_bytes)

    ext = _ext(doc.nombre_archivo_original)
    if ext in _NATIVE_TEXT_EXTS:
        # Ya es texto/markdown — no pasar por Docling
        try:
            markdown_text = file_bytes.decode("utf-8")
        except UnicodeDecodeError:
            markdown_text = file_bytes.decode("latin-1", errors="replace")
        logger.info("Cerebro fase 2: archivo nativo (%s), skip Docling", ext)
    else:
        # Cascada: Docling → pdfplumber → OCR (tesseract)
        doc.intentos_conversion += 1
        doc.save(update_fields=["intentos_conversion", "actualizado_en"])
        logger.info(
            "Cerebro fase 2: extrayendo texto de %s (cascada)",
            doc.nombre_archivo_original,
        )
        markdown_text = extraer_markdown(
            str(local_path),
            content_type=doc.archivo_content_type or "application/pdf",
        )

    # Persistir .md en MinIO
    md_bytes = markdown_text.encode("utf-8")
    md_key = f"{settings.CEREBRO_MINIO_PREFIX.rstrip('/')}/markdown/{doc.uuid_archivo}.md"
    upload_bytes(md_bytes, md_key, content_type="text/markdown; charset=utf-8")

    doc.archivo_md_key = md_key
    _set_estado(doc, "convertido", save_fields=["archivo_md_key"])
    logger.info(
        "Cerebro fase 2 OK: %s → %d chars markdown",
        doc.uuid_archivo, len(markdown_text),
    )
    return markdown_text, md_bytes


def _fase_validacion(doc, markdown_text: str) -> str:
    """Fase 3: Qwen valida que sea fiscal y extrae metadata.

    Returns: str con el resultado de la fase:
        "validado"          → continuar a embeddings
        "rechazado"         → doc no fiscal, limpiar MinIO
        "requiere_decision" → versión anterior detectada, esperar admin
        "error"             → ver doc.error_detalle
    """
    from core.services.storage_minio import upload_bytes
    from core.services.cerebro_fiscal import (
        clasificar_con_qwen, ClassifierInvalidJSON,
        detectar_version_anterior,
    )

    _set_estado(doc, "validando")
    doc.intentos_validacion += 1
    doc.save(update_fields=["intentos_validacion", "actualizado_en"])

    # Hasta 2 reintentos en caso de JSON inválido
    last_err = None
    result = None
    for attempt in range(2):
        try:
            result = clasificar_con_qwen(markdown_text)
            break
        except ClassifierInvalidJSON as e:
            last_err = e
            logger.warning(
                "Cerebro fase 3: JSON inválido (intento %d/2): %s",
                attempt + 1, e,
            )

    if result is None:
        # No se pudo parsear → tratamos como error
        doc.error_detalle = f"Qwen JSON inválido: {last_err}"[:2000]
        _set_estado(doc, "error", save_fields=["error_detalle"])
        return "error"

    # Decisión del LLM
    if not result.get("valido", False):
        motivo = result.get("motivo_rechazo") or "Documento no fiscal"
        doc.motivo_rechazo = str(motivo)[:2000]
        _set_estado(doc, "rechazado", save_fields=["motivo_rechazo"])
        logger.info(
            "Cerebro fase 3: rechazado %s — %s",
            doc.uuid_archivo, motivo,
        )
        return "rechazado"

    # Validado — llenar metadata
    doc.titulo = (result.get("titulo") or "")[:200]
    doc.descripcion = result.get("descripcion") or None
    categoria = (result.get("categoria") or "otro").lower()
    if categoria not in dict(doc.CATEGORIA_CHOICES):
        categoria = "otro"
    doc.categoria = categoria
    doc.año_vigencia = result.get("año_vigencia") or None
    doc.fecha_publicacion = _parse_date_or_none(result.get("fecha_publicacion"))
    doc.fecha_ultima_revision = _parse_date_or_none(result.get("fecha_ultima_revision"))
    doc.organismo_emisor = (result.get("organismo_emisor") or "")[:100]
    doc.temas_clave = result.get("temas_clave") or []
    doc.aplica_a = result.get("aplica_a") or []
    doc.motivo_rechazo = None

    # Guardar JSON de metadata en MinIO
    json_bytes = _json.dumps(result, ensure_ascii=False, indent=2).encode("utf-8")
    json_key = f"{settings.CEREBRO_MINIO_PREFIX.rstrip('/')}/metadata/{doc.uuid_archivo}.json"
    upload_bytes(json_bytes, json_key, content_type="application/json; charset=utf-8")
    doc.archivo_json_key = json_key

    # ── Detección de versión anterior ─────────────────────────────
    # Solo si ya tenemos título + categoría ciertamente extraídos.
    version_anterior = detectar_version_anterior(
        titulo_nuevo=doc.titulo,
        categoria=doc.categoria,
        organismo=doc.organismo_emisor,
        excluir_id=doc.id,
    )

    if version_anterior is not None:
        extra = dict(doc.metadata_extra or {})
        extra["version_anterior_id"] = str(version_anterior.id)
        extra["version_anterior_titulo"] = version_anterior.titulo
        extra["version_anterior_creado"] = version_anterior.creado_en.isoformat()
        doc.metadata_extra = extra
        _set_estado(doc, "requiere_decision", save_fields=[
            "titulo", "descripcion", "categoria", "año_vigencia",
            "fecha_publicacion", "fecha_ultima_revision",
            "organismo_emisor", "temas_clave", "aplica_a",
            "motivo_rechazo", "archivo_json_key", "metadata_extra",
        ])
        logger.info(
            "Cerebro fase 3: %s requiere_decision (anterior=%s)",
            doc.uuid_archivo, version_anterior.id,
        )
        return "requiere_decision"

    _set_estado(doc, "validado", save_fields=[
        "titulo", "descripcion", "categoria", "año_vigencia",
        "fecha_publicacion", "fecha_ultima_revision",
        "organismo_emisor", "temas_clave", "aplica_a",
        "motivo_rechazo", "archivo_json_key",
    ])
    logger.info(
        "Cerebro fase 3 OK: %s → %s [%s]",
        doc.uuid_archivo, doc.titulo, doc.categoria,
    )
    return "validado"


def _fase_embeddings(task, doc, markdown_text: str):
    """Fase 4: chunking + embeddings, reemplaza ChunkFiscals del doc."""
    from core.models import ChunkFiscal
    from core.services.cerebro_fiscal import chunk_text, generar_embedding, SparkNotAvailable

    _set_estado(doc, "embeddiendo")
    doc.intentos_embedding += 1
    doc.save(update_fields=["intentos_embedding", "actualizado_en"])

    chunks = chunk_text(markdown_text)
    if not chunks:
        raise ValueError("Chunking devolvió 0 chunks")

    logger.info(
        "Cerebro fase 4: generando %d embeddings para %s",
        len(chunks), doc.uuid_archivo,
    )

    # Generar embeddings con retry individual por chunk
    embeddings = []
    for idx, c in enumerate(chunks):
        emb = _embedding_con_retry(c["contenido"], max_attempts=3)
        embeddings.append(emb)
        if (idx + 1) % 25 == 0:
            logger.info(
                "  %d/%d embeddings completados (%s)",
                idx + 1, len(chunks), doc.uuid_archivo,
            )

    # Persistir
    with transaction.atomic():
        ChunkFiscal.objects.filter(documento=doc).delete()
        ChunkFiscal.objects.bulk_create([
            ChunkFiscal(
                documento=doc,
                contenido=c["contenido"],
                embedding=emb,
                posicion_chunk=c["posicion"],
                tokens=c["tokens"],
                metadata={"chars": len(c["contenido"])},
            )
            for c, emb in zip(chunks, embeddings)
        ], batch_size=200)

        doc.chunks_count = len(chunks)
        doc.indexado_en = datetime.now(dt_timezone.utc)
        doc.error_detalle = None
        _set_estado(doc, "indexado", save_fields=[
            "chunks_count", "indexado_en", "error_detalle",
        ])

    logger.info(
        "Cerebro fase 4 OK: %s indexado con %d chunks",
        doc.uuid_archivo, doc.chunks_count,
    )


def _embedding_con_retry(texto: str, max_attempts: int = 3) -> list[float]:
    """Genera embedding con reintentos locales (60s entre intentos)."""
    from core.services.cerebro_fiscal import generar_embedding, SparkNotAvailable
    import time

    last_err = None
    for attempt in range(max_attempts):
        try:
            return generar_embedding(texto)
        except SparkNotAvailable as e:
            last_err = e
            if attempt < max_attempts - 1:
                logger.warning(
                    "Embedding retry %d/%d tras error: %s",
                    attempt + 1, max_attempts, e,
                )
                time.sleep(60)
    raise last_err  # escala → retry del task


def _cleanup_rechazado(doc, tmpdir: Path):
    """Elimina archivos de MinIO para documento rechazado (no-fiscal)."""
    from core.services.storage_minio import delete_object

    for key_attr in ("archivo_original_key", "archivo_md_key", "archivo_json_key"):
        key = getattr(doc, key_attr, "")
        if key:
            try:
                delete_object(key)
            except Exception as e:
                logger.warning("Cleanup rechazado: no se pudo borrar %s: %s", key, e)
