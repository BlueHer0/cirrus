"""CFDI API endpoints.

Security model:
- Data-access endpoints (stored CFDIs) → require API Key + empresa access check
- Stateless conversion endpoints → public (user uploads their own XML)

Endpoints:
- GET  /api/v1/cfdis/{uuid}/pdf/         → AUTH: Generate PDF from stored CFDI (with logo)
- GET  /api/v1/cfdis/{uuid}/excel/       → AUTH: Detailed Excel for one CFDI
- GET  /api/v1/cfdis/export/excel/       → AUTH: Export filtered CFDIs to Excel
- GET  /api/v1/cfdis/export/csv/         → AUTH: Export filtered CFDIs to CSV
- POST /api/v1/cfdis/convert/pdf/        → PUBLIC: Convert raw XML to PDF
- POST /api/v1/cfdis/convert/excel/      → PUBLIC: Convert raw XML to Excel
- POST /api/v1/cfdis/convert/send/       → PUBLIC: Convert + email result
"""

import base64
import logging
from typing import Optional

from ninja import Router, File, Form, UploadedFile
from django.http import HttpResponse, JsonResponse

from sat_scrapper_core.cfdi_pdf import render_cfdi_pdf
from core.api.auth import api_key_auth

logger = logging.getLogger("core.api.cfdis")

router = Router(tags=["cfdis"])


# ── Helpers ───────────────────────────────────────────────────────────

def _lookup_cfdi_with_access(request, uuid: str):
    """Look up CFDI by UUID and verify the requesting API key has access to its empresa."""
    from core.models import CFDI

    cfdi = CFDI.objects.select_related("empresa").filter(uuid=uuid).first()
    if not cfdi:
        return None, _error("CFDI not found", 404)

    # Verify API key has access to this empresa
    allowed_ids = set(request.api_empresas.values_list("id", flat=True))
    if cfdi.empresa_id not in allowed_ids:
        return None, _error("CFDI not accessible with this API key", 403)

    return cfdi, None


def _download_xml(cfdi):
    """Download XML bytes from MinIO for a CFDI."""
    from core.services.storage_minio import download_bytes
    return download_bytes(cfdi.xml_minio_key)


def _get_logo_data_uri(empresa) -> str | None:
    """Download empresa logo from MinIO and return as data URI, or None."""
    if not empresa.logo_minio_key:
        return None
    try:
        from core.services.storage_minio import download_bytes
        logo_data = download_bytes(empresa.logo_minio_key)
        ext = empresa.logo_minio_key.rsplit(".", 1)[-1].lower()
        mime_map = {
            "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
            "svg": "image/svg+xml", "webp": "image/webp",
        }
        mime = mime_map.get(ext, "image/png")
        return f"data:{mime};base64,{base64.b64encode(logo_data).decode()}"
    except Exception:
        return None  # No logo is OK


def _error(msg: str, status: int = 400):
    return HttpResponse(
        f'{{"error": "{msg}"}}',
        status=status,
        content_type="application/json",
    )


def _apply_filters(qs, request, empresa_id, rfc, year, month, tipo, tipo_comprobante):
    """Apply query filters, always scoped to API key's allowed empresas."""
    allowed_ids = list(request.api_empresas.values_list("id", flat=True))
    qs = qs.filter(empresa_id__in=allowed_ids)

    if empresa_id:
        qs = qs.filter(empresa_id=empresa_id)
    if rfc:
        qs = qs.filter(empresa__rfc=rfc.upper())
    if year:
        qs = qs.filter(fecha__year=year)
    if month:
        qs = qs.filter(fecha__month=month)
    if tipo:
        qs = qs.filter(tipo_relacion=tipo)
    if tipo_comprobante:
        qs = qs.filter(tipo_comprobante=tipo_comprobante.upper())
    return qs


# ── Public endpoints (stateless conversion) ──────────────────────────
# IMPORTANT: These MUST be registered before /{uuid}/ routes, otherwise
# Django Ninja matches "convert" as the {uuid} parameter → 405.


