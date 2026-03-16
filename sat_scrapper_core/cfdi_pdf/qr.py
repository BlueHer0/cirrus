"""Genera QR de verificacion SAT como data URI."""

from __future__ import annotations

import base64
import io

import qrcode


def generar_qr_data_uri(url: str) -> str:
    """Genera un QR como data URI PNG para embeber en HTML."""
    qr = qrcode.QRCode(box_size=4, border=1)
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")

    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}"
