"""Money in Jaah token billing and budgets: every amount is an integer of micro-USD (AS1), and this is the ONE place one is
rounded for display — half-up to the cent, on the magnitude, so a debit and the credit that reverses it show
the same figure. Pure."""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

MICROS_PER_USD = 1_000_000
MICROS_PER_CENT = 10_000
#: The minus sign a display uses (U+2212), never a hyphen.
MINUS = "−"


def cents(micros: int) -> int:
    """`micros` in whole cents, half-up on the magnitude (5 000 µ$ → 1¢, −5 000 → −1¢)."""
    magnitude = (abs(int(micros)) + MICROS_PER_CENT // 2) // MICROS_PER_CENT
    return -magnitude if micros < 0 else magnitude


def usd_text(micros: int, *, signed: bool = False) -> str:
    """`$12.34`, `−$20.00`; with `signed`, a credit reads `+$5.00`."""
    whole = cents(micros)
    sign = MINUS if whole < 0 else ("+" if signed and whole > 0 else "")
    return f"{sign}${abs(whole) // 100:,}.{abs(whole) % 100:02d}"


def usd_to_micros(usd: Decimal | float | int | str) -> int:
    """A dollar figure (a settings knob, a typed amount) in micros, half-up to the micro."""
    return int((Decimal(str(usd)) * MICROS_PER_USD).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def round_micros(value: Decimal) -> int:
    """An exact Decimal of micros rounded half-up to a whole micro (the pricing's one rounding)."""
    return int(value.quantize(Decimal(1), rounding=ROUND_HALF_UP))
