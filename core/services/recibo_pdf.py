"""Generate electronic receipt PDFs (not CFDI)."""

from django.template.loader import render_to_string


def generar_recibo(pago):
    """Generate a receipt PDF for a StripePayment."""
    from weasyprint import HTML

    html = render_to_string("recibos/recibo.html", {
        "pago": pago,
        "user": pago.user,
        "profile": pago.user.perfil,
        "numero": f"REC-{pago.id:06d}",
        "fecha": pago.created_at,
    })

    pdf = HTML(string=html).write_pdf()
    return pdf
