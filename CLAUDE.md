# CLAUDE.md — Cirrus (cirrus.nubex.me)

> SaaS de gestión de CFDIs: descarga automática del SAT con FIEL, custodia XML, API REST,
> análisis fiscal, y (en construcción) emisión/timbrado y motor contable multi-empresa.
> Operador legal: Fernando Arizpe Pedraza, PF con actividad empresarial (RFC AIPF760625HF5).
> Creado 2026-07-28 por la dirección operativa (Claude).

## Dirección operativa del proyecto

Desde 2026-07-28 Claude actúa como **director operativo** de Cirrus por mandato de Fernando:
operar el SaaS como empresa (growth, comercial, soporte, UX, ops) y monetizarlo.

**Base de dirección (leer al arrancar cualquier sesión de trabajo):**
- `/home/farizpe/cirrus-direccion/PLAN-DIRECTOR.md` — estrategia, fases, agentes, KPIs
- `/home/farizpe/cirrus-direccion/recon-comercial-2026-07-28.md` — estado comercial real (bloqueadores y palancas)
- `/home/farizpe/cirrus-direccion/diseno-modulo-emision.md` — diseño del hub de timbrado (SW Sapien)
- `/home/farizpe/cirrus-direccion/sw-sandbox/` — PoC de timbrado funcionando + credenciales sandbox en `credenciales/` (chmod 600)
- `/home/farizpe/cirrus-direccion/legal/` — ToS y Aviso de Privacidad (interim autorizado por Fernando 2026-07-28, "bajo mi riesgo")
- `/home/farizpe/auditorias/contabilidad-2026-07/anexo-C-cirrus.md` — auditoría técnica completa (brechas: completitud de descarga, complementos sin exponer, cancelados sin re-verificar)

## Stack e infraestructura

- Django 5 + Django Ninja (API), Python 3.12, venv en `venv/`
- **systemd nativo, NO Docker**: `cirrus-web` (Gunicorn :8200), `cirrus-worker` (Celery), `cirrus-beat`, `cirrus-cerebro` (RAG fiscal). Units en `systemd/`
- PostgreSQL `cirrus_db` (host) · Redis (colas: descarga/verificacion/sistema/scheduler/cerebro) · MinIO (bucket `cirrus`: FIELs, XMLs, logos)
- Nginx: `/etc/nginx/sites-enabled/cirrus.conf` → 127.0.0.1:8200
- Descarga SAT: RPA Playwright (`sat_scrapper_core/`), ventanas por plan, máx 3 slots/15min
- Ollama en Spark (Tailscale) para IA local; OpenAI solo donde esté configurado
- Backups: cron diario 4AM → `backups/` (pg_dump + .env), retención 30 días

## Reglas duras

1. **`.env` y `cirrus_secrets/`**: NUNCA mostrar valores de secretos en salidas/reportes. Editar `.env` solo con instrucción explícita de Fernando.
2. **FIELs y CSDs de clientes** (MinIO + Fernet): no descargarlos, no moverlos, no listarlos fuera de tareas que lo requieran.
3. **Reinicios**: `sudo systemctl restart cirrus-web` solo tras cambios que lo requieran; worker/beat/cerebro no se reinician sin causa. Verificar con `systemctl status` después.
4. **BD**: SELECTs libres; UPDATE/DELETE solo con respaldo previo y aprobación. Migraciones solo con aprobación explícita de Fernando.
5. **Acciones hacia fuera** (emails a clientes/leads, publicaciones, cambios de precios): las primeras de cada tipo se muestran a Fernando antes de enviar; ya aprobado el patrón, se automatizan.
6. Los conteos/estado en docs se desactualizan — verificar en BD antes de afirmar.

## Documentación interna del repo

`docs/ARCHITECTURE.md` (verdad técnica + problemas conocidos), `docs/API.md`, `docs/PAYMENTS.md`
(runbook Stripe live), `docs/PLANS.md`, `docs/tecnico/`, `docs/usuario/` (guía + FAQ sin publicar).

## Estado comercial (2026-07-29 — actualizar al cambiar)

- **LANZADO 2026-07-29:** landing pública y registro self-service ABIERTOS (flags en `settings.py:346-347` = True, aprobado por Fernando). Stripe **LIVE** instalado y verificado (cuenta acct_1TBzsrGpAIk1tsz8 "Cirrus.Nubex.me", descriptor CIRRUS, charges+payouts enabled); 6 productos live creados vía `setup_stripe`; webhook `we_1Ty2Yq...` con 4 eventos. Claves en `.env` (backup `.env.bak-pre-live-*`) y en `/home/farizpe/cirrus-direccion/credenciales/stripe-live.env`.
- Legal interim publicado: `/terminos/` y `/privacidad/` (v1, fuentes en `cirrus-direccion/legal/*-v1.md`), checkbox obligatorio en registro con evidencia en SystemLog. Pendiente: revisión de abogado (Fernando asumió riesgo interim).
- 0 clientes externos aún; 5 empresas internas. Lead contactado: JuanPerez@gmail.com (fundador: 2 meses Básico gratis, 2026-07-28).
- SW Sapien sandbox dominado (PoC timbrado+cancelación OK, 30 timbres de prueba); folios reales pendientes; diseño del módulo de emisión en `cirrus-direccion/diseno-modulo-emision.md` (no implementado).
- Completitud de descarga: atacada en commit `e877060` (compulsa WS + re-verificación estados). Validar en operación.
- Pendientes inmediatos de dirección: commit de los cambios legales/flags, rutina diaria de ops, agente de buzón contactocirrus@nubex.me, analytics en landing, rotar la Stripe secret key (viajó por chat en el arranque).
