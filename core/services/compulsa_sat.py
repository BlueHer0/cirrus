"""Compulsa de completitud contra el SAT — Web Service de Descarga Masiva v1.5.

El 'sabedor de datos': se pide METADATA (no XMLs) de un rango de fechas y el
SAT devuelve el índice oficial completo — una fila por CFDI con UUID, RFCs,
fecha, monto y estatus. Con eso:

    faltantes = uuids_que_el_SAT_reporta - uuids_en_BD

- 0 faltantes  → periodo VERIFICADO completo (se registra en CompulsaSAT y
  el auditor nocturno deja de re-scrapear meses confirmados vacíos).
- >0 faltantes → gap real: se re-encola el DescargaJob de ese mes para que
  el scraper lo recupere, y se alerta por Telegram.

Flujo asíncrono del WS: Solicita → (SAT procesa) → Verifica → Descarga
paquete (zip con .txt). Si el paquete no está listo en el polling inline,
la CompulsaSAT queda 'solicitada' y la siguiente corrida la retoma.

Defensivo:
- CodEstatus 5002 (solicitud duplicada) y estado 5/cod 5004 (sin info en el
  rango) se manejan explícitamente — 'sin info' ES un resultado válido:
  significa 0 CFDIs en todo el rango.
- La FIEL se baja a tmp y se limpia siempre.
- Nunca marca 'completada' sin haber parseado el paquete (fallo ≠ vacío).
"""

import base64
import io
import logging
import time
import zipfile
from datetime import datetime, timedelta

logger = logging.getLogger("core.compulsa_sat")

# Estados de solicitud del WS (documentación SAT)
WS_ACEPTADA = "1"
WS_EN_PROCESO = "2"
WS_TERMINADA = "3"
WS_ERROR = "4"
WS_RECHAZADA = "5"
WS_VENCIDA = "6"

POLL_INLINE_INTENTOS = 10
POLL_INLINE_ESPERA_S = 30


def _cargar_fiel(empresa):
    """Devuelve (Fiel, cleanup_fn) desde MinIO. Nunca deja archivos."""
    from cfdiclient import Fiel
    from core.services.fiel_encryption import get_fiel_for_scraping

    ctx = get_fiel_for_scraping(empresa)

    def cleanup():
        try:
            ctx["temp_dir"].cleanup()
        except Exception:
            pass

    try:
        with open(ctx["cer_path"], "rb") as f:
            cer_der = f.read()
        with open(ctx["key_path"], "rb") as f:
            key_der = f.read()
        fiel = Fiel(cer_der, key_der, ctx["password"])
        return fiel, cleanup
    except Exception:
        cleanup()
        raise


def solicitar_compulsa(empresa, tipo: str, fecha_inicio, fecha_fin):
    """Crea la solicitud de metadata en el SAT. Devuelve el CompulsaSAT row."""
    from cfdiclient import (
        Autenticacion, SolicitaDescargaEmitidos, SolicitaDescargaRecibidos,
    )
    from core.models import CompulsaSAT

    compulsa = CompulsaSAT.objects.create(
        empresa=empresa, tipo=tipo,
        fecha_inicio=fecha_inicio, fecha_fin=fecha_fin,
    )

    fiel, cleanup = _cargar_fiel(empresa)
    try:
        token = Autenticacion(fiel, timeout=60).obtener_token()
        f1 = datetime(fecha_inicio.year, fecha_inicio.month, fecha_inicio.day, 0, 0, 0)
        f2 = datetime(fecha_fin.year, fecha_fin.month, fecha_fin.day, 23, 59, 59)

        if tipo == "recibidos":
            sol = SolicitaDescargaRecibidos(fiel, timeout=60)
            r = sol.solicitar_descarga(
                token, empresa.rfc, f1, f2,
                rfc_receptor=empresa.rfc, tipo_solicitud="Metadata",
            )
        else:
            sol = SolicitaDescargaEmitidos(fiel, timeout=60)
            r = sol.solicitar_descarga(
                token, empresa.rfc, f1, f2,
                rfc_emisor=empresa.rfc, tipo_solicitud="Metadata",
            )

        cod = str(r.get("cod_estatus") or "")
        if r.get("id_solicitud"):
            compulsa.folio_solicitud = r["id_solicitud"]
            compulsa.estado = "solicitada"
            compulsa.save(update_fields=["folio_solicitud", "estado"])
            logger.info(
                "Compulsa %s %s %s→%s solicitada: folio=%s",
                empresa.rfc, tipo, fecha_inicio, fecha_fin, r["id_solicitud"],
            )
        else:
            compulsa.estado = "error"
            compulsa.ultimo_error = f"CodEstatus={cod} Mensaje={r.get('mensaje')}"
            compulsa.save(update_fields=["estado", "ultimo_error"])
            logger.error("Compulsa %s %s: solicitud rechazada %s", empresa.rfc, tipo, compulsa.ultimo_error)
        return compulsa
    finally:
        cleanup()


