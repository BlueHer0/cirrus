"""EFOS 69-B Sync — Descarga CSV del SAT y sincroniza tabla local.

Fuente: http://omawww.sat.gob.mx/cifras_sat/Documents/Listado_Completo_69-B.csv
"""

import csv
import io
import logging
from datetime import datetime

import requests

logger = logging.getLogger("core.efos_sync")

CSV_URL = "http://omawww.sat.gob.mx/cifras_sat/Documents/Listado_Completo_69-B.csv"


def sync_efos():
    """Descarga el CSV del SAT y sincroniza la tabla EFOS."""
    from core.models import EFOS
    from core.services.monitor import log_info, log_error
    from core.services.alerts import send_telegram

    log_info("system", "Iniciando sincronización EFOS 69-B")

    try:
        response = requests.get(CSV_URL, timeout=120)
        response.raise_for_status()
    except requests.RequestException as e:
        log_error("system", f"EFOS sync: error descargando CSV — {e}")
        send_telegram(f"🔴 EFOS sync falló: {e}", "critical")
        return None

    # Detect encoding
    content = response.content
    text = None
    for encoding in ["utf-8", "latin-1", "cp1252"]:
        try:
            text = content.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        text = content.decode("latin-1", errors="replace")

    # Parse CSV — skip preamble lines until we find the header row with 'RFC'
    lines = text.split("\n")
    header_idx = 0
    for i, line in enumerate(lines[:20]):
        if "RFC" in line and "Contribuyente" in line:
            header_idx = i
            break

    csv_text = "\n".join(lines[header_idx:])
    reader = csv.DictReader(io.StringIO(csv_text))
    if reader.fieldnames:
        reader.fieldnames = [f.strip().strip("\ufeff") for f in reader.fieldnames]

    registros = []
    errores_parse = 0

    def _get_field(row, *keys):
        """Case-insensitive field lookup."""
        row_lower = {k.lower(): v for k, v in row.items()}
        for key in keys:
            val = row_lower.get(key.lower(), "")
            if val and val.strip():
                return val.strip()
        return ""

    for row in reader:
        try:
            rfc = _get_field(row, "RFC").upper()
            if not rfc or len(rfc) < 12:
                errores_parse += 1
                continue

            nombre = _get_field(
                row,
                "Nombre del Contribuyente",
                "Contribuyente",
            )

            situacion = _get_field(
                row,
                "Situación del contribuyente",
                "Situación del Contribuyente",
                "Situacion del Contribuyente",
            )

            fecha_pub = _parse_fecha_safe(
                _get_field(row, "Publicación DOF presuntos", "Publicación DOF Presuntos", "Fecha de Publicación")
            )

            registros.append({
                "rfc": rfc,
                "nombre": nombre[:500],
                "situacion": situacion[:100],
                "fecha_publicacion": fecha_pub,
                "raw_data": {k: (v.strip() if v else "") for k, v in row.items()},
            })
        except Exception:
            errores_parse += 1

    if not registros:
        log_error("system", "EFOS sync: CSV vacío o no se pudo parsear")
        send_telegram("🔴 EFOS sync falló: CSV vacío", "critical")
        return None

    # Upsert
    nuevos = 0
    actualizados = 0
    for r in registros:
        _, created = EFOS.objects.update_or_create(
            rfc=r["rfc"],
            defaults={
                "nombre": r["nombre"],
                "situacion": r["situacion"],
                "fecha_publicacion": r["fecha_publicacion"],
                "raw_data": r["raw_data"],
            },
        )
        if created:
            nuevos += 1
        else:
            actualizados += 1

    total = EFOS.objects.count()
    msg = (
        f"✅ EFOS 69-B sincronizado\n"
        f"Total: {total} registros\n"
        f"Nuevos: {nuevos} | Actualizados: {actualizados}\n"
        f"Errores parse: {errores_parse}"
    )
    log_info("system", msg)
    send_telegram(msg, "success")
    return {"total": total, "nuevos": nuevos, "actualizados": actualizados, "errores": errores_parse}


def verificar_rfc_efos(rfc):
    """Verifica si un RFC está en la lista 69-B."""
    from core.models import EFOS

    rfc = rfc.strip().upper()
    efos = EFOS.objects.filter(rfc=rfc).first()
    if efos:
        return {
            "en_lista": True,
            "rfc": efos.rfc,
            "nombre": efos.nombre,
            "situacion": efos.situacion,
            "fecha_publicacion": efos.fecha_publicacion,
        }
    return {"en_lista": False, "rfc": rfc}


def verificar_proveedores_empresa(empresa, year=None, month=None):
    """Cruza proveedores de una empresa contra la lista 69-B."""
    from core.models import CFDI, EFOS
    from django.db.models import Sum, Count

    filtro = {"empresa": empresa, "rfc_receptor": empresa.rfc}
    if year:
        filtro["fecha__year"] = year
    if month:
        filtro["fecha__month"] = month

    proveedores = (
        CFDI.objects.filter(**filtro)
        .values("rfc_emisor")
        .annotate(monto=Sum("total"), count=Count("id"))
    )

    alertas = []
    for prov in proveedores:
        efos = EFOS.objects.filter(rfc=prov["rfc_emisor"]).first()
        if efos:
            alertas.append({
                "rfc": efos.rfc,
                "nombre": efos.nombre,
                "situacion": efos.situacion,
                "monto_facturado": float(prov["monto"] or 0),
                "num_cfdis": prov["count"],
            })
    return alertas


def _parse_fecha_safe(fecha_str):
    """Intenta parsear fecha del CSV, retorna None si falla."""
    if not fecha_str or not fecha_str.strip():
        return None
    fecha_str = fecha_str.strip()
    for fmt in ["%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y", "%d/%m/%y"]:
        try:
            return datetime.strptime(fecha_str, fmt).date()
        except ValueError:
            continue
    return None
