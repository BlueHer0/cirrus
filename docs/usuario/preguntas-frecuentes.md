# Preguntas Frecuentes

---

## Sobre la FIEL (e.firma)

### Es seguro subir mi FIEL a Cirrus?

Sí. Tu contraseña se encripta inmediatamente al momento de subirla usando encriptación de grado bancario (Fernet/AES). Los archivos .cer y .key se almacenan en servidores seguros y aislados. Nadie en Cirrus tiene acceso a tu contraseña en texto plano. La conexión siempre es HTTPS.

### Mi FIEL está por vencer, qué hago?

El sistema te enviará alertas por correo a los 90, 60, 30, 15, 7, 3 y 1 día(s) antes de que expire. Para renovar:

1. Ve a [sat.gob.mx](https://sat.gob.mx) y renueva tu FIEL
2. Descarga los nuevos archivos .cer y .key
3. En Cirrus, ve a tu empresa → "Actualizar FIEL"
4. Sube los nuevos archivos y tu nueva contraseña

### Dice que mi FIEL fue rechazada

Posibles causas:
- **Contraseña incorrecta:** Verifica que estás escribiendo la contraseña correcta (distingue mayúsculas de minúsculas)
- **Archivos no coinciden:** Tu .cer y .key deben ser del mismo certificado
- **FIEL revocada:** Si solicitaste la revocación en el SAT, la FIEL ya no sirve
- **FIEL expirada:** Los certificados FIEL tienen vigencia de 4 años
- **Archivos corruptos:** Intenta descargar los archivos nuevamente desde el SAT

### Qué pasa si mi FIEL expira?

La sincronización automática se desactiva inmediatamente. No se intentarán más descargas hasta que subas una FIEL vigente. Los CFDIs que ya se descargaron siguen disponibles.

### Puedo usar mi CSD en lugar de la FIEL?

No. El CSD (Certificado de Sello Digital) es para timbrar facturas, no para descargar CFDIs del portal del SAT. Necesitas específicamente tu FIEL.

---

## Sobre las Descargas

### Cada cuándo se descargan mis CFDIs?

Depende de tu plan:
- **Free:** Una vez al mes (primera semana)
- **Basico:** Dos veces al mes
- **Pro:** Semanalmente
- **Enterprise:** Cada 3 días

Las descargas siempre se ejecutan en la madrugada (entre 10 PM y 4 AM hora México) porque el portal del SAT es más rápido en esas horas.

### Por qué no veo CFDIs de un mes específico?

Varias razones posibles:
1. **Descarga en cola:** El sistema todavía no ha procesado ese mes — revisa el estado en la sección "Descargas" de tu empresa
2. **Descarga en proceso:** Puede estar ejecutándose en este momento
3. **Sin actividad fiscal:** Si no tuviste facturas ese mes, el estado será "Completado sin CFDIs" — esto es normal
4. **Error temporal:** Si la descarga falló, el sistema reintenta automáticamente hasta 5 veces. Los reintentos se hacen con delays crecientes (5 min, 15 min, 30 min, 1 hora, 2 horas)
5. **Fuera de rango:** Tu plan tiene un límite de antigüedad (ej. Free = 1 año atrás, Basico = 2 años, Pro = 3 años)

### Puedo forzar una descarga manual?

Sí, si tu plan lo permite. Ve a tu empresa → "Descargas" → "Nueva descarga". Selecciona el año, rango de meses, y tipo (recibidos/emitidos). La descarga se ejecuta inmediatamente.

### Qué significa cada estado de descarga?

| Estado | Significado |
|--------|-------------|
| **En cola** | Programada, esperando su turno |
| **Ejecutando** | Conectándose al SAT y descargando en este momento |
| **Completado** | Descarga exitosa — CFDIs disponibles en tu panel |
| **Completado sin CFDIs** | El SAT confirmó que no hay facturas para ese periodo |
| **Error** | Falló después de varios reintentos — el equipo técnico ya fue notificado |

### Mi descarga falló, qué hago?

**No necesitas hacer nada.** El sistema reintenta automáticamente. Si falla repetidamente:
1. Los reintentos usan delays crecientes (5 min → 2 horas)
2. Si agota reintentos, el sistema intenta de nuevo en la auditoría nocturna
3. Si persiste, verifica que tu FIEL esté vigente
4. Si tu FIEL está bien y el problema continúa, es probable que el portal del SAT tenga intermitencias — espera unas horas

### Qué es "Completado sin CFDIs"?

Significa que el sistema intentó descargar CFDIs de ese mes 3 veces y en todas obtuvo 0 resultados. Normalmente indica que no tuviste actividad fiscal ese mes. Si crees que sí debería haber facturas, puedes forzar una descarga manual.

---

## Sobre CFDIs

### Qué son "Recibidos" y "Emitidos"?

- **Recibidos:** Facturas que otras personas o empresas te emitieron a ti (tus compras, servicios que contrataste)
- **Emitidos:** Facturas que tú emitiste a otros (tus ventas, servicios que prestaste)

### Puedo subir XMLs manualmente?

Sí, según los límites de tu plan. Ve a "CFDIs" → "Subir XML". Puedes subir archivos individuales o múltiples XMLs a la vez.

### Qué es la lista EFOS / 69-B?

La lista 69-B del SAT contiene contribuyentes con **Operaciones Simuladas** (empresas fantasma). Cirrus sincroniza esta lista mensualmente y te alerta si alguno de tus emisores o receptores aparece en ella. Los estados posibles son:
- **Presunto:** En investigación
- **Definitivo:** Confirmado como EFOS
- **Desvirtuado:** Demostró que no es EFOS
- **Sentencia favorable:** Ganó recurso legal

### Para qué sirve la CSF (Constancia de Situación Fiscal)?

La CSF es un documento oficial del SAT que contiene los datos registrados de tu empresa: razón social, dirección fiscal, régimen, actividades económicas, etc. Cirrus descarga tu CSF automáticamente y extrae esos datos para llenar tu perfil de empresa sin que tengas que capturarlos manualmente.

---

## Sobre Planes y Pagos

### Cómo cambio de plan?

Desde la sección de configuración de tu cuenta. El cambio se aplica inmediatamente y se prorratea el cobro del mes.

### Qué pasa si excedo mis límites?

Depende del recurso:
- **CFDIs visibles:** Solo ves los más recientes hasta el límite; los demás están descargados pero no se muestran hasta que actualices tu plan
- **Descargas manuales:** Se bloquea la opción de descarga manual
- **Conversiones PDF/Excel:** Se bloquea la conversión hasta el siguiente mes
- **Empresas:** No puedes agregar nuevas hasta que actualices tu plan

---

## Problemas Comunes

### Dice "SAT no disponible"

El portal del SAT tiene intermitencias frecuentes, especialmente en horarios de alta demanda (lunes, fin de mes, declaraciones anuales). El sistema monitorea la disponibilidad del SAT 24/7 desde 3 servidores diferentes y reintenta automáticamente cuando el SAT se recupera. **No necesitas hacer nada** — solo espera.

### No recibí el correo de confirmación

1. Revisa tu carpeta de **spam o correo no deseado**
2. Verifica que escribiste bien tu correo al registrarte
3. Desde la página de login, haz clic en **"Reenviar confirmación"**
4. Si después de 15 minutos no llega, intenta con otro correo electrónico

### Mi empresa aparece sin datos (razón social vacía)

Esto sucede cuando el parser de la Constancia de Situación Fiscal no pudo extraer los datos correctamente. El sistema reintenta el parseo automáticamente. Si persiste, el equipo técnico es notificado para revisar el formato del documento.

### Cómo contacto soporte?

Reporta tu problema en [github.com/anthropics/claude-code/issues](https://github.com/anthropics/claude-code/issues) o contacta a tu administrador de Cirrus.
