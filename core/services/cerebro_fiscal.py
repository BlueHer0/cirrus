"""Cerebro Fiscal — servicio RAG sobre legislación fiscal mexicana.

Usa embeddings locales via Ollama en el Spark DGX (10.20.0.6:11434),
modelo bge-m3 (1024 dimensiones, multilingüe, bueno para español legal).

Ventajas del setup local:
- Sin API key, sin costo variable
- Baja latencia (intranet)
- Privacidad: los PDFs fiscales no salen de la infra propia

Provee:
- Generación de embeddings (uno o batch)
- Chunking de texto respetando párrafos
- Búsqueda semántica por similitud coseno (pgvector)

No toca directamente el modelo ni MinIO — son las tasks y views quienes
orquestan esos. Este módulo es la "unidad de conocimiento" pura y reusable.
"""

import logging
from typing import Optional

import requests
from django.conf import settings

logger = logging.getLogger("core.cerebro_fiscal")


class SparkNotAvailable(Exception):
    """El Spark DGX (Ollama) no responde o el modelo bge-m3 falla."""


class DoclingNotAvailable(Exception):
    """Docling (node5) no responde o devolvió error."""


class ClassifierInvalidJSON(Exception):
    """Qwen devolvió algo que no es JSON válido."""


# ── Chunking ──────────────────────────────────────────────────────────


