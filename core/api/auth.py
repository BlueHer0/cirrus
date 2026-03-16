"""API Key authentication for Cirrus API.

Supports two methods:
1. Header: X-API-Key: <key>
2. Query param: ?api_key=<key>

Validates key, checks is active, updates ultimo_uso, and attaches
the APIKey object + allowed empresas to the request.
"""

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from ninja.security import HttpBearer, APIKeyQuery

logger = logging.getLogger("core.api.auth")


class APIKeyAuth(HttpBearer):
    """Authenticate via X-API-Key header (Bearer token style) or Authorization: Bearer <key>."""

    def authenticate(self, request, token: str) -> Optional[Any]:
        from core.models import APIKey

        try:
            api_key = APIKey.objects.select_related("owner").get(
                key=token, activa=True,
            )
        except APIKey.DoesNotExist:
            return None

        # Update last used
        api_key.ultimo_uso = datetime.now(timezone.utc)
        api_key.save(update_fields=["ultimo_uso"])

        # Attach to request for downstream use
        request.api_key = api_key
        request.api_empresas = api_key.empresas.all()

        return api_key


class APIKeyQueryAuth(APIKeyQuery):
    """Authenticate via ?api_key=<key> query parameter."""

    param_name = "api_key"

    def authenticate(self, request, key: str) -> Optional[Any]:
        from core.models import APIKey

        try:
            api_key = APIKey.objects.select_related("owner").get(
                key=key, activa=True,
            )
        except APIKey.DoesNotExist:
            return None

        api_key.ultimo_uso = datetime.now(timezone.utc)
        api_key.save(update_fields=["ultimo_uso"])

        request.api_key = api_key
        request.api_empresas = api_key.empresas.all()

        return api_key


# Use either header or query param
api_key_auth = [APIKeyAuth(), APIKeyQueryAuth()]
