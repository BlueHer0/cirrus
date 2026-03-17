# Cirrus — Programación de Descargas por Plan

## Reglas Globales

- **Ventana horaria**: Solo 10PM-4AM CST (04:00-10:00 UTC) + 22:00-23:59 UTC
- **Spacing**: Mínimo 10 minutos entre descargas del mismo RFC
- **Retry inteligente**: Login fallido → 30 min, Timeout → 15 min, Otro → escalada 5-60 min
- **Prioridad**: Enterprise > Pro > Básico > Gratis
- **Primera descarga**: Siempre permitida (bypass de horario y plan)
- **Rotación RFC**: `ultimo_scrape` ASC (el que lleva más sin descargar va primero)

---

## Plan Gratis ($0)

- **Descargas**: 1 vez al mes (días 1-5)
- **Cobertura**: Solo mes anterior completo
- **Prioridad**: Última en la cola
- **Descarga urgente extra**: $50 MXN

## Plan Básico ($199/mes)

- **Descargas**: 2 veces al mes
- **Programación**: Días 8-12 y 18-22
- **Al registrarse**: Descarga todo 2026
- **Distribución**: Tandeado por día de registro de la semana

## Plan Profesional ($499/mes)

- **Descargas**: 4 veces al mes (semanal)
- **Programación**: Día fijo por RFC → `hash(RFC) % 5`
  - 0=lunes, 1=martes, 2=miércoles, 3=jueves, 4=viernes
- **Al registrarse**: Descarga desde enero 2025

## Plan Enterprise ($1,299/mes)

- **Descargas**: 10+ veces al mes (cada ~3 días)
- **Programación**: Escalonado por RFC → `hash(RFC) % 3`
  - `dia_inicio = hash(RFC) % 3`
  - Descarga cuando `(dia - dia_inicio) % 3 == 0`
- **Al registrarse**: Descarga desde enero 2024

## Plan Owner (interno)

- Sin restricciones (misma lógica que Enterprise)
- Sin límites de empresas ni descargas

---

## Implementación

```python
# core/tasks.py

def _es_hora_optima():
    hora_utc = datetime.now(timezone.utc).hour
    return (4 <= hora_utc <= 10) or hora_utc >= 22

def _decidir_si_descargar(empresa, plan, now):
    slug = plan.slug if plan else "free"
    if slug == "free":
        return now.day <= 5
    elif slug == "basico":
        return abs(now.day - 10) <= 2 or abs(now.day - 20) <= 2
    elif slug == "pro":
        return now.weekday() == hash(empresa.rfc) % 5
    elif slug in ("enterprise", "owner"):
        return (now.day - hash(empresa.rfc) % 3) % 3 == 0
    return False
```
