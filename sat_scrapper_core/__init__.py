"""sat-scrapper-core — Librería reutilizable para descarga masiva de CFDIs del SAT."""

__version__ = "1.0.0"

from .config import ScrapeConfig
from .fiel import FIELLoader, FIELError
from .engine import SATEngine, DownloadResult

__all__ = [
    "ScrapeConfig",
    "FIELLoader",
    "FIELError",
    "SATEngine",
    "DownloadResult",
]
