"""
Navegador RPA del portal SAT para descarga de CFDIs.

Base de ZL Scrapper (battle-tested):
- Login con retry automático
- Selectores como listas con fallback
- Filtros por year/month directo
- Recovery polling con reintentos (hasta 6 min)
- Pipeline download_month() integrado
"""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path

from .config import (
    SAT_PORTAL_URL,
    SAT_EMITIDOS_URL,
    SAT_RECIBIDOS_URL,
    SEL_BTN_FIEL,
    SEL_UPLOAD_CER,
    SEL_UPLOAD_KEY,
    SEL_PASSWORD,
    SEL_BTN_SUBMIT,
    SEL_VERIFY_LOGIN,
    SEL_RADIO_FECHA,
    SEL_SELECT_ANIO,
    SEL_SELECT_MES,
    SEL_SELECT_DIA,
    SEL_SELECT_TIPO_COMPROBANTE,
    SEL_BTN_BUSCAR,
    SEL_BTN_DESCARGAR,
    SEL_CHECKBOX_ALL,
    SEL_ALERT_CLOSE,
    SEL_RECUPERAR_DESCARGAS,
    ScrapeConfig,
)
from .utils import screenshot, safe_click, safe_fill, retry_async

logger = logging.getLogger("sat_scrapper_core")


class SATNavigatorError(Exception):
    """Error de navegación en el portal SAT."""
    pass


