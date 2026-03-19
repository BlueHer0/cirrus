"""CSF Scraper — Downloads Constancia de Situación Fiscal from SAT.

Human-like navigation flow (Fernando-confirmed):
1. Go to stable SAT info page (won't change)
2. Click "Obtén la Constancia" tab
3. Click "servicio" link (the one with target="_blank" and href containing "lanzador")
4. That redirects to the login page — login with FIEL there
5. After login, click "Generar Constancia"
6. Capture the downloaded PDF

This avoids hardcoding login URLs which SAT changes periodically.
"""

import asyncio
import logging

logger = logging.getLogger("core.csf_scraper")

# ── Stable entry point — SAT informational page ──────────────────────
CSF_INFO_URL = (
    "https://www.sat.gob.mx/portal/public/tramites/"
    "constancia-de-situacion-fiscal"
)

# ── Direct CSF page (fallback if login lands elsewhere) ──────────────
CSF_DIRECT_URL = (
    "https://rfcampc.siat.sat.gob.mx/PTSC/IdcSiat/"
    "IdcGeneraConstancia.jsf"
)


async def _safe_click(page, selectors, timeout=10_000):
    """Try clicking the first visible selector from a list."""
    if isinstance(selectors, str):
        selectors = [selectors]
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            await loc.wait_for(state="visible", timeout=timeout)
            await loc.click()
            logger.info("CSF: Clicked: %s", sel)
            return True
        except Exception:
            continue
    return False


async def _safe_fill(page, selectors, value, timeout=10_000):
    """Try filling the first visible input from a list of selectors."""
    if isinstance(selectors, str):
        selectors = [selectors]
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            await loc.wait_for(state="visible", timeout=timeout)
            await loc.fill(value)
            logger.info("CSF: Filled: %s", sel)
            return True
        except Exception:
            continue
    return False


