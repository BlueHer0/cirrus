"""XML Processor — Parse downloaded CFDI XMLs and persist to MinIO + PostgreSQL.

After SATEngine downloads XMLs to a temp directory, this module:
1. Walks the temp dir for .xml files
2. Parses each XML using sat_scrapper_core.storage.parse_cfdi_xml
3. Deduplicates against PostgreSQL (check UUID before inserting)
4. Uploads XML bytes to MinIO under cfdis/{rfc}/{year}/{month}/{tipo}/{uuid}.xml
5. Creates/updates CFDI model records in PostgreSQL with tax data
"""

import logging
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from lxml import etree

from sat_scrapper_core.storage import parse_cfdi_xml

from core.models import CFDI
from core.services.storage_minio import upload_cfdi_xml

logger = logging.getLogger("core.xml_processor")

# Namespaces for direct XML tax extraction
NS_40 = {"cfdi": "http://www.sat.gob.mx/cfd/4", "tfd": "http://www.sat.gob.mx/TimbreFiscalDigital"}
NS_33 = {"cfdi": "http://www.sat.gob.mx/cfd/3", "tfd": "http://www.sat.gob.mx/TimbreFiscalDigital"}

# Map SAT TipoDeComprobante codes to our model choices
TIPO_MAP = {
    "I": "I",  # Ingreso
    "E": "E",  # Egreso
    "T": "T",  # Traslado
    "N": "N",  # Nómina
    "P": "P",  # Pago
}


def process_downloaded_xmls(download_dir: str, empresa) -> int:
    """Process all XMLs in a download directory.

    Walks the directory recursively for .xml files, parses them,
    deduplicates against PostgreSQL, and uploads new ones to MinIO.

    Args:
        download_dir: Path to directory containing downloaded XMLs
        empresa: Empresa model instance

    Returns:
        Number of new CFDIs processed and stored
    """
    download_path = Path(download_dir)
    xml_files = list(download_path.rglob("*.xml"))
    logger.info("📂 Found %d XML files in %s", len(xml_files), download_dir)

    processed = 0
    skipped = 0
    errors = 0

    for xml_path in xml_files:
        try:
            xml_bytes = xml_path.read_bytes()
            result = process_single_xml(xml_bytes, empresa)
            if result == "created":
                processed += 1
            elif result == "exists":
                skipped += 1
            else:
                errors += 1
        except Exception as e:
            logger.error("❌ Error processing %s: %s", xml_path.name, e)
            errors += 1

    logger.info(
        "✅ XML processing complete: %d new, %d skipped (dupes), %d errors",
        processed, skipped, errors,
    )
    return processed


