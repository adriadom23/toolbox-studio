"""Helpers to display monetary amounts in Spanish format.

The SCC / PerfectMile / manifest sources return order amounts with a dot
as the decimal separator ("14.99", "385.0"). Shown as-is to a Spanish
operator, "15.00" reads as "1500" (the dot looks like a thousands
separator). :func:`format_amount_es` normalizes any input to the Spanish
convention: comma decimal, dot thousands, always 2 decimals ("15,00",
"1.234,56").
"""
from __future__ import annotations

from typing import Any

__all__ = ["format_amount_es", "parse_amount", "scc_amount_to_eur"]


def parse_amount(raw: Any) -> float | None:
    """Parse ``raw`` into a float, tolerant to ES/EN number formats.

    Handles "14.99", "15,00", "1.234,56", "1,234.56" and plain numbers.
    Returns ``None`` when the value is empty or not a number.
    """
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        try:
            value = float(raw)
        except (OverflowError, ValueError):
            return None
        # Reject NaN/inf (e.g. pandas missing values).
        if value != value or value in (float("inf"), float("-inf")):
            return None
        return value
    text = str(raw).strip()
    if not text or text.lower() in {"nan", "none", "null", "n/d", "n/a"}:
        return None
    # Strip a currency symbol / code if present ("15,00 EUR", "€ 15.00").
    cleaned = text.replace("€", " ").replace("EUR", " ").replace("eur", " ").strip()
    cleaned = cleaned.replace(" ", "")
    if not cleaned:
        return None
    if "," in cleaned and "." in cleaned:
        # The right-most separator is the decimal one.
        if cleaned.rfind(",") > cleaned.rfind("."):
            cleaned = cleaned.replace(".", "").replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")
    elif "," in cleaned:
        cleaned = cleaned.replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return None


def format_amount_es(raw: Any, *, default: str = "") -> str:
    """Return ``raw`` as a Spanish-formatted amount ("15,00", "1.234,56").

    Non-numeric input returns ``default`` when provided, otherwise the
    original text unchanged (so placeholders like "N/D" survive).
    """
    value = parse_amount(raw)
    if value is None:
        if default:
            return default
        return str(raw).strip() if raw is not None else ""
    # f"{v:,.2f}" -> "1,234.56"; swap separators to the Spanish convention.
    text = f"{value:,.2f}"
    return text.replace(",", "\x00").replace(".", ",").replace("\x00", ".")


def scc_amount_to_eur(raw: Any) -> str:
    """Normalize an SCC ``orderAmount`` (minor units) to euros.

    The SCC proxy APIs (``getDrillDownData`` / ``batchGetPackageSummary``)
    return order amounts in **cents**: ``487`` is 4,87 EUR and ``462000``
    is 4620,00 EUR. Convert once at ingestion so every consumer (Flow
    Meeting PNOVs, Dwell/Errors "Importe", SCC search CSV) sees euros.

    Defensive: a value that already carries a fractional part is assumed
    to be euros and passes through unchanged, so the helper stays correct
    if SCC ever starts sending decimal euro amounts.

    Returns a dot-decimal string with 2 decimals ("4.87"); the display
    layer renders Spanish format via :func:`format_amount_es`. Empty or
    non-numeric input returns the stripped original text ("" for empty).
    """
    text = str(raw).strip() if raw is not None else ""
    value = parse_amount(raw)
    if value is None:
        return text if text.lower() not in {"nan", "none", "null"} else ""
    if value != int(value):
        return f"{value:.2f}"
    return f"{value / 100:.2f}"
