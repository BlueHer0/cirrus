"""
Configuración unificada para sat-scrapper-core.

Usa un dataclass parametrizable que se puede crear por código, desde env vars, o desde .env.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal

from dotenv import load_dotenv


@dataclass
class ScrapeConfig:
    """Configuración completa para una sesión de descarga de CFDIs."""

    # ─── FIEL (obligatorios) ──────────────────────────────────────────
    cer_path: str = ""
    key_path: str = ""
    password: str = ""

    # ─── ¿Qué descargar? ─────────────────────────────────────────────
    year: int = 2025
    month_start: int = 1
    month_end: int = 12
    tipos: list[str] = field(default_factory=lambda: ["recibidos", "emitidos"])
    tipo_comprobante: str = "todos"

    # ─── Motor de descarga ────────────────────────────────────────────
    # Solo Playwright RPA — motor SOAP eliminado
    engine: Literal["rpa"] = "rpa"

    # ─── Almacenamiento ──────────────────────────────────────────────
    download_dir: str = "./downloads"
    organize_by_rfc: bool = True
    generate_csv_index: bool = True

    # ─── Browser (solo motor RPA) ────────────────────────────────────
    headless: bool = True
    slow_mo: int = 300  # ms entre acciones
    browser_timeout: int = 60_000  # ms
    viewport_width: int = 1280
    viewport_height: int = 800

    # ─── Rate limiting / Anti-detección ──────────────────────────────
    delay_between_months: int = 3  # segundos entre cada mes
    max_retries: int = 3
    retry_delay: float = 5.0  # segundos entre reintentos

    # ─── Screenshots (debug) ────────────────────────────────────────
    screenshot_dir: str = "./screenshots"
    take_screenshots: bool = True

    # ─── Callbacks (hooks para integrar con tu app) ──────────────────
    on_cfdi_downloaded: Callable[[bytes, dict], None] | None = None
    on_month_completed: Callable[[int, int, str, list], None] | None = None
    on_progress: Callable[[str], None] | None = None
    on_error: Callable[[Exception, str], None] | None = None

    def __post_init__(self):
        """Validaciones básicas."""
        if self.month_start < 1 or self.month_start > 12:
            raise ValueError(f"month_start debe estar entre 1 y 12, recibido: {self.month_start}")
        if self.month_end < 1 or self.month_end > 12:
            raise ValueError(f"month_end debe estar entre 1 y 12, recibido: {self.month_end}")
        if self.month_start > self.month_end:
            raise ValueError(f"month_start ({self.month_start}) > month_end ({self.month_end})")

    @classmethod
    def from_env(cls, env_file: str | None = None, **overrides) -> ScrapeConfig:
        """Crea config desde variables de entorno (opcionalmente desde un archivo .env)."""
        if env_file:
            load_dotenv(env_file)
        else:
            load_dotenv()

        kwargs: dict[str, Any] = {
            "cer_path": os.getenv("SAT_CER_PATH", ""),
            "key_path": os.getenv("SAT_KEY_PATH", ""),
            "password": os.getenv("SAT_PASSWORD", ""),
            "year": int(os.getenv("SAT_YEAR", "2025")),
            "month_start": int(os.getenv("SAT_MONTH_START", "1")),
            "month_end": int(os.getenv("SAT_MONTH_END", "12")),
            "engine": os.getenv("SAT_ENGINE", "rpa"),
            "download_dir": os.getenv("SAT_DOWNLOAD_DIR", "./downloads"),
            "headless": os.getenv("SAT_HEADLESS", "true").lower() == "true",
            "slow_mo": int(os.getenv("SAT_SLOW_MO", "300")),
        }

        tipos_env = os.getenv("SAT_TIPOS", "recibidos,emitidos")
        kwargs["tipos"] = [t.strip() for t in tipos_env.split(",") if t.strip()]

        kwargs.update({k: v for k, v in overrides.items() if v is not None})
        return cls(**kwargs)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ScrapeConfig:
        """Crea config desde un diccionario (útil para Django settings, JSON, etc.)."""
        valid_fields = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in data.items() if k in valid_fields}
        return cls(**filtered)

    @property
    def download_path(self) -> Path:
        return Path(self.download_dir).resolve()

    @property
    def screenshot_path(self) -> Path:
        return Path(self.screenshot_dir).resolve()


# ─── URLs del Portal SAT ──────────────────────────────────────────────
SAT_PORTAL_URL = "https://portalcfdi.facturaelectronica.sat.gob.mx/"
SAT_EMITIDOS_URL = "https://portalcfdi.facturaelectronica.sat.gob.mx/ConsultaEmisor.aspx"
SAT_RECIBIDOS_URL = "https://portalcfdi.facturaelectronica.sat.gob.mx/ConsultaReceptor.aspx"

# ─── Tipos de comprobante ────────────────────────────────────────────
TIPOS_COMPROBANTE = {
    "todos": "",
    "ingreso": "I",
    "egreso": "E",
    "traslado": "T",
    "nomina": "N",
    "pago": "P",
}

# ─── Selectores CSS del Portal SAT ───────────────────────────────────
# Organizados como listas para intentar en orden (el SAT cambia IDs)

SEL_BTN_FIEL = [
    "#buttonFiel",
    'input[id*="buttonFiel"]',
    'button[id*="buttonFiel"]',
    'a[id*="buttonFiel"]',
    'button:has-text("e.firma")',
    'a:has-text("e.firma")',
    'a:has-text("FIEL")',
]

SEL_UPLOAD_CER = "#fileCertificate"
SEL_UPLOAD_KEY = "#filePrivateKey"

SEL_PASSWORD = [
    'input[type="password"][id*="privateKeyPassword"]',
    'input[type="password"]',
]

SEL_BTN_SUBMIT = [
    "#submit",
    'input[id*="submit"]',
    'button[id*="submit"]',
    'input[type="submit"]',
    'input[value="Enviar"]',
    'button:has-text("Enviar")',
    "#btnEnviar",
]

SEL_VERIFY_LOGIN = [
    "#ctl00_LkBtnCierraSesion",
    'a:has-text("Cerrar Sesión")',
    'a:has-text("Salir")',
]

SEL_RADIO_FECHA = [
    'input[type="radio"][id*="FechaEmision"]',
    'input[id*="RdoFechas"]',
    'xpath=//span[contains(text(), "Fecha de Emisión")]/preceding-sibling::input',
    'text="Fecha de Emisión"',
]

SEL_SELECT_ANIO = 'select[id*="Anio"]'
SEL_SELECT_MES = 'select[id*="Mes"]'
SEL_SELECT_DIA = 'select[id*="Dia"]'
SEL_SELECT_TIPO_COMPROBANTE = 'select[id*="TipoComprobante"]'

SEL_BTN_BUSCAR = [
    "#ctl00_MainContent_BtnBusqueda",
    'button:has-text("Buscar CFDI")',
    'button:has-text("Buscar")',
]

SEL_BTN_DESCARGAR = [
    "#ctl00_MainContent_BtnDescargar",
    'button:has-text("Descargar Seleccionados")',
    'input[value*="Descargar Seleccionados"]',
]

SEL_CHECKBOX_ALL = 'input[type="checkbox"][id*="headerChk"]'
SEL_ALERT_CLOSE = "#btnAlertDCCerrar"
SEL_RECUPERAR_DESCARGAS = 'a:has-text("Recuperar Descargas")'
