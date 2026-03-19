"""CSF Parser — Extracts structured data from Constancia de Situación Fiscal PDF.

Primary: Docling API on nodo5 (if available)
Fallback: pdfplumber + regex extraction
"""

import io
import logging
import re

logger = logging.getLogger("core.csf_parser")

# Docling endpoint — to be confirmed
DOCLING_URL = "http://10.20.0.5:8000/extract"


def parsear_csf_con_docling(pdf_bytes):
    """Send PDF to Docling API and extract structured CSF data."""
    # Try Docling first
    try:
        import requests

        response = requests.post(
            DOCLING_URL,
            files={"file": ("csf.pdf", pdf_bytes, "application/pdf")},
            timeout=60,
        )
        if response.ok:
            data = response.json()
            result = _extraer_campos_csf(data)
            if result:
                logger.info("CSF parsed via Docling: %d fields", len(result))
                return result
    except Exception as e:
        logger.info("Docling unavailable (%s), falling back to pdfplumber", e)

    # Fallback: pdfplumber
    return _parsear_con_pdfplumber(pdf_bytes)


def _parsear_con_pdfplumber(pdf_bytes):
    """Parse CSF PDF using pdfplumber + regex."""
    import pdfplumber

    datos = {}

    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            text = ""
            for page in pdf.pages:
                text += (page.extract_text() or "") + "\n"
    except Exception as e:
        logger.error("Failed to open PDF with pdfplumber: %s", e)
        return datos

    if not text.strip():
        logger.warning("CSF PDF has no extractable text")
        return datos

    logger.info("CSF text extracted: %d chars", len(text))

    # ── RFC ──
    rfc_match = re.search(r"RFC:\s*([A-ZÑ&]{3,4}\d{6}[A-Z0-9]{3})", text)
    if rfc_match:
        datos["rfc"] = rfc_match.group(1)

    # ── Razón Social / Denominación ──
    for pattern in [
        r"(?:Denominación|Razón Social)[:/]?\s*(.+)",
        r"Nombre[\s,]*Denominación[\s,]*o[\s,]*Razón Social[:/]?\s*(.+)",
        r"(?:Nombre del Contribuyente)[:/]?\s*(.+)",
    ]:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            val = match.group(1).strip()
            # Clean trailing labels
            val = re.split(r"\s{2,}|Régimen|Nombre Comercial", val)[0].strip()
            if val and len(val) > 2:
                datos["razon_social"] = val
                break

    # ── Régimen Capital ──
    regcap = re.search(r"Régimen Capital[:/]?\s*(.+)", text, re.IGNORECASE)
    if regcap:
        datos["regimen_capital"] = regcap.group(1).strip().split("\n")[0].strip()

    # ── Nombre Comercial ──
    nomcom = re.search(r"Nombre Comercial[:/]?\s*(.+)", text, re.IGNORECASE)
    if nomcom:
        datos["nombre_comercial"] = nomcom.group(1).strip().split("\n")[0].strip()

    # ── Código Postal ──
    cp = re.search(r"Código Postal[:/]?\s*(\d{5})", text, re.IGNORECASE)
    if cp:
        datos["codigo_postal"] = cp.group(1)

    # ── Dirección fields ──
    address_fields = [
        ("calle", r"(?:Nombre de Vialidad|Calle|Tipo de Vialidad.*?Nombre)[:/]?\s*(.+)"),
        ("num_exterior", r"Número Exterior[:/]?\s*(.+)"),
        ("num_interior", r"Número Interior[:/]?\s*(.+)"),
        ("colonia", r"(?:Nombre de la Colonia|Colonia)[:/]?\s*(.+)"),
        ("localidad", r"(?:Nombre de la Localidad|Localidad)[:/]?\s*(.+)"),
        ("municipio", r"(?:Municipio|Demarcación Territorial|Alcaldía)[:/]?\s*(.+)"),
        ("estado", r"(?:Entidad Federativa|Nombre de la Entidad|Estado)[:/]?\s*(.+)"),
    ]
    for field_name, pattern in address_fields:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            val = match.group(1).strip().split("\n")[0].strip()
            # Clean trailing labels
            val = re.split(r"\s{2,}", val)[0].strip()
            if val and val.lower() not in ("", "---", "n/a"):
                datos[field_name] = val

    # ── Fecha inicio operaciones ──
    fecha = re.search(
        r"(?:Fecha de Inicio|Fecha inicio de operaciones)[:/]?\s*(\d{1,2}[\s/.-]\w+[\s/.-]\d{4}|\d{4}[\s/.-]\d{2}[\s/.-]\d{2})",
        text,
        re.IGNORECASE,
    )
    if fecha:
        datos["fecha_inicio"] = fecha.group(1).strip()

    # ── Estatus en el padrón ──
    estatus = re.search(
        r"(?:Estatus en el padrón|Situación del contribuyente)[:/]?\s*(.+)",
        text,
        re.IGNORECASE,
    )
    if estatus:
        datos["estatus_padron"] = estatus.group(1).strip().split("\n")[0].strip()

    # ── Régimen fiscal ──
    regimen = re.search(
        r"(?:Régimen|Regímenes)[^:]*?[:/]?\s*(\d{3}\s*[-—–]\s*.+)",
        text,
    )
    if regimen:
        datos["regimen_fiscal"] = regimen.group(1).strip().split("\n")[0].strip()

    # ── Actividades económicas ──
    actividades = []
    for match in re.finditer(
        r"(\d{4,6})\s*[-—–]\s*(.+?)(?:\n|$)", text
    ):
        code = match.group(1).strip()
        desc = match.group(2).strip()
        if len(code) >= 4:
            actividades.append(f"{code} — {desc}")
    if actividades:
        datos["actividades"] = actividades

    logger.info("CSF parsed via pdfplumber: %s", list(datos.keys()))
    return datos


def _extraer_campos_csf(docling_response):
    """Extract CSF fields from Docling API response."""
    # Adapt based on Docling response format
    if isinstance(docling_response, dict):
        return docling_response
    return {}
