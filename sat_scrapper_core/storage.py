"""
Almacenamiento, parseo de CFDI XMLs y organización de archivos.

Merge de ambas implementaciones:
- Organización RFC/año/mes de SatScrapper
- Parseo XML robusto con detección de versión (CFDI 3.3 y 4.0) de ambos
- Extracción de ZIPs de ZL
- Índice CSV con deduplicación por UUID
"""

from __future__ import annotations

import csv
import logging
import shutil
import zipfile
from datetime import datetime
from pathlib import Path

from lxml import etree

logger = logging.getLogger("sat_scrapper_core")

# Namespaces del CFDI
NS_CFDI40 = {"cfdi": "http://www.sat.gob.mx/cfd/4", "tfd": "http://www.sat.gob.mx/TimbreFiscalDigital"}
NS_CFDI33 = {"cfdi": "http://www.sat.gob.mx/cfd/3", "tfd": "http://www.sat.gob.mx/TimbreFiscalDigital"}

INDEX_COLUMNS = [
    "UUID", "Fecha", "RfcEmisor", "NombreEmisor",
    "RfcReceptor", "NombreReceptor", "Total", "SubTotal",
    "TipoDeComprobante", "Moneda", "FormaPago", "MetodoPago",
    "Serie", "Folio", "Version", "ArchivoXml",
]


class CfdiStorage:
    """
    Almacena y organiza CFDIs descargados.

    Estructura:
        {base_dir}/{RFC}/{año}/{mes}/{uuid}.xml
        {base_dir}/{RFC}/indice.csv
    """

    def __init__(self, base_dir: str = "./downloads", rfc: str = ""):
        self.base_dir = Path(base_dir).resolve()
        self.rfc = rfc
        self.rfc_dir = self.base_dir / rfc if rfc else self.base_dir
        self.rfc_dir.mkdir(parents=True, exist_ok=True)

        self._index_path = self.rfc_dir / "indice.csv"
        self._known_uuids: set[str] = set()
        self._load_known_uuids()

    def _load_known_uuids(self):
        """Carga UUIDs ya conocidos del índice CSV para deduplicación."""
        if self._index_path.exists():
            try:
                with open(self._index_path, "r", encoding="utf-8") as f:
                    reader = csv.DictReader(f)
                    for row in reader:
                        uuid = row.get("UUID", "").strip()
                        if uuid:
                            self._known_uuids.add(uuid.upper())
            except Exception:
                pass

    def _ensure_index(self):
        """Crea el archivo CSV de índice si no existe."""
        if not self._index_path.exists():
            with open(self._index_path, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=INDEX_COLUMNS)
                writer.writeheader()

    def is_known(self, uuid: str) -> bool:
        """Verifica si un UUID ya fue procesado (deduplicación)."""
        return uuid.upper() in self._known_uuids

    def process_zip(self, zip_path: Path) -> list[dict]:
        """Extrae XMLs de un ZIP y los almacena organizados."""
        processed = []
        try:
            with zipfile.ZipFile(zip_path) as zf:
                xml_files = [f for f in zf.namelist() if f.lower().endswith(".xml")]
                logger.info("📂 Extrayendo %d XMLs de %s...", len(xml_files), zip_path.name)
                for xml_name in xml_files:
                    try:
                        xml_bytes = zf.read(xml_name)
                        metadata = self.process_xml_bytes(xml_bytes)
                        if metadata:
                            processed.append(metadata)
                    except Exception as e:
                        logger.warning("⚠️ Error procesando %s: %s", xml_name, e)
        except zipfile.BadZipFile:
            logger.error("❌ %s no es un ZIP válido", zip_path.name)

        logger.info("✅ %d CFDIs procesados de %s", len(processed), zip_path.name)
        return processed

    def process_xml_bytes(self, xml_bytes: bytes) -> dict | None:
        """Procesa un único XML de CFDI: extrae metadatos y lo almacena."""
        metadata = parse_cfdi_xml(xml_bytes)
        if not metadata:
            return None

        uuid = metadata.get("UUID", "")
        if not uuid:
            logger.warning("⚠️ XML sin UUID, saltando")
            return None

        # Deduplicación
        if self.is_known(uuid):
            logger.debug("⏭️ UUID %s ya existe, saltando", uuid[:8])
            return None

        # Determinar ruta destino
        fecha = metadata.get("Fecha", "")
        try:
            dt = datetime.fromisoformat(fecha)
            year = str(dt.year)
            month = f"{dt.month:02d}"
        except Exception:
            year = "sin_fecha"
            month = "00"

        target_dir = self.rfc_dir / year / month
        target_dir.mkdir(parents=True, exist_ok=True)
        target_path = target_dir / f"{uuid}.xml"

        target_path.write_bytes(xml_bytes)
        metadata["ArchivoXml"] = str(target_path.relative_to(self.base_dir))

        # Actualizar índice
        self._ensure_index()
        self._append_to_index(metadata)
        self._known_uuids.add(uuid.upper())

        return metadata

    def _append_to_index(self, metadata: dict):
        """Agrega una entrada al índice CSV."""
        with open(self._index_path, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=INDEX_COLUMNS)
            row = {col: metadata.get(col, "") for col in INDEX_COLUMNS}
            writer.writerow(row)

    def extract_all_zips(self, source_dir: Path | None = None) -> list[dict]:
        """Extrae todos los ZIPs en un directorio y procesa sus XMLs."""
        search_dir = source_dir or self.rfc_dir
        all_processed = []
        for zip_path in search_dir.rglob("*.zip"):
            processed = self.process_zip(zip_path)
            all_processed.extend(processed)
        return all_processed

    def get_stats(self) -> dict:
        """Retorna estadísticas de los CFDIs almacenados."""
        xml_count = 0
        total_size = 0
        for xml_file in self.rfc_dir.rglob("*.xml"):
            xml_count += 1
            total_size += xml_file.stat().st_size

        return {
            "rfc": self.rfc,
            "total_cfdis": xml_count,
            "total_size_mb": round(total_size / (1024 * 1024), 2),
            "known_uuids": len(self._known_uuids),
            "directory": str(self.rfc_dir),
            "index_file": str(self._index_path),
        }


