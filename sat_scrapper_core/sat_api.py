"""
Motor SOAP para Descarga Masiva del SAT via Web Service.

Portado de ZL Scrapper sat_download.py, sin dependencias Django.
Requiere la dependencia opcional 'satcfdi': pip install sat-scrapper-core[soap]

Flujo:
1. SolicitaDescargaRecibidos/Emitidos → obtiene IdSolicitud
2. VerificaSolicitudDescarga (polling) → espera que el SAT genere el paquete
3. DescargaMasivaTerceros → descarga el ZIP con XMLs
"""

from __future__ import annotations

import base64
import logging
import time
from datetime import date, datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger("sat_scrapper_core")


class SoapApiError(Exception):
    """Error en el motor SOAP de Descarga Masiva."""
    pass


def is_soap_available() -> bool:
    """Verifica si las dependencias del motor SOAP están instaladas."""
    try:
        from satcfdi.models import Signer  # noqa: F401
        from satcfdi.pacs import sat as sat_pacs  # noqa: F401
        from lxml import etree  # noqa: F401
        return True
    except ImportError:
        return False


def _build_signer(cer_path: str, key_path: str, password: str):
    """Construye un Signer de satcfdi desde los archivos FIEL."""
    from satcfdi.models import Signer

    with open(cer_path, "rb") as f:
        cert_data = f.read()
    with open(key_path, "rb") as f:
        key_data = f.read()
    return Signer.load(certificate=cert_data, key=key_data, password=password)


def _create_solicita_recibidos_class():
    """Crea la clase de solicitud para Recibidos con URLs corregidas."""
    from lxml import etree
    from satcfdi.pacs import sat as sat_pacs

    class _SolicitaRecibidos(sat_pacs._SATRequest):
        soap_url = "https://cfdidescargamasivasolicitud.clouda.sat.gob.mx/SolicitaDescargaService.svc"
        soap_action = "http://DescargaMasivaTerceros.sat.gob.mx/ISolicitaDescargaService/SolicitaDescargaRecibidos"
        solicitud_xpath = "{*}Body/{*}SolicitaDescargaRecibidos/{*}solicitud"

        def get_payload(self):
            root = etree.fromstring(
                b"""
                <s:Envelope xmlns:des="http://DescargaMasivaTerceros.sat.gob.mx"
                            xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">
                    <s:Header/>
                    <s:Body>
                        <des:SolicitaDescargaRecibidos>
                            <des:solicitud/>
                        </des:SolicitaDescargaRecibidos>
                    </s:Body>
                </s:Envelope>
                """
            )
            self._prepare_payload(root)
            return etree.tostring(root, encoding="UTF-8")

        def process_response(self, response):
            res = response.find(
                "{*}Body/{*}SolicitaDescargaRecibidosResponse/{*}SolicitaDescargaRecibidosResult"
            )
            if res is None:
                return {}
            data = dict(res.attrib)
            for i in res:
                data[etree.QName(i.tag).localname] = i.text
            return data

    return _SolicitaRecibidos


def _create_solicita_emitidos_class():
    """Crea la clase de solicitud para Emitidos con URLs corregidas."""
    from lxml import etree
    from satcfdi.pacs import sat as sat_pacs

    class _SolicitaEmitidos(sat_pacs._SATRequest):
        soap_url = "https://cfdidescargamasivasolicitud.clouda.sat.gob.mx/SolicitaDescargaService.svc"
        soap_action = "http://DescargaMasivaTerceros.sat.gob.mx/ISolicitaDescargaService/SolicitaDescargaEmitidos"
        solicitud_xpath = "{*}Body/{*}SolicitaDescargaEmitidos/{*}solicitud"

        def get_payload(self):
            root = etree.fromstring(
                b"""
                <s:Envelope xmlns:des="http://DescargaMasivaTerceros.sat.gob.mx"
                            xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">
                    <s:Header/>
                    <s:Body>
                        <des:SolicitaDescargaEmitidos>
                            <des:solicitud/>
                        </des:SolicitaDescargaEmitidos>
                    </s:Body>
                </s:Envelope>
                """
            )
            self._prepare_payload(root)
            return etree.tostring(root, encoding="UTF-8")

        def process_response(self, response):
            res = response.find(
                "{*}Body/{*}SolicitaDescargaEmitidosResponse/{*}SolicitaDescargaEmitidosResult"
            )
            if res is None:
                return {}
            data = dict(res.attrib)
            for i in res:
                data[etree.QName(i.tag).localname] = i.text
            return data

    return _SolicitaEmitidos


