"""
Cirrus Core Models
==================
Multi-tenant CFDI management: Empresa (RFC tenant), CFDI metadata,
APIKey for external apps, DescargaLog for scrapper runs, ScheduleConfig.
"""

import uuid

from django.contrib.auth.models import User
from django.db import models


class Empresa(models.Model):
    """Empresa/RFC registrada en Cirrus. Cada empresa es un tenant."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    nombre = models.CharField(max_length=200)
    rfc = models.CharField(max_length=13, db_index=True)

    # FIEL — archivos encriptados en MinIO, NUNCA en disco local
    fiel_cer_key = models.CharField(
        max_length=500, blank=True,
        help_text="Key en MinIO del .cer",
    )
    fiel_key_key = models.CharField(
        max_length=500, blank=True,
        help_text="Key en MinIO del .key",
    )
    fiel_password_encrypted = models.BinaryField(
        null=True, blank=True,
        help_text="Password encriptada con Fernet",
    )
    fiel_verificada = models.BooleanField(
        default=False,
        help_text="Login probado contra el SAT",
    )
    FIEL_STATUS_CHOICES = [
        ("sin_fiel", "Sin FIEL"),
        ("verificando", "Verificando"),
        ("verificada", "Verificada"),
        ("rechazada", "Rechazada"),
    ]
    fiel_status = models.CharField(
        max_length=20, default="sin_fiel", choices=FIEL_STATUS_CHOICES,
        help_text="Estado de la verificación FIEL",
    )
    fiel_verificada_at = models.DateTimeField(null=True, blank=True)
    fiel_expira = models.DateTimeField(null=True, blank=True)

    # Scheduling
    descarga_activa = models.BooleanField(default=True)
    ultimo_scrape = models.DateTimeField(null=True, blank=True)
    proximo_scrape = models.DateTimeField(null=True, blank=True)

    # Sincronización automática
    sync_desde_year = models.IntegerField(
        null=True, blank=True, help_text="Año desde el que sincronizar",
    )
    sync_desde_month = models.IntegerField(
        null=True, blank=True, help_text="Mes desde el que sincronizar",
    )
    sync_activa = models.BooleanField(
        default=False, help_text="Sincronización automática habilitada",
    )
    sync_completada = models.BooleanField(
        default=False, help_text="Todos los meses descargados",
    )

    # Branding (para PDF personalizados)
    logo_minio_key = models.CharField(
        max_length=500, blank=True,
        help_text="Key en MinIO del logo de la empresa",
    )

    # Metadata
    owner = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="empresas",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    notas = models.TextField(blank=True)

    class Meta:
        ordering = ["nombre"]
        verbose_name = "Empresa"
        verbose_name_plural = "Empresas"

    def __str__(self):
        return f"{self.rfc} — {self.nombre}"


class CFDI(models.Model):
    """CFDI individual — metadata del XML + referencia al raw en MinIO."""

    # Identificación
    uuid = models.UUIDField(
        primary_key=True,
        help_text="UUID del Timbre Fiscal Digital",
    )
    rfc_empresa = models.CharField(
        max_length=13, db_index=True, default="",
        help_text="RFC canónico — define quién tiene acceso (por FIEL verificada)",
    )
    empresa = models.ForeignKey(
        Empresa, on_delete=models.SET_NULL, related_name="cfdis",
        null=True, blank=True,
        help_text="Empresa que originó la descarga (referencial, NO define acceso)",
    )
    uploaded_by = models.ForeignKey(
        User, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="cfdis_uploaded",
        help_text="Usuario que subió manualmente este CFDI",
    )
    tipo_relacion = models.CharField(
        max_length=10,
        choices=[("recibido", "Recibido"), ("emitido", "Emitido")],
        db_index=True,
    )

    # Datos del comprobante
    version = models.CharField(max_length=5)  # "3.3" o "4.0"
    fecha = models.DateTimeField(db_index=True)
    serie = models.CharField(max_length=50, blank=True)
    folio = models.CharField(max_length=50, blank=True)
    total = models.DecimalField(max_digits=18, decimal_places=2, db_index=True)
    subtotal = models.DecimalField(max_digits=18, decimal_places=2)
    moneda = models.CharField(max_length=10, default="MXN")
    tipo_cambio = models.DecimalField(max_digits=10, decimal_places=4, default=1)
    tipo_comprobante = models.CharField(max_length=5, db_index=True)  # I, E, T, N, P
    forma_pago = models.CharField(max_length=10, blank=True)
    metodo_pago = models.CharField(max_length=10, blank=True)

    # Emisor
    rfc_emisor = models.CharField(max_length=13, db_index=True)
    nombre_emisor = models.CharField(max_length=300, blank=True)
    regimen_fiscal_emisor = models.CharField(max_length=10, blank=True)

    # Receptor
    rfc_receptor = models.CharField(max_length=13, db_index=True)
    nombre_receptor = models.CharField(max_length=300, blank=True)
    uso_cfdi = models.CharField(max_length=10, blank=True)

    # Impuestos (extraídos del XML para consulta rápida)
    total_impuestos_trasladados = models.DecimalField(
        max_digits=18, decimal_places=2, default=0,
    )
    total_impuestos_retenidos = models.DecimalField(
        max_digits=18, decimal_places=2, default=0,
    )
    iva = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    isr_retenido = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    iva_retenido = models.DecimalField(max_digits=18, decimal_places=2, default=0)

    # Storage
    xml_minio_key = models.CharField(
        max_length=500,
        help_text="Key del XML en MinIO",
    )
    xml_size_bytes = models.IntegerField(default=0)

    # Estado
    estado_sat = models.CharField(
        max_length=20,
        default="vigente",
        choices=[("vigente", "Vigente"), ("cancelado", "Cancelado")],
        db_index=True,
    )

    # Metadata de ingesta
    descargado_at = models.DateTimeField(auto_now_add=True)
    fuente = models.CharField(
        max_length=20, default="rpa",
        choices=[("rpa", "RPA"), ("manual", "Manual"), ("upload", "Upload")],
    )

    class Meta:
        ordering = ["-fecha"]
        verbose_name = "CFDI"
        verbose_name_plural = "CFDIs"
        indexes = [
            models.Index(fields=["empresa", "fecha"]),
            models.Index(fields=["empresa", "tipo_relacion", "fecha"]),
            models.Index(fields=["rfc_emisor", "fecha"]),
            models.Index(fields=["rfc_receptor", "fecha"]),
        ]

    def __str__(self):
        return f"{self.uuid} | {self.rfc_emisor} → {self.rfc_receptor} | ${self.total}"


class APIKey(models.Model):
    """API Key para que las apps externas consulten Cirrus."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    nombre = models.CharField(
        max_length=100,
        help_text="Ej: 'ZeroLatency Prod'",
    )
    key = models.CharField(max_length=64, unique=True, db_index=True)
    empresas = models.ManyToManyField(
        Empresa, blank=True,
        help_text="RFCs que puede consultar",
    )

    # Permisos
    puede_leer = models.BooleanField(default=True)
    puede_trigger_descarga = models.BooleanField(default=False)

    # Metadata
    owner = models.ForeignKey(User, on_delete=models.CASCADE)
    activa = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    ultimo_uso = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "API Key"
        verbose_name_plural = "API Keys"

    def __str__(self):
        return f"{self.nombre} ({'activa' if self.activa else 'inactiva'})"


