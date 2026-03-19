"""CSF Scraper — Downloads Constancia de Situación Fiscal from SAT.

Uses Playwright to:
1. Login via the CFDIAU FIEL portal (SSO session across SAT subdomains)
2. Navigate to the Constancia generation page at rfcampc.siat.sat.gob.mx
3. Click "Generar Constancia" button
4. Capture the downloaded PDF

Login URL: https://cfdiau.sat.gob.mx/nidp/wsfed/ep?id=SATUPCFDiCon&sid=0&option=credential&sid=0
CSF URL:   https://rfcampc.siat.sat.gob.mx/PTSC/IdcSiat/IdcGeneraConstancia.jsf
"""

import asyncio
import logging

logger = logging.getLogger("core.csf_scraper")

# The CFDIAU login URL — same portal used for CFDIs, creates SSO session
LOGIN_URL = (
    "https://cfdiau.sat.gob.mx/nidp/wsfed/ep"
    "?id=SATUPCFDiCon&sid=0&option=credential&sid=0"
)

# The target CSF page (requires active session from CFDIAU login)
CSF_URL = (
    "https://rfcampc.siat.sat.gob.mx/PTSC/IdcSiat/"
    "IdcGeneraConstancia.jsf"
)


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

            # ── Step 1: Go to CFDIAU FIEL login page ──
            logger.info("CSF: Navigating to CFDIAU login portal...")
            await page.goto(LOGIN_URL, timeout=60000, wait_until="domcontentloaded")
            await page.wait_for_timeout(5000)

            logger.info("CSF: Login page URL: %s", page.url[:120])
            logger.info("CSF: Login page title: %s", await page.title())

            # ── Step 2: Click e.firma / FIEL tab if present ──
            fiel_options = page.locator(
                'a:has-text("e.firma"), a:has-text("FIEL"), '
                'a:has-text("firma"), button:has-text("e.firma"), '
                'a:has-text("Certificado"), input[value*="firma"]'
            )
            fiel_count = await fiel_options.count()
            logger.info("CSF: Found %d e.firma tab(s)", fiel_count)
            if fiel_count > 0:
                logger.info("CSF: Clicking e.firma tab...")
                await fiel_options.first.click()
                await page.wait_for_timeout(3000)

            # ── Step 3: Upload .cer file ──
            file_inputs = page.locator('input[type="file"]')
            file_count = await file_inputs.count()
            logger.info("CSF: Found %d file input(s)", file_count)

            if file_count == 0:
                # Save debug info
                await page.screenshot(path="/tmp/csf_no_file_inputs.png")
                html = await page.content()
                with open("/tmp/csf_login_page.html", "w") as f:
                    f.write(html)
                # List visible elements for debugging
                all_inputs = page.locator("input")
                inp_count = await all_inputs.count()
                for i in range(min(inp_count, 15)):
                    inp = all_inputs.nth(i)
                    itype = await inp.get_attribute("type") or "text"
                    iname = await inp.get_attribute("name") or ""
                    logger.info("  input[%d]: type=%s name=%s", i, itype, iname)
                raise Exception(
                    f"No file inputs found on login page ({page.url[:80]}). "
                    "Debug: /tmp/csf_no_file_inputs.png"
                )

            if file_count >= 2:
                logger.info("CSF: Uploading .cer...")
                await file_inputs.nth(0).set_input_files(cer_path)
                await page.wait_for_timeout(1000)
                logger.info("CSF: Uploading .key...")
                await file_inputs.nth(1).set_input_files(key_path)
                await page.wait_for_timeout(1000)
            else:
                logger.info("CSF: Single file input — uploading .cer first...")
                await file_inputs.nth(0).set_input_files(cer_path)
                await page.wait_for_timeout(2000)
                file_inputs2 = page.locator('input[type="file"]')
                fc2 = await file_inputs2.count()
                if fc2 >= 2:
                    logger.info("CSF: Second input appeared, uploading .key...")
                    await file_inputs2.nth(1).set_input_files(key_path)
                await page.wait_for_timeout(1000)

            # ── Step 4: Fill password ──
            pwd_input = page.locator('input[type="password"]')
            if await pwd_input.count() > 0:
                logger.info("CSF: Filling FIEL password...")
                await pwd_input.first.fill(password)
            else:
                raise Exception("No password field found on login page")

            # ── Step 5: Submit login ──
            submit_btn = page.locator(
                'button[type="submit"], input[type="submit"], '
                'button:has-text("Enviar"), button:has-text("Ingresar"), '
                'input[value="Enviar"], input[value="Ingresar"]'
            )
            if await submit_btn.count() > 0:
                logger.info("CSF: Submitting FIEL login...")
                await submit_btn.first.click()
            else:
                raise Exception("No submit button found on login page")

            # ── Step 6: Wait for login to complete ──
            logger.info("CSF: Waiting for SAT authentication...")
            await page.wait_for_timeout(10000)
            await page.wait_for_load_state("domcontentloaded")

            # Check for login errors
            error_el = page.locator(".error, .alert-danger, .msgError")
            if await error_el.count() > 0:
                error_text = await error_el.first.text_content()
                await page.screenshot(path="/tmp/csf_login_failed.png")
                raise Exception(f"SAT login failed: {error_text}")

            logger.info("CSF: Post-login URL: %s", page.url[:120])

            # ── Step 7: Navigate to CSF page ──
            logger.info("CSF: Navigating to Generar Constancia page...")
            await page.goto(CSF_URL, timeout=60000, wait_until="domcontentloaded")
            await page.wait_for_timeout(5000)

            logger.info("CSF: CSF page URL: %s", page.url[:120])

            # Check for session errors
            error_msgs = page.locator(".ui-messages-error-summary")
            if await error_msgs.count() > 0:
                for i in range(await error_msgs.count()):
                    msg = await error_msgs.nth(i).text_content()
                    logger.warning("CSF: SAT error message: %s", msg)
                await page.screenshot(path="/tmp/csf_session_error.png")
                raise Exception(
                    "SAT session error on CSF page. "
                    "Debug: /tmp/csf_session_error.png"
                )

            # ── Step 8: Click "Generar Constancia" ──
            generar_btn = page.locator(
                'input[value="Generar Constancia"], '
                'button:has-text("Generar Constancia"), '
                'a:has-text("Generar Constancia"), '
                'input[value*="Generar"], '
                'button:has-text("Generar")'
            )

            btn_count = await generar_btn.count()
            if btn_count == 0:
                await page.screenshot(path="/tmp/csf_no_button.png")
                html = await page.content()
                with open("/tmp/csf_page.html", "w") as f:
                    f.write(html)
                raise Exception(
                    "No 'Generar Constancia' button found. "
                    "Screenshot: /tmp/csf_no_button.png"
                )

            logger.info("CSF: Found %d 'Generar' button(s), clicking...", btn_count)

            # Try to capture download
            try:
                async with page.expect_download(timeout=30000) as dl_info:
                    await generar_btn.first.click()
                download = await dl_info.value
                dl_path = await download.path()
                if dl_path:
                    with open(dl_path, "rb") as f:
                        pdf_bytes = f.read()
                    logger.info(
                        "CSF: Downloaded %d bytes via download event", len(pdf_bytes)
                    )
                    return pdf_bytes
            except Exception as dl_err:
                logger.info(
                    "CSF: No download event (%s), checking alternatives...",
                    str(dl_err)[:60],
                )

            await page.wait_for_timeout(5000)

            # Check for new tab/popup with PDF
            pages = context.pages
            if len(pages) > 1:
                new_page = pages[-1]
                await new_page.wait_for_load_state("domcontentloaded")
                new_url = new_page.url
                logger.info("CSF: New tab opened: %s", new_url[:120])

                if ".pdf" in new_url.lower():
                    resp = await new_page.request.fetch(new_url)
                    pdf_bytes = await resp.body()
                    logger.info("CSF: Got %d bytes from PDF URL", len(pdf_bytes))
                    return pdf_bytes

                # Print new page as PDF
                pdf_bytes = await new_page.pdf(
                    format="Letter", print_background=True
                )
                if pdf_bytes and len(pdf_bytes) > 1000:
                    logger.info(
                        "CSF: Generated PDF from new tab, %d bytes", len(pdf_bytes)
                    )
                    return pdf_bytes

            # Check for embedded PDF
            pdf_embed = page.locator(
                'embed[type="application/pdf"], '
                'iframe[src*=".pdf"], '
                'object[type="application/pdf"]'
            )
            if await pdf_embed.count() > 0:
                src = await pdf_embed.first.get_attribute("src")
                if not src:
                    src = await pdf_embed.first.get_attribute("data")
                if src:
                    logger.info("CSF: Found embedded PDF: %s", src[:80])
                    resp = await page.request.fetch(src)
                    return await resp.body()

            # Fallback: print current page as PDF
            logger.info("CSF: Fallback — generating PDF from page content...")
            pdf_bytes = await page.pdf(format="Letter", print_background=True)
            if pdf_bytes and len(pdf_bytes) > 1000:
                logger.info("CSF: Generated PDF from page, %d bytes", len(pdf_bytes))
                return pdf_bytes

            await page.screenshot(path="/tmp/csf_after_click.png")
            raise Exception(
                "Could not capture CSF PDF. "
                "Screenshot: /tmp/csf_after_click.png"
            )

        finally:
            await browser.close()


def descargar_csf(cer_path, key_path, password):
    """Sync wrapper for the async CSF scraper."""
    return asyncio.run(_descargar_csf_async(cer_path, key_path, password))
