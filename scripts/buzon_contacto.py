#!/usr/bin/env python3
"""Agente del buzón contactocirrus@nubex.me — poll IMAP cada 15 min (cron).

Detecta correos nuevos y alerta por Telegram con remitente, asunto y extracto,
para que el director/Fernando respondan el mismo día (SLA < 4h hábiles).

Credenciales: CONTACTO_IMAP_HOST/USER/PASS en /var/www/cirrus/.env
(fallback legacy: cirrus-direccion/credenciales/contactocirrus.env).

Estado (UIDs ya notificados): /var/www/cirrus/logs/buzon_contacto_state.json
"""
import email
import email.header
import imaplib
import json
import os
import sys

CREDS = "/home/farizpe/cirrus-direccion/credenciales/contactocirrus.env"
STATE = "/var/www/cirrus/logs/buzon_contacto_state.json"

sys.path.insert(0, "/var/www/cirrus")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "cirrus.settings")
import django  # noqa: E402

django.setup()
from core.services.alerts import send_telegram  # noqa: E402


def _load_state():
    try:
        with open(STATE) as f:
            return json.load(f)
    except Exception:
        return {"seen_uids": [], "creds_warned": False}


def _save_state(st):
    with open(STATE, "w") as f:
        json.dump(st, f)


def _decode(s):
    if not s:
        return ""
    parts = email.header.decode_header(s)
    out = []
    for txt, enc in parts:
        out.append(txt.decode(enc or "utf-8", "replace") if isinstance(txt, bytes) else txt)
    return "".join(out)


st = _load_state()

from decouple import config as env_config  # noqa: E402

cfg = {
    "IMAP_HOST": env_config("CONTACTO_IMAP_HOST", default=""),
    "IMAP_USER": env_config("CONTACTO_IMAP_USER", default=""),
    "IMAP_PASS": env_config("CONTACTO_IMAP_PASS", default=""),
}
if not cfg["IMAP_PASS"] and os.path.exists(CREDS):
    with open(CREDS) as f:
        for ln in f:
            ln = ln.strip()
            if "=" in ln and not ln.startswith("#"):
                k, v = ln.split("=", 1)
                cfg[k.strip()] = v.strip()

if not cfg["IMAP_PASS"]:
    if not st.get("creds_warned"):
        send_telegram(
            "📮 Agente de buzón contactocirrus@ montado pero SIN credenciales.\n"
            "Fernando: agrega CONTACTO_IMAP_HOST/USER/PASS al .env de Cirrus.",
            level="warning", category="buzon",
        )
        st["creds_warned"] = True
        _save_state(st)
    sys.exit(0)

try:
    M = imaplib.IMAP4_SSL(cfg.get("IMAP_HOST", "chocobo.mxrouting.net"))
    M.login(cfg["IMAP_USER"], cfg["IMAP_PASS"])
    M.select("INBOX")
    _, data = M.uid("search", None, "ALL")
    uids = data[0].split() if data and data[0] else []
    seen = set(st.get("seen_uids", []))
    primera_corrida = not st.get("inicializado", False)
    nuevos = [u.decode() for u in uids if u.decode() not in seen]

    if primera_corrida:
        # No spamear el histórico: registrar todo como visto y avisar el total.
        st["seen_uids"] = [u.decode() for u in uids]
        st["inicializado"] = True
        _save_state(st)
        send_telegram(
            f"📮 Buzón contactocirrus@ conectado. {len(uids)} correos históricos en INBOX "
            "(no se notifican). A partir de ahora aviso de cada correo nuevo.",
            level="info", category="buzon",
        )
    else:
        for uid in nuevos[:20]:
            _, msg_data = M.uid("fetch", uid, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])")
            raw = b"".join(p[1] for p in msg_data if isinstance(p, tuple))
            msg = email.message_from_bytes(raw)
            send_telegram(
                "📩 *Correo nuevo en contactocirrus@*\n"
                f"De: {_decode(msg.get('From'))}\n"
                f"Asunto: {_decode(msg.get('Subject'))}\n"
                f"Fecha: {msg.get('Date', '')}\n"
                "Responder desde contactocirrus@ (SLA 4h hábiles).",
                level="warning", category="buzon",
            )
        if nuevos:
            st["seen_uids"] = [u.decode() for u in uids]
            _save_state(st)
    M.logout()
except Exception as e:
    # Fallo de login/red: avisar solo una vez por tipo de error
    key = f"err_{type(e).__name__}"
    if not st.get(key):
        send_telegram(f"📮 Buzón contactocirrus@: error de conexión IMAP: {e}", level="error", category="buzon")
        st[key] = True
        _save_state(st)
    sys.exit(1)
