"""Django Admin registrations for Cirrus Core models."""

from django import forms
from django.contrib import admin

from .models import (
    APIKey, CFDI, DescargaLog, Empresa, ScheduleConfig,
    SATHealthProbe, SATHealthSummary, Colaborador, ColaboradorEmpresa,
    PipelineState, SystemSettings, SnowieLead,
    DocumentoFiscal, ChunkFiscal, StripeWebhookEvent, DescargaIncidente,
)


@admin.register(DescargaIncidente)
class DescargaIncidenteAdmin(admin.ModelAdmin):
    list_display = ("empresa", "tipo", "resuelto", "creado_en")
    list_filter = ("tipo", "resuelto")
    search_fields = ("empresa__rfc", "empresa__nombre", "descripcion")
    raw_id_fields = ("empresa", "job")
    readonly_fields = ("creado_en",)
    list_editable = ("resuelto",)
    actions = ["marcar_resuelto"]

    @admin.action(description="Marcar como resuelto")
    def marcar_resuelto(self, request, queryset):
        queryset.update(resuelto=True)


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
    list_display = (
        "nombre", "key_prefix", "activa", "requests_hoy", "limite_requests_dia",
        "puede_leer", "puede_trigger_descarga", "ultimo_uso",
    )
    list_filter = ("activa", "puede_leer", "puede_trigger_descarga")
    search_fields = ("nombre", "key_prefix", "owner__email")
    filter_horizontal = ("empresas",)
    readonly_fields = (
        "id", "key_hash", "key_prefix", "created_at", "ultimo_uso",
        "revocada_en", "ultimo_reset_requests", "requests_hoy",
    )


@admin.register(StripeWebhookEvent)
class StripeWebhookEventAdmin(admin.ModelAdmin):
    list_display = ("recibido_en", "event_type", "stripe_event_id", "customer_id", "estado", "intentos")
    list_filter = ("estado", "event_type")
    search_fields = ("stripe_event_id", "customer_id", "event_type")
    readonly_fields = (
        "id", "stripe_event_id", "event_type", "customer_id",
        "payload", "intentos", "recibido_en", "procesado_en",
    )
    date_hierarchy = "recibido_en"


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


@admin.register(SATHealthProbe)
class SATHealthProbeAdmin(admin.ModelAdmin):
    list_display = ('timestamp', 'node_id', 'rfc_used', 'result', 'last_phase_reached', 'time_total_ms')
    list_filter = ('result', 'node_id', 'rfc_used', 'last_phase_reached')
    search_fields = ('rfc_used', 'error_message')
    readonly_fields = ('id', 'timestamp')
    date_hierarchy = 'timestamp'


@admin.register(SATHealthSummary)
class SATHealthSummaryAdmin(admin.ModelAdmin):
    list_display = ('hour', 'availability_pct', 'total_probes', 'successful_probes', 'avg_total_time_ms', 'most_common_error')
    list_filter = ('most_common_error',)
    date_hierarchy = 'hour'


@admin.register(Colaborador)
class ColaboradorAdmin(admin.ModelAdmin):
    list_display = ("cuenta_principal", "usuario", "estado", "fecha_invitacion")
    list_filter = ("estado",)
    search_fields = ("cuenta_principal__email", "usuario__email")
    readonly_fields = ("id", "fecha_invitacion", "fecha_aceptacion", "fecha_revocacion")


@admin.register(ColaboradorEmpresa)
class ColaboradorEmpresaAdmin(admin.ModelAdmin):
    list_display = ("colaborador", "empresa", "fecha_asignacion")
    search_fields = ("colaborador__usuario__email", "empresa__rfc", "empresa__nombre")
    readonly_fields = ("id", "fecha_asignacion")


@admin.register(PipelineState)
class PipelineStateAdmin(admin.ModelAdmin):
    list_display = ("empresa", "pipeline_type", "estado", "paso_actual", "total_pasos", "paso_nombre", "intento_actual", "actualizado")
    list_filter = ("pipeline_type", "estado", "bloqueado_por_sat")
    search_fields = ("empresa__rfc", "empresa__nombre")
    readonly_fields = ("id", "iniciado", "actualizado", "completado_at")
    raw_id_fields = ("empresa",)


class SystemSettingsForm(forms.ModelForm):
    noreply_password = forms.CharField(
        required=False,
        widget=forms.PasswordInput(render_value=False),
        help_text="Dejar vacío para no cambiar el password actual",
        label="Password de noreply",
    )
    contacto_password = forms.CharField(
        required=False,
        widget=forms.PasswordInput(render_value=False),
        help_text="Dejar vacío para no cambiar el password actual",
        label="Password de contacto",
    )
    snowie_webhook_secret = forms.CharField(
        required=False,
        widget=forms.PasswordInput(render_value=False),
        help_text="Secret HMAC compartido con Snowie. Dejar vacío para no cambiar.",
        label="Snowie webhook secret",
    )

    class Meta:
        model = SystemSettings
        exclude = [
            "noreply_password_encrypted",
            "contacto_password_encrypted",
            "telegram_bot_token_encrypted",
            "snowie_webhook_secret_encrypted",
        ]


