"""
Cirrus Core Models
==================
Multi-tenant CFDI management: Empresa (RFC tenant), CFDI metadata,
APIKey for external apps, DescargaLog for scrapper runs, ScheduleConfig.
"""

import uuid

from django.contrib.auth.models import User
from django.db import models
from pgvector.django import VectorField, HnswIndex


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
        ("expirada", "Expirada"),
    ]
    fiel_status = models.CharField(
        max_length=20, default="sin_fiel", choices=FIEL_STATUS_CHOICES,
        help_text="Estado de la verificación FIEL",
    )
    fiel_verificada_at = models.DateTimeField(null=True, blank=True)
    fiel_expira = models.DateTimeField(null=True, blank=True)

    # CSD para facturación (futuro)
    csd_cer_key = models.CharField(
        max_length=500, blank=True,
        help_text="MinIO key del .cer del CSD",
    )
    csd_key_key = models.CharField(
        max_length=500, blank=True,
        help_text="MinIO key del .key del CSD",
    )
    csd_password_encrypted = models.TextField(
        blank=True,
        help_text="Password del CSD encriptado con Fernet",
    )
    csd_serial = models.CharField(max_length=50, blank=True)
    csd_expira = models.DateField(null=True, blank=True)
    csd_activo = models.BooleanField(default=False)

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

    # ── Datos oficiales de la CSF ─────────────────────────────────────
    razon_social = models.CharField(max_length=500, blank=True)
    regimen_capital = models.CharField(max_length=200, blank=True)
    nombre_comercial = models.CharField(max_length=500, blank=True)
    regimen_fiscal = models.CharField(max_length=200, blank=True)
    codigo_postal = models.CharField(max_length=5, blank=True)
    direccion_calle = models.CharField(max_length=300, blank=True)
    direccion_num_ext = models.CharField(max_length=50, blank=True)
    direccion_num_int = models.CharField(max_length=50, blank=True)
    direccion_colonia = models.CharField(max_length=200, blank=True)
    direccion_localidad = models.CharField(max_length=200, blank=True)
    direccion_municipio = models.CharField(max_length=200, blank=True)
    direccion_estado = models.CharField(max_length=100, blank=True)
    actividades_economicas = models.JSONField(default=list, blank=True)
    fecha_inicio_operaciones = models.DateField(null=True, blank=True)
    estatus_padron = models.CharField(max_length=50, blank=True)
    csf_minio_key = models.CharField(max_length=500, blank=True)
    csf_ultima_descarga = models.DateTimeField(null=True, blank=True)

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

    # ── Campos específicos de nómina (tipo_comprobante='N') ──────────
    # Extraídos del complemento nomina12:Nomina. Para conciliación contable
    # la fecha relevante es fecha_pago_nomina (cuándo se pagó al trabajador),
    # que puede diferir de `fecha` (cuándo se timbró el CFDI).
    fecha_pago_nomina = models.DateField(
        null=True, blank=True, db_index=True,
        help_text="FechaPago del complemento de nómina — fecha efectiva del pago",
    )
    fecha_inicial_pago = models.DateField(null=True, blank=True)
    fecha_final_pago = models.DateField(null=True, blank=True)
    tipo_nomina = models.CharField(
        max_length=1, blank=True,
        choices=[("O", "Ordinaria"), ("E", "Extraordinaria")],
        help_text="O=Ordinaria / E=Extraordinaria (aguinaldos, PTU, bonos)",
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
            models.Index(fields=["rfc_empresa", "fecha"]),
            models.Index(fields=["rfc_empresa", "fecha_pago_nomina"]),
        ]

    def __str__(self):
        return f"{self.uuid} | {self.rfc_emisor} → {self.rfc_receptor} | ${self.total}"