def _create_verifica_class():
    """Crea la clase de verificación con URL corregida."""
    from satcfdi.pacs.sat import _CFDIVerificaSolicitudDescarga

    class _VerificaSolicitud(_CFDIVerificaSolicitudDescarga):
        soap_url = "https://cfdidescargamasivasolicitud.clouda.sat.gob.mx/VerificaSolicitudDescargaService.svc"

    return _VerificaSolicitud


def _create_descarga_class():
    """Crea la clase de descarga con URL corregida."""
    from satcfdi.pacs.sat import _CFDIDescargaMasiva

    class _DescargaMasiva(_CFDIDescargaMasiva):
        soap_url = "https://cfdidescargamasiva.clouda.sat.gob.mx/DescargaMasivaService.svc"
        soap_action = "http://DescargaMasivaTerceros.sat.gob.mx/IDescargaMasivaService/Descargar"

    return _DescargaMasiva


def request_and_download(
    cer_path: str,
    key_path: str,
    password: str,
    month_start: date,
    month_end: date,
    rfc_solicitante: Optional[str] = None,
    emisor: bool = False,
    existing_id: Optional[str] = None,
    tipo_solicitud: str = "CFDI",
    output_dir: Optional[Path] = None,
    on_progress: Optional[callable] = None,
) -> dict:
    """
    Flujo completo contra el servicio oficial de Descarga Masiva:
    SolicitaDescarga → VerificaSolicitud (polling) → DescargaMasiva

    Args:
        cer_path: Ruta al certificado .cer
        key_path: Ruta a la llave .key
        password: Contraseña de la FIEL
        month_start: Fecha inicio del periodo
        month_end: Fecha fin del periodo
        rfc_solicitante: RFC (si no se pasa, se extrae del certificado)
        emisor: False=Recibidos, True=Emitidos
        existing_id: Si se pasa, reutiliza este IdSolicitud
        tipo_solicitud: "CFDI" (XMLs) o "Metadata"
        output_dir: Directorio donde guardar ZIPs descargados
        on_progress: Callback para reportar progreso

    Returns:
        Dict con paquetes descargados, id_solicitud, estado, etc.
    """
    if not is_soap_available():
        raise SoapApiError(
            "Dependencias SOAP no instaladas. "
            "Instala con: pip install sat-scrapper-core[soap]"
        )

    from satcfdi.pacs.sat import SAT, TipoDescargaMasivaTerceros
    from satcfdi.pacs import sat as sat_pacs

    signer = _build_signer(cer_path, key_path, password)
    sat = SAT(signer=signer)
    sat.wait_time = 20  # Poll cada 20s

    if output_dir is None:
        output_dir = Path("./downloads/soap_packages")
    output_dir.mkdir(parents=True, exist_ok=True)

    target_rfc = rfc_solicitante or signer.rfc
    fecha_inicial_dt = datetime.combine(month_start, datetime.min.time())
    fecha_final_dt = datetime.combine(month_end, datetime.max.time().replace(microsecond=0))

    # Clases con URLs corregidas
    SolicitaRecibidos = _create_solicita_recibidos_class()
    SolicitaEmitidos = _create_solicita_emitidos_class()
    VerificaSolicitud = _create_verifica_class()
    DescargaMasiva = _create_descarga_class()

    solicitud_id = existing_id
    solicitud_response = None

    if not solicitud_id:
        _report_progress(on_progress, f"Solicitando descarga {'emitidos' if emisor else 'recibidos'}...")
        tipo_enum = (
            TipoDescargaMasivaTerceros.CFDI
            if tipo_solicitud.lower() == "cfdi"
            else TipoDescargaMasivaTerceros.METADATA
        )

        try:
            args = {
                "FechaFinal": fecha_final_dt,
                "FechaInicial": fecha_inicial_dt,
                "RfcSolicitante": sat.signer.rfc,
                "TipoSolicitud": tipo_enum,
            }
            if emisor:
                args["RfcEmisor"] = target_rfc
            else:
                args["RfcReceptor"] = target_rfc
            if tipo_enum == TipoDescargaMasivaTerceros.CFDI:
                args["EstadoComprobante"] = "Vigente"

            request_cls = SolicitaEmitidos if emisor else SolicitaRecibidos
            solicitud_response = sat._execute_req(
                request_cls(signer=sat.signer, arguments=args),
                needs_token_fn=sat._get_token_comprobante,
            )

            if solicitud_response.get("IdSolicitud") and solicitud_response.get("CodEstatus") == "5000":
                solicitud_id = solicitud_response["IdSolicitud"]
                logger.info("✅ IdSolicitud: %s", solicitud_id)
            else:
                raise SoapApiError(f"Solicitud rechazada: {solicitud_response}")
        except SoapApiError:
            raise
        except Exception as exc:
            raise SoapApiError(f"Error solicitando descarga: {exc}") from exc
    else:
        solicitud_response = {"IdSolicitud": solicitud_id, "CodEstatus": "5000"}

    # Polling: verificar hasta que esté lista
    saved_packages: list[Path] = []
    paquetes_ids: list[str] = []
    max_attempts = 40  # ~13 minutos
    estado = None

    _report_progress(on_progress, f"Verificando solicitud {solicitud_id}...")

    for attempt in range(max_attempts):
        try:
            verifica_response = sat._execute_req(
                VerificaSolicitud(
                    signer=sat.signer,
                    arguments={"RfcSolicitante": sat.signer.rfc, "IdSolicitud": solicitud_id},
                ),
                needs_token_fn=sat._get_token_comprobante,
            )
        except Exception as e:
            logger.warning("Error verificando (intento %d): %s", attempt + 1, e)
            time.sleep(sat.wait_time)
            continue

        estado = verifica_response.get("EstadoSolicitud")
        ids_paquetes = verifica_response.get("IdsPaquetes") or []

        # Estado 3 = TERMINADA, con paquetes listos
        if estado == getattr(sat_pacs, "EstadoSolicitud", {}).get("TERMINADA", 3) or estado == 3 or ids_paquetes:
            for paquete_id in ids_paquetes:
                try:
                    _report_progress(on_progress, f"Descargando paquete {paquete_id}...")
                    descarga_response, paquete_bytes = sat._execute_req(
                        DescargaMasiva(
                            signer=sat.signer,
                            arguments={"RfcSolicitante": sat.signer.rfc, "IdPaquete": paquete_id},
                        ),
                        needs_token_fn=sat._get_token_comprobante,
                    )
                    paquetes_ids.append(paquete_id)
                    target = output_dir / f"cfdi_sat_{month_start:%Y%m}_{paquete_id}.zip"
                    decoded = base64.b64decode(paquete_bytes) if isinstance(paquete_bytes, str) else paquete_bytes
                    target.write_bytes(decoded)
                    saved_packages.append(target)
                    logger.info("✅ Paquete descargado: %s", target.name)
                except Exception as e:
                    logger.error("Error descargando paquete %s: %s", paquete_id, e)
            break

        # Estados 1, 2 = EN PROCESO
        if estado in (1, 2):
            _report_progress(on_progress, f"Solicitud en proceso (intento {attempt + 1}/{max_attempts})...")
            time.sleep(sat.wait_time)
            continue

        # Cualquier otro estado = error
        logger.warning("Estado inesperado: %s", verifica_response)
        break
    else:
        raise SoapApiError(
            f"Timeout verificando solicitud {solicitud_id} tras {max_attempts} intentos. Estado: {estado}"
        )

    return {
        "paquetes": [str(p) for p in saved_packages],
        "desde": month_start.isoformat(),
        "hasta": month_end.isoformat(),
        "id_solicitud": solicitud_id,
        "paquetes_ids": paquetes_ids,
        "estado": estado,
        "cod_estatus": solicitud_response.get("CodEstatus") if solicitud_response else None,
    }


def _report_progress(callback, message: str):
    """Reporta progreso si hay callback registrado."""
    logger.info(message)
    if callback:
        try:
            callback(message)
        except Exception:
            pass