class SATNavigator:
    """
    Navega el portal del SAT para descargar CFDIs usando FIEL via Playwright RPA.

    Uso:
        async with BrowserBot(config) as bot:
            nav = SATNavigator(bot.page, fiel, config)
            await nav.login()
            files = await nav.download_month(bot, 2025, 1, "recibidos")
    """

    def __init__(self, page, fiel, config: ScrapeConfig):
        self.page = page
        self.fiel = fiel
        self.config = config
        self.logged_in = False
        self.folios: list[str] = []
        self._screenshot_dir = config.screenshot_path

    async def _screenshot(self, label: str):
        """Toma screenshot si está habilitado en la config."""
        if self.config.take_screenshots:
            await screenshot(self.page, label, self._screenshot_dir)

    # ──────────────────────────────────────────────────────────────────
    #  LOGIN CON FIEL
    # ──────────────────────────────────────────────────────────────────

    @retry_async(retries=2, delay=5.0)
    async def login(self):
        """Autentica con FIEL en el portal del SAT."""
        logger.info("🔑 Iniciando login en el SAT...")
        await self.page.goto(
            SAT_PORTAL_URL,
            wait_until="domcontentloaded",
            timeout=self.config.browser_timeout,
        )
        await asyncio.sleep(2)
        await self._screenshot("01_portal_loaded")

        # Click botón e.firma
        clicked = await safe_click(self.page, SEL_BTN_FIEL, timeout=15_000)
        if not clicked:
            raise SATNavigatorError("No se encontró el botón de e.firma")
        await asyncio.sleep(2)
        await self._screenshot("02_fiel_form")

        # Subir .cer (input oculto)
        try:
            await self.page.locator(SEL_UPLOAD_CER).set_input_files(str(self.fiel.cer_path))
            logger.info("✅ Certificado .cer subido")
        except Exception:
            inputs = self.page.locator('input[type="file"]')
            count = await inputs.count()
            if count >= 1:
                await inputs.nth(0).set_input_files(str(self.fiel.cer_path))
                logger.info("✅ Certificado .cer subido (fallback)")
            else:
                raise SATNavigatorError("No se encontró input para subir .cer")

        # Subir .key (input oculto)
        try:
            await self.page.locator(SEL_UPLOAD_KEY).set_input_files(str(self.fiel.key_path))
            logger.info("✅ Llave .key subida")
        except Exception:
            inputs = self.page.locator('input[type="file"]')
            count = await inputs.count()
            if count >= 2:
                await inputs.nth(1).set_input_files(str(self.fiel.key_path))
                logger.info("✅ Llave .key subida (fallback)")
            else:
                raise SATNavigatorError("No se encontró input para subir .key")

        # Contraseña
        filled = await safe_fill(self.page, SEL_PASSWORD, self.fiel.password, timeout=10_000)
        if not filled:
            raise SATNavigatorError("No se encontró el campo de contraseña")

        await self._screenshot("03_form_filled")

        # Submit
        clicked = await safe_click(self.page, SEL_BTN_SUBMIT, timeout=10_000)
        if not clicked:
            raise SATNavigatorError("No se encontró el botón de enviar")

        # Verificar login
        await asyncio.sleep(5)
        await self._screenshot("04_after_submit")

        verified = await self._verify_login()
        if not verified:
            await self._screenshot("04_login_FAILED")
            raise SATNavigatorError(
                "Login fallido — no se encontró indicador de sesión activa. "
                "Verifica tu FIEL (archivos .cer/.key y contraseña). "
                "Revisa screenshots/ para más detalles."
            )

        self.logged_in = True
        logger.info("✅ Login exitoso como %s", self.fiel.rfc)
        await self._screenshot("05_logged_in")

    async def _verify_login(self) -> bool:
        """Verifica login buscando indicadores de sesión activa."""
        multiplexed = ", ".join(SEL_VERIFY_LOGIN)
        try:
            await self.page.locator(multiplexed).first.wait_for(
                state="visible", timeout=15_000
            )
            return True
        except Exception as e:
            logger.warning("Verificación de login falló: %s", e)
            # Fallback: verificar URL
            url = self.page.url
            if "Consulta" in url or "Portal" in url:
                return True
            return False

    # ──────────────────────────────────────────────────────────────────
    #  NAVEGACIÓN Y FILTROS
    # ──────────────────────────────────────────────────────────────────

    async def _navigate_to_query(self, tipo: str = "recibidos"):
        """Navega a la página de consulta (emitidos o recibidos)."""
        url = SAT_RECIBIDOS_URL if tipo == "recibidos" else SAT_EMITIDOS_URL
        logger.info("📂 Navegando a %s: %s", tipo, url)
        await self.page.goto(url, wait_until="domcontentloaded", timeout=self.config.browser_timeout)
        await asyncio.sleep(3)
        await self._screenshot(f"06_query_{tipo}")

    async def _set_filters(self, year: int, month: int):
        """Configura filtros de búsqueda: año, mes, día=0 (todos)."""
        logger.info("📅 Configurando filtros: %d-%02d", year, month)

        # Click radio "Fecha de Emisión"
        try:
            clicked = await safe_click(self.page, SEL_RADIO_FECHA, timeout=5_000)
            if clicked:
                logger.info("✅ Radio 'Fecha de Emisión' seleccionado")
            await asyncio.sleep(1)
        except Exception as e:
            logger.warning("No se pudo seleccionar radio de fecha: %s", e)

        # Seleccionar año
        try:
            await self.page.locator(SEL_SELECT_ANIO).select_option(str(year))
            await asyncio.sleep(0.5)
            logger.info("✅ Año: %d", year)
        except Exception as e:
            logger.warning("No se pudo seleccionar año: %s", e)

        # Seleccionar mes
        try:
            await self.page.locator(SEL_SELECT_MES).select_option(str(month).zfill(2))
            await asyncio.sleep(0.5)
            logger.info("✅ Mes: %02d", month)
        except Exception:
            try:
                await self.page.locator(SEL_SELECT_MES).select_option(str(month))
                logger.info("✅ Mes: %d (sin padding)", month)
            except Exception as e:
                logger.warning("No se pudo seleccionar mes: %s", e)

        # Seleccionar día = 0 (todos los días)
        try:
            await self.page.locator(SEL_SELECT_DIA).select_option("0")
            await asyncio.sleep(0.5)
            logger.info("✅ Día: Todos")
        except Exception:
            pass  # Algunas páginas no tienen filtro de día

        await self._screenshot(f"07_filters_{year}_{month:02d}")

    # ──────────────────────────────────────────────────────────────────
    #  BÚSQUEDA
    # ──────────────────────────────────────────────────────────────────

    async def _click_search(self) -> bool:
        """Click en el botón Buscar CFDI."""
        logger.info("🔍 Buscando CFDIs...")
        clicked = await safe_click(self.page, SEL_BTN_BUSCAR, timeout=15_000)
        if clicked:
            await asyncio.sleep(5)
            await self._screenshot("08_search_results")
        return clicked

    # ──────────────────────────────────────────────────────────────────
    #  SELECCIONAR Y SOLICITAR DESCARGA
    # ──────────────────────────────────────────────────────────────────

    async def _select_all_and_request_download(self) -> str | None:
        """Selecciona todos los resultados y solicita paquete de descarga. Retorna folio UUID."""
        # Checkbox select all
        try:
            chk = self.page.locator(SEL_CHECKBOX_ALL)
            if await chk.count() > 0:
                await chk.first.click()
                await asyncio.sleep(1)
                logger.info("✅ Todos los CFDIs seleccionados")
            else:
                chk_alt = self.page.locator('th input[type="checkbox"]').first
                await chk_alt.click()
                await asyncio.sleep(1)
                logger.info("✅ Todos seleccionados (fallback)")
        except Exception as e:
            logger.warning("No se pudo seleccionar todos: %s", e)

        await self._screenshot("09_all_selected")

        # Click Descargar Seleccionados
        clicked = await safe_click(self.page, SEL_BTN_DESCARGAR, timeout=10_000)
        if not clicked:
            logger.warning("No se encontró botón 'Descargar Seleccionados'")
            return None

        await asyncio.sleep(3)
        await self._screenshot("10_download_requested")

        # Extraer folio UUID de la respuesta
        content = await self.page.content()
        folio_match = re.search(
            r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}",
            content,
        )
        folio = folio_match.group(0) if folio_match else None
        if folio:
            logger.info("📋 Folio de descarga: %s", folio)
            self.folios.append(folio)
        else:
            logger.warning("No se encontró folio UUID en la respuesta")

        # Cerrar alerta del SAT si aparece
        try:
            alert = self.page.locator(SEL_ALERT_CLOSE)
            if await alert.count() > 0:
                await alert.click()
                await asyncio.sleep(1)
        except Exception:
            pass

        return folio

    # ──────────────────────────────────────────────────────────────────
    #  RECUPERAR DESCARGAS (POLLING)
    # ──────────────────────────────────────────────────────────────────

    async def _recover_downloads(self, bot, max_retries: int = 12) -> list[Path]:
        """
        Navega a 'Recuperar Descargas', hace polling hasta que el ZIP esté listo.

        Polling: hasta max_retries intentos con 30s entre cada uno (~6 min total).
        """
        logger.info("📦 Recuperando descargas (polling hasta %d min)...", max_retries // 2)
        downloaded_files: list[Path] = []

        for attempt in range(max_retries):
            # Cargar página de recuperación
            await self.page.goto(
                SAT_PORTAL_URL,
                wait_until="domcontentloaded",
                timeout=self.config.browser_timeout,
            )
            await asyncio.sleep(2)
            clicked = await safe_click(self.page, [SEL_RECUPERAR_DESCARGAS], timeout=10_000)
            if not clicked:
                logger.warning("No se encontró link 'Recuperar Descargas'")
                return []

            await asyncio.sleep(3)
            if attempt == 0 or attempt == max_retries - 1:
                await self._screenshot(f"11_recover_attempt_{attempt}")

            # Buscar botones de descarga en la tabla de recuperación
            rows = self.page.locator("table tr")
            row_count = await rows.count()
            logger.info(
                "Polling %d/%d: %d filas en tabla de recuperación",
                attempt + 1, max_retries, row_count,
            )

            for i in range(row_count):
                row = rows.nth(i)
                btn = row.locator(
                    'a[title*="descarga" i], button[title*="descarga" i], '
                    'img[title*="descarga" i], input[title*="descarga" i], '
                    'span[id*="descarga" i], span[title*="descarga" i], '
                    'a:has-text("Descargar"), input[id*="descarga" i]'
                )
                if await btn.count() > 0:
                    try:
                        path = await bot.wait_for_download(
                            lambda b=btn: b.first.click()
                        )
                        if path:
                            logger.info("✅ ZIP descargado: %s", path.name)
                            downloaded_files.append(path)
                    except Exception as e:
                        logger.warning("Descarga de fila %d falló: %s", i, e)

            if downloaded_files:
                break

            if attempt < max_retries - 1:
                logger.info("⏳ ZIP no listo. Esperando 30s antes de reintentar...")
                await asyncio.sleep(30)

        logger.info("📥 %d archivos descargados", len(downloaded_files))
        return downloaded_files

    # ──────────────────────────────────────────────────────────────────
    #  DESCARGA INDIVIDUAL (FALLBACK)
    # ──────────────────────────────────────────────────────────────────

    async def _try_individual_downloads(self, bot) -> list[Path]:
        """Fallback: descarga XMLs individualmente desde la tabla de resultados."""
        logger.info("🔄 Intentando descargas individuales como fallback...")
        downloaded: list[Path] = []

        links = self.page.locator('a[title*="Descargar"]')
        count = await links.count()
        logger.info("Encontrados %d links de descarga individual", count)

        for i in range(min(count, 50)):  # Tope de 50 para no saturar
            try:
                path = await bot.wait_for_download(
                    lambda idx=i: links.nth(idx).click()
                )
                if path:
                    downloaded.append(path)
                await asyncio.sleep(1)
            except Exception as e:
                logger.warning("Descarga individual %d falló: %s", i, e)

        return downloaded

    # ──────────────────────────────────────────────────────────────────
    #  PIPELINE PRINCIPAL POR MES
    # ──────────────────────────────────────────────────────────────────

    async def download_month(
        self, bot, year: int, month: int, tipo: str = "recibidos"
    ) -> list[Path]:
        """
        Pipeline completo para un mes: navegar → filtrar → buscar → descargar.

        Args:
            bot: BrowserBot (necesario para wait_for_download)
            year: Año a consultar
            month: Mes a consultar (1-12)
            tipo: 'recibidos' o 'emitidos'

        Returns:
            Lista de Paths a archivos descargados (ZIPs o XMLs)
        """
        if not self.logged_in:
            await self.login()

        await self._navigate_to_query(tipo)
        await self._set_filters(year, month)

        searched = await self._click_search()
        if not searched:
            logger.error("Búsqueda falló")
            return []

        # Verificar si hay resultados
        content = await self.page.content()
        if "No se encontraron" in content or "0 Registros" in content:
            logger.info("ℹ️ Sin resultados para %d-%02d (%s)", year, month, tipo)
            return []

        # Intentar descarga por paquete
        folio = await self._select_all_and_request_download()

        if folio:
            logger.info("⏳ Esperando %ds para que el SAT genere el ZIP...", 35)
            await asyncio.sleep(35)
            files = await self._recover_downloads(bot)
            if files:
                return files

        # Fallback: descargas individuales
        return await self._try_individual_downloads(bot)

    # ──────────────────────────────────────────────────────────────────
    #  LOGOUT
    # ──────────────────────────────────────────────────────────────────

    async def logout(self):
        """Cierra la sesión en el SAT."""
        try:
            await safe_click(self.page, SEL_VERIFY_LOGIN, timeout=5_000)
            self.logged_in = False
            logger.info("🚪 Sesión cerrada")
        except Exception:
            logger.warning("⚠️ No se pudo cerrar sesión automáticamente")
