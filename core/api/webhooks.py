"""Webhook endpoints — public, no JWT required.

Each webhook validates its origin via HMAC signature (preferred) or a
shared identifier (fallback). The router is mounted at /api/v1/webhooks/.

Endpoints:
- POST /snowie/  → captura de leads desde Snowie.ai (idempotente por session_id)
"""

import hashlib
import hmac
import json
import logging
from typing import Optional

from ninja import Router
from django.http import HttpRequest, JsonResponse
from django.views.decorators.csrf import csrf_exempt

logger = logging.getLogger("core.api.webhooks")

router = Router(tags=["webhooks"])

PLAN_KEYWORDS = {"free", "gratis", "basico", "básico", "pro", "profesional", "enterprise"}


def _join_name(first: Optional[str], last: Optional[str]) -> Optional[str]:
    """Concatenar first_name + last_name de Snowie."""
    parts = [p.strip() for p in [first or "", last or ""] if p and p.strip()]
    return " ".join(parts) or None


def _extract_plan_from_tags(tags) -> Optional[str]:
    """Buscar plan en el array de tags de Snowie."""
    if not tags or not isinstance(tags, list):
        return None
    for tag in tags:
        if isinstance(tag, str) and tag.strip().lower() in PLAN_KEYWORDS:
            return tag.strip()
    return None


def _get_snowie_config():
    """Return (agente_id_esperado, secret_plain) from SystemSettings."""
    try:
        from core.models import SystemSettings
        from core.services.fiel_encryption import decrypt_password

        s = SystemSettings.load()
        agente = (s.snowie_agente_id or "").strip()
        secret = None
        if s.snowie_webhook_secret_encrypted:
            try:
                secret = decrypt_password(bytes(s.snowie_webhook_secret_encrypted))
            except Exception as e:
                logger.warning("No se pudo descifrar snowie secret: %s", e)
        return agente, secret
    except Exception as e:
        logger.error("Error leyendo SystemSettings para Snowie: %s", e)
        return "", None


