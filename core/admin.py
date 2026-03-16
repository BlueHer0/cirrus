"""Django Admin registrations for Cirrus Core models."""

from django.contrib import admin

from .models import APIKey, CFDI, DescargaLog, Empresa, ScheduleConfig


@admin.register(Empresa)
class EmpresaAdmin(admin.ModelAdmin):
    list_display = ("rfc", "nombre", "fiel_verificada", "descarga_activa", "ultimo_scrape")
    list_filter = ("fiel_verificada", "descarga_activa")
    search_fields = ("rfc", "nombre")
    readonly_fields = ("id", "created_at", "updated_at")


@admin.register(CFDI)
class CFDIAdmin(admin.ModelAdmin):
    list_display = (
        "uuid", "empresa", "tipo_relacion", "fecha", "rfc_emisor",
        "rfc_receptor", "total", "tipo_comprobante", "estado_sat",
    )
    list_filter = ("tipo_relacion", "tipo_comprobante", "estado_sat", "moneda")
    search_fields = ("uuid", "rfc_emisor", "rfc_receptor", "nombre_emisor", "nombre_receptor")
    date_hierarchy = "fecha"
    raw_id_fields = ("empresa",)


@admin.register(APIKey)
class APIKeyAdmin(admin.ModelAdmin):
    list_display = ("nombre", "activa", "puede_leer", "puede_trigger_descarga", "ultimo_uso")
    list_filter = ("activa",)
    filter_horizontal = ("empresas",)
    readonly_fields = ("id", "created_at")


@admin.register(DescargaLog)
class DescargaLogAdmin(admin.ModelAdmin):
    list_display = (
        "empresa", "estado", "year", "month_start", "month_end",
        "cfdis_nuevos", "triggered_by", "iniciado_at", "duracion_segundos",
    )
    list_filter = ("estado", "triggered_by")
    raw_id_fields = ("empresa",)
    readonly_fields = ("id",)


@admin.register(ScheduleConfig)
class ScheduleConfigAdmin(admin.ModelAdmin):
    list_display = ("empresa", "activo", "frecuencia", "hora_preferida", "meses_atras")
    list_filter = ("activo", "frecuencia")
    raw_id_fields = ("empresa",)
