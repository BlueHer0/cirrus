"""
Cirrus — Django Settings
Multi-tenant SaaS para CFDIs del SAT.
"""

import os
from pathlib import Path

from decouple import config, Csv
import dj_database_url
from celery.schedules import crontab

# ── Paths ────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent.parent

# ── Security ─────────────────────────────────────────────────────────────
SECRET_KEY = config("SECRET_KEY", default="insecure-dev-key-change-me")
DEBUG = config("DEBUG", default=False, cast=bool)
ALLOWED_HOSTS = config("ALLOWED_HOSTS", default="localhost,127.0.0.1", cast=Csv())
CSRF_TRUSTED_ORIGINS = ["https://cirrus.nubex.me"]

# ── Applications ─────────────────────────────────────────────────────────
INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # Third-party
    "django_celery_beat",
    # Local
    "core",
    "accounts",
    "reportes",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "cirrus.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "frontend" / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "cirrus.wsgi.application"

# ── Database (Host-First: PostgreSQL on VPS2) ───────────────────────────
DATABASES = {
    "default": dj_database_url.config(
        default=config("DATABASE_URL", default="postgresql://cirrus:password@localhost:5432/cirrus_db"),
    )
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ── Auth ─────────────────────────────────────────────────────────────────
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# ── Sessions ─────────────────────────────────────────────────────────────
SESSION_COOKIE_AGE = 3600 * 8              # 8 horas máximo
SESSION_EXPIRE_AT_BROWSER_CLOSE = True     # Expira al cerrar navegador
SESSION_SAVE_EVERY_REQUEST = True           # Renueva con actividad

# ── i18n ─────────────────────────────────────────────────────────────────
LANGUAGE_CODE = "es-mx"
TIME_ZONE = "America/Mexico_City"
USE_I18N = True
USE_TZ = True

# ── Static files ─────────────────────────────────────────────────────────
STATIC_URL = "/static/"
STATICFILES_DIRS = [BASE_DIR / "frontend" / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"

# ── Redis (Host-First) ──────────────────────────────────────────────────
REDIS_URL = config("REDIS_URL", default="redis://localhost:6379/0")

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": REDIS_URL,
    }
}

# ── Celery ───────────────────────────────────────────────────────────────
CELERY_BROKER_URL = config("CELERY_BROKER_URL", default="redis://localhost:6379/1")
CELERY_RESULT_BACKEND = config("CELERY_RESULT_BACKEND", default="redis://localhost:6379/1")
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_TIMEZONE = TIME_ZONE
CELERY_BEAT_SCHEDULER = "django_celery_beat.schedulers:DatabaseScheduler"
CELERY_WORKER_SEND_TASK_EVENTS = True
CELERY_TASK_SEND_SENT_EVENT = True
CELERY_TASK_ACKS_LATE = True
CELERY_TASK_REJECT_ON_WORKER_LOST = True

# ── Telegram Alerts ──────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = config("TELEGRAM_BOT_TOKEN", default="")
TELEGRAM_CHAT_ID = config("TELEGRAM_CHAT_ID", default="")
TELEGRAM_ALERTS_ENABLED = config("TELEGRAM_ALERTS_ENABLED", default=False, cast=bool)
CELERY_BEAT_SCHEDULE = {
    "procesar-cola-descargas": {
        "task": "core.tasks.procesar_cola_descargas",
        "schedule": 300,  # every 5 minutes
        "options": {"queue": "descarga"},
    },
    "generar-jobs-mensuales": {
        "task": "core.tasks.generar_jobs_mes",
        "schedule": crontab(day_of_month="1", hour="3", minute="0"),
        "options": {"queue": "scheduler"},
    },
    "health-check-playwright": {
        "task": "core.tasks.health_check_playwright",
        "schedule": 900,  # every 15 minutes
    },
    "benchmark-hourly-report": {
        "task": "core.tasks.benchmark_hourly_report",
        "schedule": 3600,  # every hour
    },
    "supervisor-cirrus": {
        "task": "core.tasks.supervisor_cirrus",
        "schedule": 900,  # every 15 minutes
        "options": {"queue": "sistema"},
    },
    "sync-efos-mensual": {
        "task": "core.tasks.sync_efos_task",
        "schedule": crontab(day_of_month="1", hour="5", minute="0"),
        "options": {"queue": "sistema"},
    },
    "descargar-csf-mensual": {
        "task": "core.tasks.descargar_csf_mensual",
        "schedule": crontab(day_of_month="2", hour="6", minute="0"),
        "options": {"queue": "descarga"},
    },
    "alertas-fiel-vencimiento": {
        "task": "core.tasks.alertas_vencimiento_fiel",
        "schedule": crontab(hour="8", minute="0"),
        "options": {"queue": "sistema"},
    },
    "sat-health-probe": {
        "task": "core.tasks.sat_health_probe",
        # 04:00 UTC (22:00 CST, ventana SAT 90% disponibilidad) y
        # 16:00 UTC (10:00 CST, fuera de la zona de degradación 09-13 UTC).
        # Antes: every 300s (288 logins/día). Ahora: 2/día.
        # Reduce el soft-throttling SAT que disparaba login_failed creciente.
        "schedule": crontab(hour="4,16", minute="0"),
        "options": {"queue": "sistema"},
    },
    "sat-health-summarize": {
        "task": "core.tasks.sat_health_summarize",
        "schedule": 3600,  # every hour
        "options": {"queue": "sistema"},
    },
    "supervisor-pipelines": {
        "task": "core.tasks.supervisor_pipelines",
        "schedule": 900,  # every 15 minutes (era 300)
        "options": {"queue": "sistema"},
    },
    "limpiar-tmp-fiel": {
        "task": "core.tasks.limpiar_tmp_fiel",
        "schedule": 1800,  # every 30 minutes
        "options": {"queue": "sistema"},
    },
    # ── API Keys maintenance ────────────────────────────────────────
    "reset-apikey-requests-diarios": {
        "task": "core.tasks_api_keys.reset_apikey_requests_diarios",
        "schedule": crontab(hour="0", minute="5"),  # cada día a las 00:05
        "options": {"queue": "sistema"},
    },
    "desactivar-apikeys-plan-cancelado": {
        "task": "core.tasks_api_keys.desactivar_apikeys_plan_cancelado",
        "schedule": 3600,  # cada hora
        "options": {"queue": "sistema"},
    },
}

