"""
SAT Health Worker — servicio ligero que ejecuta probes de login al SAT.
Se instala en cada nodo y escucha en puerto 8300 (solo red interna).

Selectores tomados de sat_scrapper_core/config.py (battle-tested).
"""
from fastapi import FastAPI, HTTPException, Depends, Header
from pydantic import BaseModel
import asyncio
import time
import tempfile
import os
import base64
import shutil

app = FastAPI(title="SAT Health Worker")

# ── Configuración ────────────────────────────────────────────────────────
NODE_ID = os.environ.get("SAT_WORKER_NODE_ID", "unknown")
NODE_IP = os.environ.get("SAT_WORKER_NODE_IP", "0.0.0.0")
SAT_HEALTH_TOKEN = os.environ.get("SAT_HEALTH_TOKEN", "")
LISTEN_PORT = 8300

# ── Selectores SAT (copiados de sat_scrapper_core/config.py) ─────────────
SAT_PORTAL_URL = "https://portalcfdi.facturaelectronica.sat.gob.mx/"

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


# ── Auth ──────────────────────────────────────────────────────────────────
async def verify_token(authorization: str = Header(default="")):
    """Verify Bearer token from cerebro."""
    if not SAT_HEALTH_TOKEN:
        return  # No token configured = no auth (dev mode)
    expected = f"Bearer {SAT_HEALTH_TOKEN}"
    if authorization != expected:
        raise HTTPException(status_code=401, detail="Invalid token")


# ── Schemas ───────────────────────────────────────────────────────────────
class ProbeRequest(BaseModel):
    """Instrucción del cerebro para ejecutar un probe."""
    probe_id: str  # UUID asignado por el cerebro
    rfc: str
    cer_b64: str  # archivo .cer en base64
    key_b64: str  # archivo .key en base64
    fiel_password: str  # password desencriptado
    sat_url: str = SAT_PORTAL_URL
    timeout_seconds: int = 90


class ProbeResponse(BaseModel):
    """Resultado del probe."""
    probe_id: str
    node_id: str
    node_ip: str
    result: str
    last_phase_reached: str
    error_message: str = ""
    http_status: int | None = None
    time_dns_ms: int | None = None
    time_page_load_ms: int | None = None
    time_form_visible_ms: int | None = None
    time_fiel_upload_ms: int | None = None
    time_login_submit_ms: int | None = None
    time_session_active_ms: int | None = None
    time_total_ms: int = 0
    screenshot_b64: str = ""
    user_agent: str = ""


# ── Helpers ───────────────────────────────────────────────────────────────
async def _safe_click(page, selectors, timeout=10_000):
    """Try clicking the first matching selector from a list."""
    if isinstance(selectors, str):
        selectors = [selectors]
    for sel in selectors:
        try:
            loc = page.locator(sel)
            if await loc.count() > 0:
                await loc.first.click(timeout=timeout)
                return True
        except Exception:
            continue
    return False


async def _safe_fill(page, selectors, value, timeout=10_000):
    """Try filling the first matching selector from a list."""
    if isinstance(selectors, str):
        selectors = [selectors]
    for sel in selectors:
        try:
            loc = page.locator(sel)
            if await loc.count() > 0:
                await loc.first.fill(value, timeout=timeout)
                return True
        except Exception:
            continue
    return False