class DescargaLog(models.Model):
    """Log de cada ejecución del scrapper."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    empresa = models.ForeignKey(
        Empresa, on_delete=models.CASCADE, related_name="descargas",
    )

    estado = models.CharField(
        max_length=20,
        choices=[
            ("pendiente", "Pendiente"),
            ("ejecutando", "Ejecutando"),
            ("completado", "Completado"),
            ("error", "Error"),
            ("cancelado", "Cancelado"),
        ],
        default="pendiente",
    )

    # Parámetros
    year = models.IntegerField()
    month_start = models.IntegerField()
    month_end = models.IntegerField()
    tipos = models.JSONField(default=list)  # ["recibidos", "emitidos"]

    # Resultado
    cfdis_descargados = models.IntegerField(default=0)
    cfdis_nuevos = models.IntegerField(default=0)
    cfdis_duplicados = models.IntegerField(default=0)
    errores = models.JSONField(default=list)
    progreso = models.TextField(
        blank=True,
        help_text="Último mensaje de progreso",
    )

    # Timing
    celery_task_id = models.CharField(max_length=100, blank=True)
    iniciado_at = models.DateTimeField(null=True, blank=True)
    completado_at = models.DateTimeField(null=True, blank=True)
    duracion_segundos = models.IntegerField(default=0)

    # Trigger
    triggered_by = models.CharField(
        max_length=20,
        choices=[
            ("manual", "Manual"),
            ("schedule", "Programado"),
            ("api", "API"),
        ],
        default="manual",
    )

    class Meta:
        ordering = ["-iniciado_at"]
        verbose_name = "Descarga Log"
        verbose_name_plural = "Descargas Log"

    def __str__(self):
        return f"{self.empresa.rfc} | {self.year}-{self.month_start:02d} | {self.estado}"


class ScheduleConfig(models.Model):
    """Configuración de descarga automática por empresa."""

    empresa = models.OneToOneField(
        Empresa, on_delete=models.CASCADE, related_name="schedule",
    )

    activo = models.BooleanField(default=True)

    # Frecuencia
    frecuencia = models.CharField(
        max_length=20,
        choices=[
            ("diaria", "Diaria"),
            ("semanal", "Semanal"),
            ("quincenal", "Quincenal"),
            ("mensual", "Mensual"),
        ],
        default="semanal",
    )

    # Ventana horaria preferida (para no saturar el SAT)
    hora_preferida = models.TimeField(
        help_text="Hora preferida para ejecutar (UTC)",
    )
    dia_semana = models.IntegerField(
        null=True, blank=True,
        help_text="0=lunes, 6=domingo",
    )

    # Anti-detección
    jitter_minutos = models.IntegerField(
        default=30,
        help_text="Variación aleatoria en minutos",
    )
    max_reintentos = models.IntegerField(default=3)

    # Rango de descarga
    meses_atras = models.IntegerField(
        default=1,
        help_text="Cuántos meses atrás descargar",
    )

    class Meta:
        verbose_name = "Schedule Config"
        verbose_name_plural = "Schedule Configs"

    def __str__(self):
        return f"Schedule {self.empresa.rfc}: {self.frecuencia}"


class ConversionLead(models.Model):
    """Lead capturado del conversor público."""

    email = models.EmailField(unique=True, db_index=True)
    conversiones = models.IntegerField(default=1)
    primera_conversion = models.DateTimeField(auto_now_add=True)
    ultima_conversion = models.DateTimeField(auto_now=True)
    total_pdfs = models.IntegerField(default=0)
    total_excels = models.IntegerField(default=0)
    ip = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=500, blank=True)
    notas = models.TextField(blank=True)
    contactado = models.BooleanField(default=False)
    es_cliente = models.BooleanField(default=False)

    class Meta:
        verbose_name = "Conversion Lead"
        verbose_name_plural = "Conversion Leads"
        ordering = ["-ultima_conversion"]

    def __str__(self):
        return f"{self.email} ({self.conversiones} conversiones)"


class ConversionLog(models.Model):
    """Registro de cada conversión individual."""

    lead = models.ForeignKey(
        ConversionLead, on_delete=models.CASCADE, related_name="logs"
    )
    formato = models.CharField(max_length=10)  # "pdf" o "excel"
    uuid_cfdi = models.CharField(max_length=50, blank=True)
    rfc_emisor = models.CharField(max_length=13, blank=True)
    rfc_receptor = models.CharField(max_length=13, blank=True)
    total = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    archivo_size = models.IntegerField(default=0)
    enviado = models.BooleanField(default=False)
    error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Conversion Log"
        verbose_name_plural = "Conversion Logs"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.lead.email} → {self.formato} ({self.created_at:%Y-%m-%d %H:%M})"


class SystemLog(models.Model):
    """Centralized system log for monitoring."""

    LEVEL_CHOICES = [
        ("info", "Info"),
        ("warning", "Warning"),
        ("error", "Error"),
        ("critical", "Critical"),
    ]
    CATEGORY_CHOICES = [
        ("email", "Email"),
        ("conversion", "Conversión"),
        ("download", "Descarga SAT"),
        ("fiel", "FIEL"),
        ("api", "API"),
        ("auth", "Autenticación"),
        ("system", "Sistema"),
        ("upload", "Upload XML"),
    ]

    level = models.CharField(max_length=10, choices=LEVEL_CHOICES, db_index=True)
    category = models.CharField(max_length=20, choices=CATEGORY_CHOICES, db_index=True)
    message = models.CharField(max_length=500)
    detail = models.TextField(blank=True)
    user_email = models.CharField(max_length=200, blank=True, db_index=True)
    ip = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "System Log"
        verbose_name_plural = "System Logs"
        indexes = [
            models.Index(fields=["level", "created_at"]),
            models.Index(fields=["category", "created_at"]),
        ]

    def __str__(self):
        return f"[{self.level.upper()}] {self.category}: {self.message[:80]}"


class DescargaTelemetria(models.Model):
    """Telemetría detallada de cada paso de una descarga."""

    descarga_log = models.ForeignKey(
        DescargaLog, on_delete=models.CASCADE, related_name="telemetria",
    )

    FASE_CHOICES = [
        ("fiel_download", "Descarga FIEL de MinIO"),
        ("fiel_decrypt", "Desencriptar FIEL"),
        ("browser_launch", "Lanzar navegador"),
        ("sat_navigate", "Navegar al portal SAT"),
        ("sat_login", "Login en SAT"),
        ("sat_select_dates", "Seleccionar fechas"),
        ("sat_search", "Búsqueda de CFDIs"),
        ("sat_download_wait", "Espera de descarga SAT"),
        ("sat_download_file", "Descarga archivo ZIP/XML"),
        ("engine_run", "Motor RPA completo"),
        ("xml_parse", "Parseo de XMLs"),
        ("minio_upload", "Upload a MinIO"),
        ("db_insert", "Inserción en PostgreSQL"),
        ("xml_process", "Procesar XMLs descargados"),
        ("browser_close", "Cerrar navegador"),
        ("cleanup", "Limpieza temporal"),
    ]

    fase = models.CharField(max_length=30, choices=FASE_CHOICES)

    inicio = models.DateTimeField()
    fin = models.DateTimeField(null=True, blank=True)
    duracion_ms = models.IntegerField(default=0)

    ATRIBUCION_CHOICES = [
        ("cirrus", "Nuestro servidor"),
        ("sat", "Portal del SAT"),
        ("red", "Red/conexión"),
        ("minio", "MinIO storage"),
    ]
    atribuible_a = models.CharField(
        max_length=20, choices=ATRIBUCION_CHOICES, default="cirrus",
    )

    exitoso = models.BooleanField(default=True)
    error = models.TextField(blank=True)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["inicio"]
        verbose_name = "Descarga Telemetría"
        verbose_name_plural = "Descarga Telemetría"

    def __str__(self):
        return f"{self.fase} | {self.duracion_ms}ms | {'✅' if self.exitoso else '❌'}"


class Plan(models.Model):
    """Plan de suscripción con límites definidos en BD."""

    nombre = models.CharField(max_length=50, unique=True)
    slug = models.SlugField(unique=True)  # free, basico, pro, enterprise
    precio_mensual = models.DecimalField(max_digits=10, decimal_places=2, default=0)

    # Límites
    max_empresas = models.IntegerField(default=1)
    max_descargas_mes = models.IntegerField(default=1)
    max_cfdis_visibles = models.IntegerField(default=50)
    max_conversiones_pdf = models.IntegerField(default=10)
    max_conversiones_excel = models.IntegerField(default=3)
    max_uploads_mes = models.IntegerField(default=10)

    # Features
    api_rest = models.BooleanField(default=False)
    api_nivel = models.CharField(
        max_length=20, default="none",
        choices=[("none", "Sin API"), ("read", "Solo lectura"), ("full", "Completa")],
    )
    logo_en_pdf = models.BooleanField(default=False)
    historial_desde_year = models.IntegerField(default=2026)
    historial_desde_month = models.IntegerField(default=1)
    branding_pdf = models.CharField(
        max_length=20, default="vistoso",
        choices=[
            ("vistoso", "Cirrus vistoso"),
            ("discreto", "Cirrus discreto"),
            ("logo", "Logo cliente"),
            ("white_label", "Sin marca"),
        ],
    )

    # Excedentes (precio unitario MXN)
    precio_descarga_extra = models.DecimalField(max_digits=10, decimal_places=2, default=29)
    precio_100_cfdis_extra = models.DecimalField(max_digits=10, decimal_places=2, default=49)
    precio_50_pdfs_extra = models.DecimalField(max_digits=10, decimal_places=2, default=19)
    precio_rfc_extra_mes = models.DecimalField(max_digits=10, decimal_places=2, default=49)
    precio_mes_historico = models.DecimalField(max_digits=10, decimal_places=2, default=15)

    orden = models.IntegerField(default=0)
    activo = models.BooleanField(default=True)
    destacado = models.BooleanField(default=False)

    class Meta:
        ordering = ["orden"]
        verbose_name = "Plan"
        verbose_name_plural = "Planes"

    def __str__(self):
        return f"{self.nombre} (${self.precio_mensual}/mes)"


class SolicitudHistorico(models.Model):
    """Solicitud de compra de año histórico por empresa."""

    empresa = models.ForeignKey(Empresa, on_delete=models.CASCADE, related_name="solicitudes_historico")
    usuario = models.ForeignKey(User, on_delete=models.CASCADE, related_name="solicitudes_historico")
    year = models.IntegerField()
    precio = models.DecimalField(max_digits=10, decimal_places=2, default=500)
    estado = models.CharField(
        max_length=20, default="pendiente",
        choices=[("pendiente", "Pendiente"), ("pagado", "Pagado"), ("rechazado", "Rechazado")],
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Solicitud Histórico"
        verbose_name_plural = "Solicitudes Histórico"

    def __str__(self):
        return f"{self.empresa.rfc} | {self.year} | {self.estado}"