async def _descargar_csf_async(cer_path, key_path, password):
    """Navigate SAT portal like a human and download CSF PDF."""
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
            ],
        )

        try:
            context = await browser.new_context(
                locale="es-MX",
                timezone_id="America/Mexico_City",
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/131.0.0.0 Safari/537.36"
                ),
                accept_downloads=True,
                viewport={"width": 1280, "height": 800},
            )
            context.set_default_timeout(60_000)
            page = await context.new_page()

            # Apply stealth if available
            try:
                from playwright_stealth import Stealth
                await Stealth().apply_stealth_async(page)
                logger.info("CSF: Stealth applied")
            except ImportError:
                pass

            # ══════════════════════════════════════════════════════════
            #  PASO 1: Ir a la página informativa (URL estable)
            # ══════════════════════════════════════════════════════════
            logger.info("CSF: Navegando a página informativa: %s", CSF_INFO_URL)
            await page.goto(CSF_INFO_URL, timeout=60000, wait_until="domcontentloaded")
            await asyncio.sleep(5)  # SvelteKit SPA needs JS to render

            logger.info("CSF: Info page loaded. URL: %s", page.url[:120])

            # ══════════════════════════════════════════════════════════
            #  PASO 2: Click en tab "Obtén la Constancia"
            # ══════════════════════════════════════════════════════════
            obten_tab = page.locator('text=Obtén la Constancia')
            if await obten_tab.count() > 0:
                logger.info("CSF: Clicking 'Obtén la Constancia' tab...")
                await obten_tab.first.click()
                await asyncio.sleep(2)
            else:
                logger.info("CSF: No 'Obtén la Constancia' tab found, continuing...")

            # ══════════════════════════════════════════════════════════
            #  PASO 3: Click en link "servicio" (the specific one)
            #  This link has target="_blank" and href containing "lanzador"
            #  or "operacion/43824" — NOT the nav "Trámites y servicios"
            # ══════════════════════════════════════════════════════════
            logger.info("CSF: Buscando link 'servicio'...")

            # Priority 1: exact link with lanzador.jsf in href
            servicio_link = page.locator('a[href*="lanzador"][href*="operacion"]')

            if await servicio_link.count() == 0:
                # Priority 2: link with operacion/43824 in href
                servicio_link = page.locator('a[href*="operacion/43824"]')

            if await servicio_link.count() == 0:
                # Priority 3: link with reimprime-tus-acuses in href
                servicio_link = page.locator('a[href*="reimprime-tus-acuses"]')

            if await servicio_link.count() == 0:
                # Priority 4: external link with text "servicio" and target=_blank
                servicio_link = page.locator('a[target="_blank"]:has-text("servicio")')

            if await servicio_link.count() == 0:
                # Priority 5: any link with text "servicio" that has target=_blank
                all_links = page.locator("a")
                link_count = await all_links.count()
                logger.info("CSF: Total links on page: %d", link_count)
                for i in range(link_count):
                    link = all_links.nth(i)
                    text = (await link.text_content() or "").strip().lower()
                    href = (await link.get_attribute("href") or "")
                    target = (await link.get_attribute("target") or "")
                    if "servicio" in text and target == "_blank":
                        logger.info(
                            "CSF: Found by scan: text='%s' href='%s'", text[:50], href[:80]
                        )
                        servicio_link = all_links.nth(i)
                        break

            count = await servicio_link.count() if hasattr(servicio_link, 'count') else 1
            if count == 0:
                await page.screenshot(path="/tmp/csf_no_servicio.png")
                html = await page.content()
                with open("/tmp/csf_page_debug.html", "w") as f:
                    f.write(html)
                raise Exception(
                    "No se encontró link 'servicio' en la página. "
                    "Debug: /tmp/csf_no_servicio.png, /tmp/csf_page_debug.html"
                )

            # Get the href for logging
            if hasattr(servicio_link, 'get_attribute'):
                servicio_href = await servicio_link.get_attribute("href") or ""
            else:
                servicio_href = await servicio_link.first.get_attribute("href") or ""
            logger.info("CSF: Found 'servicio' link → %s", servicio_href[:120])

            # Navigate directly to the href (instead of clicking, which opens a new tab)
            # This keeps cookies in the same context
            logger.info("CSF: Navigating to servicio URL...")
            await page.goto(servicio_href, timeout=60000, wait_until="domcontentloaded")
            await asyncio.sleep(5)
            await page.wait_for_load_state("domcontentloaded")

            logger.info("CSF: After servicio redirect → URL: %s", page.url[:120])
            logger.info("CSF: Title: %s", await page.title())

            # ══════════════════════════════════════════════════════════
            #  PASO 4: Login con FIEL
            # ══════════════════════════════════════════════════════════

            # 4a. Click e.firma / FIEL tab if present
            logger.info("CSF: Buscando opción de e.firma...")
            fiel_selectors = [
                "#buttonFiel",
                'input[id*="buttonFiel"]',
                'button[id*="buttonFiel"]',
                'a[id*="buttonFiel"]',
                'button:has-text("e.firma")',
                'a:has-text("e.firma")',
                'a:has-text("FIEL")',
                'a:has-text("Certificado")',
                'a:has-text("firma electrónica")',
                'input[value*="firma"]',
            ]
            clicked = await _safe_click(page, fiel_selectors, timeout=15_000)
            if clicked:
                await asyncio.sleep(3)
            else:
                logger.info("CSF: No e.firma tab found — may already be on FIEL form")

            # Debug: screenshot + log current state
            await page.screenshot(path="/tmp/csf_before_upload.png")

            # 4b. Upload .cer
            logger.info("CSF: Uploading .cer...")
            cer_uploaded = False
            for sel in ["#fileCertificate", 'input[name*="cer"]', 'input[accept*=".cer"]']:
                loc = page.locator(sel)
                if await loc.count() > 0:
                    await loc.first.set_input_files(cer_path)
                    cer_uploaded = True
                    logger.info("CSF: .cer uploaded via %s", sel)
                    break
            if not cer_uploaded:
                file_inputs = page.locator('input[type="file"]')
                fc = await file_inputs.count()
                logger.info("CSF: Found %d file input(s)", fc)
                if fc == 0:
                    await page.screenshot(path="/tmp/csf_no_file_inputs.png")
                    html = await page.content()
                    with open("/tmp/csf_login_page.html", "w") as f:
                        f.write(html)
                    # List all inputs for debug
                    all_inputs = page.locator("input")
                    inp_count = await all_inputs.count()
                    for i in range(min(inp_count, 20)):
                        inp = all_inputs.nth(i)
                        itype = await inp.get_attribute("type") or "text"
                        iname = await inp.get_attribute("name") or ""
                        iid = await inp.get_attribute("id") or ""
                        logger.info("  input[%d]: type=%s name=%s id=%s", i, itype, iname, iid)
                    raise Exception(
                        f"No file inputs on login page ({page.url[:80]}). "
                        "Debug: /tmp/csf_no_file_inputs.png, /tmp/csf_login_page.html"
                    )
                await file_inputs.nth(0).set_input_files(cer_path)
                logger.info("CSF: .cer uploaded via file input[0]")
            await asyncio.sleep(1.5)

            # 4c. Upload .key
            logger.info("CSF: Uploading .key...")
            key_uploaded = False
            for sel in ["#filePrivateKey", 'input[name*="key"]', 'input[accept*=".key"]']:
                loc = page.locator(sel)
                if await loc.count() > 0:
                    await loc.first.set_input_files(key_path)
                    key_uploaded = True
                    logger.info("CSF: .key uploaded via %s", sel)
                    break
            if not key_uploaded:
                file_inputs = page.locator('input[type="file"]')
                fc = await file_inputs.count()
                if fc >= 2:
                    await file_inputs.nth(1).set_input_files(key_path)
                    logger.info("CSF: .key uploaded via file input[1]")
                else:
                    await asyncio.sleep(2)
                    file_inputs2 = page.locator('input[type="file"]')
                    fc2 = await file_inputs2.count()
                    if fc2 >= 2:
                        await file_inputs2.nth(1).set_input_files(key_path)
                        logger.info("CSF: .key uploaded after wait (input[1])")
                    else:
                        raise Exception(
                            f"Only {fc2} file input(s), need at least 2 for .key"
                        )
            await asyncio.sleep(1.5)

            # 4d. Fill password
            pwd_selectors = [
                'input[type="password"][id*="privateKeyPassword"]',
                'input[type="password"]',
            ]
            filled = await _safe_fill(page, pwd_selectors, password, timeout=10_000)
            if not filled:
                raise Exception("No password field found on login page")
            logger.info("CSF: Password filled")

            # 4e. Submit
            submit_selectors = [
                "#submit",
                'input[id*="submit"]',
                'button[id*="submit"]',
                'input[type="submit"]',
                'input[value="Enviar"]',
                'button:has-text("Enviar")',
                'button:has-text("Ingresar")',
                'button:has-text("Aceptar")',
                "#btnEnviar",
            ]
            clicked = await _safe_click(page, submit_selectors, timeout=10_000)
            if not clicked:
                raise Exception("No submit button found on login page")
            logger.info("CSF: Login submitted")

            # 4f. Wait for authentication
            logger.info("CSF: Waiting for SAT authentication...")
            await asyncio.sleep(8)
            # Wait for full page load — the post-login page is a SPA
            try:
                await page.wait_for_load_state("networkidle", timeout=30000)
            except Exception:
                await page.wait_for_load_state("domcontentloaded")
            await asyncio.sleep(5)  # Extra time for SPA JS to render

            # Check for errors
            error_el = page.locator(".error, .alert-danger, .msgError")
            if await error_el.count() > 0:
                error_text = await error_el.first.text_content()
                await page.screenshot(path="/tmp/csf_login_failed.png")
                raise Exception(f"SAT login failed: {error_text}")

            logger.info("CSF: Post-login URL: %s", page.url[:120])
            await page.screenshot(path="/tmp/csf_post_login.png")

            # ══════════════════════════════════════════════════════════
            #  PASO 5: Find "Generar Constancia" — it's inside an iframe
            #  <iframe id="iframetoload" src="rfcampc.siat.sat.gob.mx/...">
            #  Use page.frames to access the cross-origin iframe content.
            # ══════════════════════════════════════════════════════════
            logger.info("CSF: Looking for 'Generar Constancia'...")
            # Wait for the iframe to load
            await asyncio.sleep(8)

            generar_selectors = [
                'input[value="Generar Constancia"]',
                'button:has-text("Generar Constancia")',
                'a:has-text("Generar Constancia")',
                'input[value*="Generar"]',
                'button:has-text("Generar")',
            ]

            generar_btn = None
            target_frame = page  # will be set to the iframe frame if found

            # List all frames and find the CSF iframe
            all_frames = page.frames
            logger.info("CSF: Page has %d frame(s)", len(all_frames))
            for fr in all_frames:
                logger.info("CSF:   frame: %s", fr.url[:120] if fr.url else "(empty)")

            # Find the iframe by URL pattern (rfcampc.siat.sat.gob.mx)
            for fr in all_frames:
                if fr.url and ("rfcampc" in fr.url or "siat.sat" in fr.url
                               or "ConsultaTramite" in fr.url
                               or "IdcSiat" in fr.url):
                    target_frame = fr
                    logger.info("CSF: Found CSF iframe: %s", fr.url[:120])
                    break

            # Search for the button in the target frame
            for sel in generar_selectors:
                try:
                    loc = target_frame.locator(sel).first
                    await loc.wait_for(state="visible", timeout=15_000)
                    generar_btn = loc
                    logger.info("CSF: Found 'Generar': %s", sel)
                    break
                except Exception:
                    continue

            # If not found in iframe, try main page as fallback
            if not generar_btn and target_frame != page:
                logger.info("CSF: Not in iframe, trying main page...")
                for sel in generar_selectors:
                    loc = page.locator(sel)
                    if await loc.count() > 0:
                        generar_btn = loc.first
                        break

            if not generar_btn:
                await page.screenshot(path="/tmp/csf_no_generar.png")
                html = await page.content()
                with open("/tmp/csf_generar_page.html", "w") as f:
                    f.write(html)
                raise Exception(
                    "No 'Generar Constancia' button found. "
                    "Debug: /tmp/csf_no_generar.png"
                )

            # ══════════════════════════════════════════════════════════
            #  PASO 6: Click "Generar" and capture PDF
            # ══════════════════════════════════════════════════════════
            logger.info("CSF: Clicking 'Generar Constancia'...")

            # Try download event
            try:
                async with page.expect_download(timeout=30000) as dl_info:
                    await generar_btn.click()
                download = await dl_info.value
                dl_path = await download.path()
                if dl_path:
                    with open(dl_path, "rb") as f:
                        pdf_bytes = f.read()
                    logger.info("CSF: PDF downloaded: %d bytes", len(pdf_bytes))
                    return pdf_bytes
            except Exception as dl_err:
                logger.info(
                    "CSF: No download event (%s), trying alternatives...",
                    str(dl_err)[:60],
                )

            await asyncio.sleep(5)

            # Check for new tab with PDF
            all_pages = context.pages
            if len(all_pages) > 1:
                new_pg = all_pages[-1]
                await new_pg.wait_for_load_state("domcontentloaded")
                new_url = new_pg.url
                logger.info("CSF: New tab: %s", new_url[:120])

                if ".pdf" in new_url.lower():
                    resp = await new_pg.request.fetch(new_url)
                    pdf_bytes = await resp.body()
                    logger.info("CSF: PDF from URL: %d bytes", len(pdf_bytes))
                    return pdf_bytes

                # Print page as PDF
                pdf_bytes = await new_pg.pdf(
                    format="Letter", print_background=True
                )
                if pdf_bytes and len(pdf_bytes) > 1000:
                    logger.info("CSF: PDF from new tab print: %d bytes", len(pdf_bytes))
                    return pdf_bytes

            # Check for embedded PDF
            pdf_embed = page.locator(
                'embed[type="application/pdf"], '
                'iframe[src*=".pdf"], '
                'object[type="application/pdf"]'
            )
            if await pdf_embed.count() > 0:
                src = (
                    await pdf_embed.first.get_attribute("src")
                    or await pdf_embed.first.get_attribute("data")
                )
                if src:
                    logger.info("CSF: Embedded PDF: %s", src[:80])
                    resp = await page.request.fetch(src)
                    return await resp.body()

            # Fallback: print current page
            logger.info("CSF: Fallback — printing page as PDF...")
            pdf_bytes = await page.pdf(
                format="Letter", print_background=True
            )
            if pdf_bytes and len(pdf_bytes) > 1000:
                logger.info("CSF: Page-print PDF: %d bytes", len(pdf_bytes))
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