CELERY_TASK_ROUTES = {
    "core.tasks.descargar_cfdis": {"queue": "descarga"},
    "core.tasks.procesar_cola_descargas": {"queue": "descarga"},
    "core.tasks.generar_jobs_mes": {"queue": "scheduler"},
    "core.tasks.verificar_fiel": {"queue": "verificacion"},
    "core.tasks.health_check_playwright": {"queue": "sistema"},
    "core.tasks.agente_sincronizacion": {"queue": "scheduler"},
    "core.tasks.benchmark_hourly_report": {"queue": "celery"},
    "core.tasks.sync_efos_task": {"queue": "sistema"},
    "core.tasks.supervisor_cirrus": {"queue": "sistema"},
    "core.tasks.verificar_fiel_y_descargar_csf": {"queue": "descarga"},
    "core.tasks.descargar_csf_mensual": {"queue": "descarga"},
    "core.tasks.descargar_csf_empresa": {"queue": "descarga"},
    "core.tasks.alertas_vencimiento_fiel": {"queue": "sistema"},
    "core.tasks.sat_health_probe": {"queue": "sistema"},
    "core.tasks.sat_health_summarize": {"queue": "sistema"},
    "core.tasks.supervisor_pipelines": {"queue": "sistema"},
    "core.tasks.limpiar_tmp_fiel": {"queue": "sistema"},
    # Cerebro Fiscal — worker dedicado cirrus-cerebro.service
    "core.cerebro_tasks.procesar_documento_fiscal": {"queue": "cerebro"},
}
CELERY_TASK_DEFAULT_QUEUE = "sistema"

# ── Session Configuration ────────────────────────────────────────────────
SESSION_COOKIE_AGE = 3600 * 8              # 8 hours max
SESSION_EXPIRE_AT_BROWSER_CLOSE = True     # Expire on browser close
SESSION_SAVE_EVERY_REQUEST = True          # Renew with activity
SESSION_COOKIE_SECURE = True              # Only HTTPS
SESSION_COOKIE_HTTPONLY = True             # No JS access
SESSION_COOKIE_SAMESITE = "Lax"           # CSRF protection
CSRF_COOKIE_SECURE = True                 # CSRF only HTTPS

# ── Docling (CSF Parser) ────────────────────────────────────────────────
DOCLING_URL = config("DOCLING_URL", default="http://10.20.0.5:8000/extract")

# ── Cerebro Fiscal (RAG sobre legislación fiscal) ──────────────────────
# Embeddings locales via Ollama en Spark DGX — sin API key, sin costo variable.
OLLAMA_BASE_URL = config("OLLAMA_BASE_URL", default="http://10.20.0.6:11434")
OLLAMA_EMBEDDING_MODEL = config("OLLAMA_EMBEDDING_MODEL", default="bge-m3")
OLLAMA_TIMEOUT = config("OLLAMA_TIMEOUT", default=60, cast=int)
CEREBRO_EMBEDDING_DIMS = config("CEREBRO_EMBEDDING_DIMS", default=1024, cast=int)
CEREBRO_CHUNK_TOKENS = config("CEREBRO_CHUNK_TOKENS", default=300, cast=int)
CEREBRO_CHUNK_OVERLAP = config("CEREBRO_CHUNK_OVERLAP", default=50, cast=int)
# Qwen 72B para clasificación / extracción de metadata
OLLAMA_CLASSIFIER_MODEL = config("OLLAMA_CLASSIFIER_MODEL", default="qwen2.5:72b")
OLLAMA_CLASSIFIER_TIMEOUT = config("OLLAMA_CLASSIFIER_TIMEOUT", default=300, cast=int)
# Los documentos se almacenan en el bucket `cirrus` con prefix `cerebro-fiscal/`
# para no requerir permisos de creación de buckets en MinIO.
CEREBRO_MINIO_PREFIX = config("CEREBRO_MINIO_PREFIX", default="cerebro-fiscal")

