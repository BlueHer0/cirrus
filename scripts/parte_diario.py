#!/usr/bin/env python3
"""Parte diario de operaciones — Telegram 8:00 AM MX (cron 14:00 UTC).

Resumen de las últimas 24h: servicios, descargas, registros nuevos, leads,
pagos Stripe, compulsas SAT, disco y tráfico de la landing. Creado por la
dirección operativa 2026-07-28.
"""
import os
import subprocess
import sys
from datetime import timedelta

sys.path.insert(0, "/var/www/cirrus")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "cirrus.settings")
import django  # noqa: E402

django.setup()

from django.contrib.auth.models import User  # noqa: E402
from django.utils import timezone  # noqa: E402

from accounts.models import StripePayment  # noqa: E402
from core.models import (  # noqa: E402
    CompulsaSAT,
    ConversionLead,
    DescargaJob,
    Empresa,
    SnowieLead,
    StripeWebhookEvent,
)
from core.services.alerts import send_telegram  # noqa: E402

ahora = timezone.now()
hace_24h = ahora - timedelta(hours=24)
lineas = [f"📋 *Parte diario Cirrus* — {timezone.localtime(ahora):%Y-%m-%d %H:%M} MX"]

# ── Servicios systemd ──
servicios = ["cirrus-web", "cirrus-worker", "cirrus-beat", "cirrus-cerebro"]
caidos = []
for s in servicios:
    r = subprocess.run(["systemctl", "is-active", s], capture_output=True, text=True)
    if r.stdout.strip() != "active":
        caidos.append(f"{s}={r.stdout.strip()}")
lineas.append("🟢 Servicios OK" if not caidos else f"🔴 Servicios: {', '.join(caidos)}")

# ── Descargas 24h ──
from django.db.models import Q  # noqa: E402

jobs = DescargaJob.objects.filter(
    Q(created_at__gte=hace_24h) | Q(iniciado_at__gte=hace_24h) | Q(completado_at__gte=hace_24h)
)
tot = jobs.count()
comp = jobs.filter(estado="completado", completado_at__gte=hace_24h).count()
err = jobs.filter(estado="error").count()
lineas.append(f"⬇️ Descargas 24h: {tot} jobs ({comp} ok, {err} error)")

# ── Registros nuevos ──
nuevos = User.objects.filter(date_joined__gte=hace_24h)
sin_confirmar = nuevos.filter(is_active=False).count()
if nuevos.exists():
    detalle = ", ".join(u.email for u in nuevos[:10])
    lineas.append(f"🆕 Registros 24h: {nuevos.count()} ({sin_confirmar} sin confirmar) — {detalle}")
else:
    lineas.append("🆕 Registros 24h: 0")

# ── Empresas con FIEL pendiente ──
pend_fiel = Empresa.objects.filter(fiel_verificada=False).count()
if pend_fiel:
    lineas.append(f"🔑 Empresas con FIEL sin verificar: {pend_fiel}")

# ── Leads ──
snowie_nuevos = SnowieLead.objects.filter(estado="nuevo").count()
conv_24h = ConversionLead.objects.filter(ultima_conversion__gte=hace_24h).count()
lineas.append(f"🎯 Leads: {snowie_nuevos} Snowie sin atender · {conv_24h} conversiones lead 24h")

# ── Stripe ──
pagos = StripePayment.objects.filter(created_at__gte=hace_24h, status__in=["succeeded", "paid", "pending"])
try:
    n_pagos = pagos.count()
    monto = sum(float(p.amount or 0) for p in pagos)
    lineas.append(f"💳 Stripe 24h: {n_pagos} pagos (${monto:,.0f})")
except Exception:
    lineas.append("💳 Stripe 24h: (sin datos)")
wh_err = StripeWebhookEvent.objects.filter(recibido_en__gte=hace_24h, estado="error").count()
if wh_err:
    lineas.append(f"⚠️ Webhooks Stripe en error 24h: {wh_err}")

# ── Compulsas SAT ──
c_pend = CompulsaSAT.objects.filter(estado="solicitada").count()
c_falt = sum(
    c.faltantes_count or 0
    for c in CompulsaSAT.objects.filter(estado="completada", actualizado_at__gte=hace_24h)
)
if c_pend or c_falt:
    lineas.append(f"🧾 Compulsas SAT: {c_pend} pendientes · {c_falt} faltantes detectados 24h")

# ── Disco ──
r = subprocess.run(["df", "--output=pcent", "/"], capture_output=True, text=True)
try:
    disco = r.stdout.strip().splitlines()[-1].strip()
    lineas.append(f"💾 Disco: {disco}")
except Exception:
    pass

# ── Tráfico landing (nginx) ──
try:
    log = "/var/log/nginx/cirrus.access.log"
    if os.path.exists(log):
        ips = set()
        hits = 0
        with open(log, errors="ignore") as f:
            for ln in f:
                hits += 1
                ips.add(ln.split(" ", 1)[0])
        lineas.append(f"🌐 Tráfico (log actual): {hits} hits, {len(ips)} IPs únicas")
except Exception:
    pass

send_telegram("\n".join(lineas), level="info", category="parte_diario")
print("\n".join(lineas))
