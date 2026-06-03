# Alertas y Notificaciones

## 1. Canales de Notificación

| Canal | Quién lo recibe | Tipo de alertas |
|-------|----------------|-----------------|
| **Email** | Todos los usuarios | Confirmaciones, reportes de descarga, alertas de FIEL |
| **Telegram** | Solo administradores del sistema | Alertas operativas en tiempo real |

---

## 2. Alertas por Email

### Registro y acceso

| Evento | Asunto del correo | Cuándo se envía |
|--------|-------------------|----------------|
| Nuevo registro | "Confirma tu cuenta en Cirrus" | Inmediatamente al registrarte |
| Reenvío de confirmación | "Confirma tu cuenta en Cirrus" | Al solicitar reenvío |
| Recuperación de contraseña | "Recupera tu contraseña" | Al solicitar reset (enlace válido 1 hora) |

### FIEL y empresa

| Evento | Asunto | Cuándo se envía |
|--------|--------|----------------|
| FIEL verificada | "Tu FIEL fue verificada" | Al completar el proceso de verificación |
| FIEL rechazada | "Problema con tu FIEL" | Si la verificación falla |
| Empresa registrada | "Tu empresa [RFC] está lista" | Al completar el pipeline de alta |
| FIEL por vencer | "Tu FIEL vence en X días" | A 90, 60, 30, 15, 7, 3 y 1 día(s) de la expiración |
| CSD por vencer | "Tu Sello Digital vence en X días" | Mismos intervalos |
| FIEL expirada | "Tu FIEL ha expirado" | El día de la expiración (desactiva sync automáticamente) |

### Descargas

| Evento | Asunto | Cuándo se envía |
|--------|--------|----------------|
| Descarga exitosa | Reporte ejecutivo con resumen | Al completar una descarga |
| Descarga falló definitivamente | "Actualización sobre tu descarga" | Solo después de agotar todos los reintentos (10) |

---

## 3. Significado de Estados en el Panel

### Estados de Pipeline (al dar de alta una empresa)

Cuando das de alta tu empresa o actualizas tu FIEL, verás un indicador de progreso con estos pasos:

| Paso | Nombre | Qué significa |
|------|--------|--------------|
| 1/6 | Validando FIEL | Verificando que tus archivos y contraseña son correctos |
| 2/6 | Verificando con SAT | Probando tu FIEL contra el portal del SAT |
| 3/6 | Descargando CSF | Obteniendo tu Constancia de Situación Fiscal |
| 4/6 | Analizando CSF | Extrayendo datos oficiales del PDF |
| 5/6 | Registrando datos | Guardando la información de tu empresa |
| 6/6 | Programando descargas | Creando la cola de descargas |

**Estados del pipeline:**
| Estado | Color | Significado |
|--------|-------|-------------|
| En proceso | Azul | Ejecutándose normalmente |
| Esperando SAT | Amarillo | El SAT no está disponible — se reintentará cuando se recupere |
| Reintentando | Naranja | Hubo un error, reintentando automáticamente |
| Completado | Verde | Todo listo |
| Error | Rojo | Falló y no se pudo recuperar — revisa tu FIEL |

### Estados de Descarga (en la lista de descargas)

| Estado | Significado | Acción requerida |
|--------|-------------|-----------------|
| **En cola** | Programada, esperando turno | Ninguna — se ejecutará automáticamente |
| **Ejecutando** | Descargando del SAT en este momento | Esperar (puede tomar 1-10 minutos) |
| **Completado** | Descarga exitosa | Tus CFDIs están disponibles |
| **Completado sin CFDIs** | No se encontraron facturas para ese periodo | Ninguna — es normal si no hubo actividad |
| **Error** | Falló después de varios reintentos | Si tu FIEL está vigente, se reintentará automáticamente en la auditoría nocturna |

### Estados de FIEL

| Estado | Significado | Acción requerida |
|--------|-------------|-----------------|
| **Sin FIEL** | No has subido tu e.firma | Sube tu FIEL para activar descargas |
| **Verificando** | Se está probando tu FIEL | Espera unos minutos |
| **Verificada** | Tu FIEL funciona correctamente | Ninguna — las descargas están activas |
| **Rechazada** | La verificación falló | Revisa tus archivos y contraseña, intenta de nuevo |
| **Expirada** | Tu certificado venció | Renueva tu FIEL en sat.gob.mx |

---

## 4. Qué Hacer si Recibes una Alerta

### "Tu FIEL vence en X días"

**Urgencia:** Alta si quedan menos de 30 días

1. Ve a [sat.gob.mx](https://sat.gob.mx) → Trámites → e.firma
2. Renueva tu FIEL (necesitas presentar documentación si es presencial)
3. Descarga los nuevos archivos .cer y .key
4. En Cirrus, ve a tu empresa → "Actualizar FIEL"
5. Sube los nuevos archivos

Si tu FIEL expira antes de renovarla:
- Las descargas automáticas se pausan
- Los CFDIs ya descargados siguen disponibles
- Al subir la nueva FIEL, las descargas se reactivan automáticamente

### "Tu FIEL fue rechazada"

**Urgencia:** Media

1. Verifica que tu contraseña sea correcta (mayúsculas/minúsculas importan)
2. Verifica que los archivos .cer y .key correspondan al mismo certificado
3. Si no estás seguro, descarga tus archivos nuevamente desde el SAT
4. Intenta subir la FIEL de nuevo

### "Descarga falló"

**Urgencia:** Baja (el sistema reintenta automáticamente)

No necesitas hacer nada inmediatamente. El sistema:
1. Reintenta automáticamente con delays crecientes (5 min → 2 horas)
2. Si falla repetidamente, la auditoría nocturna lo reintenta
3. Si el problema es del SAT, se resolverá cuando el SAT se recupere

Si después de 24-48 horas sigue fallando:
- Verifica que tu FIEL esté vigente
- Revisa si hay algún aviso del SAT sobre mantenimiento

### "SAT no disponible" (en el dashboard)

**Urgencia:** Ninguna

Esto es informativo. El portal del SAT tiene intermitencias regulares. Cirrus monitorea la disponibilidad 24/7 y reintenta automáticamente. No necesitas hacer nada.

---

## 5. Monitor de Salud del SAT

En el panel de administración, el monitor de salud del SAT muestra:

| Indicador | Significado |
|-----------|-------------|
| **Verde (>70%)** | SAT funcionando correctamente |
| **Amarillo (30-70%)** | SAT con intermitencias — las descargas pueden ser lentas |
| **Rojo (<30%)** | SAT con problemas graves — las descargas se pausan automáticamente |
| **Gris** | Sin datos recientes |

El sistema prueba la conexión al SAT cada 5 minutos desde 3 servidores diferentes para tener datos precisos.

---

## Documentos Relacionados

- [Guía de Inicio](guia-inicio.md) — Cómo configurar tu cuenta
- [Preguntas Frecuentes](preguntas-frecuentes.md) — Dudas comunes
