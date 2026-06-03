"""Cirrus API — Django Ninja router."""

from ninja import NinjaAPI

from core.api.cfdis import router as cfdis_router
from core.api.empresas import router as empresas_router
from core.api.analysis import router as analysis_router
from core.api.sat_health import router as sat_health_router
from core.api.pipelines import router as pipelines_router
from core.api.webhooks import router as webhooks_router

api = NinjaAPI(
    title="Cirrus API",
    version="1.0.0",
    description="Multi-tenant CFDI management API",
    urls_namespace="api",
)


# Custom handler para AuthenticationError — propaga 402/429 del _api_auth_error
# attachado en core.api.auth
from ninja.errors import AuthenticationError
from django.http import JsonResponse


@api.exception_handler(AuthenticationError)
def handle_auth_error(request, exc):
    err = getattr(request, "_api_auth_error", None)
    if err is not None:
        resp = JsonResponse({"detail": err.message}, status=err.status)
        for k, v in err.headers.items():
            resp[k] = v
        return resp
    return JsonResponse({"detail": "Unauthorized"}, status=401)

# Register routers
api.add_router("/cfdis/", cfdis_router)
api.add_router("/empresas/", empresas_router)
api.add_router("/analysis/", analysis_router)
api.add_router("/sat-health/", sat_health_router)
api.add_router("/pipelines/", pipelines_router)
api.add_router("/webhooks/", webhooks_router)


@api.get("/health/", tags=["system"])
def health_check(request):
    """Health check endpoint."""
    return {"status": "ok", "service": "cirrus"}
