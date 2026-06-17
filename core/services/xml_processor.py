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

    # 4b. Extract nomina data if TipoComprobante == N
    nomina_data = _extract_nomina(xml_bytes) if tipo_comprobante == "N" else {}

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
        # Nómina (vacíos si no es tipo N)
        fecha_pago_nomina=nomina_data.get("fecha_pago"),
        fecha_inicial_pago=nomina_data.get("fecha_inicial_pago"),
        fecha_final_pago=nomina_data.get("fecha_final_pago"),
        tipo_nomina=nomina_data.get("tipo_nomina", ""),
    )

    logger.info("💾 Stored CFDI %s | %s → %s | $%s", uuid[:8], rfc_emisor, rfc_receptor, total)
    return "created"


def _extract_nomina(xml_bytes: bytes) -> dict:
    """Extract payroll-specific fields from nomina12:Nomina complement.

    Returns dict with: fecha_pago (date|None), fecha_inicial_pago, fecha_final_pago, tipo_nomina ('O'|'E'|'')
    """
    from datetime import date

    result = {
        "fecha_pago": None,
        "fecha_inicial_pago": None,
        "fecha_final_pago": None,
        "tipo_nomina": "",
    }

    try:
        root = etree.fromstring(xml_bytes)
    except etree.XMLSyntaxError:
        return result

    # nomina12 o nomina13 — namespace variable
    ns_candidates = [
        "{http://www.sat.gob.mx/nomina12}Nomina",
        "{http://www.sat.gob.mx/nomina13}Nomina",
    ]
    nom = None
    for ns in ns_candidates:
        for elem in root.iter(ns):
            nom = elem
            break
        if nom is not None:
            break

    if nom is None:
        return result

    def _parse_date(s):
        if not s: return None
        try: return date.fromisoformat(s[:10])
        except (ValueError, TypeError): return None

    result["fecha_pago"] = _parse_date(nom.get("FechaPago"))
    result["fecha_inicial_pago"] = _parse_date(nom.get("FechaInicialPago"))
    result["fecha_final_pago"] = _parse_date(nom.get("FechaFinalPago"))
    tn = nom.get("TipoNomina", "").strip().upper()
    if tn in ("O", "E"):
        result["tipo_nomina"] = tn

    return result


def extract_cfdi_atributos_basicos(xml_bytes: bytes) -> dict:
    """Bloque C: extrae atributos sueltos del CFDI raiz que el parser previo
    no leia.

    Returns: dict con regimen_fiscal_emisor, regimen_fiscal_receptor, uso_cfdi.
    Tolera atributos ausentes (default '').
    """
    out = {"regimen_fiscal_emisor": "", "regimen_fiscal_receptor": "", "uso_cfdi": ""}
    try:
        root = etree.fromstring(xml_bytes)
    except etree.XMLSyntaxError:
        return out
    tag = root.tag.lower()
    ns = NS_40 if "cfd/4" in tag else NS_33
    emisor = root.find("cfdi:Emisor", namespaces=ns)
    if emisor is not None:
        out["regimen_fiscal_emisor"] = (emisor.get("RegimenFiscal") or "")[:10]
    receptor = root.find("cfdi:Receptor", namespaces=ns)
    if receptor is not None:
        out["regimen_fiscal_receptor"] = (receptor.get("RegimenFiscalReceptor") or "")[:5]
        out["uso_cfdi"] = (receptor.get("UsoCFDI") or "")[:10]
    return out