@router.post("/convert/pdf/", summary="Convert XML to PDF (stateless, public)")
def convert_xml_to_pdf(request, xml_file: UploadedFile = File(...)):
    """Convert a raw CFDI XML to PDF without storing anything. No auth required."""
    try:
        xml_bytes = xml_file.read()
    except Exception:
        return _error("Failed to read uploaded file")

    if not xml_bytes or len(xml_bytes) < 50:
        return _error("XML file is empty or too small")

    try:
        pdf_bytes = render_cfdi_pdf(xml_bytes)
    except Exception as e:
        logger.error("XML→PDF conversion failed: %s", e)
        return _error("Failed to convert XML to PDF. Ensure valid CFDI 3.3/4.0.", 422)

    pdf_name = (xml_file.name or "cfdi").rsplit(".", 1)[0] + ".pdf"
    return HttpResponse(
        pdf_bytes,
        content_type="application/pdf",
        headers={
            "Content-Disposition": f'inline; filename="{pdf_name}"',
            "Content-Length": str(len(pdf_bytes)),
        },
    )


@router.post("/convert/excel/", summary="Convert XML to Excel (stateless, public)")
def convert_xml_to_excel(request, xml_file: UploadedFile = File(...)):
    """Convert a raw CFDI XML to a detailed 3-sheet Excel. No auth required."""
    from core.services.excel_export import export_cfdi_detail_excel

    try:
        xml_bytes = xml_file.read()
    except Exception:
        return _error("Failed to read uploaded file")

    if not xml_bytes or len(xml_bytes) < 50:
        return _error("XML file is empty or too small")

    try:
        xlsx_bytes = export_cfdi_detail_excel(xml_bytes)
    except Exception as e:
        logger.error("XML→Excel conversion failed: %s", e)
        return _error("Failed to convert XML to Excel. Ensure valid CFDI 3.3/4.0.", 422)

    xlsx_name = (xml_file.name or "cfdi").rsplit(".", 1)[0] + ".xlsx"
    return HttpResponse(
        xlsx_bytes,
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": f'attachment; filename="{xlsx_name}"',
            "Content-Length": str(len(xlsx_bytes)),
        },
    )


