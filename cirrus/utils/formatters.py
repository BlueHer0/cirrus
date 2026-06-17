"""Helpers de formato presentacional (centralizados).

Reglas:
- Solo afectan presentacion. NO modifican valores ni hacen aritmetica.
- Negativos: signo ANTES del simbolo ($), tipografia estandar mexicana.
- Separador de miles: coma. Punto decimal.
- None y errores se mapean a "$0.00" (no levantan).
"""

from decimal import Decimal, InvalidOperation


def fmt_mxn(amount, decimals: int = 2) -> str:
    """Formatea un monto en pesos mexicanos con separadores de miles.

    Examples:
        >>> fmt_mxn(249915.70)
        '$249,915.70'
        >>> fmt_mxn(-32741.08)
        '-$32,741.08'
        >>> fmt_mxn(0)
        '$0.00'
        >>> fmt_mxn(None)
        '$0.00'
        >>> fmt_mxn(1234567.89, decimals=0)
        '$1,234,568'
        >>> fmt_mxn(Decimal('123.4'))
        '$123.40'
    """
    if amount is None:
        amount = 0
    try:
        val = Decimal(str(amount))
    except (InvalidOperation, TypeError, ValueError):
        return "$0." + ("0" * decimals) if decimals else "$0"
    sign = "-" if val < 0 else ""
    val = abs(val)
    if decimals > 0:
        body = f"{val:,.{decimals}f}"
    else:
        body = f"{val:,.0f}"
    return f"{sign}${body}"


# Tests basicos — se ejecutan con: python -m doctest cirrus/utils/formatters.py
def _run_tests():
    cases = [
        (fmt_mxn(249915.70), "$249,915.70"),
        (fmt_mxn(-32741.08), "-$32,741.08"),
        (fmt_mxn(0), "$0.00"),
        (fmt_mxn(None), "$0.00"),
        (fmt_mxn(Decimal("1234567.89")), "$1,234,567.89"),
        # 0.005 -> ROUND_HALF_EVEN de Python f-string => 0.00 (bankers).
        (fmt_mxn(Decimal("0.005")), "$0.00"),
        (fmt_mxn(1234567.89, decimals=0), "$1,234,568"),
        (fmt_mxn(-0.50), "-$0.50"),
        (fmt_mxn("abc"), "$0.00"),  # input invalido
    ]
    for got, expected in cases:
        assert got == expected, f"FAIL: got {got!r} expected {expected!r}"
    print("OK: 9/9 casos pasan")


if __name__ == "__main__":
    _run_tests()