def extract_cfdi_relacionados(xml_bytes: bytes) -> list:
    """Bloque C: extrae <cfdi:CfdiRelacionados> en lista de relaciones.

    Returns: list[dict{uuid_relacionado, tipo_relacion}]. Vacia si no hay
    nodo CfdiRelacionados o el XML no parsea. Para v4.0 el SAT permite UN solo
    nodo CfdiRelacionados (con un TipoRelacion) con N CfdiRelacionado dentro.
    """
    rows = []
    try:
        root = etree.fromstring(xml_bytes)
    except etree.XMLSyntaxError:
        return rows
    tag = root.tag.lower()
    ns = NS_40 if "cfd/4" in tag else NS_33
    # Pueden existir multiples nodos CfdiRelacionados (v4.0 permite varios con distinto TipoRelacion)
    for rels in root.findall("cfdi:CfdiRelacionados", namespaces=ns):
        tipo_relacion = (rels.get("TipoRelacion") or "")[:2]
        if not tipo_relacion:
            continue
        for rel in rels.findall("cfdi:CfdiRelacionado", namespaces=ns):
            uuid_rel = (rel.get("UUID") or "").strip().upper()
            if not uuid_rel:
                continue
            rows.append({"uuid_relacionado": uuid_rel, "tipo_relacion": tipo_relacion})
    return rows


def extract_nomina12_detalle(xml_bytes: bytes) -> dict | None:
    """Extrae los totales fiscales del complemento nomina12:Nomina.

    Devuelve un dict con las llaves del modelo NominaDetalle, listo para
    crear/actualizar la fila. Soporta nomina12 y nomina13. Tolera
    atributos/nodos ausentes (defaults Decimal('0') / '').

    Returns None si el XML no tiene complemento nomina (e.g. no es tipo='N').
    No extrae datos personales del trabajador (CURP, NSS, salarios, banco).
    """
    try:
        root = etree.fromstring(xml_bytes)
    except etree.XMLSyntaxError:
        return None

    ns_candidates = [
        ("http://www.sat.gob.mx/nomina12", "1.2"),
        ("http://www.sat.gob.mx/nomina13", "1.3"),
    ]
    nom = None
    ns_used = None
    version_fallback = "1.2"
    for ns_uri, ver in ns_candidates:
        elem = root.find(f".//{{{ns_uri}}}Nomina")
        if elem is not None:
            nom = elem
            ns_used = ns_uri
            version_fallback = ver
            break
    if nom is None:
        return None

    def _dec(v):
        return _safe_decimal(v if v not in (None, "") else "0")

    out = {
        "version": nom.get("Version", version_fallback),
        "num_dias_pagados": _dec(nom.get("NumDiasPagados")),
        "total_percepciones": _dec(nom.get("TotalPercepciones")),
        "total_deducciones": _dec(nom.get("TotalDeducciones")),
        "total_otros_pagos": _dec(nom.get("TotalOtrosPagos")),
        "total_sueldos": Decimal("0"),
        "total_gravado": Decimal("0"),
        "total_exento": Decimal("0"),
        "total_separacion_indemnizacion": Decimal("0"),
        "total_jubilacion_pension": Decimal("0"),
        "total_impuestos_retenidos_nomina": Decimal("0"),
        "total_otras_deducciones": Decimal("0"),
        "subsidio_causado": Decimal("0"),
        "tipo_regimen": "",
        "periodicidad_pago": "",
        "registro_patronal": "",
    }

    percep = nom.find(f"{{{ns_used}}}Percepciones")
    if percep is not None:
        out["total_sueldos"] = _dec(percep.get("TotalSueldos"))
        out["total_gravado"] = _dec(percep.get("TotalGravado"))
        out["total_exento"] = _dec(percep.get("TotalExento"))
        out["total_separacion_indemnizacion"] = _dec(percep.get("TotalSeparacionIndemnizacion"))
        out["total_jubilacion_pension"] = _dec(percep.get("TotalJubilacionPensionRetiro"))

    deduc = nom.find(f"{{{ns_used}}}Deducciones")
    if deduc is not None:
        out["total_impuestos_retenidos_nomina"] = _dec(deduc.get("TotalImpuestosRetenidos"))
        out["total_otras_deducciones"] = _dec(deduc.get("TotalOtrasDeducciones"))

    subsidio = nom.find(f"{{{ns_used}}}OtrosPagos/{{{ns_used}}}OtroPago/{{{ns_used}}}SubsidioAlEmpleo")
    if subsidio is None:
        subsidio = nom.find(f".//{{{ns_used}}}SubsidioAlEmpleo")
    if subsidio is not None:
        out["subsidio_causado"] = _dec(subsidio.get("SubsidioCausado"))

    receptor = nom.find(f"{{{ns_used}}}Receptor")
    if receptor is not None:
        out["tipo_regimen"] = receptor.get("TipoRegimen", "")[:10]
        out["periodicidad_pago"] = receptor.get("PeriodicidadPago", "")[:10]

    emisor = nom.find(f"{{{ns_used}}}Emisor")
    if emisor is not None:
        out["registro_patronal"] = emisor.get("RegistroPatronal", "")[:30]

    return out


