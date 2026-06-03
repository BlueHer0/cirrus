# Análisis de monitoreo SAT — temporada fiscal 2026

- **Fecha del análisis:** 2026-05-24
- **Periodo de datos:** 2026-03-21 → 2026-05-24 (~64 días)
- **Autor:** análisis asistido (Claude) sobre datos productivos de CIRRUS
- **Fuentes:** `SATHealthProbe`, `DescargaLog`, `DescargaJob`, `django_celery_beat`

> Esta carpeta (`/var/www/cirrus/inteligencia/`) es la base de inteligencia del
> negocio. Cada análisis importante genera un `.md` fechado aquí.

---

## 1. Contexto

CIRRUS corre auditores/agentes que cada cierto tiempo verifican la disponibilidad
del portal del SAT (probes con login FIEL real desde 3 nodos) y el resultado de los
jobs de descarga de CFDIs. Tras varios meses corriendo —incluyendo **marzo
(declaración anual de morales)** y **abril (personas físicas)**, supuestos picos de
saturación— se revisó qué aprendimos y si conviene relajar la frecuencia del
monitoreo.

**Escala actual:** 5 empresas activas (`sync_activa=True`). Deployment todavía chico,
carga real baja.

**Limitación de datos:** los probes arrancaron el 21-mar, así que no hay baseline
pre-temporada. Se compara cola de morales (mar) vs físicas (abr) vs post-temporada (may).

---

## 2. Hallazgos principales (números reales de la BD)

### 2.1 La saturación estacional NO aparece en los datos

| Mes | Probes | Disponibilidad SAT | Latencia media | Error descargas (DescargaLog) |
|-----|-------:|-------------------:|---------------:|------------------------------:|
| Marzo (morales) | 3,189 | **78.0 %** | 19.3 s | 16 % (72/445) |
| Abril (físicas) | 8,639 | **77.8 %** | 19.8 s | 12 % (14/113) |
| Mayo (post)     | 6,793 | **72.8 %** | 19.3 s | 17 % (2/12) |

El portal se comportó **prácticamente igual** en el "pico" de abril que en marzo, e
incluso un poco peor en mayo (temporada baja). **No hay evidencia de que abril/físicas
sature más.** Monitorear más agresivo durante temporada fiscal no se justifica con
estos datos.

### 2.2 El patrón dominante es CIRCADIANO, no estacional

Disponibilidad del SAT por hora del día (UTC), todo el periodo:

| Hora UTC | Hora CST | Disponibilidad | Zona |
|---------:|---------:|---------------:|------|
| 00 | 18 | 82.7 % | buena |
| 01 | 19 | 87.8 % | buena |
| 02 | 20 | **90.9 %** | **óptima** |
| 03 | 21 | 89.0 % | óptima |
| 04 | 22 | 90.2 % | óptima |
| 05 | 23 | 90.0 % | óptima |
| 06 | 00 | 88.9 % | óptima |
| 07 | 01 | 86.9 % | buena |
| 08 | 02 | 74.3 % | degradando |
| 09 | 03 | 63.1 % | mala |
| 10 | 04 | **51.9 %** | **peor** |
| 11 | 05 | 54.1 % | mala |
| 12 | 06 | 54.7 % | mala |
| 13 | 07 | 62.4 % | mala |
| 14 | 08 | 69.2 % | media |
| 15 | 09 | 69.4 % | media |
| 16 | 10 | 65.4 % | media |
| 17 | 11 | 69.4 % | media |
| 18 | 12 | 76.1 % | buena |
| 19 | 13 | 75.9 % | buena |
| 20 | 14 | 80.6 % | buena |
| 21 | 15 | 84.2 % | buena |
| 22 | 16 | 82.6 % | buena |
| 23 | 17 | 84.7 % | buena |

**Swing de ~40 puntos** entre la mejor hora (02 UTC, 90.9 %) y la peor (10 UTC,
51.9 %). La zona 09–13 UTC (03–07 CST) es la ventana de mantenimiento/degradación del
SAT. Este patrón es estable y predecible → es la palanca de optimización más
importante, muy por encima de la frecuencia de probes.

### 2.3 `login_failed` creciente — anomalía a investigar

Evolución del resultado `login_failed` en los probes:

| Mes | login_failed |
|-----|-------------:|
| Marzo | **0 %** |
| Abril | **2 %** |
| Mayo  | **9 %** |

Repartido de forma pareja entre los 4 RFCs de prueba (216 / 210 / 197 / 195) → **no es
una FIEL venciéndose**, es sistémico. Hipótesis: cambio en el flujo de login del SAT, o
soft-throttling del portal por pegarle cada 5 min con los mismos RFCs 24/7. El resto de
fallas (`page_error` ~16–20 %) es uniforme entre los 3 nodos (74–77 % de éxito cada
uno) → es comportamiento del SAT, no infra de CIRRUS.

