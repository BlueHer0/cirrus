from django.contrib import admin
from accounts.models import ClienteProfile


@admin.register(ClienteProfile)
class ClienteProfileAdmin(admin.ModelAdmin):
    list_display = ("user", "plan_legacy", "plan_fk", "empresa_nombre", "created_at")
    list_filter = ("plan_legacy", "plan_fk")
    search_fields = ("user__email", "empresa_nombre", "rfc_facturacion")