def extract_pago20(xml_bytes: bytes) -> list:
    """Extrae el complemento pago20:Pagos en una lista de filas DoctoRelacionado.

    Cada fila representa un <pago20:DoctoRelacionado> con los datos de su
    <pago20:Pago> padre denormalizados. Soporta pago20 y pago22.

    Returns: list[dict] con las llaves del modelo PagoDoctoRelacionado.
    Vacia si el XML no tiene complemento de pago o falla el parseo.
    """
    rows = []
    try:
        root = etree.fromstring(xml_bytes)
    except etree.XMLSyntaxError:
        return rows

    ns_pago_candidates = [
        "{http://www.sat.gob.mx/Pagos20}",
        "{http://www.sat.gob.mx/Pagos22}",
    ]
    pagos_root = None
    for ns_p in ns_pago_candidates:
        for elem in root.iter(f"{ns_p}Pagos"):
            pagos_root = elem
            ns_used = ns_p
            break
        if pagos_root is not None:
            break
    if pagos_root is None:
        return rows

    def _dec(s, default="0"):
        return _safe_decimal(s if s not in (None, "") else default)

    def _dt(s):
        if not s:
            return None
        try:
            return datetime.fromisoformat(s)
        except (ValueError, TypeError):
            return None

    pago_elems = pagos_root.findall(f"{ns_used}Pago")
    for p_idx, pago in enumerate(pago_elems, start=1):
        pago_fecha = _dt(pago.get("FechaPago"))
        if pago_fecha is None:
            continue
        pago_forma = pago.get("FormaDePagoP", "")
        pago_moneda = pago.get("MonedaP", "MXN")
        pago_tipo_cambio = _dec(pago.get("TipoCambioP"), "1")
        pago_monto = _dec(pago.get("Monto"))
        pago_num_operacion = pago.get("NumOperacion", "")
        doctos = pago.findall(f"{ns_used}DoctoRelacionado")
        for d_idx, dr in enumerate(doctos, start=1):
            id_doc = (dr.get("IdDocumento") or "").strip().upper()
            if not id_doc:
                continue
            rows.append({
                "pago_index": p_idx,
                "docto_index": d_idx,
                "pago_fecha": pago_fecha,
                "pago_forma": pago_forma,
                "pago_moneda": pago_moneda,
                "pago_tipo_cambio": pago_tipo_cambio,
                "pago_monto": pago_monto,
                "pago_num_operacion": pago_num_operacion,
                "id_documento": id_doc,
                "folio": dr.get("Folio", ""),
                "serie": dr.get("Serie", ""),
                "moneda_dr": dr.get("MonedaDR", "MXN"),
                "equivalencia_dr": _dec(dr.get("EquivalenciaDR"), "1"),
                "num_parcialidad": int(dr.get("NumParcialidad") or 1),
                "imp_saldo_anterior": _dec(dr.get("ImpSaldoAnt")),
                "imp_pagado": _dec(dr.get("ImpPagado")),
                "imp_saldo_insoluto": _dec(dr.get("ImpSaldoInsoluto")),
                "objeto_imp_dr": dr.get("ObjetoImpDR", ""),
            })
    return rows


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