def parse_cfdi_xml(xml_bytes: bytes) -> dict | None:
    """
    Parsea un XML de CFDI (3.3 o 4.0) y retorna metadatos estructurados.

    Returns:
        Dict con UUID, Fecha, RFCs, Totales, etc. o None si inválido.
    """
    try:
        root = etree.fromstring(xml_bytes)
    except etree.XMLSyntaxError as e:
        logger.warning("⚠️ XML inválido: %s", e)
        return None

    # Detectar versión por namespace
    tag = root.tag.lower()
    if "cfd/4" in tag:
        ns = NS_CFDI40
    elif "cfd/3" in tag:
        ns = NS_CFDI33
    else:
        ver = root.get("Version", root.get("version", ""))
        ns = NS_CFDI40 if ver.startswith("4") else NS_CFDI33

    # Comprobante
    data = {
        "Version": root.get("Version", root.get("version", "")),
        "Fecha": root.get("Fecha", ""),
        "Serie": root.get("Serie", ""),
        "Folio": root.get("Folio", ""),
        "Total": root.get("Total", "0"),
        "SubTotal": root.get("SubTotal", "0"),
        "TipoDeComprobante": root.get("TipoDeComprobante", ""),
        "Moneda": root.get("Moneda", "MXN"),
        "FormaPago": root.get("FormaPago", ""),
        "MetodoPago": root.get("MetodoPago", ""),
    }

    # Emisor
    emisor = root.find(".//cfdi:Emisor", ns)
    if emisor is None:
        emisor = root.find(f".//{{{ns['cfdi']}}}Emisor")
    if emisor is not None:
        data["RfcEmisor"] = emisor.get("Rfc", emisor.get("rfc", ""))
        data["NombreEmisor"] = emisor.get("Nombre", emisor.get("nombre", ""))
    else:
        data["RfcEmisor"] = ""
        data["NombreEmisor"] = ""

    # Receptor
    receptor = root.find(".//cfdi:Receptor", ns)
    if receptor is None:
        receptor = root.find(f".//{{{ns['cfdi']}}}Receptor")
    if receptor is not None:
        data["RfcReceptor"] = receptor.get("Rfc", receptor.get("rfc", ""))
        data["NombreReceptor"] = receptor.get("Nombre", receptor.get("nombre", ""))
    else:
        data["RfcReceptor"] = ""
        data["NombreReceptor"] = ""

    # UUID del Timbre Fiscal Digital
    tfd = root.find(".//tfd:TimbreFiscalDigital", ns)
    if tfd is not None:
        data["UUID"] = tfd.get("UUID", "")
    else:
        data["UUID"] = ""

    return data
