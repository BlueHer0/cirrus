"""Cirrus URL Configuration."""

from django.contrib import admin
from django.urls import path, include

from core.api.router import api
from core.views import landing_view

urlpatterns = [
    path("", landing_view, name="landing"),
    path("djadmin-8x7k/", admin.site.urls),
    path("api/v1/", api.urls),
    path("panel/", include("core.urls")),
    path("app/", include("accounts.urls")),
]