def _verify_signature(body_bytes: bytes, signature_header: Optional[str], secret: str) -> bool:
    """Verify HMAC-SHA256 signature against the raw body."""
    if not signature_header or not secret:
        return False

    # Algunos servicios prefijan con "sha256="; aceptamos ambos
    sig = signature_header.strip()
    if sig.startswith("sha256="):
        sig = sig[7:]

    expected = hmac.new(
        secret.encode("utf-8"),
        body_bytes,
        hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(expected, sig.lower())


@router.post("/snowie/", auth=None)
@csrf_exempt
def snowie_webhook(request: HttpRequest):
    """Captura de leads desde Snowie.ai.

    Validación:
    1. Body debe ser JSON con session_id requerido
    2. agente_id debe coincidir con el configurado en SystemSettings (si está set)
    3. Si SystemSettings tiene snowie_webhook_secret, exige header X-Snowie-Signature válido

    Idempotencia: si session_id ya existe, retorna 200 con status='duplicate'
    sin reprocesar ni reenviar emails/telegram.

    El endpoint SIEMPRE retorna 200 si el lead se guardó exitosamente, aunque
    las tareas async (telegram, email) fallen al encolarse.
    """
    from core.models import SnowieLead

    # ── 0. DEBUG: log everything that comes in ────────────────────────
    body_bytes = request.body
    headers_dict = {k: v for k, v in request.headers.items()}
    logger.info(
        "SNOWIE_WEBHOOK_INCOMING method=%s headers=%s body=%s",
        request.method,
        json.dumps(headers_dict, default=str)[:1000],
        body_bytes[:2000].decode("utf-8", errors="replace"),
    )

    # ── 1. Parse body ─────────────────────────────────────────────────
    try:
        payload = json.loads(body_bytes.decode("utf-8") or "{}")
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        logger.warning("Snowie webhook: JSON inválido: %s", e)
        return JsonResponse(
            {"status": "error", "message": "Invalid JSON body"},
            status=400,
        )

    if not isinstance(payload, dict):
        return JsonResponse(
            {"status": "error", "message": "Body must be a JSON object"},
            status=400,
        )

    # ── 2. Obtener session_id (o generar uno sintético en modo debug) ─
    # Snowie puede mandar el identificador con otros nombres. Intentamos
    # varios candidatos antes de generar uno sintético.
    session_id = (
        payload.get("session_id")
        or payload.get("sessionId")
        or payload.get("conversation_id")
        or payload.get("conversationId")
        or payload.get("id")
        or ""
    )
    session_id = str(session_id).strip()
    synthetic = False
    if not session_id:
        # Generar uno sintético basado en hash del payload para mantener idempotencia
        import hashlib as _h
        payload_hash = _h.sha256(body_bytes).hexdigest()[:16]
        session_id = f"snowie_auto_{payload_hash}"
        synthetic = True
        logger.warning(
            "Snowie webhook: session_id no vino en el body. Generado sintético: %s",
            session_id,
        )

    # ── 3. Validar agente_id contra config ────────────────────────────
    # Snowie envía "agent_code" (UUID), no "agente_id".
    agente_recibido = (
        payload.get("agent_code")
        or payload.get("agente_id")
        or ""
    ).strip()
    agente_esperado, secret = _get_snowie_config()

    if agente_esperado and agente_recibido != agente_esperado:
        logger.warning(
            "Snowie webhook: agente_id mismatch (recibido=%s esperado=%s)",
            agente_recibido, agente_esperado,
        )
        return JsonResponse(
            {"status": "error", "message": "Unknown agente_id"},
            status=401,
        )

    # ── 4. Validar HMAC si hay secret configurado ─────────────────────
    if secret:
        sig_header = request.headers.get("X-Snowie-Signature", "")
        if not _verify_signature(body_bytes, sig_header, secret):
            logger.warning("Snowie webhook: HMAC signature inválida")
            return JsonResponse(
                {"status": "error", "message": "Invalid signature"},
                status=401,
            )

    # ── 5. Idempotencia ───────────────────────────────────────────────
    existing = SnowieLead.objects.filter(session_id=session_id).first()
    if existing:
        return JsonResponse({
            "status": "duplicate",
            "id": str(existing.id),
            "message": "session_id already processed",
        }, status=200)

    # ── 6. Mapear campos (Snowie → Cirrus) ──────────────────────────
    # Snowie envía campos con nombres distintos a nuestro schema.
    # Prioridad: post_call_analysis > campos directos > aliases.
    pca = payload.get("post_call_analysis") or {}

    nombre = (
        pca.get("nombre")
        or _join_name(payload.get("first_name"), payload.get("last_name"))
        or payload.get("nombre")
        or None
    )
    email = (
        (payload.get("email") or "").strip()
        or (pca.get("email") or "").strip()
        or None
    )
    telefono = (
        (pca.get("telefono") or "").strip()
        or (payload.get("phone_number") or "").strip()
        or (payload.get("telefono") or "").strip()
        or None
    )
    rfc_empresa = (
        (pca.get("rfc_empresa") or "").strip().upper()
        or (payload.get("rfc_empresa") or "").strip().upper()
        or None
    )
    plan_interesado = (
        (pca.get("plan_interesado") or "").strip()
        or (payload.get("plan_interesado") or "").strip()
        or _extract_plan_from_tags(payload.get("tags"))
        or None
    )
    summary = (
        (payload.get("summary") or "").strip()
        or None
    )
    agente_id_final = (
        (payload.get("agent_code") or "").strip()
        or agente_recibido
        or "unknown"
    )

    # ── 7. Guardar lead ───────────────────────────────────────────────
    try:
        lead = SnowieLead.objects.create(
            session_id=session_id,
            agente_id=agente_id_final,
            nombre=nombre,
            email=email,
            telefono=telefono,
            rfc_empresa=rfc_empresa or None,
            plan_interesado=plan_interesado,
            summary=summary,
            payload_raw=payload,
        )
    except Exception as e:
        logger.error("Snowie webhook: error guardando lead: %s", e)
        return JsonResponse(
            {"status": "error", "message": "Internal error saving lead"},
            status=500,
        )

    # ── 7. Disparar tareas async (no bloquean ni rompen el response) ──
    try:
        from core.tasks_snowie import (
            notificar_telegram_snowie_lead,
            enviar_email_bienvenida_snowie,
        )
        notificar_telegram_snowie_lead.delay(str(lead.id))
        if lead.email:
            enviar_email_bienvenida_snowie.delay(str(lead.id))
    except Exception as e:
        # Si Celery falla al encolar, lo logueamos pero retornamos 200 igual
        # — el lead ya está guardado, las acciones async son best-effort.
        logger.error("Snowie webhook: no se pudo encolar tareas async: %s", e)

    return JsonResponse({
        "status": "ok",
        "id": str(lead.id),
        "message": "Lead saved",
        "synthetic_session_id": synthetic,
    }, status=200)
