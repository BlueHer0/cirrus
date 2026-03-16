"""Parse CFDI 3.3 y 4.0 XML into normalized dictionaries for PDF rendering.

Standalone — no depende de Django.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from lxml import etree


# Namespaces
NS_40 = {
    "cfdi": "http://www.sat.gob.mx/cfd/4",
    "tfd": "http://www.sat.gob.mx/TimbreFiscalDigital",
}
NS_33 = {
    "cfdi": "http://www.sat.gob.mx/cfd/3",
    "tfd": "http://www.sat.gob.mx/TimbreFiscalDigital",
}


@dataclass
class ParsedCFDI:
    comprobante: dict[str, Any]
    emisor: dict[str, Any]
    receptor: dict[str, Any]
    conceptos: list[dict[str, Any]]
    impuestos: dict[str, Any]
    relacionados: list[dict[str, Any]]
    timbre: dict[str, Any]
    cadena_tfd: str


class CFDIParseError(Exception):
    """Error al parsear un XML de CFDI."""


def parse_cfdi_xml(xml_bytes: bytes) -> ParsedCFDI:
    """Parsea un XML de CFDI (3.3 o 4.0) y retorna estructura normalizada."""
    try:
        root = etree.fromstring(xml_bytes)
    except etree.XMLSyntaxError as exc:
        raise CFDIParseError(f"XML invalido: {exc}") from exc

    # Detectar version por namespace
    ns = _detect_namespace(root)

    # Timbre fiscal digital
    tfd = root.find(".//tfd:TimbreFiscalDigital", namespaces=ns)
    if tfd is None:
        raise CFDIParseError("El CFDI no contiene timbre fiscal digital")

    # Comprobante
    fecha = _parse_datetime(root.get("Fecha"))
    comprobante = {
        "version": root.get("Version", root.get("version", "")),
        "serie": root.get("Serie", ""),
        "folio": root.get("Folio", ""),
        "fecha": fecha,
        "moneda": root.get("Moneda", "MXN"),
        "tipo_cambio": root.get("TipoCambio", ""),
        "lugar_expedicion": root.get("LugarExpedicion", ""),
        "tipo_comprobante": root.get("TipoDeComprobante", ""),
        "forma_pago": root.get("FormaPago", ""),
        "metodo_pago": root.get("MetodoPago", ""),
        "condiciones_pago": root.get("CondicionesDePago", ""),
        "sub_total": Decimal(root.get("SubTotal", "0")),
        "descuento": _decimal_or_none(root.get("Descuento")),
        "total": Decimal(root.get("Total", "0")),
        "exportacion": root.get("Exportacion", ""),
        "exportacion_desc": _catalogo_exportacion(root.get("Exportacion")),
        "no_certificado": root.get("NoCertificado", ""),
        "sello": root.get("Sello", ""),
        "certificado": root.get("Certificado", ""),
        "forma_pago_desc": _catalogo_forma_pago(root.get("FormaPago")),
        "metodo_pago_desc": _catalogo_metodo_pago(root.get("MetodoPago")),
        "tipo_comprobante_desc": _catalogo_tipo_comprobante(root.get("TipoDeComprobante")),
        "xml_checksum": hashlib.sha256(xml_bytes).hexdigest(),
    }

    # Emisor
    emisor_node = root.find("cfdi:Emisor", namespaces=ns)
    emisor = _parse_persona(emisor_node) if emisor_node is not None else {}
    if emisor.get("regimen_fiscal"):
        emisor["regimen_fiscal_desc"] = _catalogo_regimen_fiscal(emisor["regimen_fiscal"])

    # Receptor
    receptor_node = root.find("cfdi:Receptor", namespaces=ns)
    receptor = {}
    if receptor_node is not None:
        receptor = _parse_persona(receptor_node)
        receptor["domicilio_fiscal"] = receptor_node.get("DomicilioFiscalReceptor", "")
        receptor["uso_cfdi"] = receptor_node.get("UsoCFDI", "")
        receptor["uso_cfdi_desc"] = _catalogo_uso_cfdi(receptor_node.get("UsoCFDI"))
        receptor["regimen_fiscal_receptor"] = receptor_node.get("RegimenFiscalReceptor", "")
        receptor["regimen_fiscal_receptor_desc"] = _catalogo_regimen_fiscal(
            receptor_node.get("RegimenFiscalReceptor")
        )

    # Conceptos
    conceptos: list[dict[str, Any]] = []
    impuestos_acumulados: dict[str, list] = {"traslados": [], "retenciones": []}

    for idx, concepto in enumerate(
        root.findall("cfdi:Conceptos/cfdi:Concepto", namespaces=ns), start=1
    ):
        concept_dict = _parse_concepto(concepto, ns, idx)
        conceptos.append(concept_dict)

        # Acumular impuestos
        for t in concept_dict["traslados"]:
            impuestos_acumulados["traslados"].append(t)
        for r in concept_dict["retenciones"]:
            impuestos_acumulados["retenciones"].append(r)

    # Impuestos globales del comprobante (si existen, son mas precisos)
    imp_globales = _parse_impuestos_globales(root, ns)
    if imp_globales["traslados"] or imp_globales["retenciones"]:
        impuestos = imp_globales
    else:
        impuestos = impuestos_acumulados

    # Relacionados
    relacionados = _parse_relacionados(root, ns)

    # Timbre
    timbre = {
        "uuid": tfd.get("UUID", ""),
        "version": tfd.get("Version", ""),
        "fecha_timbrado": _parse_datetime(tfd.get("FechaTimbrado")),
        "rfc_prov_certif": tfd.get("RfcProvCertif", ""),
        "no_certificado_sat": tfd.get("NoCertificadoSAT", ""),
        "sello_cfd": tfd.get("SelloCFD", ""),
        "sello_sat": tfd.get("SelloSAT", ""),
    }

    cadena_tfd = (
        f"||{tfd.get('Version')}|{tfd.get('UUID')}|{tfd.get('FechaTimbrado')}"
        f"|{tfd.get('RfcProvCertif')}|{tfd.get('SelloCFD')}"
        f"|{tfd.get('NoCertificadoSAT')}||"
    )

    return ParsedCFDI(
        comprobante=comprobante,
        emisor=emisor,
        receptor=receptor,
        conceptos=conceptos,
        impuestos=impuestos,
        relacionados=relacionados,
        timbre=timbre,
        cadena_tfd=cadena_tfd,
    )


# --- Helpers internos ---

def _detect_namespace(root) -> dict:
    """Detecta si es CFDI 3.3 o 4.0 por namespace del tag raiz."""
    tag = root.tag.lower()
    if "cfd/4" in tag:
        return NS_40
    elif "cfd/3" in tag:
        return NS_33
    # Fallback por atributo Version
    ver = root.get("Version", root.get("version", ""))
    return NS_40 if ver.startswith("4") else NS_33


def _parse_persona(node) -> dict[str, Any]:
    return {
        "rfc": node.get("Rfc", node.get("rfc", "")),
        "nombre": node.get("Nombre", node.get("nombre", "")),
        "regimen_fiscal": node.get("RegimenFiscal", node.get("RegimenFiscalReceptor", "")),
    }


def _parse_concepto(concepto, ns: dict, idx: int) -> dict[str, Any]:
    concept_dict: dict[str, Any] = {
        "index": idx,
        "clave_prod_serv": concepto.get("ClaveProdServ", ""),
        "descripcion": concepto.get("Descripcion", ""),
        "clave_unidad": concepto.get("ClaveUnidad", ""),
        "unidad": concepto.get("Unidad", ""),
        "cantidad": Decimal(concepto.get("Cantidad", "0")),
        "valor_unitario": Decimal(concepto.get("ValorUnitario", "0")),
        "importe": Decimal(concepto.get("Importe", "0")),
        "descuento": _decimal_or_none(concepto.get("Descuento")),
        "objeto_impuesto": concepto.get("ObjetoImp", ""),
        "objeto_impuesto_desc": _catalogo_objeto_impuesto(concepto.get("ObjetoImp")),
        "traslados": [],
        "retenciones": [],
    }

    for traslado in concepto.findall(
        "cfdi:Impuestos/cfdi:Traslados/cfdi:Traslado", namespaces=ns
    ):
        concept_dict["traslados"].append({
            "impuesto": traslado.get("Impuesto", ""),
            "tipo_factor": traslado.get("TipoFactor", ""),
            "tasa": traslado.get("TasaOCuota", ""),
            "importe": _decimal_or_none(traslado.get("Importe")),
            "base": _decimal_or_none(traslado.get("Base")),
        })

    for retencion in concepto.findall(
        "cfdi:Impuestos/cfdi:Retenciones/cfdi:Retencion", namespaces=ns
    ):
        concept_dict["retenciones"].append({
            "impuesto": retencion.get("Impuesto", ""),
            "tipo_factor": retencion.get("TipoFactor", ""),
            "tasa": retencion.get("TasaOCuota", ""),
            "importe": _decimal_or_none(retencion.get("Importe")),
            "base": _decimal_or_none(retencion.get("Base")),
        })

    return concept_dict


def _parse_impuestos_globales(root, ns: dict) -> dict[str, list]:
    """Parsea el nodo Impuestos del comprobante (totales)."""
    result: dict[str, list] = {"traslados": [], "retenciones": []}

    for traslado in root.findall("cfdi:Impuestos/cfdi:Traslados/cfdi:Traslado", namespaces=ns):
        result["traslados"].append({
            "impuesto": traslado.get("Impuesto", ""),
            "tipo_factor": traslado.get("TipoFactor", ""),
            "tasa": traslado.get("TasaOCuota", ""),
            "importe": _decimal_or_none(traslado.get("Importe")),
            "base": _decimal_or_none(traslado.get("Base")),
        })

    for retencion in root.findall("cfdi:Impuestos/cfdi:Retenciones/cfdi:Retencion", namespaces=ns):
        result["retenciones"].append({
            "impuesto": retencion.get("Impuesto", ""),
            "tipo_factor": retencion.get("TipoFactor", ""),
            "tasa": retencion.get("TasaOCuota", ""),
            "importe": _decimal_or_none(retencion.get("Importe")),
            "base": _decimal_or_none(retencion.get("Base")),
        })

    return result


def _parse_relacionados(root, ns: dict) -> list[dict[str, Any]]:
    relacionados = []
    for rel in root.findall("cfdi:CfdiRelacionados", namespaces=ns):
        tipo_relacion = rel.get("TipoRelacion", "")
        for uuid_node in rel.findall("cfdi:CfdiRelacionado", namespaces=ns):
            relacionados.append({
                "tipo_relacion": tipo_relacion,
                "uuid": uuid_node.get("UUID", ""),
            })
    return relacionados


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _decimal_or_none(value: str | None) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(value)
    except Exception:
        return None


# --- Catalogos SAT ---

def _catalogo_forma_pago(clave: str | None) -> str:
    catalogo = {
        "01": "Efectivo", "02": "Cheque nominativo", "03": "Transferencia electronica",
        "04": "Tarjeta de credito", "05": "Monedero electronico", "06": "Dinero electronico",
        "08": "Vales de despensa", "12": "Dacion en pago", "13": "Pago por subrogacion",
        "14": "Pago por consignacion", "15": "Condonacion", "17": "Compensacion",
        "23": "Novacion", "24": "Confusion", "25": "Remision de deuda",
        "26": "Prescripcion o caducidad", "27": "A satisfaccion del acreedor",
        "28": "Tarjeta de debito", "29": "Tarjeta de servicios", "30": "Aplicacion de anticipos",
        "31": "Intermediario pagos", "99": "Por definir",
    }
    return catalogo.get(clave or "", "")


def _catalogo_metodo_pago(clave: str | None) -> str:
    catalogo = {
        "PUE": "Pago en una sola exhibicion",
        "PPD": "Pago en parcialidades o diferido",
    }
    return catalogo.get(clave or "", "")


def _catalogo_tipo_comprobante(clave: str | None) -> str:
    catalogo = {
        "I": "Ingreso", "E": "Egreso", "T": "Traslado",
        "N": "Nomina", "P": "Pago",
    }
    return catalogo.get(clave or "", "")


def _catalogo_uso_cfdi(clave: str | None) -> str:
    catalogo = {
        "G01": "Adquisicion de mercancias", "G02": "Devoluciones, descuentos o bonificaciones",
        "G03": "Gastos en general", "I01": "Construcciones", "I02": "Mobiliario y equipo de oficina",
        "I03": "Equipo de transporte", "I04": "Equipo de computo y accesorios",
        "I05": "Dados, troqueles, moldes, matrices y herramental",
        "I06": "Comunicaciones telefonicas", "I07": "Comunicaciones satelitales",
        "I08": "Otra maquinaria y equipo", "D01": "Honorarios medicos, dentales y gastos hospitalarios",
        "D02": "Gastos medicos por incapacidad o discapacidad",
        "D03": "Gastos funerales", "D04": "Donativos",
        "D05": "Intereses reales efectivamente pagados por creditos hipotecarios",
        "D06": "Aportaciones voluntarias al SAR", "D07": "Primas por seguros de gastos medicos",
        "D08": "Gastos de transportacion escolar obligatoria",
        "D09": "Depositos en cuentas para el ahorro, primas de pensiones",
        "D10": "Pagos por servicios educativos (colegiaturas)",
        "S01": "Sin efectos fiscales", "CP01": "Pagos", "CN01": "Nomina",
    }
    return catalogo.get(clave or "", "")


def _catalogo_exportacion(clave: str | None) -> str:
    catalogo = {
        "01": "No aplica",
        "02": "Definitiva",
        "03": "Temporal",
        "04": "Definitiva con clave distinta a A1",
    }
    return catalogo.get(clave or "", "")


def _catalogo_regimen_fiscal(clave: str | None) -> str:
    catalogo = {
        "601": "General de Ley Personas Morales",
        "603": "Personas Morales con Fines no Lucrativos",
        "605": "Sueldos y Salarios e Ingresos Asimilados a Salarios",
        "606": "Arrendamiento",
        "607": "Régimen de Enajenación o Adquisición de Bienes",
        "608": "Demás ingresos",
        "610": "Residentes en el Extranjero sin Establecimiento Permanente en México",
        "611": "Ingresos por Dividendos (socios y accionistas)",
        "612": "Personas Físicas con Actividades Empresariales y Profesionales",
        "614": "Ingresos por intereses",
        "615": "Régimen de los ingresos por obtención de premios",
        "616": "Sin obligaciones fiscales",
        "620": "Sociedades Cooperativas de Producción que optan por diferir sus ingresos",
        "621": "Incorporación Fiscal",
        "622": "Actividades Agrícolas, Ganaderas, Silvícolas y Pesqueras",
        "623": "Opcional para Grupos de Sociedades",
        "624": "Coordinados",
        "625": "Régimen de las Actividades Empresariales con ingresos a través de Plataformas Tecnológicas",
        "626": "Régimen Simplificado de Confianza",
    }
    return catalogo.get(clave or "", "")


def _catalogo_objeto_impuesto(clave: str | None) -> str:
    catalogo = {
        "01": "No objeto de impuesto",
        "02": "Sí objeto de impuesto",
        "03": "Sí objeto del impuesto y no obligado al desglose",
        "04": "Sí objeto del impuesto y no causa impuesto",
    }
    return catalogo.get(clave or "", "")

