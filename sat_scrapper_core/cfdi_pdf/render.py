"""Genera PDFs de CFDI a partir de XML bytes. Standalone, sin Django.

Uso:
    from sat_scrapper_core.cfdi_pdf import render_cfdi_pdf

    xml_bytes = open("factura.xml", "rb").read()
    pdf_bytes = render_cfdi_pdf(xml_bytes)
    open("factura.pdf", "wb").write(pdf_bytes)
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from pathlib import Path

from jinja2 import Environment, FileSystemLoader
from weasyprint import HTML

from .number_to_words import numero_a_letra_mxn
from .qr import generar_qr_data_uri
from .xml_parse import ParsedCFDI, parse_cfdi_xml

TEMPLATES_DIR = Path(__file__).parent / "templates"


def render_cfdi_pdf(
    xml_bytes: bytes,
    template_name: str = "cfdi_pdf.html",
    branded: bool = True,
) -> bytes:
    """Genera un PDF a partir de los bytes de un XML de CFDI (3.3 o 4.0).

    Args:
        xml_bytes: Contenido del XML de CFDI
        template_name: Nombre del template HTML a usar
        branded: True = footer vistoso (gratuito), False = footer discreto (de paga)

    Returns:
        Bytes del PDF generado
    """
    parsed = parse_cfdi_xml(xml_bytes)
    return _render_pdf(parsed, template_name, branded=branded)


def render_cfdi_pdf_from_dict(
    parsed: ParsedCFDI,
    template_name: str = "cfdi_pdf.html",
    branded: bool = True,
) -> bytes:
    """Genera un PDF a partir de un ParsedCFDI ya procesado."""
    return _render_pdf(parsed, template_name, branded=branded)


def _render_pdf(parsed: ParsedCFDI, template_name: str, branded: bool = True) -> bytes:
    """Render interno: construye contexto, renderiza HTML, genera PDF."""
    context = _build_context(parsed, branded=branded)

    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=True,
    )

    # Filtros custom para el template
    env.filters["monto"] = _filter_monto
    env.filters["datefmt"] = _filter_datefmt
    env.filters["tasa_pct"] = _filter_tasa_pct

    template = env.get_template(template_name)
    html_string = template.render(**context)

    html = HTML(string=html_string, base_url=str(TEMPLATES_DIR))
    return html.write_pdf()


def _build_context(parsed: ParsedCFDI, branded: bool = True) -> dict:
    """Construye el diccionario de contexto para el template."""
    comprobante = parsed.comprobante
    emisor = parsed.emisor
    receptor = parsed.receptor
    timbre = parsed.timbre

    total = comprobante.get("total") or 0
    total_letra = numero_a_letra_mxn(total)

    # QR de verificacion SAT
    total_decimal = f"{Decimal(str(total)):.6f}"
    sello = (timbre.get("sello_cfd") or "")[-8:]
    qr_url = (
        "https://verificacfdi.facturaelectronica.sat.gob.mx/default.aspx"
        f"?id={timbre.get('uuid', '')}"
        f"&re={emisor.get('rfc', '')}"
        f"&rr={receptor.get('rfc', '')}"
        f"&tt={total_decimal}"
        f"&fe={sello}"
    )
    qr_data_uri = generar_qr_data_uri(qr_url)

    return {
        "comprobante": comprobante,
        "emisor": emisor,
        "receptor": receptor,
        "conceptos": parsed.conceptos,
        "impuestos": parsed.impuestos,
        "relacionados": parsed.relacionados,
        "timbre": timbre,
        "cadena_tfd": parsed.cadena_tfd,
        "total_letra": total_letra,
        "qr_data_uri": qr_data_uri,
        "branded": branded,
    }


# --- Filtros Jinja2 ---

def _filter_monto(value) -> str:
    """Formatea un numero como monto con 2 decimales y comas."""
    if value is None:
        return "0.00"
    try:
        d = Decimal(str(value))
        # Formato con comas como separador de miles
        parts = f"{d:,.2f}"
        return parts
    except Exception:
        return str(value)


def _filter_datefmt(value) -> str:
    """Formatea una fecha como 'dd/mm/yyyy HH:MM'."""
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.strftime("%d/%m/%Y %H:%M")
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return dt.strftime("%d/%m/%Y %H:%M")
        except Exception:
            return value
    return str(value)


def _filter_tasa_pct(value) -> str:
    """Convierte tasa decimal (0.160000) a porcentaje (16%)."""
    if value is None:
        return ""
    try:
        d = Decimal(str(value))
        pct = d * 100
        if pct == int(pct):
            return f"{int(pct)}%"
        return f"{pct:.2f}%"
    except Exception:
        return str(value)


__all__ = ["render_cfdi_pdf", "render_cfdi_pdf_from_dict"]
