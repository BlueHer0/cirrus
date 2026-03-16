# 🧾 sat-scrapper-core

Librería reutilizable para descarga masiva de CFDIs del SAT usando FIEL (e.firma).

**Motor dual**: Playwright RPA (portal web) + API SOAP (Descarga Masiva) con auto-fallback.

## 🚀 Instalación

```bash
# Core (RPA solamente)
pip install -e .

# Con soporte SOAP API (recomendado)
pip install -e ".[soap]"

# Instalar Chromium para Playwright
playwright install chromium
```

## 📥 Uso Rápido — CLI

```bash
# Descargar CFDIs (motor automático: intenta SOAP, cae a RPA)
sat-scrapper download \
    --cer /ruta/a/mi.cer \
    --key /ruta/a/mi.key \
    --password "mi_contraseña" \
    --year 2025 \
    --month-start 1 --month-end 6

# Con navegador visible (debug)
sat-scrapper download \
    --cer mi.cer --key mi.key \
    --year 2025 --headed --engine rpa

# Verificar FIEL
sat-scrapper verify-fiel --cer mi.cer --key mi.key

# Estadísticas de descargas
sat-scrapper stats --dir ./downloads
```

## 🐍 Uso como Librería (Python)

### Básico
```python
from sat_scrapper_core import ScrapeConfig, SATEngine

config = ScrapeConfig(
    cer_path="mi.cer",
    key_path="mi.key",
    password="xxx",
    year=2025,
    month_start=1,
    month_end=6,
    engine="auto",  # "rpa", "soap_api", o "auto"
)

async with SATEngine(config) as engine:
    result = await engine.download_all()
    print(f"Descargados: {result.total_cfdis} CFDIs")
```

### Con Callbacks
```python
config = ScrapeConfig(
    cer_path="mi.cer",
    key_path="mi.key",
    password="xxx",
    year=2025,
    on_progress=lambda msg: print(f"📌 {msg}"),
    on_month_completed=lambda y, m, t, files: guardar_en_bd(files),
    on_error=lambda e, ctx: enviar_alerta(e),
)
```

### Standalone (bloqueante)
```python
from sat_scrapper_core.adapters.standalone import run_download

result = run_download("mi.cer", "mi.key", "pass", year=2025)
```

### Django
```python
from sat_scrapper_core.adapters.django_adapter import launch_sat_download, get_download_status

# Lanzar en background
task_id = launch_sat_download(
    cer_path=settings.SAT_CER_PATH,
    key_path=settings.SAT_KEY_PATH,
    password=settings.SAT_PASSWORD,
    year=2025,
)

# Consultar estado
status = get_download_status(task_id)
# {'status': 'running', 'step': 'Descargando recibidos 2025-03...'}
```

## 📂 Estructura de Descargas

```
downloads/
└── VEN191127M21/           # RFC
    ├── 2025/
    │   ├── 01/
    │   │   ├── uuid1.xml
    │   │   └── uuid2.xml
    │   └── 02/
    │       └── uuid3.xml
    └── indice.csv          # Índice con metadatos de todos los CFDIs
```

## ⚙️ Motores de Descarga

| Motor | Método | Estabilidad | Velocidad | Dependencias |
|-------|--------|-------------|-----------|-------------|
| `rpa` | Playwright (portal web) | Media (HTML cambia) | Lenta | playwright, playwright-stealth |
| `soap_api` | Web Service SOAP | Alta (API oficial) | Rápida | satcfdi |
| `auto` | SOAP → RPA (fallback) | **Alta** | **Óptima** | Ambas (soap opcional) |

## 🛡️ Anti-detección

- `playwright-stealth` (evita detección de bot)
- User agent realista (Chrome 120 en Mac)
- Locale `es-MX` + timezone `America/Mexico_City`
- `--disable-blink-features=AutomationControlled`
- Rate limiting configurable entre consultas
- Consultas mes por mes (nunca rangos grandes)

## 📁 Arquitectura

```
sat_scrapper_core/
├── __init__.py              # API pública
├── config.py                # ScrapeConfig dataclass + selectores SAT
├── fiel.py                  # Carga FIEL (e.firma)
├── browser_bot.py           # Playwright + stealth
├── sat_navigator.py         # RPA engine (portal web)
├── sat_api.py               # SOAP engine (Descarga Masiva)
├── engine.py                # Orquestador dual (RPA + SOAP)
├── storage.py               # XML parser + organización archivos
├── utils.py                 # Retry, screenshots, safe actions
├── cli.py                   # CLI (Click)
└── adapters/
    ├── django_adapter.py    # Background tasks para Django
    └── standalone.py        # Script runner bloqueante
```
