# Cirrus — Sistema de Planes y Descargas

## Filosofía
El cliente NUNCA elige manualmente qué descargar.
Cirrus administra todo automáticamente según el plan.
El cliente solo ve progreso y fechas de próxima descarga.

## Planes

### Gratis ($0/mes)
- 1 empresa (RFC)
- Descarga automática: solo mes anterior al cerrar
- 30 CFDIs visibles
- PDFs ilimitados (con publicidad Cirrus)
- Excel: 3/mes
- Sin API REST
- Historial: solo meses descargados (no compra histórico)
- Solo primeros 100 registros, luego se elimina plan gratis
- Prioridad de descarga: BAJA (después de planes de pago)
- Programación: después de todos los clientes de pago

### Básico ($199/mes)
- 2 empresas
- Al registrarse: descarga todo 2026 (enero → mes anterior)
- Después: descarga automática día 1 y 15 de cada mes
- 500 CFDIs visibles (entre todas las empresas)
- PDFs ilimitados (con publicidad Cirrus)
- Excel: 10/mes
- Sin API REST
- Puede comprar 2025: $500 MXN por empresa (pago único)
- CFDI extra sobre límite: $0.25 c/u (cuenta por pagar)
- Prioridad: MEDIA

### Profesional ($499/mes)
- 6 empresas
- Al registrarse: descarga todo desde enero 2025
- Después: descarga semanal automática (4/mes)
- 5,000 CFDIs visibles
- PDFs ilimitados (con publicidad Cirrus + tu logo)
- Excel: 50/mes
- API REST completa
- Puede comprar 2024: $500 MXN por empresa (pago único)
- CFDI extra: $0.25 c/u
- Prioridad: ALTA

### Enterprise ($1,299/mes)
- 15 empresas
- Al registrarse: descarga todo desde enero 2024
- Después: descarga cada ~2.5 días (12/mes)
- 50,000 CFDIs visibles
- PDFs ilimitados (con publicidad Cirrus + tu logo)
- Excel: ilimitado
- API REST completa
- Puede comprar 2023: $500 MXN por empresa (pago único)
- CFDI extra: $0.25 c/u
- Prioridad: MÁXIMA

### Owner (oculto, $0)
- Todo ilimitado, no aparece en pricing
- Solo para cuentas de Fernando

## Excedentes (a destajo)
- RFC adicional sobre el plan: $49 MXN/mes
- CFDI extra sobre límite visible: $0.25 MXN c/u
- Año histórico adicional por empresa: $500 MXN (pago único)

## Compra de histórico
- Cada plan tiene un año base incluido
- Años anteriores se compran POR EMPRESA, no por cuenta
- Ejemplo: Plan Básico + 3 empresas + quiere 2025 para 2 de ellas:
  $199/mes + $500×2 = $199/mes + $1,000 único
- Máximo hacia atrás: 2023 (Enterprise)
- El botón "Comprar 2024" aparece solo si su plan lo permite
- Al comprar, se encolan las descargas automáticamente

## Lógica del agente de sincronización
1. Revisa cada 15 min qué empresas necesitan descarga
2. Prioridad: Enterprise > Pro > Básico > Gratis
3. No encola si ya existe descarga completada para ese periodo
4. Respeta los límites del plan (no descarga periodos no incluidos)
5. Gratis: solo programa al cerrar el mes
6. Básico: programa día 1 y 15
7. Pro: programa semanal
8. Enterprise: programa cada ~2.5 días

## Lo que ve el cliente en /app/descargas/
- NO hay formulario manual de descarga
- Card por empresa con:
  - Cobertura (desde cuándo hasta presente)
  - Progreso (% sincronizado)
  - Última descarga (fecha + cuántos CFDIs)
  - Próxima descarga (fecha estimada)
  - Total CFDIs
- Si hay año histórico disponible para comprar:
  Botón "¿Necesitas {año}? $500 MXN por empresa → [Solicitar]"
- Mensaje: "Cirrus descarga automáticamente según tu plan.
  Las descargas se programan en horarios de baja demanda del SAT."