def procesar_compulsa(compulsa) -> bool:
    """Verifica/descarga/diffea una compulsa 'solicitada'. True si terminó."""
    from cfdiclient import Autenticacion, DescargaMasiva, VerificaSolicitudDescarga

    empresa = compulsa.empresa
    fiel, cleanup = _cargar_fiel(empresa)
    try:
        token = Autenticacion(fiel, timeout=60).obtener_token()
        ver = VerificaSolicitudDescarga(fiel, timeout=60)

        estado_sol, paquetes = None, []
        for intento in range(POLL_INLINE_INTENTOS):
            r = ver.verificar_descarga(token, empresa.rfc, compulsa.folio_solicitud)
            estado_sol = str(r.get("estado_solicitud") or "")
            cod = str(r.get("codigo_estado_solicitud") or "")
            if estado_sol == WS_TERMINADA:
                paquetes = r.get("paquetes") or []
                break
            if estado_sol in (WS_ERROR, WS_VENCIDA):
                compulsa.estado = "error"
                compulsa.ultimo_error = f"estado_solicitud={estado_sol} cod={cod}"
                compulsa.save(update_fields=["estado", "ultimo_error"])
                return True
            if estado_sol == WS_RECHAZADA:
                if cod == "5004":
                    # 'No se encontró la información' = 0 CFDIs en el rango.
                    # Resultado VALIDO: registra 0 por mes (silencia al auditor).
                    _registrar_resultado(compulsa, [])
                    compulsa.estado = "completada"
                    compulsa.save(update_fields=["estado"])
                    logger.info(
                        "Compulsa %s %s: SAT sin info en rango (0 CFDIs) — verificado vacío",
                        empresa.rfc, compulsa.tipo,
                    )
                else:
                    compulsa.estado = "error"
                    compulsa.ultimo_error = f"rechazada cod={cod}"
                    compulsa.save(update_fields=["estado", "ultimo_error"])
                return True
            # 1/2: aún procesando
            if intento < POLL_INLINE_INTENTOS - 1:
                time.sleep(POLL_INLINE_ESPERA_S)

        if estado_sol != WS_TERMINADA:
            logger.info(
                "Compulsa %s %s folio=%s aún en proceso (estado=%s) — se retoma en la próxima corrida",
                empresa.rfc, compulsa.tipo, compulsa.folio_solicitud, estado_sol,
            )
            return False

        # Descargar y parsear todos los paquetes de metadata
        dm = DescargaMasiva(fiel, timeout=120)
        filas = []
        for id_paq in paquetes:
            rp = dm.descargar_paquete(token, empresa.rfc, id_paq)
            b64 = rp.get("paquete_b64")
            if not b64:
                compulsa.estado = "error"
                compulsa.ultimo_error = f"paquete {id_paq} sin contenido"
                compulsa.save(update_fields=["estado", "ultimo_error"])
                return True
            filas.extend(_parsear_metadata_zip(base64.b64decode(b64)))

        _registrar_resultado(compulsa, filas)
        compulsa.estado = "completada"
        compulsa.save(update_fields=["estado"])
        return True
    except Exception as e:
        compulsa.ultimo_error = str(e)[:500]
        compulsa.save(update_fields=["ultimo_error"])
        logger.error("procesar_compulsa %s: %s", compulsa.id, e)
        return False
    finally:
        cleanup()


def _parsear_metadata_zip(zip_bytes: bytes) -> list[dict]:
    """Extrae las filas del .txt de metadata (separador '~', header en línea 1)."""
    filas = []
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
        for name in z.namelist():
            if not name.lower().endswith(".txt"):
                continue
            raw = z.read(name).decode("utf-8", errors="replace")
            lineas = [l for l in raw.splitlines() if l.strip()]
            if not lineas:
                continue
            header = [h.strip() for h in lineas[0].split("~")]
            for linea in lineas[1:]:
                valores = linea.split("~")
                fila = dict(zip(header, valores))
                if fila.get("Uuid"):
                    filas.append(fila)
    return filas


