"""ISO currency conversion and exact totals in integer minor units."""

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext

# SIX ISO 4217 List One. Codes without decimal minor units are excluded.
_CURRENCIES = {
    scale: frozenset(codes.split())
    for scale, codes in {
        0: "BIF CLP DJF GNF ISK JPY KMF KRW PYG RWF UGX UYI VND VUV XAF XOF XPF",
        2: """AED AFN ALL AMD AOA ARS AUD AWG AZN BAM BBD BDT BMD BND BOB BOV BRL BSD BTN BWP BYN
        BZD CAD CDF CHE CHF CHW CNY COP COU CRC CUP CVE CZK DKK DOP DZD EGP ERN ETB EUR FJD FKP GBP
        GEL GHS GIP GMD GTQ GYD HKD HNL HTG HUF IDR ILS INR IRR JMD KES KGS KHR KPW KYD KZT LAK
        LBP LKR LRD LSL MAD MDL MGA MKD MMK MNT MOP MRU MUR MVR MWK MXN MXV MYR MZN NAD NGN NIO
        NOK NPR NZD PAB PEN PGK PHP PKR PLN QAR RON RSD RUB SAR SBD SCR SDG SEK SGD SHP SLE SOS
        SRD SSP STN SVC SYP SZL THB TJS TMT TOP TRY TTD TWD TZS UAH USD USN UYU UZS VED VES WST
        XAD XCD XCG YER ZAR ZMW ZWG""",
        3: "BHD IQD JOD KWD LYD OMR TND",
        4: "CLF UYW",
    }.items()
}


def currency_scale(currency: str) -> int:
    """Return the decimal minor-unit exponent for a supported currency."""
    for scale, codes in _CURRENCIES.items():
        if currency in codes:
            return scale
    raise ValueError(f"Unsupported currency: {currency!r}.")


def decimal(value: str | int | float | Decimal) -> Decimal:
    """Parse a bounded finite decimal, preserving textual decimal arithmetic."""
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("Expected a finite decimal number.") from exc
    if not result.is_finite() or result.copy_abs() > 10**12 or result.as_tuple().exponent < -12:
        raise ValueError("Number is outside the supported range or precision.")
    return result


def round_minor(value: Decimal) -> int:
    """Round a minor-unit amount half up and keep it within SQLite's range."""
    result = int(value.to_integral_value(rounding=ROUND_HALF_UP))
    if abs(result) > 10**16:
        raise ValueError("Money amount is too large.")
    return result


def minor(value: str | int | float | Decimal, currency: str) -> int:
    """Convert a major-unit amount to an integer minor-unit amount."""
    with localcontext() as ctx:
        ctx.prec = 50
        return round_minor(decimal(value).scaleb(currency_scale(currency)))


def major(value: int, currency: str) -> str:
    """Format minor units as a fixed-scale decimal for people and CSV."""
    with localcontext() as ctx:
        ctx.prec = 50
        return format(Decimal(value).scaleb(-currency_scale(currency)), "f")


def line_total(quantity: Decimal, unit_price_minor: int) -> int:
    """Calculate a line total from an exact quantity and a stored unit price."""
    with localcontext() as ctx:
        ctx.prec = 50
        return round_minor(quantity * unit_price_minor)


def totals(lines: list[dict], tax_rate: Decimal) -> dict[str, int]:
    """Sum validated lines and apply the owner's flat tax rate once."""
    subtotal = sum(line["total_minor"] for line in lines)
    with localcontext() as ctx:
        ctx.prec = 50
        tax = round_minor(Decimal(subtotal) * tax_rate / 100)
    total = round_minor(Decimal(subtotal + tax))
    return {"subtotal_minor": subtotal, "tax_minor": tax, "total_minor": total}
