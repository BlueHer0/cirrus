"""Cirrus PDF service — wraps sat_scrapper_core.cfdi_pdf with logo support.

Adds empresa logo (from MinIO) to the CFDI PDF by injecting
logo_data_uri into the Jinja2 template context.
"""

import logging
from pathlib import Path

from jinja2 import Environment, FileSystemLoader
from weasyprint import HTML

from sat_scrapper_core.cfdi_pdf.render import (
    _build_context, _filter_monto, _filter_datefmt, _filter_tasa_pct,
)
from sat_scrapper_core.cfdi_pdf.xml_parse import parse_cfdi_xml

logger = logging.getLogger("core.pdf_service")

TEMPLATES_DIR = Path(__file__).parent.parent.parent / "sat_scrapper_core" / "cfdi_pdf" / "templates"


def render_cfdi_pdf_with_logo(
    xml_bytes: bytes,
    logo_data_uri: str | None = None,
    template_name: str = "cfdi_pdf.html",
) -> bytes:
    """Render CFDI XML to PDF with optional empresa logo.

    Args:
        xml_bytes: Raw CFDI XML bytes
        logo_data_uri: Pre-built data:image/... URI string (or None)
        template_name: HTML template to use

    Returns:
        PDF bytes
    """
    parsed = parse_cfdi_xml(xml_bytes)
    context = _build_context(parsed, branded=False)

    # Inject logo data URI
    context["logo_data_uri"] = logo_data_uri

    # Render with Jinja2 + WeasyPrint (same pipeline as sat_scrapper_core)
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=True,
    )
    env.filters["monto"] = _filter_monto
    env.filters["datefmt"] = _filter_datefmt
    env.filters["tasa_pct"] = _filter_tasa_pct

    template = env.get_template(template_name)
    html_string = template.render(**context)

    html = HTML(string=html_string, base_url=str(TEMPLATES_DIR))
    return html.write_pdf()
