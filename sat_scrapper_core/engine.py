"""
Orquestador de descarga de CFDIs del SAT via Playwright RPA.

SATEngine es el punto de entrada principal de la librería:
1. Acepta un ScrapeConfig
2. Usa Playwright RPA para navegar el portal del SAT
3. Recorre mes por mes, descarga y organiza CFDIs
4. Llama callbacks en cada paso
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from .config import ScrapeConfig
from .fiel import FIELLoader, FIELError
from .storage import CfdiStorage

logger = logging.getLogger("sat_scrapper_core")


@dataclass
class DownloadResult:
    """Resultado de una sesión de descarga."""
    total_files: int = 0
    total_cfdis: int = 0
    total_size_mb: float = 0.0
    months_processed: int = 0
    months_with_data: int = 0
    folios: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    details: list[dict] = field(default_factory=list)

    def summary(self) -> dict:
        return {
            "total_files": self.total_files,
            "total_cfdis": self.total_cfdis,
            "total_size_mb": self.total_size_mb,
            "months_processed": self.months_processed,
            "months_with_data": self.months_with_data,
            "errors_count": len(self.errors),
        }


class SATEngine:
    """
    Orquestador principal para descarga de CFDIs del SAT via Playwright RPA.

    Uso:
        config = ScrapeConfig(
            cer_path="mi.cer", key_path="mi.key", password="xxx",
            year=2025, month_start=1, month_end=6,
        )
        async with SATEngine(config) as engine:
            result = await engine.download_all()
            print(result.summary())
    """

    def __init__(self, config: ScrapeConfig):
        self.config = config
        self.fiel: FIELLoader | None = None
        self.storage: CfdiStorage | None = None
        self.result = DownloadResult()
        self._cancel_event = asyncio.Event()

    async def __aenter__(self):
        self._load_fiel()
        self.storage = CfdiStorage(
            base_dir=self.config.download_dir,
            rfc=self.fiel.rfc if self.config.organize_by_rfc else "",
        )
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        self.storage = None

    def cancel(self):
        """Cancela la descarga en curso (se detiene entre meses)."""
        self._cancel_event.set()
        self._progress("Cancelacion solicitada...")

    def _load_fiel(self):
        """Carga y valida la FIEL."""
        self._progress("Cargando FIEL...")
        self.fiel = FIELLoader(
            self.config.cer_path,
            self.config.key_path,
            self.config.password,
        )
        valid = "vigente" if self.fiel.is_valid else "EXPIRADA"
        self._progress(f"FIEL cargada: RFC={self.fiel.rfc} | {valid}")

    def _progress(self, message: str):
        """Reporta progreso via callback y logger."""
        logger.info(message)
        if self.config.on_progress:
            try:
                self.config.on_progress(message)
            except Exception:
                pass

    async def download_all(self) -> DownloadResult:
        """
        Descarga todos los meses y tipos configurados via Playwright RPA.

        Flujo:
        1. Abre navegador con anti-deteccion
        2. Login con FIEL
        3. Para cada tipo (recibidos, emitidos):
           4. Para cada mes (month_start -> month_end):
              5. Navega, filtra, busca, descarga
              6. Procesa ZIPs -> organiza XMLs
              7. Llama callbacks
        8. Logout y cierra navegador
        9. Retorna DownloadResult con estadisticas
        """
        from .browser_bot import BrowserBot
        from .sat_navigator import SATNavigator

        self._progress("Iniciando motor RPA (Playwright)...")

        async with BrowserBot(self.config) as bot:
            nav = SATNavigator(bot.page, self.fiel, self.config)

            # Login una sola vez
            await nav.login()
            self._progress("Login exitoso en el SAT")

            for tipo in self.config.tipos:
                self._progress(f"--- DESCARGANDO CFDIs {tipo.upper()} {self.config.year} ---")

                for month in range(self.config.month_start, self.config.month_end + 1):
                    # Verificar cancelacion entre meses
                    if self._cancel_event.is_set():
                        self._progress("Descarga cancelada por el usuario")
                        break

                    self._progress(f"{tipo.upper()} {self.config.year}-{month:02d}")
                    self.result.months_processed += 1

                    try:
                        files = await nav.download_month(bot, self.config.year, month, tipo)

                        if files:
                            self.result.months_with_data += 1
                            for f in files:
                                if f.suffix.lower() == ".zip" and self.storage:
                                    processed = self.storage.process_zip(f)
                                    self.result.total_files += len(processed)
                                elif f.suffix.lower() == ".xml" and self.storage:
                                    xml_bytes = f.read_bytes()
                                    meta = self.storage.process_xml_bytes(xml_bytes)
                                    if meta:
                                        self.result.total_files += 1
                            self._call_month_callback(month, self.config.year, tipo, files)

                    except Exception as e:
                        error_msg = f"Error {tipo} {self.config.year}-{month:02d}: {e}"
                        logger.error(error_msg)
                        self.result.errors.append(error_msg)
                        self._call_error_callback(e, error_msg)

                    # Rate limiting entre meses
                    await asyncio.sleep(self.config.delay_between_months)

                if self._cancel_event.is_set():
                    break

            # Guardar folios pendientes
            self.result.folios = nav.folios

            # Logout
            await nav.logout()

        # Estadisticas finales
        if self.storage:
            stats = self.storage.get_stats()
            self.result.total_cfdis = stats["total_cfdis"]
            self.result.total_size_mb = stats["total_size_mb"]

        return self.result

    async def verify_login(self) -> bool:
        """
        Login de prueba para verificar que la FIEL funciona contra el SAT.

        Abre navegador, intenta login, y cierra. No descarga nada.

        Returns:
            True si el login fue exitoso, False si fallo.
        """
        from .browser_bot import BrowserBot
        from .sat_navigator import SATNavigator, SATNavigatorError

        self._progress("Verificando login contra el SAT...")

        try:
            async with BrowserBot(self.config) as bot:
                nav = SATNavigator(bot.page, self.fiel, self.config)
                await nav.login()
                await nav.logout()
            self._progress("Login verificado exitosamente")
            return True
        except (SATNavigatorError, Exception) as e:
            self._progress(f"Login fallido: {e}")
            return False

    def _call_month_callback(self, month: int, year: int, tipo: str, data):
        """Ejecuta callback on_month_completed si esta configurado."""
        if self.config.on_month_completed:
            try:
                self.config.on_month_completed(year, month, tipo, data)
            except Exception as e:
                logger.warning("Error en callback on_month_completed: %s", e)

    def _call_error_callback(self, error: Exception, context: str):
        """Ejecuta callback on_error si esta configurado."""
        if self.config.on_error:
            try:
                self.config.on_error(error, context)
            except Exception:
                pass