class APIKey(models.Model):
    """API Key para que las apps externas consulten Cirrus.

    La key plana se genera al crear y se muestra UNA vez al usuario.
    En BD solo se guarda:
      - key_hash   : SHA-256 hex (64 chars) — para lookup al autenticar
      - key_prefix : primeros chars visibles para identificación (ej: cirrus_ab12cd34)
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    nombre = models.CharField(
        max_length=100,
        help_text="Ej: 'ZeroLatency Prod'",
    )
    # Nuevo esquema seguro. Migración 0030 eliminó el campo `key` plano;
    # `key_hash` (SHA-256) es lo único que se persiste.
    key_hash = models.CharField(
        max_length=64, blank=True, null=True, db_index=True,
        help_text="SHA-256 hex de la key plana",
    )
    key_prefix = models.CharField(
        max_length=32, blank=True, db_index=True,
        help_text="Primeros chars de la key para identificación visual",
    )

    empresas = models.ManyToManyField(
        Empresa, blank=True,
        help_text="RFCs que puede consultar",
    )

    # Permisos
    puede_leer = models.BooleanField(default=True)
    puede_trigger_descarga = models.BooleanField(default=False)

    # Rate limiting
    requests_hoy = models.IntegerField(default=0)
    limite_requests_dia = models.IntegerField(
        default=1000,
        help_text="Limit diario según plan (free=0, basico=1000, pro=5000, enterprise=20000)",
    )
    ultimo_reset_requests = models.DateField(null=True, blank=True)

    # Plan / vigencia
    plan_slug_al_crear = models.CharField(max_length=20, blank=True)

    # Metadata
    owner = models.ForeignKey(User, on_delete=models.CASCADE)
    activa = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    ultimo_uso = models.DateTimeField(null=True, blank=True)
    revocada_en = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "API Key"
        verbose_name_plural = "API Keys"

    def __str__(self):
        return f"{self.nombre} ({self.key_prefix or '?'}) {'activa' if self.activa else 'inactiva'}"


class DescargaJob(models.Model):
    """Un job de descarga = 1 empresa + 1 mes + 1 tipo.

    Ordered queue: prioridad ASC, programado_para ASC.
    unique_together prevents duplicate jobs.
    """

    empresa = models.ForeignKey(
        Empresa, on_delete=models.CASCADE, related_name="jobs",
    )
    year = models.IntegerField()
    month = models.IntegerField()
    tipo = models.CharField(max_length=15, choices=[
        ("recibidos", "Recibidos"),
        ("emitidos", "Emitidos"),
    ])

    estado = models.CharField(max_length=20, default="en_cola", choices=[
        ("en_cola", "En cola"),
        ("ejecutando", "Ejecutando"),
        ("completado", "Completado"),
        ("completado_vacio", "Completado sin CFDIs"),
        ("error", "Error"),
    ])

    prioridad = models.IntegerField(
        default=5,
        help_text="1=máxima (owner/enterprise), 3=alta (pro), 5=media (basico), 9=baja (free)",
    )

    programado_para = models.DateTimeField(
        help_text="No ejecutar antes de esta fecha/hora",
    )

    # Results
    cfdis_descargados = models.IntegerField(default=0)
    cfdis_nuevos = models.IntegerField(default=0)
    duracion_segundos = models.IntegerField(default=0)
    intentos = models.IntegerField(default=0)
    max_intentos = models.IntegerField(default=5)
    ultimo_error = models.TextField(blank=True)

    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    iniciado_at = models.DateTimeField(null=True, blank=True)
    completado_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["prioridad", "programado_para"]
        unique_together = ["empresa", "year", "month", "tipo"]
        verbose_name = "Descarga Job"
        verbose_name_plural = "Descarga Jobs"

    def __str__(self):
        return f"{self.empresa.rfc} {self.year}-{self.month:02d} {self.tipo} [{self.estado}]"


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
    max_colaboradores = models.IntegerField(default=0)
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

    # Stripe
    stripe_product_id = models.CharField(max_length=100, blank=True)
    stripe_price_id = models.CharField(max_length=100, blank=True)

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


class EFOS(models.Model):
    """Contribuyentes en listado 69-B del SAT (EFOS)."""

    rfc = models.CharField(max_length=13, db_index=True, unique=True)
    nombre = models.CharField(max_length=500, blank=True)
    situacion = models.CharField(max_length=100, blank=True,
        help_text="Presunto, Definitivo, Desvirtuado, Sentencia Favorable")
    fecha_publicacion = models.DateField(null=True, blank=True)
    fecha_publicacion_dof = models.DateField(null=True, blank=True)
    numero_oficio_presuncion = models.CharField(max_length=200, blank=True)
    fecha_oficio_presuncion = models.DateField(null=True, blank=True)
    numero_oficio_definitivo = models.CharField(max_length=200, blank=True)
    fecha_oficio_definitivo = models.DateField(null=True, blank=True)
    raw_data = models.JSONField(default=dict, blank=True,
        help_text="Todos los campos originales del CSV")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-fecha_publicacion"]
        verbose_name = "EFOS (69-B)"
        verbose_name_plural = "EFOS (69-B)"
        indexes = [
            models.Index(fields=["situacion"]),
        ]

    def __str__(self):
        return f"{self.rfc} — {self.situacion}"


class SATHealthProbe(models.Model):
    """Registro individual de un intento de login al SAT para medir disponibilidad."""

    class ProbeResult(models.TextChoices):
        SUCCESS = 'success', 'Login exitoso'
        TIMEOUT = 'timeout', 'Timeout'
        LOGIN_FAILED = 'login_failed', 'Login fallido'
        PAGE_ERROR = 'page_error', 'Error cargando página'
        CAPTCHA = 'captcha', 'Captcha detectado'
        MAINTENANCE = 'maintenance', 'SAT en mantenimiento'
        NETWORK_ERROR = 'network_error', 'Error de red'
        BROWSER_ERROR = 'browser_error', 'Error de browser'
        UNKNOWN = 'unknown', 'Error desconocido'

    class ProbePhase(models.TextChoices):
        DNS = 'dns', 'Resolución DNS'
        PAGE_LOAD = 'page_load', 'Carga de página'
        FORM_VISIBLE = 'form_visible', 'Formulario visible'
        FIEL_UPLOAD = 'fiel_upload', 'Subida de FIEL'
        LOGIN_SUBMIT = 'login_submit', 'Envío de login'
        SESSION_ACTIVE = 'session_active', 'Sesión activa'

    # Identificación
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    timestamp = models.DateTimeField(auto_now_add=True, db_index=True)

    # Qué nodo y qué FIEL
    node_id = models.CharField(max_length=20, db_index=True)  # 'vps1', 'vps2', 'vpsx', 'spark'
    node_ip = models.GenericIPAddressField()
    rfc_used = models.CharField(max_length=13, db_index=True)
    empresa = models.ForeignKey(
        'Empresa', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='health_probes',
    )

    # Resultado
    result = models.CharField(max_length=20, choices=ProbeResult.choices, db_index=True)
    last_phase_reached = models.CharField(max_length=20, choices=ProbePhase.choices)
    error_message = models.TextField(blank=True, default='')
    http_status = models.IntegerField(null=True, blank=True)

    # Telemetría de tiempos (milisegundos, null si no se alcanzó esa fase)
    time_dns_ms = models.IntegerField(null=True, blank=True)
    time_page_load_ms = models.IntegerField(null=True, blank=True)
    time_form_visible_ms = models.IntegerField(null=True, blank=True)
    time_fiel_upload_ms = models.IntegerField(null=True, blank=True)
    time_login_submit_ms = models.IntegerField(null=True, blank=True)
    time_session_active_ms = models.IntegerField(null=True, blank=True)
    time_total_ms = models.IntegerField()

    # Screenshot en fallo (ruta en MinIO)
    screenshot_path = models.CharField(max_length=500, blank=True, default='')

    # Metadata
    sat_url = models.URLField(default='https://portalcfdi.facturaelectronica.sat.gob.mx/')
    user_agent = models.CharField(max_length=500, blank=True, default='')

    class Meta:
        ordering = ['-timestamp']
        verbose_name = 'SAT Health Probe'
        verbose_name_plural = 'SAT Health Probes'
        indexes = [
            models.Index(fields=['timestamp', 'result']),
            models.Index(fields=['node_id', 'timestamp']),
            models.Index(fields=['rfc_used', 'timestamp']),
        ]

    def __str__(self):
        return f"{self.timestamp:%Y-%m-%d %H:%M} | {self.node_id} | {self.rfc_used} | {self.result}"

    @property
    def is_success(self):
        return self.result == self.ProbeResult.SUCCESS


class SATHealthSummary(models.Model):
    """Resumen horario de disponibilidad del SAT. Se calcula automáticamente."""

    hour = models.DateTimeField(unique=True, db_index=True)  # truncado a la hora
    total_probes = models.IntegerField(default=0)
    successful_probes = models.IntegerField(default=0)
    failed_probes = models.IntegerField(default=0)
    availability_pct = models.FloatField(default=0)  # 0-100
    avg_total_time_ms = models.IntegerField(null=True, blank=True)
    avg_login_time_ms = models.IntegerField(null=True, blank=True)
    min_total_time_ms = models.IntegerField(null=True, blank=True)
    max_total_time_ms = models.IntegerField(null=True, blank=True)
    most_common_error = models.CharField(max_length=20, blank=True, default='')

    # Desglose por nodo
    results_by_node = models.JSONField(default=dict)  # {'vps1': {'success': 3, 'failed': 1}, ...}

    class Meta:
        ordering = ['-hour']
        verbose_name = 'SAT Health Summary'
        verbose_name_plural = 'SAT Health Summaries'

    def __str__(self):
        return f"{self.hour:%Y-%m-%d %H:00} | {self.availability_pct:.0f}% up | {self.total_probes} probes"


class Colaborador(models.Model):
    """
    Relación entre una cuenta principal y un usuario colaborador.
    El colaborador ve las empresas de la cuenta principal según los permisos asignados.
    """
    
    class Estado(models.TextChoices):
        ACTIVO = 'activo', 'Activo'
        INACTIVO = 'inactivo', 'Inactivo'  # suspendido temporalmente
        REVOCADO = 'revocado', 'Revocado'  # eliminado definitivamente
    
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    
    # Quién invita (cuenta principal que paga)
    cuenta_principal = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='colaboradores_creados'
    )
    
    # Quién es invitado (debe ser un usuario registrado en Cirrus)
    usuario = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='invitaciones_como_colaborador'
    )
    
    # Estado
    estado = models.CharField(max_length=10, choices=Estado.choices, default=Estado.ACTIVO)
    
    # Permisos globales (aplican a todas las empresas asignadas)
    puede_ver_cfdis = models.BooleanField(default=True)
    puede_ver_analisis = models.BooleanField(default=True)
    puede_exportar = models.BooleanField(default=True)
    puede_subir_fiel = models.BooleanField(default=False)  # subir/editar FIEL y CSD
    puede_subir_xmls = models.BooleanField(default=False)  # upload manual de XMLs
    puede_crear_empresa = models.BooleanField(default=False)  # dar de alta nuevas empresas
    puede_descargar_sat = models.BooleanField(default=False)  # disparar descargas del SAT
    puede_ver_csf = models.BooleanField(default=False)  # ver/descargar CSF
    
    # Fechas
    fecha_invitacion = models.DateTimeField(auto_now_add=True)
    fecha_aceptacion = models.DateTimeField(null=True, blank=True)
    fecha_revocacion = models.DateTimeField(null=True, blank=True)
    
    # Notas internas
    notas = models.TextField(blank=True, default='')
    
    class Meta:
        unique_together = ['cuenta_principal', 'usuario']  # no duplicar invitaciones
        ordering = ['-fecha_invitacion']
        verbose_name = "Colaborador"
        verbose_name_plural = "Colaboradores"
    
    def __str__(self):
        return f"{self.usuario.email} → {self.cuenta_principal.email} ({self.estado})"
    
    @property
    def is_active(self):
        return self.estado == self.Estado.ACTIVO


class ColaboradorEmpresa(models.Model):
    """
    Permisos específicos de un colaborador sobre una empresa.
    Si no existe registro, el colaborador NO tiene acceso a esa empresa.
    La existencia del registro = acceso concedido.
    """
    
    colaborador = models.ForeignKey(
        Colaborador,
        on_delete=models.CASCADE,
        related_name='empresas_asignadas'
    )
    empresa = models.ForeignKey(
        Empresa,
        on_delete=models.CASCADE,
        related_name='colaboradores_con_acceso'
    )
    
    # Override de permisos a nivel empresa
    puede_ver_cfdis = models.BooleanField(default=True)
    puede_subir_fiel = models.BooleanField(default=False)
    puede_exportar = models.BooleanField(default=True)
    
    fecha_asignacion = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        unique_together = ['colaborador', 'empresa']
        verbose_name = "Colaborador de Empresa"
        verbose_name_plural = "Colaboradores de Empresas"
    
    def __str__(self):
        return f"{self.colaborador.usuario.email} → {self.empresa.rfc}"


class PipelineState(models.Model):
    """Estado actual de un pipeline para una empresa.

    Cada empresa puede tener múltiples pipelines activos simultáneamente.
    """

    class PipelineType(models.TextChoices):
        ALTA_EMPRESA = 'alta_empresa', 'Alta de Empresa'
        DESCARGA_CFDIS = 'descarga_cfdis', 'Descarga de CFDIs'
        CSF_MENSUAL = 'csf_mensual', 'CSF Mensual'
        VERIFICACION_FIEL = 'verificacion_fiel', 'Verificación FIEL'

    class StepStatus(models.TextChoices):
        PENDIENTE = 'pendiente', 'Pendiente'
        EN_PROCESO = 'en_proceso', 'En proceso'
        ESPERANDO_SAT = 'esperando_sat', 'Esperando SAT'
        COMPLETADO = 'completado', 'Completado'
        ERROR = 'error', 'Error'
        REINTENTANDO = 'reintentando', 'Reintentando'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    empresa = models.ForeignKey(
        Empresa, on_delete=models.CASCADE, related_name='pipelines',
    )

    # Tipo de pipeline
    pipeline_type = models.CharField(
        max_length=30, choices=PipelineType.choices, db_index=True,
    )

    # Estado general
    estado = models.CharField(
        max_length=20, choices=StepStatus.choices, default=StepStatus.PENDIENTE,
    )
    paso_actual = models.IntegerField(default=1)
    total_pasos = models.IntegerField(default=1)
    paso_nombre = models.CharField(max_length=100, default='')

    # Progreso detallado — JSON con el estado de cada paso
    pasos_detalle = models.JSONField(default=list)

    # Reintentos del paso actual
    intento_actual = models.IntegerField(default=1)
    max_intentos = models.IntegerField(default=5)
    proximo_intento = models.DateTimeField(null=True, blank=True)

    # Integración con SAT Health
    bloqueado_por_sat = models.BooleanField(default=False)
    sat_health_al_iniciar = models.FloatField(null=True, blank=True)

    # Mensajes para el cliente
    mensaje_cliente = models.CharField(max_length=500, default='Iniciando proceso...')

    # Error tracking
    ultimo_error = models.TextField(blank=True, default='')
    errores_acumulados = models.IntegerField(default=0)

    # Timestamps
    iniciado = models.DateTimeField(auto_now_add=True)
    actualizado = models.DateTimeField(auto_now=True)
    completado_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-actualizado']
        indexes = [
            models.Index(fields=['empresa', 'pipeline_type', 'estado']),
            models.Index(fields=['estado', 'actualizado']),
        ]
        verbose_name = 'Pipeline State'
        verbose_name_plural = 'Pipeline States'

    def __str__(self):
        return (
            f"{self.empresa.rfc} | {self.get_pipeline_type_display()} | "
            f"Paso {self.paso_actual}/{self.total_pasos} | {self.estado}"
        )

    @property
    def progreso_pct(self):
        if self.total_pasos == 0:
            return 0
        completados = sum(
            1 for p in self.pasos_detalle if p.get('status') == 'completado'
        )
        return round((completados / self.total_pasos) * 100)

    @property
    def is_active(self):
        return self.estado in ['pendiente', 'en_proceso', 'esperando_sat', 'reintentando']


class SystemSettings(models.Model):
    """Configuración global del sistema (singleton).

    Almacena credenciales SMTP de las cuentas noreply y contacto.
    Los passwords se encriptan con Fernet (misma llave que FIEL).
    Editable desde el admin de Django.
    """

    # SMTP shared config
    smtp_host = models.CharField(
        max_length=200, default="chocobo.mxrouting.net",
        help_text="Servidor SMTP",
    )
    smtp_port = models.IntegerField(default=465, help_text="Puerto SMTP (465=SSL)")
    smtp_use_ssl = models.BooleanField(default=True)
    smtp_imap_port = models.IntegerField(default=993, help_text="Puerto IMAP (lectura)")

    # Cuenta noreply — emails automáticos del sistema
    noreply_email = models.EmailField(
        default="noreply@nubex.me",
        help_text="Cuenta para emails automáticos (confirmaciones, alertas, reportes)",
    )
    noreply_password_encrypted = models.BinaryField(null=True, blank=True)
    noreply_display_name = models.CharField(max_length=100, default="Cirrus")

    # Cuenta contacto — comunicación con clientes
    contacto_email = models.EmailField(
        default="contactocirrus@nubex.me",
        help_text="Cuenta para contacto con clientes",
    )
    contacto_password_encrypted = models.BinaryField(null=True, blank=True)
    contacto_display_name = models.CharField(max_length=100, default="Contacto Cirrus")

    # Telegram — bot para alertas del sistema
    telegram_enabled = models.BooleanField(
        default=False,
        help_text="Habilita el envío de alertas vía Telegram",
    )
    telegram_bot_token_encrypted = models.BinaryField(
        null=True, blank=True,
        help_text="Token del bot de Telegram (encriptado con Fernet)",
    )
    telegram_bot_username = models.CharField(
        max_length=100, blank=True,
        help_text="Username del bot (ej: Cirrusbot_bot)",
    )
    telegram_admin_chat_id = models.CharField(
        max_length=50, blank=True,
        help_text="Chat ID del admin principal que recibe alertas",
    )
    telegram_send_info = models.BooleanField(
        default=False,
        help_text="Enviar alertas nivel 'info' al admin",
    )
    telegram_send_warning = models.BooleanField(
        default=True,
        help_text="Enviar alertas nivel 'warning' al admin",
    )
    telegram_send_error = models.BooleanField(
        default=True,
        help_text="Enviar alertas nivel 'error' al admin",
    )
    telegram_send_critical = models.BooleanField(
        default=True,
        help_text="Enviar alertas nivel 'critical' al admin",
    )
    telegram_solo_stack = models.BooleanField(
        default=True,
        help_text=(
            "Si está activo, SOLO se envían al admin alertas de stack "
            "(salud SAT, patrones de falla de jobs, servicios caídos, "
            "incidentes). Se silencian eventos individuales de descarga, "
            "FIEL de clientes y probes individuales."
        ),
    )

    # Snowie webhook (captura de leads desde Snowie.ai)
    snowie_agente_id = models.CharField(
        max_length=100, blank=True,
        help_text="agente_id esperado en los webhooks de Snowie (validación)",
    )
    snowie_webhook_secret_encrypted = models.BinaryField(
        null=True, blank=True,
        help_text="Secret HMAC compartido con Snowie (Fernet encriptado). Si está vacío, solo se valida agente_id.",
    )

    # Metadata
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        User, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="system_settings_updates",
    )

    class Meta:
        verbose_name = "Configuración del Sistema"
        verbose_name_plural = "Configuración del Sistema"

    def __str__(self):
        return f"SystemSettings (actualizado {self.updated_at:%Y-%m-%d %H:%M})"

    def save(self, *args, **kwargs):
        self.pk = 1  # Singleton
        super().save(*args, **kwargs)

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


class TelegramAlert(models.Model):
    """Log de alertas enviadas (o intentadas) via Telegram bot.

    Registra cada intento de envío con su estado, permitiendo auditar el
    canal de alertas desde el panel admin.
    """

    LEVEL_CHOICES = [
        ("info", "Info"),
        ("warning", "Warning"),
        ("error", "Error"),
        ("critical", "Critical"),
        ("success", "Success"),
    ]

    STATUS_CHOICES = [
        ("sent", "Enviado"),
        ("failed", "Fallido"),
        ("skipped", "Omitido (deshabilitado)"),
    ]

    level = models.CharField(max_length=10, choices=LEVEL_CHOICES, db_index=True)
    category = models.CharField(max_length=30, blank=True, db_index=True)
    message = models.CharField(max_length=500)
    chat_id = models.CharField(max_length=50, help_text="Chat ID destino")
    recipient_user = models.ForeignKey(
        User, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="telegram_alerts",
        help_text="Usuario destinatario si está vinculado",
    )
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, db_index=True)
    http_status = models.IntegerField(null=True, blank=True)
    error = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Alerta Telegram"
        verbose_name_plural = "Alertas Telegram"
        indexes = [
            models.Index(fields=["status", "created_at"]),
            models.Index(fields=["level", "created_at"]),
        ]

    def __str__(self):
        return f"[{self.level}] {self.message[:60]} → {self.chat_id} ({self.status})"


class SnowieLead(models.Model):
    """Lead capturado vía webhook desde Snowie.ai.

    Idempotente por session_id. El payload completo se guarda para auditoría
    y debug. Las acciones (notificar Telegram, enviar email) se disparan
    de forma asíncrona vía Celery.
    """

    ESTADO_CHOICES = [
        ("nuevo", "Nuevo"),
        ("contactado", "Contactado"),
        ("convertido", "Convertido"),
        ("descartado", "Descartado"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # Identificación de Snowie
    session_id = models.CharField(max_length=200, unique=True, db_index=True)
    agente_id = models.CharField(max_length=100, db_index=True)

    # Datos capturados (todos opcionales — Snowie puede mandar parcial)
    nombre = models.CharField(max_length=200, blank=True, null=True)
    email = models.EmailField(blank=True, null=True, db_index=True)
    telefono = models.CharField(max_length=20, blank=True, null=True)
    rfc_empresa = models.CharField(max_length=13, blank=True, null=True)
    plan_interesado = models.CharField(max_length=50, blank=True, null=True)
    summary = models.TextField(blank=True, null=True)

    # Payload completo del POST original (auditoría / debug)
    payload_raw = models.JSONField(default=dict)

    # Estado comercial
    estado = models.CharField(
        max_length=20, choices=ESTADO_CHOICES, default="nuevo", db_index=True,
    )

    # Tracking de acciones automáticas
    notificado_telegram = models.BooleanField(default=False)
    email_enviado = models.BooleanField(default=False)

    # Metadata
    creado_en = models.DateTimeField(auto_now_add=True, db_index=True)
    actualizado_en = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-creado_en"]
        verbose_name = "Lead Snowie"
        verbose_name_plural = "Leads Snowie"
        indexes = [
            models.Index(fields=["estado", "creado_en"]),
            models.Index(fields=["agente_id", "creado_en"]),
        ]

    def __str__(self):
        return f"{self.email or self.nombre or self.session_id} [{self.estado}]"


# ═══════════════════════════════════════════════════════════════════════
# CEREBRO FISCAL — biblioteca documental con RAG
# ═══════════════════════════════════════════════════════════════════════


class DocumentoFiscal(models.Model):
    """Documento fiscal subido para indexación en el Cerebro Fiscal.

    Pipeline de 4 fases (task procesar_documento_fiscal):
      recibido → convirtiendo → convertido → validando → (rechazado | validado)
                                                         → embeddiendo → indexado

    Al subir, el usuario solo envía el archivo. Todo el resto (título,
    categoría, metadata fiscal) lo extrae el LLM (Qwen 2.5 72B) en fase 3.

    MinIO layout:
      cerebro-fiscal/originales/{uuid_archivo}_{nombre}      → archivo_original_key
      cerebro-fiscal/markdown/{uuid_archivo}.md              → archivo_md_key
      cerebro-fiscal/metadata/{uuid_archivo}.json            → archivo_json_key
    """

    CATEGORIA_CHOICES = [
        ("ley", "Ley Federal"),
        ("reglamento", "Reglamento"),
        ("rmf", "Resolución Miscelánea Fiscal"),
        ("criterio", "Criterio Normativo SAT"),
        ("guia", "Guía / Instructivo SAT"),
        ("jurisprudencia", "Jurisprudencia / Tesis"),
        ("nif", "Norma de Información Financiera"),
        ("catalogo", "Catálogo SAT"),
        ("otro", "Otro documento"),
    ]

    ESTADO_CHOICES = [
        ("recibido",           "Recibido"),
        ("convirtiendo",       "Convirtiendo (Docling)"),
        ("convertido",         "Convertido a Markdown"),
        ("validando",          "Validando (Qwen)"),
        ("rechazado",          "Rechazado — no fiscal"),
        ("validado",           "Validado — metadata OK"),
        ("requiere_decision",  "Versión anterior detectada — requiere acción"),
        ("embeddiendo",        "Generando embeddings"),
        ("indexado",           "Indexado"),
        ("archivado",          "Archivado — reemplazado por nueva versión"),
        ("error",              "Error"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    uuid_archivo = models.UUIDField(
        default=uuid.uuid4, unique=True, db_index=True, editable=False,
        help_text="UUID usado para el naming de archivos en MinIO",
    )

    # Metadata extraída por Qwen (todos en blank hasta fase 3)
    titulo = models.CharField(max_length=200, blank=True)
    descripcion = models.TextField(blank=True, null=True)
    categoria = models.CharField(
        max_length=20, choices=CATEGORIA_CHOICES, db_index=True, blank=True,
    )
    fuente_url = models.URLField(blank=True, null=True, max_length=500)
    año_vigencia = models.IntegerField(blank=True, null=True, db_index=True)
    fecha_publicacion = models.DateField(null=True, blank=True)
    fecha_ultima_revision = models.DateField(null=True, blank=True)
    organismo_emisor = models.CharField(max_length=100, blank=True)
    temas_clave = models.JSONField(default=list, blank=True)
    aplica_a = models.JSONField(
        default=list, blank=True,
        help_text="Lista: persona_fisica, persona_moral, etc.",
    )

    # Archivo original y derivados en MinIO
    nombre_archivo_original = models.CharField(max_length=300, blank=True)
    archivo_original_key = models.CharField(max_length=500, blank=True)
    archivo_md_key = models.CharField(max_length=500, blank=True)
    archivo_json_key = models.CharField(max_length=500, blank=True)
    archivo_tamano_bytes = models.BigIntegerField(default=0)
    archivo_content_type = models.CharField(max_length=100, blank=True)
    hash_sha256 = models.CharField(
        max_length=64, unique=True, db_index=True,
        help_text="Hash del archivo original para evitar duplicados",
    )

    # Pipeline
    estado = models.CharField(
        max_length=20, choices=ESTADO_CHOICES,
        default="recibido", db_index=True,
    )
    motivo_rechazo = models.TextField(blank=True, null=True)
    error_detalle = models.TextField(blank=True, null=True)
    chunks_count = models.IntegerField(default=0)
    metadata_extra = models.JSONField(
        default=dict, blank=True,
        help_text="Datos auxiliares (ej. version_anterior_id para decisión admin)",
    )
    intentos_conversion = models.IntegerField(default=0)
    intentos_validacion = models.IntegerField(default=0)
    intentos_embedding = models.IntegerField(default=0)
    job_id_celery = models.CharField(max_length=200, blank=True)

    # Metadata de sistema
    subido_por = models.ForeignKey(
        User, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="documentos_fiscales",
    )
    creado_en = models.DateTimeField(auto_now_add=True, db_index=True)
    actualizado_en = models.DateTimeField(auto_now=True)
    indexado_en = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-creado_en"]
        verbose_name = "Documento Fiscal"
        verbose_name_plural = "Documentos Fiscales"
        indexes = [
            models.Index(fields=["categoria", "estado"]),
            models.Index(fields=["estado", "creado_en"]),
        ]

    def __str__(self):
        return f"{self.titulo or self.nombre_archivo_original} [{self.estado}]"


class ChunkFiscal(models.Model):
    """Fragmento (chunk) de un DocumentoFiscal con su embedding.

    Se genera automáticamente al indexar. El embedding permite búsqueda
    semántica vía similitud coseno en pgvector.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    documento = models.ForeignKey(
        DocumentoFiscal, on_delete=models.CASCADE, related_name="chunks",
    )
    contenido = models.TextField()
    embedding = VectorField(dimensions=1024)  # bge-m3 via Ollama local (Spark DGX)
    pagina = models.IntegerField(null=True, blank=True)
    posicion_chunk = models.IntegerField(db_index=True)
    tokens = models.IntegerField(default=0)
    metadata = models.JSONField(default=dict, blank=True)
    creado_en = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["documento", "posicion_chunk"]
        verbose_name = "Chunk Fiscal"
        verbose_name_plural = "Chunks Fiscales"
        indexes = [
            models.Index(fields=["documento", "posicion_chunk"]),
            # El índice HNSW sobre embedding se crea en RunSQL en la migración
            # porque requiere opciones específicas no soportadas nativamente.
        ]

    def __str__(self):
        return f"chunk#{self.posicion_chunk} de {self.documento.titulo[:40]}"


