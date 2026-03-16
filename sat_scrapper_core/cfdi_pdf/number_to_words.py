"""Convierte montos numericos a texto en espanol para MXN."""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

_UNIDADES = ["", "UNO", "DOS", "TRES", "CUATRO", "CINCO", "SEIS", "SIETE", "OCHO", "NUEVE"]

_DECENAS = [
    "", "DIEZ", "VEINTE", "TREINTA", "CUARENTA", "CINCUENTA",
    "SESENTA", "SETENTA", "OCHENTA", "NOVENTA",
]

_CENTENAS = [
    "", "CIENTO", "DOSCIENTOS", "TRESCIENTOS", "CUATROCIENTOS", "QUINIENTOS",
    "SEISCIENTOS", "SETECIENTOS", "OCHOCIENTOS", "NOVECIENTOS",
]

_TEENS = {
    11: "ONCE", 12: "DOCE", 13: "TRECE", 14: "CATORCE", 15: "QUINCE",
    16: "DIECISEIS", 17: "DIECISIETE", 18: "DIECIOCHO", 19: "DIECINUEVE",
}

_TWENTIES = {
    21: "VEINTIUN", 22: "VEINTIDOS", 23: "VEINTITRES", 24: "VEINTICUATRO",
    25: "VEINTICINCO", 26: "VEINTISEIS", 27: "VEINTISIETE", 28: "VEINTIOCHO",
    29: "VEINTINUEVE",
}


def numero_a_letra_mxn(valor) -> str:
    """Convierte un monto a texto en espanol para MXN."""
    cuantia = Decimal(str(valor)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    entero = int(cuantia)
    centavos = int((cuantia - entero) * 100)

    if entero == 0:
        palabras = "CERO"
    else:
        palabras = _seccion_millones(entero)

    palabras += " PESO" if entero == 1 else " PESOS"
    palabras += f" {centavos:02d}/100 M.N."
    return palabras


def _seccion_millones(numero: int) -> str:
    millones = numero // 1_000_000
    resto = numero % 1_000_000
    partes: list[str] = []
    if millones > 0:
        if millones == 1:
            partes.append("UN MILLON")
        else:
            partes.append(f"{_seccion_miles(millones)} MILLONES")
    if resto > 0:
        partes.append(_seccion_miles(resto))
    return " ".join(partes).strip()


def _seccion_miles(numero: int) -> str:
    miles = numero // 1000
    resto = numero % 1000
    partes: list[str] = []
    if miles > 0:
        if miles == 1:
            partes.append("MIL")
        else:
            partes.append(f"{_seccion_cientos(miles)} MIL")
    if resto > 0:
        partes.append(_seccion_cientos(resto))
    return " ".join(partes).strip()


def _seccion_cientos(numero: int) -> str:
    if numero == 100:
        return "CIEN"
    centenas = numero // 100
    resto = numero % 100
    partes: list[str] = []
    if centenas > 0:
        partes.append(_CENTENAS[centenas])
    if resto > 0:
        partes.append(_seccion_decenas(resto))
    return " ".join(partes).strip()


def _seccion_decenas(numero: int) -> str:
    if 10 < numero < 20:
        return _TEENS[numero]
    if 20 < numero < 30:
        return _TWENTIES[numero]
    decenas = numero // 10
    unidades = numero % 10
    if numero <= 10:
        return _numeros_basicos(numero)
    base = _DECENAS[decenas]
    if unidades == 0:
        return base
    return f"{base} Y {_numeros_basicos(unidades)}"


def _numeros_basicos(numero: int) -> str:
    if numero == 0:
        return "CERO"
    if numero == 1:
        return "UN"
    return _UNIDADES[numero]