# ── MinIO (S3-compatible Object Storage) ─────────────────────────────────
MINIO_ENDPOINT = config("MINIO_ENDPOINT", default="localhost:9000")
MINIO_ACCESS_KEY = config("MINIO_ACCESS_KEY", default="minioadmin")
MINIO_SECRET_KEY = config("MINIO_SECRET_KEY", default="minioadmin")
MINIO_BUCKET = config("MINIO_BUCKET", default="cirrus")
MINIO_USE_SSL = config("MINIO_USE_SSL", default=False, cast=bool)

# ── FIEL Encryption ─────────────────────────────────────────────────────
FIEL_ENCRYPTION_KEY = config("FIEL_ENCRYPTION_KEY", default="")

# ── SAT Health Monitor ───────────────────────────────────────────────────
SAT_HEALTH_TOKEN = config("SAT_HEALTH_TOKEN", default="")

# ── Playwright ───────────────────────────────────────────────────────────
PLAYWRIGHT_BROWSERS_PATH = config("PLAYWRIGHT_BROWSERS_PATH", default=str(BASE_DIR / ".browsers"))
os.environ["PLAYWRIGHT_BROWSERS_PATH"] = PLAYWRIGHT_BROWSERS_PATH

# ── Logging ──────────────────────────────────────────────────────────────
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {
            "format": "[{asctime}] {levelname} {name} {message}",
            "style": "{",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "verbose",
        },
        "file": {
            "class": "logging.FileHandler",
            "filename": BASE_DIR / "logs" / "cirrus.log",
            "formatter": "verbose",
        },
    },
    "root": {
        "handlers": ["console"],
        "level": "INFO",
    },
    "loggers": {
        "django": {
            "handlers": ["console"],
            "level": "INFO",
            "propagate": False,
        },
        "core": {
            "handlers": ["console", "file"],
            "level": "DEBUG",
            "propagate": False,
        },
        "satscrapper": {
            "handlers": ["console", "file"],
            "level": "DEBUG",
            "propagate": False,
        },
        "celery": {
            "handlers": ["console", "file"],
            "level": "INFO",
            "propagate": False,
        },
    },
}

# ── Email (SMTP) ─────────────────────────────────────────────────────
EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
EMAIL_HOST = config("EMAIL_HOST", default="chocobo.mxrouting.net")
EMAIL_PORT = config("EMAIL_PORT", default=465, cast=int)
EMAIL_USE_SSL = config("EMAIL_USE_SSL", default=True, cast=bool)
EMAIL_HOST_USER = config("EMAIL_HOST_USER", default="")
EMAIL_HOST_PASSWORD = config("EMAIL_HOST_PASSWORD", default="")
EMAIL_TIMEOUT = 15  # seconds — prevents hanging on slow SMTP
DEFAULT_FROM_EMAIL = f'Cirrus <{config("EMAIL_HOST_USER", default="noreply@nubex.me")}>'

# Cuenta dedicada para reportes fiscales (separada de la cuenta de sistema)
EMAIL_REPORTES_USER = config("EMAIL_REPORTES_USER", default="")
EMAIL_REPORTES_PASSWORD = config("EMAIL_REPORTES_PASSWORD", default="")
EMAIL_REPORTES_FROM = f'Cirrus Reportes <{EMAIL_REPORTES_USER}>' if EMAIL_REPORTES_USER else DEFAULT_FROM_EMAIL

# ── Stripe Payments ──────────────────────────────────────────────────
STRIPE_PUBLISHABLE_KEY = config("STRIPE_PUBLISHABLE_KEY", default="")
STRIPE_SECRET_KEY = config("STRIPE_SECRET_KEY", default="")
STRIPE_WEBHOOK_SECRET = config("STRIPE_WEBHOOK_SECRET", default="")
STRIPE_TEST_MODE = config("STRIPE_TEST_MODE", default=True, cast=bool)

# ── Acceso público (toggleable, defaults cerrados para reparación) ──
# Cuando False: /app/registro/ redirige a login y la landing pública (/)
# redirige a /app/login/. Reversible cambiando a True + restart cirrus-web.
REGISTRO_PUBLICO_ABIERTO = False
LANDING_PUBLICA_HABILITADA = False
