"""
Carga y validación de la FIEL (e.firma) del SAT.

Merge de ambas implementaciones:
- Extracción de RFC inteligente (ZL): busca todos los OID, valida longitud, prefiere empresa (12 chars).
- Validación robusta con errores descriptivos (SatScrapper).
- Propiedad is_valid para chequeo rápido (ZL).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from cryptography.x509 import load_der_x509_certificate
from cryptography.hazmat.primitives.serialization import load_der_private_key

logger = logging.getLogger("sat_scrapper_core")


class FIELError(Exception):
    """Error relacionado con la carga o validación de FIEL."""
    pass


class FIELLoader:
    """
    Carga y valida los archivos de la e.firma (FIEL) del SAT.

    Uso:
        fiel = FIELLoader("mi_fiel.cer", "mi_fiel.key", "mi_password")
        print(fiel.rfc)           # "XXXX123456XXX"
        print(fiel.is_valid)      # True
        cer_path = fiel.cer_path  # Path para upload en el navegador
    """

    def __init__(self, cer_path: str, key_path: str, password: str):
        self.cer_path = Path(cer_path).resolve()
        self.key_path = Path(key_path).resolve()
        self._password = password

        self._certificate = None
        self.rfc: str = ""
        self.serial_number: str = ""
        self.valid_from: datetime | None = None
        self.valid_to: datetime | None = None

        self._load()

    def _load(self):
        """Carga y valida los archivos FIEL."""
        self._load_certificate()
        self._validate_private_key()
        self._extract_info()
        self._validate_expiry()
        logger.info(
            "✅ FIEL cargada | RFC: %s | Vigente hasta: %s",
            self.rfc,
            self.valid_to.strftime("%Y-%m-%d") if self.valid_to else "N/A",
        )

    def _load_certificate(self):
        """Carga el archivo .cer (certificado DER)."""
        if not self.cer_path.exists():
            raise FIELError(f"Archivo .cer no encontrado: {self.cer_path}")

        cer_der = self.cer_path.read_bytes()
        try:
            self._certificate = load_der_x509_certificate(cer_der)
        except Exception as e:
            raise FIELError(f"Error al leer el certificado .cer: {e}")

    def _validate_private_key(self):
        """Valida que el .key se pueda abrir con la contraseña."""
        if not self.key_path.exists():
            raise FIELError(f"Archivo .key no encontrado: {self.key_path}")

        key_der = self.key_path.read_bytes()
        try:
            load_der_private_key(key_der, password=self._password.encode("utf-8"))
        except ValueError:
            raise FIELError(
                "Contraseña incorrecta o archivo .key corrupto. "
                "Verifica que la contraseña sea correcta."
            )
        except Exception as e:
            raise FIELError(f"Error al leer la llave privada .key: {e}")

    def _extract_info(self):
        """Extrae RFC, número de serie y vigencia del certificado."""
        # Número de serie
        serial_hex = format(self._certificate.serial_number, "x")
        self.serial_number = "".join(
            chr(int(serial_hex[i: i + 2], 16))
            for i in range(0, len(serial_hex), 2)
            if int(serial_hex[i: i + 2], 16) >= 32
        )

        # RFC — extracción inteligente (busca todos los OID del subject)
        self.rfc = self._extract_rfc(self._certificate)

        # Vigencia
        self.valid_from = self._certificate.not_valid_before_utc
        self.valid_to = self._certificate.not_valid_after_utc

    @staticmethod
    def _extract_rfc(cert) -> str:
        """
        Extrae el RFC del certificado de forma inteligente.

        Busca todos los atributos del subject, extrae candidatos que parezcan RFC
        (12 chars para persona moral, 13 para persona física), y prefiere el de empresa.
        """
        candidates: list[str] = []
        for attr in cert.subject:
            val = attr.value.strip()
            # Split por '/' si está presente (ej: "RFC_empresa / RFC_representante")
            parts = [p.strip() for p in val.split("/") if p.strip()]
            for p in parts:
                p = p.replace(" ", "")
                if len(p) in (12, 13) and p.isalnum():
                    candidates.append(p)

        if not candidates:
            return "UNKNOWN"

        # Preferir RFC de 12 chars (persona moral / empresa)
        for cand in candidates:
            if len(cand) == 12:
                return cand

        # Si no, usar el de 13 chars (persona física)
        for cand in candidates:
            if len(cand) == 13:
                return cand

        return candidates[0]

    def _validate_expiry(self):
        """Valida que el certificado esté vigente."""
        now = datetime.now(timezone.utc)
        if self.valid_to and now > self.valid_to:
            raise FIELError(
                f"El certificado FIEL ha expirado el "
                f"{self.valid_to.strftime('%Y-%m-%d %H:%M')}. "
                f"Debes renovar tu e.firma en el SAT."
            )
        if self.rfc == "UNKNOWN":
            logger.warning("⚠️ No se pudo extraer el RFC del certificado.")

    @property
    def password(self) -> str:
        """Retorna la contraseña de la llave privada."""
        return self._password

    @property
    def is_valid(self) -> bool:
        """Verifica si el certificado está vigente."""
        if not self.valid_to:
            return False
        return datetime.now(timezone.utc) < self.valid_to

    def summary(self) -> dict:
        """Retorna un diccionario con la información del FIEL."""
        return {
            "rfc": self.rfc,
            "serial_number": self.serial_number,
            "valid_from": self.valid_from.isoformat() if self.valid_from else None,
            "valid_to": self.valid_to.isoformat() if self.valid_to else None,
            "is_valid": self.is_valid,
            "cer_path": str(self.cer_path),
            "key_path": str(self.key_path),
        }

    def __repr__(self):
        return (
            f"FIELLoader(rfc='{self.rfc}', "
            f"valid={'✅' if self.is_valid else '❌'}, "
            f"expires='{self.valid_to}')"
        )