def _tokens(text: str) -> int:
    """Conteo aproximado de tokens (tiktoken cl100k_base; fallback heurístico)."""
    try:
        import tiktoken
        enc = tiktoken.get_encoding("cl100k_base")
        return len(enc.encode(text))
    except Exception:
        return max(1, len(text) // 4)


def chunk_text(text: str, chunk_tokens: int = None, overlap_tokens: int = None) -> list[dict]:
    """Divide texto en chunks respetando párrafos.

    Args:
        text: texto a dividir
        chunk_tokens: objetivo de tokens por chunk (default settings.CEREBRO_CHUNK_TOKENS)
        overlap_tokens: tokens compartidos entre chunks adyacentes (default settings.CEREBRO_CHUNK_OVERLAP)

    Returns: [{"contenido": str, "tokens": int, "posicion": int}, ...]
    """
    chunk_tokens = chunk_tokens or settings.CEREBRO_CHUNK_TOKENS
    overlap_tokens = overlap_tokens or settings.CEREBRO_CHUNK_OVERLAP

    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    if not paragraphs:
        return []

    chunks = []
    current = []
    current_tokens = 0
    posicion = 0

    for p in paragraphs:
        p_tokens = _tokens(p)

        # Párrafo gigante → partir por oraciones
        if p_tokens > chunk_tokens:
            if current:
                chunks.append({
                    "contenido": "\n\n".join(current),
                    "tokens": current_tokens,
                    "posicion": posicion,
                })
                posicion += 1
                current = []
                current_tokens = 0
            sentences = p.replace("\n", " ").split(". ")
            buf = []
            buf_tokens = 0
            for sent in sentences:
                s_tokens = _tokens(sent)
                if buf_tokens + s_tokens > chunk_tokens and buf:
                    chunks.append({
                        "contenido": ". ".join(buf) + ".",
                        "tokens": buf_tokens,
                        "posicion": posicion,
                    })
                    posicion += 1
                    buf = [sent]
                    buf_tokens = s_tokens
                else:
                    buf.append(sent)
                    buf_tokens += s_tokens
            if buf:
                chunks.append({
                    "contenido": ". ".join(buf) + ".",
                    "tokens": buf_tokens,
                    "posicion": posicion,
                })
                posicion += 1
            continue

        # Si excede el límite, cerrar chunk y arrancar otro con overlap
        if current_tokens + p_tokens > chunk_tokens and current:
            chunks.append({
                "contenido": "\n\n".join(current),
                "tokens": current_tokens,
                "posicion": posicion,
            })
            posicion += 1
            overlap_buf = []
            overlap_acc = 0
            for prev in reversed(current):
                t = _tokens(prev)
                if overlap_acc + t > overlap_tokens:
                    break
                overlap_buf.insert(0, prev)
                overlap_acc += t
            current = overlap_buf
            current_tokens = overlap_acc

        current.append(p)
        current_tokens += p_tokens

    if current:
        chunks.append({
            "contenido": "\n\n".join(current),
            "tokens": current_tokens,
            "posicion": posicion,
        })

    return chunks


# ── Embeddings (Ollama local en Spark DGX) ────────────────────────────


def _ollama_url() -> str:
    """URL completa del endpoint de embeddings de Ollama."""
    base = settings.OLLAMA_BASE_URL.rstrip("/")
    return f"{base}/api/embeddings"


def spark_disponible(timeout: float = 3.0) -> bool:
    """Health check rápido al Spark DGX — ¿responde Ollama y tiene bge-m3?"""
    try:
        base = settings.OLLAMA_BASE_URL.rstrip("/")
        r = requests.get(f"{base}/api/tags", timeout=timeout)
        if r.status_code != 200:
            return False
        models = r.json().get("models", [])
        target = settings.OLLAMA_EMBEDDING_MODEL
        return any(
            target in m.get("name", m.get("model", ""))
            for m in models
        )
    except Exception:
        return False


def generar_embedding(texto: str) -> list[float]:
    """Genera un embedding vía Ollama bge-m3 en el Spark DGX.

    Raises SparkNotAvailable si el Spark no responde o el modelo falla.
    """
    try:
        r = requests.post(
            _ollama_url(),
            json={
                "model": settings.OLLAMA_EMBEDDING_MODEL,
                "prompt": texto,
            },
            timeout=settings.OLLAMA_TIMEOUT,
        )
        r.raise_for_status()
        data = r.json()
    except requests.exceptions.ConnectionError as e:
        raise SparkNotAvailable(
            f"Spark DGX no disponible en {settings.OLLAMA_BASE_URL} — "
            f"embeddings pausados ({e})"
        ) from e
    except requests.exceptions.Timeout as e:
        raise SparkNotAvailable(
            f"Spark DGX timeout tras {settings.OLLAMA_TIMEOUT}s "
            f"({settings.OLLAMA_BASE_URL})"
        ) from e
    except requests.exceptions.HTTPError as e:
        raise SparkNotAvailable(
            f"Ollama respondió {r.status_code}: {r.text[:300]}"
        ) from e

    emb = data.get("embedding")
    if not emb or not isinstance(emb, list):
        raise SparkNotAvailable(
            f"Ollama devolvió respuesta sin 'embedding': {data}"
        )
    if len(emb) != settings.CEREBRO_EMBEDDING_DIMS:
        raise SparkNotAvailable(
            f"Ollama devolvió {len(emb)} dims, esperaba "
            f"{settings.CEREBRO_EMBEDDING_DIMS}. ¿Modelo incorrecto?"
        )
    return emb


def generar_embeddings_batch(textos: list[str]) -> list[list[float]]:
    """Genera embeddings secuencialmente.

    Nota: Ollama no soporta batch nativo en /api/embeddings, así que hacemos
    llamadas secuenciales. El Spark DGX procesa rápido (~50ms por chunk en
    bge-m3). Para documentos típicos (50-200 chunks) el overhead es aceptable.
    """
    if not textos:
        return []
    result = []
    for i, t in enumerate(textos):
        emb = generar_embedding(t)
        result.append(emb)
        if (i + 1) % 50 == 0:
            logger.info("Cerebro: %d/%d embeddings generados", i + 1, len(textos))
    return result


# ── Búsqueda RAG ──────────────────────────────────────────────────────


def buscar_contexto(query: str, top_k: int = 5, categoria: str = None) -> list[dict]:
    """Busca los chunks más relevantes para un query via similitud coseno.

    Args:
        query: texto de búsqueda en lenguaje natural
        top_k: cantidad de chunks a devolver
        categoria: filtro opcional por categoría del documento padre

    Returns:
        [{
            "chunk_id": str,
            "documento_id": str,
            "documento_titulo": str,
            "categoria": str,
            "año_vigencia": int | None,
            "pagina": int | None,
            "posicion_chunk": int,
            "contenido": str,
            "distancia": float,  # 0.0 = idéntico, 2.0 = opuesto
            "similitud": float,  # 1.0 = idéntico, -1.0 = opuesto
            "metadata": dict,
        }, ...]

    Raises SparkNotAvailable si el Spark no responde.
    """
    from core.models import ChunkFiscal
    from pgvector.django import CosineDistance

    query = (query or "").strip()
    if not query:
        return []

    q_vec = generar_embedding(query)

    qs = ChunkFiscal.objects.select_related("documento")
    if categoria:
        qs = qs.filter(documento__categoria=categoria)
    qs = qs.filter(documento__estado="indexado")

    results = (
        qs.annotate(distancia=CosineDistance("embedding", q_vec))
          .order_by("distancia")[:top_k]
    )

    return [
        {
            "chunk_id": str(c.id),
            "documento_id": str(c.documento.id),
            "documento_titulo": c.documento.titulo,
            "categoria": c.documento.categoria,
            "año_vigencia": c.documento.año_vigencia,
            "pagina": c.pagina,
            "posicion_chunk": c.posicion_chunk,
            "contenido": c.contenido,
            "distancia": float(c.distancia),
            "similitud": 1.0 - float(c.distancia),
            "metadata": c.metadata,
        }
        for c in results
    ]


# ── Versionado — detección de documento previo similar ──────────────


def detectar_version_anterior(titulo_nuevo: str, categoria: str,
                              organismo: str = "", excluir_id=None):
    """Busca un DocumentoFiscal ya indexado que sea versión anterior del que
    se está subiendo.

    Heurística simple (suficiente para un corpus fiscal pequeño):
      - misma categoría
      - mismo organismo_emisor (si viene)
      - intersección de palabras del título ≥ 60% del título nuevo

    Solo considera documentos en estado 'indexado' (los archivados y
    requiere_decision se excluyen por diseño).

    Args:
        titulo_nuevo: título extraído por Qwen para el doc actual
        categoria: slug de categoría (ley, rmf, etc.)
        organismo: organismo emisor, opcional
        excluir_id: UUID del doc que se está evaluando (para no detectarse a sí mismo)

    Returns:
        instancia DocumentoFiscal de la versión anterior, o None si no hay.
    """
    from core.models import DocumentoFiscal

    titulo_nuevo = (titulo_nuevo or "").strip()
    if not titulo_nuevo or not categoria:
        return None

    qs = DocumentoFiscal.objects.filter(
        categoria=categoria,
        estado="indexado",
    )
    if organismo:
        qs = qs.filter(organismo_emisor=organismo)
    if excluir_id:
        qs = qs.exclude(id=excluir_id)

    palabras_nuevo = {
        w for w in titulo_nuevo.lower().split()
        if len(w) > 2  # ignorar "de", "la", "el"
    }
    if not palabras_nuevo:
        return None

    for doc in qs[:50]:  # límite defensivo
        palabras_existente = {
            w for w in (doc.titulo or "").lower().split() if len(w) > 2
        }
        if not palabras_existente:
            continue
        interseccion = palabras_nuevo & palabras_existente
        ratio = len(interseccion) / max(len(palabras_nuevo), 1)
        if ratio >= 0.6:
            logger.info(
                "Cerebro: versión anterior detectada — nuevo=%r anterior=%r ratio=%.2f",
                titulo_nuevo, doc.titulo, ratio,
            )
            return doc
    return None


# ── Util: estado ──────────────────────────────────────────────────────


def esta_configurado() -> bool:
    """Returns True si el Spark DGX responde y tiene bge-m3.

    Esta función hace HEALTH CHECK real, no solo configuración. Úsala para
    decidir si mostrar advertencia en el panel.
    """
    return spark_disponible()


# ── Extracción con Docling (fase 2) ─────────────────────────────────


def extraer_markdown(archivo_path, content_type: str = "application/pdf") -> str:
    """Cascada de extracción de texto desde un archivo a Markdown.

    Estrategia en 3 niveles (se corta en el primero que dé ≥200 chars útiles):
      1. Docling (Node 5 Vision API) — rápido, estructurado, bueno con PDFs nativos
      2. pdfplumber — PDFs con capa de texto nativa sin OCR
      3. pytesseract + pdf2image — PDFs escaneados, aplica OCR en español

    Args:
        archivo_path: ruta local (str o Path) al archivo en disco
        content_type: MIME type — default application/pdf

    Returns: markdown extraído (string)
    Raises: DoclingNotAvailable si los 3 niveles fallan.
    """
    from pathlib import Path

    path = Path(archivo_path)
    if not path.exists():
        raise DoclingNotAvailable(f"Archivo no existe: {path}")

    MIN_CHARS = 200  # umbral para considerar "útil" el texto extraído

    # ── NIVEL 1: Docling ──────────────────────────────────────────────
    try:
        md = _extraer_con_docling(path, content_type)
        if md and len(md.strip()) >= MIN_CHARS:
            logger.info("Extracción vía Docling OK (%d chars)", len(md))
            return md
        logger.warning(
            "Docling devolvió texto muy corto (%d chars) — fallback pdfplumber",
            len(md) if md else 0,
        )
    except Exception as e:
        logger.warning("Docling falló (%s) — fallback pdfplumber", e)

    # pdfplumber + OCR solo tienen sentido para PDFs
    is_pdf = (
        content_type.startswith("application/pdf")
        or path.suffix.lower() == ".pdf"
    )

    if is_pdf:
        # ── NIVEL 2: pdfplumber ───────────────────────────────────────
        try:
            md = _extraer_con_pdfplumber(path)
            if md and len(md.strip()) >= MIN_CHARS:
                logger.info("Extracción vía pdfplumber OK (%d chars)", len(md))
                return md
            logger.warning(
                "pdfplumber devolvió texto muy corto (%d chars) — fallback OCR",
                len(md) if md else 0,
            )
        except Exception as e:
            logger.warning("pdfplumber falló (%s) — fallback OCR", e)

        # ── NIVEL 3: OCR con tesseract ────────────────────────────────
        try:
            md = _extraer_con_ocr(path)
            if md and len(md.strip()) >= MIN_CHARS:
                logger.info("Extracción vía OCR (tesseract) OK (%d chars)", len(md))
                return md
            logger.error(
                "OCR también devolvió texto muy corto (%d chars)",
                len(md) if md else 0,
            )
        except Exception as e:
            logger.error("OCR falló: %s", e)

    raise DoclingNotAvailable(
        "Los 3 métodos de extracción fallaron para este archivo. "
        "Verifica que el PDF no esté protegido, corrupto, o sea imagen con muy poca resolución."
    )


def _extraer_con_docling(path, content_type: str) -> str:
    """Nivel 1: envía a Docling /extract. Devuelve markdown o vacío.

    Raises requests exceptions al caller si la conexión falla.
    """
    url = settings.DOCLING_URL  # http://10.20.0.5:8000/extract
    with open(path, "rb") as f:
        r = requests.post(
            url,
            files={"file": (path.name, f, content_type)},
            timeout=300,  # 5 min para PDFs grandes
        )
    r.raise_for_status()
    data = r.json()
    # Docling puede devolver el markdown en distintas keys según versión
    for key in ("markdown", "md_content", "content", "text", "result"):
        val = data.get(key)
        if val:
            if isinstance(val, list):
                return "\n\n".join(str(v) for v in val)
            return str(val)
    return ""


def _extraer_con_pdfplumber(path) -> str:
    """Nivel 2: extrae texto de PDFs con capa nativa."""
    import pdfplumber

    textos = []
    with pdfplumber.open(str(path)) as pdf:
        for i, page in enumerate(pdf.pages):
            texto = page.extract_text() or ""
            if texto.strip():
                textos.append(texto.strip())
    return "\n\n".join(textos)


def _extraer_con_ocr(path) -> str:
    """Nivel 3: convierte PDF a imágenes (poppler) y aplica Tesseract OCR.

    Usa lang='spa+eng' y PSM 1 (orientación automática de página).
    """
    import pytesseract
    from pdf2image import convert_from_path

    # dpi=300 es buen balance calidad/tiempo para documentos legales
    paginas = convert_from_path(str(path), dpi=300)
    textos = []
    for i, pagina in enumerate(paginas, 1):
        try:
            texto = pytesseract.image_to_string(
                pagina,
                lang="spa+eng",
                config="--psm 1",
            )
        except Exception as e:
            logger.warning("OCR página %d falló: %s", i, e)
            continue
        if texto.strip():
            textos.append(f"## Página {i}\n\n{texto.strip()}")
    return "\n\n".join(textos)


# Alias legacy para no romper imports existentes — delega en extraer_markdown
# pero convierte bytes → archivo temporal.
def extraer_markdown_docling(file_bytes: bytes, filename: str, content_type: str = "application/pdf") -> str:
    """DEPRECATED. Use extraer_markdown(path, content_type). Se mantiene como adaptador."""
    import tempfile
    from pathlib import Path

    with tempfile.NamedTemporaryFile(
        suffix=Path(filename).suffix, delete=False,
    ) as tmp:
        tmp.write(file_bytes)
        tmp_path = tmp.name
    try:
        return extraer_markdown(tmp_path, content_type)
    finally:
        try:
            import os
            os.unlink(tmp_path)
        except Exception:
            pass


# ── Clasificación con Qwen (fase 3) ─────────────────────────────────


_PROMPT_CLASIFICADOR = """Eres un clasificador experto en legislación fiscal, contable, \
financiera y mercantil mexicana.

Analiza el siguiente documento y responde ÚNICAMENTE con un JSON válido, sin \
texto adicional, sin markdown, sin explicaciones.

DOCUMENTO:
{contenido}

INSTRUCCIONES:
1. Determina si este documento es relevante para: legislación fiscal mexicana, \
contabilidad, finanzas empresariales, sociedades mercantiles, SAPIes, NIF, SAT, \
IMSS, INFONAVIT, o temas directamente relacionados.

2. Si NO es relevante, responde:
{{"valido": false, "motivo_rechazo": "explicación breve"}}

3. Si SÍ es relevante, responde:
{{
  "valido": true,
  "titulo": "título descriptivo del documento",
  "categoria": "ley|reglamento|rmf|criterio|guia|jurisprudencia|nif|catalogo|otro",
  "descripcion": "resumen de 2-3 líneas del contenido",
  "año_vigencia": 2026,
  "fecha_publicacion": "YYYY-MM-DD o null",
  "fecha_ultima_revision": "YYYY-MM-DD o null",
  "organismo_emisor": "SAT|SCJN|CINIF|IMSS|SHCP|DOF|otro",
  "temas_clave": ["tema1", "tema2"],
  "aplica_a": ["persona_fisica", "persona_moral"],
  "motivo_rechazo": null
}}
"""


def clasificar_con_qwen(contenido_md: str, max_chars: int = 8000) -> dict:
    """Usa Qwen 2.5 72B en Spark para validar y extraer metadata.

    Returns: dict parsed from JSON response (valido, titulo, categoria, ...).
    Raises:
        SparkNotAvailable — Ollama no responde o timeout
        ClassifierInvalidJSON — respuesta de Qwen no es JSON válido
    """
    import json as _json

    contenido = (contenido_md or "")[:max_chars]
    prompt = _PROMPT_CLASIFICADOR.format(contenido=contenido)

    base = settings.OLLAMA_BASE_URL.rstrip("/")
    url = f"{base}/api/generate"

    try:
        r = requests.post(
            url,
            json={
                "model": settings.OLLAMA_CLASSIFIER_MODEL,
                "prompt": prompt,
                "stream": False,
                "options": {
                    "temperature": 0,
                    "num_predict": 1500,
                },
                "format": "json",  # Ollama forzará JSON válido
            },
            timeout=settings.OLLAMA_CLASSIFIER_TIMEOUT,
        )
        r.raise_for_status()
        data = r.json()
    except requests.exceptions.ConnectionError as e:
        raise SparkNotAvailable(f"Spark no responde ({e})") from e
    except requests.exceptions.Timeout as e:
        raise SparkNotAvailable(
            f"Qwen timeout tras {settings.OLLAMA_CLASSIFIER_TIMEOUT}s"
        ) from e
    except requests.exceptions.HTTPError as e:
        raise SparkNotAvailable(
            f"Ollama HTTP {r.status_code}: {r.text[:300]}"
        ) from e

    raw = (data.get("response") or "").strip()

    # Intentar parsear. Si viene con ``` wrapper lo limpiamos.
    cleaned = raw
    if cleaned.startswith("```"):
        # quitar cerca markdown fence
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:].strip()
        # cortar al último }
        last_brace = cleaned.rfind("}")
        if last_brace > 0:
            cleaned = cleaned[:last_brace + 1]

    try:
        result = _json.loads(cleaned)
    except _json.JSONDecodeError as e:
        raise ClassifierInvalidJSON(
            f"Qwen devolvió JSON inválido: {e}. Raw: {raw[:300]}"
        ) from e

    if not isinstance(result, dict):
        raise ClassifierInvalidJSON(f"Qwen no devolvió dict: {type(result).__name__}")

    return result
