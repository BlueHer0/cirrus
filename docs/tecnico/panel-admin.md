# Panel Admin — Documentación Técnica

## 1. Filosofía

El panel admin de Cirrus está diseñado para el **dueño del negocio**, no para administrar datos de clientes. Su propósito es:

- Visualizar KPIs comerciales (MRR, churn, funnel, ARPU)
- Gestionar la relación comercial con clientes (planes, contacto, churn)
- Monitorear la salud operativa del sistema (workers, SAT, jobs)
- Configurar parámetros del sistema (Telegram, SMTP, etc.)

**No es** una herramienta para ver, manipular ni exportar datos fiscales de clientes (CFDIs, RFCs de empresas, montos facturados). Esos datos pertenecen al cliente y solo deben ser accesibles desde el panel del propio cliente.

## 2. Política de privacidad de datos fiscales

| Tipo de dato | ¿Visible en admin? |
|-------------|-------------------|
| Email del cliente | ✅ Sí |
| Plan / MRR / fechas suscripción | ✅ Sí |
| Pagos Stripe | ✅ Sí |
| Razón social y RFC del cliente (para facturarle) | ✅ Sí |
| **Número de empresas** que tiene registradas | ✅ Sí |
| Última actividad (timestamp) | ✅ Sí |
| **RFCs de las empresas** del cliente | ❌ NO |
| **Nombres de las empresas** del cliente | ❌ NO |
| **CFDIs** descargados | ❌ NO |
| **Montos facturados** del cliente | ❌ NO |
| Detalle de descargas individuales por cliente | ❌ NO |

En el monitor operativo, las descargas y pipelines se muestran con un identificador opaco (`cliente-{uuid_short}`) en lugar del RFC, para que el dueño pueda diagnosticar problemas sin exponer datos fiscales.

## 3. Exclusión de staff/superusers

Todas las métricas comerciales del dashboard excluyen automáticamente usuarios con `is_staff=True` o `is_superuser=True`. Esto garantiza que los KPIs reflejen el negocio real y no contaminen las métricas con cuentas internas de Cirrus.

Implementación: `core/services/dashboard_stats.py`, helpers `_client_profiles_qs()` y `_client_users_qs()`.

## 4. Estructura del sidebar

```
📊 Dashboard       — KPIs de negocio + agregados del sistema
👤 Clientes        — Gestión comercial (sin datos fiscales)
🎯 Leads           — Funnel de conversión (ConversionLead)
💬 Telegram        — Configuración del bot + log de alertas
🔔 Monitor         — Salud operativa (workers, SAT, jobs, pipelines)
```

**Removido del sidebar:** Empresas, CFDIs, Descargas, API Keys (son datos de cliente, no de negocio). Las URLs siguen existiendo en `core/urls.py` para mantener compatibilidad con referencias internas, pero no son accesibles desde la navegación principal.

## 5. Vistas y archivos clave

| Vista | URL | Archivo template | View function |
|-------|-----|------------------|---------------|
| Dashboard | `/panel/` | `panel/dashboard.html` | `core.views.dashboard` |
| Clientes (lista) | `/panel/clientes/` | `panel/clientes_list.html` | `core.views.clientes_list` |
| Cliente (detalle) | `/panel/clientes/<id>/` | `panel/cliente_detalle.html` | `core.views.cliente_detalle` |
| Leads | `/panel/crm/` | `panel/crm.html` | `core.views.crm_list` |
| Telegram | `/panel/telegram/` | `panel/telegram_config.html` | `core.views.telegram_config` |
| Monitor | `/panel/monitor/` | `panel/monitor.html` | `core.views.monitor_view` |

## 6. Servicio de stats

`core/services/dashboard_stats.py` provee las siguientes funciones puras:

| Función | Devuelve |
|---------|----------|
| `business_kpis()` | MRR, clientes activos, nuevos 30d, churn 30d, revenue mes, ARPU |
| `plan_distribution()` | Lista de planes con clientes/MRR por cada uno |
| `growth_series(months=6)` | Nuevos clientes por mes (sparkline) |
| `funnel_conversion()` | Funnel: leads → registrados → pagados (7 etapas) |
| `operational_health()` | SAT health, empresas con FIEL, jobs en cola, fallos 24h |
| `system_aggregate_stats()` | Agregados informativos: API keys, downloads activos, CFDIs sistema |
| `attention_required()` | Lista accionable de alertas (past_due, FIEL por vencer, etc.) |
| `clientes_list_data()` | Lista de clientes con métricas comerciales (sin datos fiscales) |

Todas excluyen staff/superusers automáticamente.

## 7. Acciones comerciales en `cliente_detalle`

La vista de cliente acepta POST con las siguientes acciones:

| Action | Efecto |
|--------|--------|
| `change_plan` (con `plan_slug`) | Cambia `plan_fk` y `plan_legacy` del perfil. **No toca Stripe** |
| `mark_churned` | Marca `subscription_status='canceled'` y `cancel_at_period_end=True`. **No toca Stripe** |
| `reactivate` | Marca `subscription_status='active'` y `cancel_at_period_end=False` |

Para cambios que sí afecten Stripe (cobros, cancelaciones reales), usar el dashboard de Stripe directamente.

## 8. Seguridad

- Todas las views llevan `@staff_required` (decorator definido en `core/views.py`)
- `cliente_detalle` rechaza acceder a un usuario `is_staff=True` o `is_superuser=True` (redirect con mensaje de error)
- Los cambios de plan y churn quedan auditables vía `messages.success()` en el panel y logs de Django

## 9. Limpieza de usuarios de prueba

Comando: `python manage.py delete_test_users [--dry-run] [--emails ...]`

- Por defecto borra `arizpef@gmail.com` y `farizpe@nubex.me`
- Con `--dry-run` solo muestra el SELECT de relaciones
- Protege automáticamente a usuarios con `is_staff=True` o `is_superuser=True`
- Hard delete con `transaction.atomic()` y CASCADE en BD

## Documentos relacionados

- [Modelo de datos](modelo-datos.md) — tablas usadas por el panel
- [Jobs y tasks](jobs-y-tasks.md) — qué muestra el monitor
