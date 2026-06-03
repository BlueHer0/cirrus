"""Reportes URL Configuration."""

from django.urls import path
from reportes import views

app_name = "reportes"

urlpatterns = [
    path("", views.selector_view, name="selector"),
    path("ver/", views.ver_view, name="ver"),
    path("pdf/", views.pdf_view, name="pdf"),
    path("generar-ia/", views.generar_ia_view, name="generar_ia"),
    path("trigger-email/", views.trigger_email_view, name="trigger_email"),
]
