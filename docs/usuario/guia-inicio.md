# Guía de Inicio — Cirrus

## Qué es Cirrus

Cirrus es una plataforma que descarga automáticamente tus facturas electrónicas (CFDIs) del portal del SAT. En lugar de entrar manualmente al portal cada mes, Cirrus se conecta con tu e.firma (FIEL), descarga todos tus comprobantes, y los organiza para que puedas consultarlos, exportarlos y analizarlos desde un solo lugar.

---

## 1. Registro

1. Entra a **cirrus.nubex.me** y haz clic en "Registrarse"
2. Ingresa tu correo electrónico y elige una contraseña (mínimo 6 caracteres)
3. Recibirás un correo de confirmación — haz clic en el enlace para activar tu cuenta
   - El enlace es válido por **48 horas**
   - Si no lo ves, revisa tu carpeta de spam
4. Una vez confirmado, ya puedes iniciar sesión

---

## 2. Dar de Alta tu Empresa

### Qué necesitas

Para que Cirrus pueda descargar tus CFDIs del SAT, necesitas tu **e.firma (FIEL)**. Son 3 archivos:

| Archivo | Extensión | Dónde lo obtienes |
|---------|-----------|-------------------|
| Certificado | `.cer` | sat.gob.mx o tu trámite presencial de FIEL |
| Llave privada | `.key` | Junto con el .cer |
| Contraseña | (texto) | La que elegiste al crear tu FIEL |

### Pasos

1. En el menú lateral, haz clic en **"Empresas"** → **"Nueva Empresa"**
2. Ingresa el **RFC** de tu empresa
3. Sube los 3 archivos de tu FIEL:
   - Selecciona tu archivo `.cer`
   - Selecciona tu archivo `.key`
   - Escribe la contraseña de tu FIEL
4. Haz clic en **"Verificar FIEL"**

### Seguridad de tu FIEL

- Tu contraseña se **encripta inmediatamente** al subirla (nunca se almacena en texto plano)
- Los archivos .cer y .key se guardan en servidores seguros y aislados (MinIO)
- Cirrus **nunca** tiene acceso a tu contraseña en texto claro después del momento de encriptación
- La conexión es HTTPS y las cookies son seguras

---

## 3. Qué Pasa Después de Subir tu FIEL

El sistema ejecuta un proceso automático de 6 pasos. Puedes ver el progreso en tiempo real en la página de tu empresa:

| Paso | Qué hace | Duración típica |
|------|----------|----------------|
| 1 | **Validando FIEL** — Verifica que los archivos y contraseña son correctos | 2-5 segundos |
| 2 | **Verificando con el SAT** — Se conecta al portal del SAT para confirmar que tu FIEL funciona | 30-90 segundos |
| 3 | **Descargando Constancia Fiscal** — Obtiene tu CSF (Constancia de Situación Fiscal) del SAT | 15-30 segundos |
| 4 | **Analizando CSF** — Extrae automáticamente tus datos oficiales (razón social, dirección, régimen fiscal) | 5-15 segundos |
| 5 | **Registrando datos** — Guarda la información oficial de tu empresa | 1-2 segundos |
| 6 | **Programando descargas** — Crea la cola de descargas para todos tus meses pendientes | 1-2 segundos |

Si algún paso falla, el sistema reintenta automáticamente. Recibirás un correo cuando el proceso termine.

### Si tu FIEL es rechazada

Posibles causas:
- Contraseña incorrecta
- Archivos .cer y .key no corresponden al mismo certificado
- FIEL revocada o expirada
- Archivos corruptos

Solución: Verifica tus archivos e intenta de nuevo. Si persiste, puedes renovar tu FIEL en sat.gob.mx.

---

## 4. Descargas Automáticas

Una vez que tu FIEL está verificada, Cirrus programa descargas automáticas de tus CFDIs.

### Frecuencia según tu plan

| Plan | Cuándo descarga | Horario |
|------|----------------|---------|
| Free | Primera semana del mes | Madrugada (10 PM - 4 AM) |
| Basico | Dos veces al mes (semana 2 y semana 3) | Madrugada |
| Pro | Semanalmente | Madrugada |
| Enterprise | Cada 3 días | Madrugada |

**Importante:** Las descargas se ejecutan en horario nocturno (madrugada México) porque el portal del SAT es más rápido en esas horas.

### Qué se descarga

- **CFDIs Recibidos:** Facturas que te hicieron (tus proveedores te facturaron)
- **CFDIs Emitidos:** Facturas que tú emitiste (tú facturaste a tus clientes)

El sistema descarga ambos tipos para cada mes, desde el inicio de tu sincronización hasta el mes actual.

### Primera descarga

Cuando das de alta tu empresa por primera vez, el sistema programa la descarga de todo el historial disponible según tu plan. El proceso comienza por los **meses más recientes** para que veas datos útiles lo antes posible.

---

## 5. Colaboradores

Puedes invitar a tu contador, equipo administrativo o cualquier persona que necesite acceso a los CFDIs de tu empresa.

### Cómo invitar

1. Ve a **"Colaboradores"** en el menú lateral
2. Ingresa el correo electrónico de la persona
3. Haz clic en **"Invitar"**
4. La persona recibirá un correo para crear su cuenta (si no tiene una) o se vinculará automáticamente

### Permisos disponibles

| Permiso | Qué puede hacer |
|---------|----------------|
| Ver CFDIs | Consultar facturas en el panel |
| Ver análisis | Acceder a reportes y estadísticas |
| Exportar | Descargar CFDIs como PDF o Excel |
| Subir FIEL | Subir o editar la FIEL de la empresa |
| Subir XMLs | Cargar XMLs manualmente |
| Crear empresa | Dar de alta nuevas empresas |
| Disparar descargas | Forzar descarga manual del SAT |
| Ver CSF | Consultar la Constancia de Situación Fiscal |

Los permisos se pueden personalizar por empresa — un colaborador puede tener acceso total a una empresa pero solo lectura en otra.

---

## 6. Límites por Plan

| Característica | Free | Basico | Pro | Enterprise |
|---------------|------|--------|-----|------------|
| Empresas (RFCs) | 1 | Según plan | Según plan | Según plan |
| Descargas manuales/mes | 1 | Según plan | Según plan | Ilimitadas |
| CFDIs visibles | 50 | Según plan | Según plan | Ilimitados |
| Conversiones PDF/mes | 10 | Según plan | Según plan | Según plan |
| Conversiones Excel/mes | 3 | Según plan | Según plan | Según plan |
| API REST | No | No | Solo lectura | Completa |
| Colaboradores | 0 | Según plan | Según plan | Según plan |
| Logo en PDFs | No | No | Sí | Sí |

Los límites exactos de tu plan aparecen en la sección de configuración de tu cuenta.

---

## Preguntas Frecuentes

Consulta nuestra [guía de preguntas frecuentes](preguntas-frecuentes.md) para dudas comunes sobre FIEL, descargas, CFDIs y más.

## Alertas

Consulta la [guía de alertas](alertas-y-notificaciones.md) para entender las notificaciones que recibes.