def process_single_xml(xml_bytes: bytes, empresa) -> str:
    """Process a single CFDI XML.

    Returns:
        "created" — new CFDI created in DB + uploaded to MinIO
        "exists" — UUID already exists (dedup)
        "error" — parsing or processing failed
    """
    # 1. Parse XML metadata
    metadata = parse_cfdi_xml(xml_bytes)
    if not metadata:
        return "error"

    uuid = metadata.get("UUID", "").strip().upper()
    if not uuid:
        logger.warning("⚠️ XML without UUID, skipping")
        return "error"

    # 2. Deduplication — check PostgreSQL
    if CFDI.objects.filter(uuid=uuid).exists():
        logger.debug("⏭️ UUID %s already in DB", uuid[:8])
        return "exists"

    # 3. Extract fields for the CFDI model
    fecha_str = metadata.get("Fecha", "")
    try:
        fecha = datetime.fromisoformat(fecha_str)
    except (ValueError, TypeError):
        fecha = None

    tipo_comprobante = TIPO_MAP.get(metadata.get("TipoDeComprobante", ""), "I")

    # Determine direction: if empresa RFC == emisor RFC → emitido, else → recibido
    rfc_emisor = metadata.get("RfcEmisor", "")
    rfc_receptor = metadata.get("RfcReceptor", "")
    if rfc_emisor.upper() == empresa.rfc.upper():
        tipo_relacion = "emitido"
    else:
        tipo_relacion = "recibido"

    # Parse monetary values safely
    total = _safe_decimal(metadata.get("Total", "0"))
    subtotal = _safe_decimal(metadata.get("SubTotal", "0"))

    # 4. Extract tax data directly from XML
    taxes = _extract_taxes(xml_bytes)

    # 5. Upload XML to MinIO
    year = fecha.year if fecha else datetime.now().year
    month = fecha.month if fecha else datetime.now().month
    minio_key = upload_cfdi_xml(
        rfc=empresa.rfc,
        year=year,
        month=month,
        tipo=tipo_relacion,
        uuid=uuid,
        xml_data=xml_bytes,
    )

    # 6. Create CFDI record in PostgreSQL
    cfdi = CFDI.objects.create(
        uuid=uuid,
        rfc_empresa=empresa.rfc,
        empresa=empresa,
        tipo_relacion=tipo_relacion,
        version=metadata.get("Version", "4.0"),
        fecha=fecha,
        serie=metadata.get("Serie", ""),
        folio=metadata.get("Folio", ""),
        total=total,
        subtotal=subtotal,
        moneda=metadata.get("Moneda", "MXN"),
        tipo_comprobante=tipo_comprobante,
        forma_pago=metadata.get("FormaPago", ""),
        metodo_pago=metadata.get("MetodoPago", ""),
        rfc_emisor=rfc_emisor,
        nombre_emisor=metadata.get("NombreEmisor", ""),
        regimen_fiscal_emisor=metadata.get("RegimenFiscalEmisor", ""),
        rfc_receptor=rfc_receptor,
        nombre_receptor=metadata.get("NombreReceptor", ""),
        uso_cfdi=metadata.get("UsoCFDI", ""),
        total_impuestos_trasladados=taxes["total_trasladados"],
        total_impuestos_retenidos=taxes["total_retenidos"],
        iva=taxes["iva"],
        isr_retenido=taxes["isr_retenido"],
        iva_retenido=taxes["iva_retenido"],
        xml_minio_key=minio_key,
        xml_size_bytes=len(xml_bytes),
    )

    logger.info("💾 Stored CFDI %s | %s → %s | $%s", uuid[:8], rfc_emisor, rfc_receptor, total)
    return "created"


def _extract_taxes(xml_bytes: bytes) -> dict:
    """Extract tax totals directly from XML.

    Returns dict with: total_trasladados, total_retenidos, iva, isr_retenido, iva_retenido
    """
    result = {
        "total_trasladados": Decimal("0"),
        "total_retenidos": Decimal("0"),
        "iva": Decimal("0"),
        "isr_retenido": Decimal("0"),
        "iva_retenido": Decimal("0"),
    }

    try:
        root = etree.fromstring(xml_bytes)
    except Exception:
        return result

    # Detect namespace
    tag = root.tag.lower()
    ns = NS_40 if "cfd/4" in tag else NS_33

    # Global tax totals
    impuestos = root.find("cfdi:Impuestos", namespaces=ns)
    if impuestos is not None:
        result["total_trasladados"] = _safe_decimal(
            impuestos.get("TotalImpuestosTrasladados", "0")
        )
        result["total_retenidos"] = _safe_decimal(
            impuestos.get("TotalImpuestosRetenidos", "0")
        )

    # IVA trasladado (Impuesto="002")
    for traslado in root.findall("cfdi:Impuestos/cfdi:Traslados/cfdi:Traslado", namespaces=ns):
        if traslado.get("Impuesto") == "002":
            result["iva"] += _safe_decimal(traslado.get("Importe", "0"))

    # Retenciones
    for retencion in root.findall("cfdi:Impuestos/cfdi:Retenciones/cfdi:Retencion", namespaces=ns):
        imp = retencion.get("Impuesto", "")
        importe = _safe_decimal(retencion.get("Importe", "0"))
        if imp == "001":  # ISR
            result["isr_retenido"] += importe
        elif imp == "002":  # IVA
            result["iva_retenido"] += importe

    return result


def _safe_decimal(value: str) -> Decimal:
    """Safely parse a string to Decimal."""
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, ValueError, TypeError):
        return Decimal("0")
