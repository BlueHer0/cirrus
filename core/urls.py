"""Cirrus Admin Panel URL Configuration."""

from django.urls import path
from core import views

app_name = "panel"

urlpatterns = [
    # Auth
    path("login/", views.login_view, name="login"),
    path("logout/", views.logout_view, name="logout"),

    # Dashboard
    path("", views.dashboard, name="dashboard"),

    # Empresas
    path("empresas/", views.empresas_list, name="empresas"),
    path("empresas/<uuid:empresa_id>/", views.empresa_detalle, name="empresa_detalle"),
    path("empresas/<uuid:empresa_id>/fiel/", views.empresa_fiel, name="empresa_fiel"),
    path("empresas/<uuid:empresa_id>/verificar/", views.empresa_verificar, name="empresa_verificar"),
    path("empresas/<uuid:empresa_id>/descargar/", views.empresa_descargar, name="empresa_descargar"),
    path("empresas/<uuid:empresa_id>/logo/", views.empresa_logo, name="empresa_logo"),

    # CFDIs
    path("cfdis/", views.cfdis_list, name="cfdis"),
    path("cfdis/<uuid:cfdi_uuid>/", views.cfdi_detail, name="cfdi_detail"),
    path("cfdis/<uuid:cfdi_uuid>/pdf/", views.cfdi_download_pdf, name="cfdi_pdf"),
    path("cfdis/<uuid:cfdi_uuid>/xml/", views.cfdi_download_xml, name="cfdi_xml"),
    path("cfdis/<uuid:cfdi_uuid>/excel/", views.cfdi_download_excel, name="cfdi_excel"),

    # Descargas
    path("descargas/", views.descargas_list, name="descargas"),
    path("descargas/<uuid:descarga_id>/telemetria/", views.descarga_telemetria, name="descarga_telemetria"),
    path("descargas/toggle-sync/<uuid:empresa_id>/", views.empresa_toggle_sync, name="toggle_sync"),

    # Sync config
    path("empresas/<uuid:empresa_id>/sync/", views.empresa_sync_config, name="empresa_sync"),

    # API Keys
    path("api-keys/", views.api_keys_view, name="api_keys"),
    path("api-keys/<uuid:key_id>/revoke/", views.api_key_revoke, name="api_key_revoke"),

    # CRM
    path("crm/", views.crm_list, name="crm"),
    path("crm/<int:lead_id>/", views.crm_detail, name="crm_detail"),

    # Monitor
    path("monitor/", views.monitor_view, name="monitor"),
]
