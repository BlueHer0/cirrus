"""Servicio de gestión de API keys.

Principios:
- La key plana solo existe en memoria al momento de crear. Nunca se guarda
  en BD en texto plano ni aparece en logs.
- En BD se persisten únicamente:
    * key_hash  — SHA-256 hex, usado para lookup en autenticación
    * key_prefix — primeros chars del formato "cirrus_abc12345" para que el
                   usuario pueda identificar cuál es cuál
- Formato de key plana: "cirrus_<prefix 8 chars>_<random 48 chars>"
  (longitud total = 7 + 1 + 8 + 1 + 48 = 65 chars)
- La key se muestra UNA VEZ al crearla. Si el usuario la pierde, debe
  generar otra.

Rate limits según plan:
    free       → 0 (NO puede usar API)
    basico     → 1000 req/día
    pro        → 5000 req/día
    enterprise → 20000 req/día
    owner      → 50000 req/día
"""

import hashlib
import logging
import secrets
from typing import Optional

logger = logging.getLogger("core.api_keys")


RATE_LIMITS_POR_PLAN = {
    "free": 0,
    "basico": 1000,
    "pro": 5000,
    "enterprise": 20000,
    "owner": 50000,
}


def hash_key(key_plain: str) -> str:
    """SHA-256 hex de la key plana."""
    return hashlib.sha256(key_plain.encode("utf-8")).hexdigest()


def generate_key() -> tuple[str, str, str]:
    """Genera una nueva API key.

    Returns:
        (key_plain, key_hash, key_prefix)

        key_plain = "cirrus_abc12345_<48-char-random>"   → mostrar al usuario UNA vez
        key_hash  = sha256(key_plain)                    → guardar en BD
        key_prefix = "cirrus_abc12345"                   → guardar en BD
    """
    # Prefix identificable de 8 chars hex
    prefix_random = secrets.token_hex(4)  # 8 chars
    random_secret = secrets.token_urlsafe(36).replace("-", "").replace("_", "")[:48]
    # Fallback por si token_urlsafe depuró de más:
    while len(random_secret) < 48:
        random_secret += secrets.token_hex(1)
    random_secret = random_secret[:48]

    key_prefix = f"cirrus_{prefix_random}"
    key_plain = f"{key_prefix}_{random_secret}"
    key_hash = hash_key(key_plain)

    return key_plain, key_hash, key_prefix


def crear_api_key(
    owner,
    nombre: str,
    empresas=None,
    puede_leer: bool = True,
    puede_trigger_descarga: bool = False,
) -> tuple["APIKey", str]:
    """Crea una nueva APIKey y devuelve la instancia + la key plana.

    IMPORTANTE: la key plana solo puede recuperarse en este return. Si el
    caller no la guarda/muestra al usuario, se pierde.

    Returns: (apikey_instance, key_plain_para_mostrar_una_vez)
    """
    from core.models import APIKey

    key_plain, key_hash, key_prefix = generate_key()

    # Determinar plan y rate limit actual
    plan_slug = _plan_slug_del_usuario(owner)
    limite = RATE_LIMITS_POR_PLAN.get(plan_slug, 0)

    apikey = APIKey.objects.create(
        nombre=nombre,
        owner=owner,
        key_hash=key_hash,
        key_prefix=key_prefix,
        puede_leer=puede_leer,
        puede_trigger_descarga=puede_trigger_descarga,
        plan_slug_al_crear=plan_slug,
        limite_requests_dia=limite,
    )

    if empresas:
        apikey.empresas.set(empresas)

    logger.info(
        "API key creada: owner=%s prefix=%s plan=%s limite=%d/día",
        owner.email or owner.username, key_prefix, plan_slug, limite,
    )

    return apikey, key_plain


def _plan_slug_del_usuario(user) -> str:
    """Devuelve el slug del plan actual del usuario."""
    profile = getattr(user, "perfil", None)
    if profile is None:
        return "free"

    if user.is_staff or user.is_superuser:
        return "owner"

    plan = profile.plan_fk
    if plan and plan.slug:
        return plan.slug

    return profile.plan_legacy or "free"