@admin.register(SystemSettings)
class SystemSettingsAdmin(admin.ModelAdmin):
    form = SystemSettingsForm
    fieldsets = (
        ("SMTP", {
            "fields": ("smtp_host", "smtp_port", "smtp_use_ssl", "smtp_imap_port"),
        }),
        ("Cuenta noreply (emails automáticos del sistema)", {
            "fields": ("noreply_email", "noreply_display_name", "noreply_password"),
        }),
        ("Cuenta contacto (comunicación con clientes)", {
            "fields": ("contacto_email", "contacto_display_name", "contacto_password"),
        }),
        ("Telegram (alertas)", {
            "fields": (
                "telegram_enabled", "telegram_bot_username", "telegram_admin_chat_id",
                "telegram_solo_stack",
                "telegram_send_info", "telegram_send_warning",
                "telegram_send_error", "telegram_send_critical",
            ),
            "description": "El token del bot se configura desde /panel/telegram/",
        }),
        ("Snowie (captura de leads)", {
            "fields": ("snowie_agente_id", "snowie_webhook_secret"),
            "description": "agente_id y secret HMAC compartido con Snowie.ai",
        }),
        ("Metadata", {
            "fields": ("updated_at", "updated_by"),
            "classes": ("collapse",),
        }),
    )
    readonly_fields = ("updated_at", "updated_by")

    def has_add_permission(self, request):
        return not SystemSettings.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False

    def save_model(self, request, obj, form, change):
        obj.updated_by = request.user
        super().save_model(request, obj, form, change)
        from core.services.system_settings import set_noreply_password, set_contacto_password
        from core.services.fiel_encryption import encrypt_password
        noreply_pwd = form.cleaned_data.get("noreply_password")
        contacto_pwd = form.cleaned_data.get("contacto_password")
        snowie_secret = form.cleaned_data.get("snowie_webhook_secret")
        if noreply_pwd:
            set_noreply_password(noreply_pwd, user=request.user)
        if contacto_pwd:
            set_contacto_password(contacto_pwd, user=request.user)
        if snowie_secret:
            obj.snowie_webhook_secret_encrypted = encrypt_password(snowie_secret)
            obj.save(update_fields=["snowie_webhook_secret_encrypted"])


@admin.register(SnowieLead)
class SnowieLeadAdmin(admin.ModelAdmin):
    list_display = (
        "creado_en", "estado", "nombre", "email", "telefono",
        "plan_interesado", "agente_id", "notificado_telegram", "email_enviado",
    )
    list_filter = ("estado", "agente_id", "plan_interesado", "notificado_telegram", "email_enviado")
    search_fields = ("session_id", "email", "nombre", "telefono", "rfc_empresa")
    readonly_fields = (
        "id", "session_id", "agente_id", "payload_raw",
        "creado_en", "actualizado_en",
        "notificado_telegram", "email_enviado",
    )
    date_hierarchy = "creado_en"
    fieldsets = (
        ("Identificación", {
            "fields": ("id", "session_id", "agente_id", "creado_en", "actualizado_en"),
        }),
        ("Datos del lead", {
            "fields": ("nombre", "email", "telefono", "rfc_empresa", "plan_interesado", "summary"),
        }),
        ("Estado comercial", {
            "fields": ("estado",),
        }),
        ("Tracking automático", {
            "fields": ("notificado_telegram", "email_enviado"),
        }),
        ("Payload original", {
            "fields": ("payload_raw",),
            "classes": ("collapse",),
        }),
    )


@admin.register(DocumentoFiscal)
class DocumentoFiscalAdmin(admin.ModelAdmin):
    list_display = ("nombre_archivo_original", "titulo", "categoria", "estado", "chunks_count", "creado_en")
    list_filter = ("categoria", "estado", "año_vigencia", "organismo_emisor")
    search_fields = ("titulo", "descripcion", "nombre_archivo_original", "hash_sha256")
    readonly_fields = (
        "id", "uuid_archivo", "hash_sha256",
        "archivo_original_key", "archivo_md_key", "archivo_json_key",
        "archivo_tamano_bytes", "archivo_content_type",
        "chunks_count", "error_detalle", "motivo_rechazo",
        "intentos_conversion", "intentos_validacion", "intentos_embedding",
        "job_id_celery",
        "creado_en", "actualizado_en", "indexado_en", "subido_por",
    )
    date_hierarchy = "creado_en"


@admin.register(ChunkFiscal)
class ChunkFiscalAdmin(admin.ModelAdmin):
    list_display = ("documento", "posicion_chunk", "pagina", "tokens", "creado_en")
    list_filter = ("documento__categoria",)
    search_fields = ("contenido", "documento__titulo")
    readonly_fields = ("id", "documento", "contenido", "embedding", "tokens", "metadata", "creado_en")
    raw_id_fields = ("documento",)
