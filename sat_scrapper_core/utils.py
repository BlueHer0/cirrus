"""
Utilidades compartidas: retry, screenshots, safe actions, logging.

Merge de ambas implementaciones:
- safe_click/safe_fill aceptan list[str] (ZL)
- retry_async usa asyncio.sleep (ZL)
- Screenshots configurables (SatScrapper)
"""

from __future__ import annotations

import asyncio
import functools
import logging
import sys
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("sat_scrapper_core")


def setup_logging(verbose: bool = False) -> logging.Logger:
    """Configura logging con formato bonito para consola."""
    level = logging.DEBUG if verbose else logging.INFO
    fmt = "%(asctime)s │ %(levelname)-7s │ %(message)s"
    datefmt = "%H:%M:%S"

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(fmt, datefmt=datefmt))

    root = logging.getLogger("sat_scrapper_core")
    root.setLevel(level)
    if not root.handlers:
        root.addHandler(handler)
    return root


def retry_async(retries: int = 3, delay: float = 5.0):
    """Decorator para reintentar funciones async en caso de error."""
    def decorator(func):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            last_exc = None
            for attempt in range(1, retries + 1):
                try:
                    return await func(*args, **kwargs)
                except Exception as e:
                    last_exc = e
                    logger.warning(
                        "⚠️ %s intento %d/%d falló: %s",
                        func.__name__, attempt, retries, e,
                    )
                    if attempt < retries:
                        logger.info("⏳ Reintentando en %.1fs...", delay)
                        await asyncio.sleep(delay)
            raise last_exc  # type: ignore
        return wrapper
    return decorator


async def screenshot(page, label: str = "step", directory: Path | None = None):
    """Captura un screenshot del estado actual del navegador."""
    if directory is None:
        directory = Path("./screenshots")
    directory.mkdir(parents=True, exist_ok=True)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_label = label.replace(" ", "_").lower()
    filepath = directory / f"{ts}_{safe_label}.png"

    try:
        await page.screenshot(path=str(filepath), full_page=False)
        logger.debug("📸 Screenshot: %s", filepath.name)
    except Exception as e:
        logger.warning("⚠️ No se pudo tomar screenshot: %s", e)

    return filepath


async def safe_click(page, selectors: list[str] | str, timeout: int = 10_000) -> bool:
    """
    Intenta hacer click en el primer selector visible de una lista.

    Args:
        page: Playwright page
        selectors: Selector CSS/XPath o lista de selectores a intentar en orden
        timeout: Tiempo máximo de espera por selector (ms)
    """
    if isinstance(selectors, str):
        selectors = [selectors]

    for sel in selectors:
        try:
            loc = page.locator(sel).first
            await loc.wait_for(state="visible", timeout=timeout)
            await loc.click()
            logger.info("✅ Clicked: %s", sel)
            return True
        except Exception:
            continue

    logger.warning("❌ No se pudo hacer click en: %s", selectors)
    return False


async def safe_fill(page, selectors: list[str] | str, value: str, timeout: int = 10_000) -> bool:
    """
    Intenta llenar el primer input visible de una lista de selectores.

    Args:
        page: Playwright page
        selectors: Selector CSS/XPath o lista de selectores a intentar en orden
        value: Valor a escribir
        timeout: Tiempo máximo de espera por selector (ms)
    """
    if isinstance(selectors, str):
        selectors = [selectors]

    for sel in selectors:
        try:
            loc = page.locator(sel).first
            await loc.wait_for(state="visible", timeout=timeout)
            await loc.fill(value)
            logger.info("✅ Filled: %s", sel)
            return True
        except Exception:
            continue

    logger.warning("❌ No se pudo llenar: %s", selectors)
    return False


async def wait_for_any(page, selectors: list[str], timeout: int = 15_000) -> str | None:
    """
    Espera a que cualquiera de los selectores aparezca.

    Returns:
        El selector que apareció primero, o None si ninguno apareció.
    """
    per_selector_timeout = max(timeout // len(selectors), 2000) if selectors else timeout
    for sel in selectors:
        try:
            await page.locator(sel).first.wait_for(state="visible", timeout=per_selector_timeout)
            return sel
        except Exception:
            continue
    return None
