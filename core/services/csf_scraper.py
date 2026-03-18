"""CSF Scraper — Downloads Constancia de Situación Fiscal from SAT.

Uses Playwright to:
1. Login to SAT portal with e.firma (FIEL)
2. Navigate to Constancia de Situación Fiscal
3. Download the PDF

IMPORTANT: SAT portal selectors may need iterative adjustment.
"""

import asyncio
import logging

logger = logging.getLogger("core.csf_scraper")


async def _descargar_csf_async(cer_path, key_path, password):
    """Navigate SAT portal and download CSF PDF."""
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
            ],
        )

        try:
            context = await browser.new_context(
                accept_downloads=True,
                viewport={"width": 1280, "height": 720},
            )
            page = await context.new_page()

            # Step 1: Go to the SAT CFDIAU login for CSF
            login_url = (
                "https://loginda.siat.sat.gob.mx/nidp/idff/sso"
                "?id=mat-ptsc-totp&sid=0&option=credential&sid=0"
            )
            logger.info("CSF: Navigating to SAT login...")
            await page.goto(login_url, timeout=60000, wait_until="domcontentloaded")
            await page.wait_for_timeout(3000)

            # Step 2: Click "e.firma" tab if available
            fiel_tab = page.locator(
                'a:has-text("e.firma"), '
                'button:has-text("e.firma"), '
                'a:has-text("FIEL")'
            )
            if await fiel_tab.count() > 0:
                await fiel_tab.first.click()
                await page.wait_for_timeout(2000)

            # Step 3: Upload .cer
            file_inputs = page.locator('input[type="file"]')
            cer_input = file_inputs.first
            await cer_input.set_input_files(cer_path)
            await page.wait_for_timeout(1000)

            # Step 4: Upload .key
            key_input = file_inputs.nth(1)
            await key_input.set_input_files(key_path)
            await page.wait_for_timeout(1000)

            # Step 5: Fill password
            pwd_input = page.locator('input[type="password"]')
            await pwd_input.fill(password)

            # Step 6: Submit login
            submit_btn = page.locator(
                'button[type="submit"], '
                'input[type="submit"], '
                'button:has-text("Enviar"), '
                'button:has-text("Aceptar")'
            )
            await submit_btn.first.click()

            # Step 7: Wait for login to complete
            logger.info("CSF: Waiting for SAT authentication...")
            await page.wait_for_timeout(10000)
            await page.wait_for_load_state("domcontentloaded")

            # Check for login errors
            error_el = page.locator(".error, .alert-danger, .msgError")
            if await error_el.count() > 0:
                error_text = await error_el.first.text_content()
                raise Exception(f"SAT login failed: {error_text}")

            # Step 8: Navigate to CSF page
            csf_url = (
                "https://rfcampc.siat.sat.gob.mx/PTSC/IdcSiat/"
                "IdcGeneraConstancia.jsf"
            )
            logger.info("CSF: Navigating to CSF generation page...")
            await page.goto(csf_url, timeout=60000, wait_until="domcontentloaded")
            await page.wait_for_timeout(5000)

            # Step 9: Look for "Generar Constancia" button
            generar_btn = page.locator(
                'button:has-text("Generar"), '
                'a:has-text("Generar"), '
                'input[value*="Generar"], '
                'button:has-text("Constancia"), '
                'a:has-text("Imprimir")'
            )

            if await generar_btn.count() > 0:
                # Try download approach
                try:
                    async with page.expect_download(timeout=30000) as download_info:
                        await generar_btn.first.click()
                    download = await download_info.value
                    path = await download.path()
                    if path:
                        with open(path, "rb") as f:
                            pdf_bytes = f.read()
                        logger.info("CSF: Downloaded %d bytes", len(pdf_bytes))
                        return pdf_bytes
                except Exception:
                    # If no download triggered, try printing the page as PDF
                    logger.info("CSF: No download triggered, trying page PDF...")
                    await generar_btn.first.click()
                    await page.wait_for_timeout(5000)

            # Fallback: Print current page as PDF
            logger.info("CSF: Generating PDF from page content...")
            pdf_bytes = await page.pdf(format="Letter", print_background=True)
            if pdf_bytes and len(pdf_bytes) > 1000:
                logger.info("CSF: Generated PDF from page, %d bytes", len(pdf_bytes))
                return pdf_bytes

            # Debug: save screenshot
            await page.screenshot(path="/tmp/csf_debug.png")
            raise Exception("Could not download or generate CSF PDF")

        finally:
            await browser.close()


def descargar_csf(cer_path, key_path, password):
    """Sync wrapper for the async CSF scraper."""
    return asyncio.run(_descargar_csf_async(cer_path, key_path, password))