# ── Main Probe Endpoint ──────────────────────────────────────────────────
@app.post("/probe", response_model=ProbeResponse, dependencies=[Depends(verify_token)])
async def execute_probe(req: ProbeRequest):
    """
    Ejecuta un intento de login al SAT midiendo tiempos por fase.

    Fases:
    1. dns — navegar a sat_url (DNS + conexión)
    2. page_load — esperar carga completa
    3. form_visible — detectar formulario FIEL visible
    4. fiel_upload — subir archivos .cer y .key + password
    5. login_submit — enviar formulario
    6. session_active — detectar indicador de sesión activa
    """
    from playwright.async_api import async_playwright

    t_start = time.time()
    timings = {}
    last_phase = "dns"
    result = "unknown"
    error_msg = ""
    http_status = None
    screenshot_b64 = ""
    user_agent = ""

    # Escribir archivos FIEL temporales
    tmp_dir = tempfile.mkdtemp(prefix="sat_probe_")
    cer_path = os.path.join(tmp_dir, f"{req.rfc}.cer")
    key_path = os.path.join(tmp_dir, f"{req.rfc}.key")

    try:
        with open(cer_path, "wb") as f:
            f.write(base64.b64decode(req.cer_b64))
        with open(key_path, "wb") as f:
            f.write(base64.b64decode(req.key_b64))

        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=[
                    '--no-sandbox',
                    '--disable-gpu',
                    '--disable-dev-shm-usage',
                    '--disable-setuid-sandbox',
                    '--disable-blink-features=AutomationControlled',
                ]
            )
            context = await browser.new_context(
                viewport={'width': 1280, 'height': 720},
                locale='es-MX',
                timezone_id='America/Mexico_City',
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/131.0.0.0 Safari/537.36"
                ),
            )
            page = await context.new_page()
            user_agent = await page.evaluate("navigator.userAgent")

            try:
                # === FASE 1: DNS / Navegación ===
                t0 = time.time()
                response = await page.goto(
                    req.sat_url,
                    timeout=req.timeout_seconds * 1000,
                    wait_until="domcontentloaded",
                )
                timings['dns'] = int((time.time() - t0) * 1000)
                last_phase = "dns"

                if response:
                    http_status = response.status

                # === FASE 2: Page Load completo ===
                t0 = time.time()
                await page.wait_for_load_state("networkidle", timeout=30000)
                timings['page_load'] = int((time.time() - t0) * 1000)
                last_phase = "page_load"

                await asyncio.sleep(2)

                # === FASE 3: Formulario visible ===
                # Click botón e.firma primero
                t0 = time.time()
                clicked = await _safe_click(page, SEL_BTN_FIEL, timeout=15_000)
                if not clicked:
                    result = "page_error"
                    error_msg = "No se encontró el botón de e.firma"
                    timings['form_visible'] = int((time.time() - t0) * 1000)
                    raise Exception(error_msg)

                await asyncio.sleep(2)

                # Esperar inputs de archivo
                await page.locator(SEL_UPLOAD_CER).wait_for(state="attached", timeout=15_000)
                timings['form_visible'] = int((time.time() - t0) * 1000)
                last_phase = "form_visible"

                # === FASE 4: Subir FIEL (.cer, .key, password) ===
                t0 = time.time()

                # Subir .cer
                try:
                    await page.locator(SEL_UPLOAD_CER).set_input_files(cer_path)
                except Exception:
                    inputs = page.locator('input[type="file"]')
                    count = await inputs.count()
                    if count >= 1:
                        await inputs.nth(0).set_input_files(cer_path)
                    else:
                        raise Exception("No se encontró input para subir .cer")

                # Subir .key
                try:
                    await page.locator(SEL_UPLOAD_KEY).set_input_files(key_path)
                except Exception:
                    inputs = page.locator('input[type="file"]')
                    count = await inputs.count()
                    if count >= 2:
                        await inputs.nth(1).set_input_files(key_path)
                    else:
                        raise Exception("No se encontró input para subir .key")

                # Ingresar password
                filled = await _safe_fill(page, SEL_PASSWORD, req.fiel_password, timeout=10_000)
                if not filled:
                    raise Exception("No se encontró el campo de contraseña")

                timings['fiel_upload'] = int((time.time() - t0) * 1000)
                last_phase = "fiel_upload"

                # === FASE 5: Enviar login ===
                t0 = time.time()
                clicked = await _safe_click(page, SEL_BTN_SUBMIT, timeout=10_000)
                if not clicked:
                    raise Exception("No se encontró el botón de enviar")

                timings['login_submit'] = int((time.time() - t0) * 1000)
                last_phase = "login_submit"

                # === FASE 6: Sesión activa ===
                t0 = time.time()
                await asyncio.sleep(5)

                # Verificar login con selectores reales
                multiplexed = ", ".join(SEL_VERIFY_LOGIN)
                try:
                    await page.locator(multiplexed).first.wait_for(
                        state="visible", timeout=15_000,
                    )
                    timings['session_active'] = int((time.time() - t0) * 1000)
                    last_phase = "session_active"
                    result = "success"
                except Exception:
                    # Fallback: verificar URL
                    url = page.url
                    if "Consulta" in url or "Portal" in url:
                        timings['session_active'] = int((time.time() - t0) * 1000)
                        last_phase = "session_active"
                        result = "success"
                    else:
                        timings['session_active'] = int((time.time() - t0) * 1000)
                        result = "login_failed"
                        error_msg = "No se encontró indicador de sesión activa después de enviar login"

                        # Screenshot en fallo
                        screenshot_bytes = await page.screenshot(full_page=False)
                        screenshot_b64 = base64.b64encode(screenshot_bytes).decode()

            except asyncio.TimeoutError:
                result = "timeout"
                error_msg = f"Timeout en fase {last_phase}"
                try:
                    screenshot_bytes = await page.screenshot(full_page=False)
                    screenshot_b64 = base64.b64encode(screenshot_bytes).decode()
                except Exception:
                    pass

            except Exception as e:
                error_str = str(e).lower()
                if "net::" in error_str or "dns" in error_str:
                    result = "network_error"
                elif "maintenance" in error_str or "mantenimiento" in error_str:
                    result = "maintenance"
                elif "captcha" in error_str:
                    result = "captcha"
                elif result == "unknown":
                    result = "page_error"
                error_msg = str(e)[:500]

                try:
                    screenshot_bytes = await page.screenshot(full_page=False)
                    screenshot_b64 = base64.b64encode(screenshot_bytes).decode()
                except Exception:
                    pass

            finally:
                await browser.close()

    except Exception as e:
        if result == "unknown":
            result = "browser_error"
            error_msg = f"Error inicializando browser: {str(e)[:500]}"

    finally:
        # Limpiar archivos temporales
        shutil.rmtree(tmp_dir, ignore_errors=True)

    t_total = int((time.time() - t_start) * 1000)

    return ProbeResponse(
        probe_id=req.probe_id,
        node_id=NODE_ID,
        node_ip=NODE_IP,
        result=result,
        last_phase_reached=last_phase,
        error_message=error_msg,
        http_status=http_status,
        time_dns_ms=timings.get('dns'),
        time_page_load_ms=timings.get('page_load'),
        time_form_visible_ms=timings.get('form_visible'),
        time_fiel_upload_ms=timings.get('fiel_upload'),
        time_login_submit_ms=timings.get('login_submit'),
        time_session_active_ms=timings.get('session_active'),
        time_total_ms=t_total,
        screenshot_b64=screenshot_b64,
        user_agent=user_agent,
    )


@app.get("/health")
async def health():
    """Health check del worker."""
    return {"status": "ok", "node_id": NODE_ID, "node_ip": NODE_IP}
