"""Deterministic, optional COP quotation arithmetic; never a supplier price engine.

Legacy quotes without entered money fields remain valid. When money exists,
validate every item line and any recorded group subtotal atomically.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass


class EstimateRejected(ValueError):
    """Client-entered quotation amount/reconciliation is not trustworthy."""


# Quantity or sourceGroup alone can exist in older drafts without a price.
MONEY_FIELDS = frozenset({"unitPriceCOP", "lineTotalCOP"})
MAX_LINES = 500
MAX_COP = 10**15


@dataclass(frozen=True)
class QuotationEstimate:
    row_count: int
    grand_total_cop: int
    group_subtotals_cop: dict[str, int]


def _cop_number(value: object, name: str, *, positive: bool = False) -> int:
    if type(value) is not int or not (0 <= value <= MAX_COP) or (positive and value == 0):
        raise EstimateRejected(f"{name} must be a bounded integer COP amount")
    return value


def reconcile_quotation_cop(payload: dict) -> QuotationEstimate | None:
    """Check arithmetic of manually entered quantities/prices, never infer costs.

    Called by shared quotation ownership validation for V1 and V2. This does
    not certify a price source or authorize publication, approval, or sending.
    """
    lines = payload.get("lines")
    if not isinstance(lines, list):
        return None  # Existing ownership validation rejects malformed/empty lines.
    money_present = (
        "commercialTotalCOP" in payload
        or "groupSubtotalsCOP" in payload
        or any(isinstance(line, dict) and bool(MONEY_FIELDS.intersection(line))
               for line in lines)
    )
    if not money_present:
        return None
    if not lines or len(lines) > MAX_LINES:
        raise EstimateRejected("priced quotation must contain a bounded number of lines")
    if payload.get("currency") != "COP" or payload.get("locale") != "es-CO":
        raise EstimateRejected("priced quotation must use es-CO/COP")
    declared_total = _cop_number(payload.get("commercialTotalCOP"), "commercialTotalCOP")
    group_totals: dict[str, int] = defaultdict(int)
    seen_rows: set[str] = set()
    total = 0
    for row in lines:
        if not isinstance(row, dict):
            raise EstimateRejected("priced line is not a JSON object")
        row_id = row.get("id")
        if not isinstance(row_id, str) or not row_id.strip() or row_id in seen_rows:
            raise EstimateRejected("priced line identity missing or duplicated")
        seen_rows.add(row_id)
        if not isinstance(row.get("itemID"), str) or not row["itemID"].strip():
            raise EstimateRejected("priced line must refer to a canonical Item")
        group = row.get("sourceGroup")
        if type(group) is not int or not (1 <= group <= MAX_LINES):
            raise EstimateRejected("priced line sourceGroup must be a bounded integer")
        quantity = _cop_number(row.get("quantity"), "quantity", positive=True)
        unit = _cop_number(row.get("unitPriceCOP"), "unitPriceCOP")
        expected = _cop_number(row.get("lineTotalCOP"), "lineTotalCOP")
        if quantity * unit != expected:
            raise EstimateRejected("lineTotalCOP does not match quantity times entered price")
        total += expected
        group_totals[str(group)] += expected
        if total > MAX_COP:
            raise EstimateRejected("quotation total exceeds bounded COP amount")
    if total != declared_total:
        raise EstimateRejected("commercialTotalCOP does not match line amounts")
    raw_groups = payload.get("groupSubtotalsCOP")
    if type(raw_groups) is not dict or not raw_groups:
        raise EstimateRejected("priced quotation requires recorded group subtotals")
    if set(raw_groups) != set(group_totals):
        raise EstimateRejected("groupSubtotalCOP keys do not match priced groups")
    for key, expected in group_totals.items():
        actual = _cop_number(raw_groups[key], "group subtotal")
        if actual != expected:
            raise EstimateRejected("group subtotal does not reconcile")
    return QuotationEstimate(
        row_count=len(lines),
        grand_total_cop=total,
        group_subtotals_cop=dict(sorted(group_totals.items(), key=lambda v: int(v[0]))),
    )


def format_cop_es_co(amount: int) -> str:
    amount = _cop_number(amount, "formatted amount")
    return f"$ {amount:,}".replace(",", ".")