---

## 3. Decisiones tomadas y justificación

### 3.1 Telegram: solo alertas de stack
El admin solo recibe alertas de **stack**, no de eventos de clientes. Implementado con
un gate central en `core/services/alerts.py` (`STACK_CATEGORIES`) controlado por
`SystemSettings.telegram_solo_stack` (default `True`, toggle en `/panel/telegram/`).

- **Sí notifica:** SAT >24h con éxito <60 % (ventana móvil), 3+ descargas fallidas en
  24h, 3+ incidentes en 24h, worker Celery caído.
- **Ya NO notifica:** éxito/fallo de descargas individuales, FIEL de clientes, probes
  individuales, reportes informativos, Stripe, leads.
- *Justificación:* el ruido por evento no es accionable; el dueño quiere señal de
  stack, no operación del cliente.

### 3.2 FIEL por vencer → proceso al cliente
Nueva task `verificar_fiel_por_vencer` (diaria 09:00 CST / 15:00 UTC): email al **dueño
de la empresa** (no al admin) a 30/15/7 días, con instrucciones de renovación; registra
en `SystemLog`. También absorbe la lógica de FIEL vencida (marca `expirada` + desactiva
`sync_activa`) y avisos de CSD. Se **desactivó** la task antigua
`alertas-fiel-vencimiento` para evitar emails duplicados y alertas de FIEL al admin.

### 3.3 Incidentes operativos
Nuevo modelo `DescargaIncidente` (tipos: timeout / sat_error / fiel_error /
gap_detectado / otro). La task `vigilancia_stack` (horaria) crea un incidente cuando un
`DescargaJob` lleva **>48h sin completar**, y alerta al admin si hay 3+ incidentes
nuevos en 24h. Visibles en `/panel/monitor/` → sección **Incidentes** y en el admin de
Django (con acción "marcar resuelto").

### 3.4 Relajar frecuencias (django_celery_beat)

| Task | Antes | Ahora | Motivo |
|------|------:|------:|--------|
| `sat-health-probe` | 5 min | **30 min** | señal estable/predecible; 5 min era redundante. 288 → 48 logins/día (−83 %), baja el riesgo de auto-provocar `login_failed`. |
| `supervisor-pipelines` | 5 min | **15 min** | con ~12 descargas/mes, 5 min no aporta. |
| Ventana de descargas (`_calcular_programacion`) | 04–10 UTC | **00–07 UTC** | mueve las descargas a la zona de 82–91 % de disponibilidad y saca la cola fuera de 08–10 UTC (que ya cae a 52–74 %). |
| `verificar-fiel-por-vencer` | — | **diaria 15:00 UTC** | nueva. |
| `vigilancia-stack` | — | **cada hora** | nueva. |

---

## 4. Próximos pasos pendientes

1. **Investigar `login_failed` creciente (0 %→2 %→9 %)** — es la única señal que huele a
   problema real. Revisar: (a) ¿cambió el flujo de login del SAT?, (b) ¿el portal está
   soft-bloqueando los RFCs de prueba por frecuencia? La baja a 30 min de los probes
   debería ayudar a descartar (b) — re-medir en ~2 semanas.
2. **Re-medir disponibilidad tras mover la ventana a 00–07 UTC** — confirmar que baja la
   tasa de error de descargas (esperado: de ~15 % hacia <10 %).
3. **Validar que las nuevas alertas Telegram disparan** (ver sección de verificación en
   el resumen de cambios).
4. Considerar un campo `failed_at`/timestamp explícito en `DescargaJob` para métricas de
   falla más precisas (hoy se usa `DescargaLog.completado_at`).

---

## 5. Recomendaciones para futuras temporadas fiscales

- **No subir la frecuencia de probes en temporada** salvo que los datos cambien: la
  saturación esperada por morales/físicas no se materializó en 2026.
- **Programar las descargas pesadas en 00–07 UTC** (18–01 CST). Evitar 09–13 UTC
  (03–07 CST) por completo: es la ventana de mantenimiento del SAT.
- **Mantener el monitoreo barato y con alertas accionables** (solo stack). El valor está
  en detectar caídas sostenidas y patrones de falla, no en muestrear más seguido una
  señal estable.
- **Recoger baseline pre-temporada** el próximo ciclo: empezar a guardar probes desde
  enero para tener comparación real morales (mar) vs base (ene–feb).
- **Vigilar `login_failed`** como indicador temprano de cambios en el portal del SAT.
