"""Generador de PDFs a partir de XMLs de CFDI (3.3 y 4.0). Standalone, sin Django."""

from .render import render_cfdi_pdf
from .xml_parse import parse_cfdi_xml, ParsedCFDI, CFDIParseError

__all__ = ["render_cfdi_pdf", "parse_cfdi_xml", "ParsedCFDI", "CFDIParseError"]