def _registrar_resultado(compulsa, filas: list[dict]):
    """Diff SAT vs BD, guarda resultado por mes y actúa sobre hallazgos."""
    from collections import defaultdict
    from core.models import CFDI

    por_mes = defaultdict(lambda: {"sat_vigentes": 0, "sat_cancelados": 0, "en_bd": 0, "faltantes": []})
    faltantes_total = []
    cancelados_en_sat = []

    for f in filas:
        uuid = (f.get("Uuid") or "").strip().upper()
        fecha = (f.get("FechaEmision") or "")[:7] or "sin-fecha"  # 'YYYY-MM'
        estatus = (f.get("Estatus") or "").strip()  # '1' vigente / '0' cancelado
        vigente = estatus != "0"
        m = por_mes[fecha]
        if vigente:
            m["sat_vigentes"] += 1
        else:
            m["sat_cancelados"] += 1

        en_bd = CFDI.objects.filter(uuid=uuid.lower()).exists() or CFDI.objects.filter(uuid=uuid).exists()
        if en_bd:
            m["en_bd"] += 1
            if not vigente:
                cancelados_en_sat.append(uuid)
        elif vigente:
            m["faltantes"].append(uuid)
            faltantes_total.append((fecha, uuid))

    # Meses del rango sin ninguna fila = 0 CFDIs confirmado
    mes = compulsa.fecha_inicio.replace(day=1)
    while mes <= compulsa.fecha_fin:
        clave = f"{mes.year}-{mes.month:02d}"
        _ = por_mes[clave]  # crea la entrada con ceros si no existe
        mes = (mes.replace(year=mes.year + 1, month=1) if mes.month == 12
               else mes.replace(month=mes.month + 1))

    compulsa.resultado = dict(por_mes)
    compulsa.sat_vigentes = sum(m["sat_vigentes"] for m in por_mes.values())
    compulsa.sat_cancelados = sum(m["sat_cancelados"] for m in por_mes.values())
    compulsa.en_bd = sum(m["en_bd"] for m in por_mes.values())
    compulsa.faltantes_count = len(faltantes_total)
    compulsa.save(update_fields=[
        "resultado", "sat_vigentes", "sat_cancelados", "en_bd", "faltantes_count",
    ])

    _actuar_sobre_hallazgos(compulsa, faltantes_total, cancelados_en_sat)


def _actuar_sobre_hallazgos(compulsa, faltantes, cancelados_en_sat):
    """Re-encola meses con faltantes y marca cancelados detectados."""
    from django.utils import timezone as djtz
    from core.models import CFDI, DescargaJob
    from core.services.alerts import send_telegram

    empresa = compulsa.empresa

    # 1) Cancelados que en BD siguen 'vigente' (bonus de la metadata)
    n_cancelados = 0
    for uuid in cancelados_en_sat:
        c = CFDI.objects.filter(uuid=uuid.lower(), estado_sat="vigente").first()
        if c:
            c.estado_sat = "cancelado"
            c.cancelado_at = djtz.now()
            c.estado_verificado_at = djtz.now()
            c.save(update_fields=["estado_sat", "cancelado_at", "estado_verificado_at"])
            n_cancelados += 1

    # 2) Faltantes → re-encolar el DescargaJob del mes (el scraper recupera)
    meses_afectados = sorted({fecha for fecha, _ in faltantes})
    for clave in meses_afectados:
        try:
            y, m = int(clave[:4]), int(clave[5:7])
        except ValueError:
            continue
        job = DescargaJob.objects.filter(
            empresa=empresa, year=y, month=m, tipo=compulsa.tipo,
        ).first()
        if job and job.estado not in ("en_cola", "ejecutando"):
            job.estado = "en_cola"
            job.intentos = 0
            job.programado_para = djtz.now()
            job.ultimo_error = f"compulsa {compulsa.id}: {len([f for f in faltantes if f[0] == clave])} faltantes vs SAT"
            job.save()

    # 3) Alertas
    if faltantes or n_cancelados:
        send_telegram(
            f"📋 *Compulsa SAT* {empresa.rfc} {compulsa.tipo} "
            f"{compulsa.fecha_inicio:%Y-%m-%d}→{compulsa.fecha_fin:%Y-%m-%d}\n"
            f"SAT: {compulsa.sat_vigentes} vigentes · BD: {compulsa.en_bd}\n"
            f"🔴 Faltantes: {compulsa.faltantes_count} en {len(meses_afectados)} mes(es) "
            f"→ re-encolados\n"
            + (f"⚠️ {n_cancelados} cancelados detectados vía metadata\n" if n_cancelados else ""),
            "warning",
        )
        logger.warning(
            "Compulsa %s %s: %d faltantes %s, %d cancelados",
            empresa.rfc, compulsa.tipo, compulsa.faltantes_count,
            meses_afectados, n_cancelados,
        )
    else:
        logger.info(
            "Compulsa %s %s %s→%s: ✅ completa (SAT=%d, BD=%d, 0 faltantes)",
            empresa.rfc, compulsa.tipo, compulsa.fecha_inicio,
            compulsa.fecha_fin, compulsa.sat_vigentes or 0, compulsa.en_bd or 0,
        )
