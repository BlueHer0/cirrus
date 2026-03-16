"""
Robot de navegador con Playwright y stealth anti-detección.

Base de SatScrapper (context manager limpio) + error handling de ZL.
"""

from __future__ import annotations

import logging
from pathlib import Path

from playwright.async_api import async_playwright, Browser, BrowserContext, Page

from .config import ScrapeConfig

logger = logging.getLogger("sat_scrapper_core")


class BrowserBot:
    """
    Gestiona el ciclo de vida del navegador Playwright con configuración anti-detección.

    Uso:
        config = ScrapeConfig(...)
        async with BrowserBot(config) as bot:
            page = bot.page
            await page.goto("https://...")
    """

    def __init__(self, config: ScrapeConfig):
        self.config = config
        self.download_dir = config.download_path
        self.screenshot_dir = config.screenshot_path

        self._playwright = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self.page: Page | None = None

        # Asegurar directorios
        self.download_dir.mkdir(parents=True, exist_ok=True)
        self.screenshot_dir.mkdir(parents=True, exist_ok=True)

    async def start(self) -> Page:
        """Inicia el navegador con configuración anti-detección."""
        logger.info(
            "🚀 Iniciando navegador | headless=%s | slow_mo=%dms",
            self.config.headless,
            self.config.slow_mo,
        )

        self._playwright = await async_playwright().start()

        # Lanzar Chromium con opciones anti-detección
        launch_args = [
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
        ]

        self._browser = await self._playwright.chromium.launch(
            headless=self.config.headless,
            slow_mo=self.config.slow_mo if not self.config.headless else 0,
            args=launch_args,
        )

        # Crear contexto con locale mexicano y user agent realista
        self._context = await self._browser.new_context(
            locale="es-MX",
            timezone_id="America/Mexico_City",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            accept_downloads=True,
            viewport={
                "width": self.config.viewport_width,
                "height": self.config.viewport_height,
            },
        )

        # Timeout por defecto
        self._context.set_default_timeout(self.config.browser_timeout)

        # Crear página y aplicar stealth
        self.page = await self._context.new_page()
        await self._apply_stealth()

        logger.info("✅ Navegador iniciado correctamente")
        return self.page

    async def _apply_stealth(self):
        """Aplica stealth anti-detección al navegador."""
        try:
            from playwright_stealth import Stealth
            await Stealth().apply_stealth_async(self.page)
            logger.info("🥷 Stealth aplicado")
        except ImportError:
            logger.warning(
                "⚠️ playwright-stealth no instalado. "
                "Instálalo con: pip install playwright-stealth"
            )

    async def wait_for_download(self, callback, timeout: int = 120_000) -> Path | None:
        """
        Ejecuta un callback que dispara una descarga y espera el archivo.

        Args:
            callback: Función async que dispara la descarga (ej: click en botón)
            timeout: Tiempo máximo de espera en ms

        Returns:
            Path al archivo descargado, o None si falló
        """
        try:
            async with self.page.expect_download(timeout=timeout) as download_info:
                await callback()
            download = await download_info.value
            suggested = download.suggested_filename
            target = self.download_dir / suggested
            target.parent.mkdir(parents=True, exist_ok=True)
            await download.save_as(str(target))
            logger.info("📥 Descargado: %s", target.name)
            return target
        except Exception as e:
            logger.error("❌ Error en descarga: %s", e)
            return None

    async def close(self):
        """Cierra el navegador y limpia recursos."""
        logger.info("🛑 Cerrando navegador...")
        try:
            if self._context:
                await self._context.close()
            if self._browser:
                await self._browser.close()
            if self._playwright:
                await self._playwright.stop()
        except Exception as e:
            logger.warning("⚠️ Error al cerrar navegador: %s", e)
        finally:
            self.page = None
            self._context = None
            self._browser = None
            self._playwright = None
            logger.info("🛑 Navegador cerrado")

    async def __aenter__(self):
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()
