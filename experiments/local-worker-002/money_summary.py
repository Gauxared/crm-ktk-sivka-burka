"""Isolated cash summary benchmark.

This module is a stdlib-only benchmark. It is not production CRM code and does
not introduce business rules beyond the specified contract.
"""

_MAX_TOTAL = 9_000_000_000_000
_MAX_ELEMENT = 9_000_000_000_000


def _is_exact_int(value):
    """Return True only for exact int values (bool excluded)."""
    return type(value) is int


def _validate_total(total_minor):
    if total_minor is None:
        return
    if not _is_exact_int(total_minor):
        raise ValueError("total_minor must be None or an exact int")
    if total_minor < 0 or total_minor > _MAX_TOTAL:
        raise ValueError("total_minor out of range")


def _validate_elements(elements, name):
    if not isinstance(elements, list):
        raise ValueError(f"{name} must be a list")
    for element in elements:
        if not _is_exact_int(element):
            raise ValueError(f"{name} elements must be exact ints")
        if element < 1 or element > _MAX_ELEMENT:
            raise ValueError(f"{name} element out of range")


def summarize_cash(total_minor, receipts, refunds):
    """Summarize cash receipts and refunds.

    Args:
        total_minor: None or exact int in 0..9000000000000.
        receipts: list of exact ints in 1..9000000000000.
        refunds: list of exact ints in 1..9000000000000.

    Returns:
        dict with keys received_minor, returned_minor, net_received_minor,
        balance_minor, warning.

    Raises:
        ValueError: if any argument is invalid.
    """
    _validate_total(total_minor)
    _validate_elements(receipts, "receipts")
    _validate_elements(refunds, "refunds")

    received_minor = sum(receipts)
    returned_minor = sum(refunds)
    net_received_minor = received_minor - returned_minor

    if total_minor is None:
        balance_minor = None
    else:
        balance_minor = total_minor - net_received_minor

    warning = "DATA_REVIEW_REQUIRED" if returned_minor > received_minor else None

    return {
        "received_minor": received_minor,
        "returned_minor": returned_minor,
        "net_received_minor": net_received_minor,
        "balance_minor": balance_minor,
        "warning": warning,
    }
