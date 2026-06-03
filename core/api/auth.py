"""API Key authentication for Cirrus API.

Flujo de autenticación:
1. Se recibe la key plana en header `Authorization: Bearer <key>` o `?api_key=`
2. Se calcula SHA-256 y se busca en la BD (NUNCA buscamos por key plana)
3. Se valida: activa + plan vigente + rate limit
4. Se incrementa requests_hoy y se guarda ultimo_uso

Respuestas de error:
- 401 Unauthorized — key no existe o no es válida
- 402 Payment Required — plan vencido/cancelado
- 429 Too Many Requests — rate limit excedido (incluye Retry-After)
"""

import logging
from typing import Any, Optional

from django.http import JsonResponse
from ninja.security import APIKeyQuery, HttpBearer

logger = logging.getLogger("core.api.auth")


class _AuthError(Exception):
    """Interno: propaga errores de auth con status code específico."""
    def __init__(self, status: int, message: str, headers: dict = None):
        self.status = status
        self.message = message
        self.headers = headers or {}


def _validate_and_attach(request, token: str):
    """Core de la autenticación. Devuelve APIKey o lanza _AuthError."""
    from core.services.api_keys_service import (
        autenticar_key, plan_vigente, verificar_rate_limit,
    )

    if not token:
        raise _AuthError(401, "Missing API key")

    apikey = autenticar_key(token)
    if not apikey:
        raise _AuthError(401, "Invalid API key")

    # Verificar plan vigente
    ok, motivo = plan_vigente(apikey)
    if not ok:
        raise _AuthError(
            402,
            f"Subscription not active ({motivo}). "
            f"Renueva tu plan para seguir usando la API.",
        )

    # Verificar rate limit (incrementa contador si pasa)
    allowed, restantes = verificar_rate_limit(apikey)
    if not allowed:
        # 86400 = hasta la medianoche en peor caso
        raise _AuthError(
            429,
            f"Rate limit exceeded ({apikey.limite_requests_dia}/day). "
            f"Upgrade your plan or wait until tomorrow.",
            headers={"Retry-After": "86400"},
        )

    # Attach al request para los endpoints downstream
    request.api_key = apikey
    request.api_empresas = apikey.empresas.all()
    request.api_empresa_rfcs = list(
        request.api_empresas.values_list("rfc", flat=True)
    )
    request.api_requests_restantes = restantes

    return apikey


def _build_error_response(err: _AuthError) -> JsonResponse:
    """Construye JsonResponse con headers apropiados."""
    resp = JsonResponse({"detail": err.message}, status=err.status)
    for k, v in err.headers.items():
        resp[k] = v
    return resp


def _store_error(request, err: "_AuthError"):
    """Guarda el error solo si es más específico que el existente.

    Ninja prueba múltiples authenticators en orden. Para evitar que un
    "Missing API key" del último authenticator sobrescriba un error real
    (401 Invalid, 402 Plan, 429 Rate) del anterior, aplicamos esta regla:
      - No sobrescribir un error existente si el nuevo es 401 con "Missing"
      - Sí sobrescribir si el nuevo es 402 o 429 (más específico)
    """
    existing = getattr(request, "_api_auth_error", None)
    if existing is None:
        request._api_auth_error = err
        return
    # Preferir el error más "grave" (402/429 > 401 específico > 401 Missing)
    if err.status in (402, 429):
        request._api_auth_error = err
    elif err.status == 401 and "Missing" not in err.message:
        # "Invalid API key" es mejor que "Missing API key"
        if "Missing" in existing.message:
            request._api_auth_error = err


class APIKeyAuth(HttpBearer):
    """Autenticación via `Authorization: Bearer <key>`."""

    def authenticate(self, request, token: str) -> Optional[Any]:
        try:
            return _validate_and_attach(request, token)
        except _AuthError as e:
            _store_error(request, e)
            return None


class APIKeyQueryAuth(APIKeyQuery):
    """Autenticación via `?api_key=<key>`."""

    param_name = "api_key"

    def authenticate(self, request, key: str) -> Optional[Any]:
        # Si no hay query param, no adjuntar "Missing" — deja que el Bearer
        # auth decida el error final si ambos fallan.
        if not key:
            return None
        try:
            return _validate_and_attach(request, key)
        except _AuthError as e:
            _store_error(request, e)
            return None


api_key_auth = [APIKeyAuth(), APIKeyQueryAuth()]