def autenticar_key(key_plain: str) -> Optional["APIKey"]:
    """Busca una APIKey por su hash (nunca por el valor plano en BD).

    Returns: instancia APIKey activa + no revocada, o None.
    """
    from core.models import APIKey

    if not key_plain:
        return None

    h = hash_key(key_plain.strip())
    return APIKey.objects.filter(
        key_hash=h, activa=True,
    ).select_related("owner__perfil__plan_fk").first()


def plan_vigente(apikey) -> tuple[bool, str]:
    """¿El plan asociado al owner de esta key sigue vigente?

    Returns: (ok, motivo). ok=True significa acceso permitido.
    """
    profile = getattr(apikey.owner, "perfil", None)
    if profile is None:
        return False, "Cliente sin perfil"

    # Staff y superuser siempre vigentes
    if apikey.owner.is_staff or apikey.owner.is_superuser:
        return True, "staff"

    status = profile.subscription_status or "none"
    if status == "active":
        return True, "active"
    if status == "trialing":
        return True, "trialing"

    # past_due: damos gracia por ahora (no bloqueamos en auth, pero marcamos)
    if status == "past_due":
        return True, "past_due_grace"

    if status == "canceled":
        return False, "subscription_canceled"

    # none o vacío
    if not profile.plan_fk:
        return False, "sin_plan"

    return False, f"status={status}"


def verificar_rate_limit(apikey) -> tuple[bool, int]:
    """Incrementa contador del día y valida rate limit.

    Returns: (permitido, requests_restantes).
    Si (False, 0) → HTTP 429.
    """
    from datetime import date
    from django.db.models import F
    from django.utils import timezone

    today = timezone.now().date()

    # Reset automático si cambió el día respecto al último reset
    if apikey.ultimo_reset_requests != today:
        apikey.requests_hoy = 0
        apikey.ultimo_reset_requests = today
        apikey.save(update_fields=["requests_hoy", "ultimo_reset_requests"])

    limite = apikey.limite_requests_dia or 0

    # Staff/superuser sin límite práctico
    if apikey.owner.is_staff or apikey.owner.is_superuser:
        limite = max(limite, 50000)

    if limite <= 0:
        return False, 0

    if apikey.requests_hoy >= limite:
        return False, 0

    # Incrementar atómicamente
    from core.models import APIKey
    APIKey.objects.filter(pk=apikey.pk).update(
        requests_hoy=F("requests_hoy") + 1,
        ultimo_uso=timezone.now(),
    )
    apikey.requests_hoy += 1

    return True, max(0, limite - apikey.requests_hoy)


def revocar_key(apikey, motivo: str = "manual") -> None:
    """Marca una key como revocada (soft delete)."""
    from django.utils import timezone
    apikey.activa = False
    apikey.revocada_en = timezone.now()
    apikey.save(update_fields=["activa", "revocada_en"])
    logger.info(
        "API key revocada: owner=%s prefix=%s motivo=%s",
        apikey.owner.email or apikey.owner.username,
        apikey.key_prefix, motivo,
    )


def desactivar_keys_por_plan_cancelado() -> int:
    """Desactiva todas las keys de usuarios con subscription_status canceled.

    Returns: cantidad de keys desactivadas.
    """
    from django.utils import timezone
    from core.models import APIKey

    qs = APIKey.objects.filter(
        activa=True,
        owner__perfil__subscription_status="canceled",
    ).exclude(owner__is_staff=True).exclude(owner__is_superuser=True)

    count = qs.update(
        activa=False,
        revocada_en=timezone.now(),
    )
    if count > 0:
        logger.info("Desactivadas %d API keys por plan cancelado", count)
    return count


def reset_requests_diarios() -> int:
    """Reset del contador diario de requests en TODAS las keys.

    Usado por el Celery task nocturno. Returns: número de keys reseteadas.
    """
    from django.utils import timezone
    from core.models import APIKey

    today = timezone.now().date()
    count = APIKey.objects.filter(activa=True).exclude(
        ultimo_reset_requests=today,
    ).update(requests_hoy=0, ultimo_reset_requests=today)
    logger.info("Reset diario requests_hoy: %d keys", count)
    return count
