"""Verificador de estado de CFDIs contra ConsultaCFDIService (SAT público).

Es el web service que valida los QR de las facturas: dado (RFC emisor,
RFC receptor, total, UUID) responde el estatus actual — Vigente/Cancelado,
EsCancelable, EstatusCancelacion y ValidacionEFOS del emisor. Público, sin
FIEL, sin CAPTCHA. NO descarga XMLs ni descubre CFDIs desconocidos: solo
re-verifica lo que ya tenemos (la completitud la cubre la compulsa vía WS
de Descarga Masiva, ver compulsa_sat.py).

Diseño defensivo:
- Timeout corto y pausa entre consultas (no abusar del servicio público).
- Una consulta fallida no tumba la corrida; >50% de fallas la aborta
  (SAT caído — reintentar otro día, no martillar).
- Solo se marca `cancelado` con respuesta explícita "Cancelado". Un
  "No Encontrado" NO cambia nada (puede ser CFDI recién timbrado que el
  índice del SAT aún no refleja).
- `estado_verificado_at` solo se actualiza en consultas exitosas, para que
  la rotación no se salte CFDIs que fallaron.
"""

import logging
import re
import time

import requests

logger = logging.getLogger("core.verificador_sat")

CONSULTA_URL = "https://consultaqr.facturaelectronica.sat.gob.mx/ConsultaCFDIService.svc"
SOAP_ACTION = "http://tempuri.org/IConsultaCFDIService/Consulta"
TIMEOUT_S = 15
PAUSA_ENTRE_CONSULTAS_S = 2.0  # ~30 req/min

_SOAP_BODY = """<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" xmlns:tem="http://tempuri.org/">
  <soapenv:Header/>
  <soapenv:Body>
    <tem:Consulta>
      <tem:expresionImpresa><![CDATA[?re={re}&rr={rr}&tt={tt}&id={id}]]></tem:expresionImpresa>
    </tem:Consulta>
  </soapenv:Body>
</soapenv:Envelope>"""

_RE_CAMPO = {
    "codigo": re.compile(r"<a:CodigoEstatus>([^<]*)</a:CodigoEstatus>"),
    "estado": re.compile(r"<a:Estado>([^<]*)</a:Estado>"),
    "es_cancelable": re.compile(r"<a:EsCancelable>([^<]*)</a:EsCancelable>"),
    "estatus_cancelacion": re.compile(r"<a:EstatusCancelacion>([^<]*)</a:EstatusCancelacion>"),
    "efos": re.compile(r"<a:ValidacionEFOS>([^<]*)</a:ValidacionEFOS>"),
}


def consultar_estado_cfdi(rfc_emisor: str, rfc_receptor: str, total, uuid: str) -> dict:
    """Consulta el estatus de UN CFDI. Devuelve dict con estado/efos o lanza.

    Returns:
        {"ok": True, "codigo": "S - ...", "estado": "Vigente"|"Cancelado"|"No Encontrado",
         "es_cancelable": ..., "estatus_cancelacion": ..., "efos": "200"|"100"|""}
    """
    body = _SOAP_BODY.format(
        re=rfc_emisor.strip().upper(),
        rr=rfc_receptor.strip().upper(),
        tt=f"{total:.2f}",
        id=str(uuid).upper(),
    )
    resp = requests.post(
        CONSULTA_URL,
        data=body.encode("utf-8"),
        headers={
            "Content-Type": "text/xml; charset=utf-8",
            "SOAPAction": SOAP_ACTION,
        },
        timeout=TIMEOUT_S,
    )
    resp.raise_for_status()
    texto = resp.text
    out = {"ok": True}
    for campo, rx in _RE_CAMPO.items():
        m = rx.search(texto)
        out[campo] = m.group(1).strip() if m else ""
    if not out.get("estado"):
        raise ValueError(f"Respuesta SAT sin campo Estado: {texto[:200]}")
    return out


def verificar_lote(cfdis, on_cancelado=None, on_efos=None, pausa=PAUSA_ENTRE_CONSULTAS_S) -> dict:
    """Verifica una lista de CFDIs (modelos) contra el SAT y actualiza BD.

    Args:
        cfdis: iterable de core.models.CFDI (estado_sat='vigente')
        on_cancelado: callback(cfdi, resultado) al detectar cancelación
        on_efos: callback(cfdi, resultado) si ValidacionEFOS != '200'

    Returns: resumen {verificados, cancelados_detectados, no_encontrados,
                      efos_alertas, errores, abortado}
    """
    from django.utils import timezone

    resumen = {
        "verificados": 0, "cancelados_detectados": 0, "no_encontrados": 0,
        "efos_alertas": 0, "errores": 0, "abortado": False,
    }
    procesados = 0

    for cfdi in cfdis:
        procesados += 1
        try:
            r = consultar_estado_cfdi(
                cfdi.rfc_emisor, cfdi.rfc_receptor, cfdi.total, cfdi.uuid,
            )
        except Exception as e:
            resumen["errores"] += 1
            logger.warning("Consulta estado %s fallo: %s", str(cfdi.uuid)[:8], e)
            # Circuit breaker: si el SAT esta caido, no martillar
            if procesados >= 10 and resumen["errores"] / procesados > 0.5:
                resumen["abortado"] = True
                logger.error(
                    "verificar_lote ABORTADO: %d/%d errores — SAT posiblemente caido",
                    resumen["errores"], procesados,
                )
                break
            time.sleep(pausa)
            continue

        estado = r["estado"]
        if estado == "Cancelado":
            cfdi.estado_sat = "cancelado"
            cfdi.cancelado_at = timezone.now()
            cfdi.estado_verificado_at = timezone.now()
            cfdi.save(update_fields=["estado_sat", "cancelado_at", "estado_verificado_at"])
            resumen["cancelados_detectados"] += 1
            logger.warning(
                "🔴 CFDI CANCELADO detectado: %s %s→%s $%s (%s)",
                str(cfdi.uuid)[:13], cfdi.rfc_emisor, cfdi.rfc_receptor,
                cfdi.total, r.get("estatus_cancelacion"),
            )
            if on_cancelado:
                try:
                    on_cancelado(cfdi, r)
                except Exception:
                    pass
        elif estado == "Vigente":
            cfdi.estado_verificado_at = timezone.now()
            cfdi.save(update_fields=["estado_verificado_at"])
            resumen["verificados"] += 1
        else:
            # "No Encontrado" u otro: NO tocar estado_sat. Sí marcamos
            # verificado para no atorar la rotación en el mismo CFDI.
            cfdi.estado_verificado_at = timezone.now()
            cfdi.save(update_fields=["estado_verificado_at"])
            resumen["no_encontrados"] += 1
            logger.info("CFDI %s: SAT respondio %r", str(cfdi.uuid)[:8], estado)

        # EFOS del emisor: '200' = limpio; otro codigo = en lista 69-B
        efos = r.get("efos", "")
        if efos and efos != "200":
            resumen["efos_alertas"] += 1
            logger.warning(
                "⚠️ EFOS: emisor %s de CFDI %s con ValidacionEFOS=%s",
                cfdi.rfc_emisor, str(cfdi.uuid)[:8], efos,
            )
            if on_efos:
                try:
                    on_efos(cfdi, r)
                except Exception:
                    pass

        time.sleep(pausa)

    return resumen
