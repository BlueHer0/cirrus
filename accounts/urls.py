"""Cirrus Client App URL Configuration."""

from django.urls import path
from accounts import views

app_name = "app"

urlpatterns = [
    # Auth
    path("registro/", views.app_register, name="register"),
    path("login/", views.app_login, name="login"),
    path("logout/", views.app_logout, name="logout"),
    path("confirmar/<str:token>/", views.confirmar_email, name="confirmar"),
    path("reenviar/", views.reenviar_confirmacion, name="reenviar"),
    path("recuperar-password/", views.recuperar_password, name="recuperar_password"),
    path("reset-password/<str:token>/", views.reset_password, name="reset_password"),

    # Dashboard
    path("", views.app_dashboard, name="dashboard"),

    # Empresas
    path("empresas/", views.app_empresas_list, name="empresas"),
    path("empresas/nueva/", views.app_crear_empresa, name="crear_empresa"),
    path("empresas/<uuid:empresa_id>/", views.app_empresa_detail, name="empresa_detail"),
    path("empresas/<uuid:empresa_id>/fiel/", views.app_empresa_fiel, name="empresa_fiel"),
    path("empresas/<uuid:empresa_id>/verificar/", views.app_empresa_verificar, name="empresa_verificar"),
    path("empresas/<uuid:empresa_id>/subir-csd/", views.app_empresa_subir_csd, name="empresa_subir_csd"),
    # CFDIs
    path("cfdis/", views.app_cfdis_list, name="cfdis"),
    path("cfdis/<str:uuid>/", views.app_cfdi_detail, name="cfdi_detail"),
    path("cfdis/<uuid:cfdi_uuid>/pdf/", views.app_cfdi_pdf, name="cfdi_pdf"),
    path("cfdis/<uuid:cfdi_uuid>/xml/", views.app_cfdi_xml, name="cfdi_xml"),

    # Descargas
    path("descargas/", views.app_descargas, name="descargas"),
    path("empresas/<uuid:empresa_id>/descargar/", views.app_empresa_descargar, name="empresa_descargar"),

    # Upload
    path("upload/", views.app_upload_xmls, name="upload"),

    # API Keys
    path("api-keys/", views.app_api_keys, name="api_keys"),

    # Account
    path("perfil/", views.app_perfil, name="perfil"),
    path("facturacion/", views.app_facturacion, name="facturacion"),

    # Analysis
    path("analysis/summary/", views.analysis_summary_view, name="analysis_summary"),
    path("analysis/fiscal/", views.analysis_fiscal_view, name="analysis_fiscal"),
    path("analysis/iva/", views.analysis_iva_view, name="analysis_iva"),
    path("analysis/income/", views.analysis_income_view, name="analysis_income"),
    path("analysis/top-rfc/", views.analysis_top_rfc_view, name="analysis_top_rfc"),
    path("analysis/risks/", views.analysis_risks_view, name="analysis_risks"),

    # Purchase
    path("comprar-historico/", views.app_comprar_historico, name="comprar_historico"),

    # Payments (Stripe)
    path("mejorar-plan/", views.mejorar_plan, name="mejorar_plan"),
    path("checkout/", views.crear_checkout, name="crear_checkout"),
    path("pago-exitoso/", views.pago_exitoso, name="pago_exitoso"),
    path("comprar-historico/checkout/", views.comprar_historico_checkout, name="comprar_historico_checkout"),
    path("cancelar-plan/", views.cancelar_plan, name="cancelar_plan"),
    path("reactivar-plan/", views.reactivar_plan, name="reactivar_plan"),
    path("recibo/<int:pago_id>/", views.descargar_recibo, name="descargar_recibo"),
]