@router.post("/convert/send/", summary="Convert XML and email the result (public)")
def convert_and_send(
    request,
    xml_file: UploadedFile = File(...),
    email: str = Form(...),
    format: str = Form("pdf"),
):
    """Convert a CFDI XML, email the result, and capture lead + log."""
    import re
    from django.db.models import F
    from django.core.mail import EmailMessage
    from django.template.loader import render_to_string
    from core.models import ConversionLead, ConversionLog
    from sat_scrapper_core.cfdi_pdf.xml_parse import parse_cfdi_xml

    # Validate email
    if not email or not re.match(r'^[^@]+@[^@]+\.[^@]+$', email):
        return JsonResponse({"error": "Email inválido"}, status=400)

    # Validate format
    if format not in ("pdf", "excel"):
        return JsonResponse({"error": "Formato debe ser 'pdf' o 'excel'"}, status=400)

    # Read XML
    try:
        xml_bytes = xml_file.read()
    except Exception:
        return JsonResponse({"error": "No se pudo leer el archivo"}, status=400)

    if not xml_bytes or len(xml_bytes) < 50:
        return JsonResponse({"error": "Archivo XML vacío o muy pequeño"}, status=400)

    # Parse CFDI metadata
    uuid_cfdi = rfc_emisor = rfc_receptor = ""
    total_cfdi = None
    try:
        parsed = parse_cfdi_xml(xml_bytes)
        uuid_cfdi = parsed.timbre.get("uuid", "")
        rfc_emisor = parsed.emisor.get("rfc", "")
        rfc_receptor = parsed.receptor.get("rfc", "")
        total_cfdi = parsed.comprobante.get("total")
    except Exception:
        pass  # Non-critical: we can still convert without metadata

    # Convert
    try:
        if format == "pdf":
            file_bytes = render_cfdi_pdf(xml_bytes, branded=True)
            content_type = "application/pdf"
            ext = ".pdf"
            formato_display = "PDF"
        else:
            from core.services.excel_export import export_cfdi_detail_excel
            file_bytes = export_cfdi_detail_excel(xml_bytes)
            content_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            ext = ".xlsx"
            formato_display = "Excel"
    except Exception as e:
        logger.error("Conversion failed for email send: %s", e)
        return JsonResponse(
            {"error": "No se pudo convertir. Asegúrate de que es un CFDI 3.3/4.0 válido."},
            status=422,
        )

    uuid_short = uuid_cfdi[:8] if uuid_cfdi else "cfdi"
    filename = f"{uuid_cfdi or (xml_file.name or 'cfdi').rsplit('.', 1)[0]}{ext}"

    # Upsert lead
    ip = request.META.get("HTTP_X_FORWARDED_FOR", request.META.get("REMOTE_ADDR", ""))
    if ip and "," in ip:
        ip = ip.split(",")[0].strip()
    user_agent = request.META.get("HTTP_USER_AGENT", "")[:500]

    try:
        lead, created = ConversionLead.objects.get_or_create(
            email=email,
            defaults={"ip": ip, "user_agent": user_agent},
        )
        if not created:
            lead.conversiones = F("conversiones") + 1
            lead.ip = ip
            lead.user_agent = user_agent

        if format == "pdf":
            lead.total_pdfs = F("total_pdfs") + 1
        else:
            lead.total_excels = F("total_excels") + 1

        if created:
            lead.total_pdfs = 1 if format == "pdf" else 0
            lead.total_excels = 1 if format == "excel" else 0

        lead.save()
        lead.refresh_from_db()
    except Exception as e:
        logger.warning("Failed to upsert lead %s: %s", email, e)
        lead = None

    # Create log
    log = None
    try:
        if lead:
            log = ConversionLog.objects.create(
                lead=lead,
                formato=format,
                uuid_cfdi=uuid_cfdi,
                rfc_emisor=rfc_emisor,
                rfc_receptor=rfc_receptor,
                total=total_cfdi,
                archivo_size=len(file_bytes),
            )
    except Exception as e:
        logger.warning("Failed to create log for %s: %s", email, e)

    # Send email
    email_sent = False
    try:
        html_body = render_to_string("emails/conversion_result.html", {
            "formato_display": formato_display,
            "filename": filename,
            "uuid_cfdi": uuid_cfdi,
            "rfc_emisor": rfc_emisor,
            "rfc_receptor": rfc_receptor,
            "total_cfdi": f"${total_cfdi:,.2f}" if total_cfdi else "",
        })

        msg = EmailMessage(
            subject=f"Tu CFDI convertido — {uuid_short}{ext}",
            body=html_body,
            to=[email],
        )
        msg.content_subtype = "html"
        msg.attach(filename, file_bytes, content_type)
        msg.send(fail_silently=False)
        email_sent = True
    except Exception as e:
        logger.error("Failed to send conversion email to %s: %s", email, e)
        if log:
            log.error = str(e)
            log.save(update_fields=["error"])
        return JsonResponse(
            {"error": "Conversión exitosa pero no se pudo enviar el email. Intenta de nuevo."},
            status=500,
        )

    # Mark log as sent
    if log and email_sent:
        log.enviado = True
        log.save(update_fields=["enviado"])

    return JsonResponse({"status": "sent", "email": email, "message": "Revisa tu correo"})


# ── Authenticated endpoints (data access) ────────────────────────────


