"""Cirrus API — Django Ninja router."""

from ninja import NinjaAPI

from core.api.cfdis import router as cfdis_router
from core.api.empresas import router as empresas_router

api = NinjaAPI(
    title="Cirrus API",
    version="1.0.0",
    description="Multi-tenant CFDI management API",
    urls_namespace="api",
)

# Register routers
api.add_router("/cfdis/", cfdis_router)
api.add_router("/empresas/", empresas_router)


@api.get("/health/", tags=["system"])
def health_check(request):
    """Health check endpoint."""
    return {"status": "ok", "service": "cirrus"}