# ═══════════════════════════════════════════════════════════════════════
# STRIPE — log persistente de webhooks con idempotencia
# ═══════════════════════════════════════════════════════════════════════


class StripeWebhookEvent(models.Model):
    """Log persistente de TODO evento Stripe recibido.

    Garantiza idempotencia vía `stripe_event_id` (unique) y deja auditoría
    completa para reconciliación. Los eventos pueden reprocesarse manualmente
    desde el panel admin cuando hay error.
    """

    ESTADO_CHOICES = [
        ("recibido", "Recibido"),
        ("procesado", "Procesado"),
        ("error", "Error"),
        ("ignorado", "Ignorado (tipo no manejado)"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    stripe_event_id = models.CharField(
        max_length=200, unique=True, db_index=True,
        help_text="ID del evento en Stripe — garantiza idempotencia",
    )
    event_type = models.CharField(max_length=100, db_index=True)
    customer_id = models.CharField(max_length=100, blank=True, db_index=True)
    estado = models.CharField(
        max_length=20, choices=ESTADO_CHOICES,
        default="recibido", db_index=True,
    )
    payload = models.JSONField()
    error_detalle = models.TextField(blank=True, null=True)
    intentos = models.IntegerField(default=0)
    recibido_en = models.DateTimeField(auto_now_add=True, db_index=True)
    procesado_en = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-recibido_en"]
        verbose_name = "Evento Stripe"
        verbose_name_plural = "Eventos Stripe"
        indexes = [
            models.Index(fields=["estado", "recibido_en"]),
            models.Index(fields=["event_type", "recibido_en"]),
        ]

    def __str__(self):
        return f"{self.event_type} [{self.estado}] {self.stripe_event_id}"


class DescargaIncidente(models.Model):
    """Incidente operativo de descarga — inteligencia para el admin.

    Se crea automáticamente cuando un DescargaJob lleva demasiado tiempo
    sin completarse (stuck) o cuando se detecta un patrón de falla.
    Sirve como log curado de problemas reales (no ruido de eventos).
    """

    TIPO_CHOICES = [
        ("timeout", "Timeout"),
        ("sat_error", "Error SAT"),
        ("fiel_error", "Error FIEL"),
        ("gap_detectado", "Gap detectado"),
        ("otro", "Otro"),
    ]

    empresa = models.ForeignKey(
        Empresa, on_delete=models.CASCADE, related_name="incidentes",
    )
    tipo = models.CharField(
        max_length=20, choices=TIPO_CHOICES, default="otro", db_index=True,
    )
    descripcion = models.TextField(blank=True)
    job = models.ForeignKey(
        DescargaJob, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="incidentes",
        help_text="Job que originó el incidente (opcional)",
    )
    resuelto = models.BooleanField(default=False, db_index=True)
    creado_en = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-creado_en"]
        verbose_name = "Incidente de Descarga"
        verbose_name_plural = "Incidentes de Descarga"
        indexes = [
            models.Index(fields=["resuelto", "creado_en"]),
            models.Index(fields=["tipo", "creado_en"]),
        ]

    def __str__(self):
        estado = "resuelto" if self.resuelto else "abierto"
        return f"[{self.tipo}] {self.empresa.rfc} ({estado})"