@router.get("/{uuid}/pdf/", auth=api_key_auth, summary="Generate PDF from stored CFDI")
def get_cfdi_pdf(request, uuid: str):
    """Generate a PDF for a CFDI stored in MinIO. Includes empresa logo if configured."""
    from core.services.pdf_service import render_cfdi_pdf_with_logo

    cfdi, err = _lookup_cfdi_with_access(request, uuid)
    if err:
        return err
    if not cfdi.xml_minio_key:
        return _error("CFDI has no XML stored", 404)

    try:
        xml_bytes = _download_xml(cfdi)

        # Get logo from empresa if available
        logo_data_uri = _get_logo_data_uri(cfdi.empresa)

        if logo_data_uri:
            pdf_bytes = render_cfdi_pdf_with_logo(xml_bytes, logo_data_uri=logo_data_uri)
        else:
            pdf_bytes = render_cfdi_pdf(xml_bytes, branded=False)

    except Exception as e:
        logger.error("PDF generation failed for %s: %s", uuid, e)
        return _error("Failed to generate PDF", 500)

    return HttpResponse(
        pdf_bytes,
        content_type="application/pdf",
        headers={
            "Content-Disposition": f'inline; filename="{uuid}.pdf"',
            "Content-Length": str(len(pdf_bytes)),
        },
    )


@router.get("/{uuid}/excel/", auth=api_key_auth, summary="Detailed Excel for one CFDI")
def get_cfdi_detail_excel(request, uuid: str):
    """Generate a detailed 3-sheet Excel from a stored CFDI XML."""
    from core.services.excel_export import export_cfdi_detail_excel

    cfdi, err = _lookup_cfdi_with_access(request, uuid)
    if err:
        return err
    if not cfdi.xml_minio_key:
        return _error("CFDI has no XML stored", 404)

    try:
        xml_bytes = _download_xml(cfdi)
        xlsx_bytes = export_cfdi_detail_excel(xml_bytes)
    except Exception as e:
        logger.error("Detail Excel failed for %s: %s", uuid, e)
        return _error("Failed to generate Excel", 500)

    return HttpResponse(
        xlsx_bytes,
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": f'attachment; filename="{uuid}.xlsx"',
            "Content-Length": str(len(xlsx_bytes)),
        },
    )


@router.get("/export/excel/", auth=api_key_auth, summary="Export CFDIs to Excel")
def export_cfdis_to_excel(
    request,
    empresa_id: Optional[str] = None,
    rfc: Optional[str] = None,
    year: Optional[int] = None,
    month: Optional[int] = None,
    tipo: Optional[str] = None,
    tipo_comprobante: Optional[str] = None,
):
    """Export filtered CFDIs to a styled .xlsx file. Scoped to API key's empresas."""
    from core.models import CFDI
    from core.services.excel_export import export_cfdis_excel

    qs = CFDI.objects.all()
    qs = _apply_filters(qs, request, empresa_id, rfc, year, month, tipo, tipo_comprobante)

    title = f"CFDIs_{year or 'all'}_{month or 'all'}"
    xlsx_bytes = export_cfdis_excel(qs, title=title)

    return HttpResponse(
        xlsx_bytes,
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": f'attachment; filename="{title}.xlsx"',
            "Content-Length": str(len(xlsx_bytes)),
        },
    )


@router.get("/export/csv/", auth=api_key_auth, summary="Export CFDIs to CSV")
def export_cfdis_to_csv(
    request,
    empresa_id: Optional[str] = None,
    rfc: Optional[str] = None,
    year: Optional[int] = None,
    month: Optional[int] = None,
    tipo: Optional[str] = None,
    tipo_comprobante: Optional[str] = None,
):
    """Export filtered CFDIs to CSV (UTF-8 BOM). Scoped to API key's empresas."""
    from core.models import CFDI
    from core.services.excel_export import export_cfdis_csv

    qs = CFDI.objects.all()
    qs = _apply_filters(qs, request, empresa_id, rfc, year, month, tipo, tipo_comprobante)

    csv_bytes = export_cfdis_csv(qs)
    filename = f"CFDIs_{year or 'all'}_{month or 'all'}.csv"

    return HttpResponse(
        csv_bytes,
        content_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Length": str(len(csv_bytes)),
        },
    )


