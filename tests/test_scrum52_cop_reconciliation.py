"""SCRUM-52/68 deterministic arithmetic for user-entered COP quote revisions.

Public tests use synthetic values only. The real Villa Camila quotation
fixture stays in a separately owned private path, opt-in for local acceptance.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
from collections import defaultdict
from pathlib import Path

import pytest

from app.quotation_estimate import (
    EstimateRejected,
    format_cop_es_co,
    reconcile_quotation_cop,
)


def synthetic_quote() -> dict:
    rows = []
    sums = defaultdict(int)
    for i in range(20):
        group = min(16, i+1)
        quantity = 3 if i == 2 else 1
        unit = 1000 * (i+1)
        rows.append({
            "id": f"synthetic-line-{i+1:02d}",
            "itemID": f"synthetic-item-{i+1:02d}",
            "sourceGroup": group,
            "quantity": quantity,
            "unitPriceCOP": unit,
            "lineTotalCOP": quantity * unit,
        })
        sums[str(group)] += quantity * unit
    return {
        "id": "synthetic-quote",
        "projectID": "synthetic-project",
        "customerID": "synthetic-customer",
        "status": "draft",
        "currency": "COP",
        "locale": "es-CO",
        "lines": rows,
        "groupSubtotalsCOP": dict(sums),
        "commercialTotalCOP": sum(sums.values()),
    }


def test_synthetic_20_rows_16_groups_three_unit_entry() -> None:
    result = reconcile_quotation_cop(synthetic_quote())
    assert result is not None
    assert result.row_count == 20
    assert len(result.group_subtotals_cop) == 16
    assert result.grand_total_cop == synthetic_quote()["commercialTotalCOP"]
    assert format_cop_es_co(64245000) == "$ 64.245.000"


@pytest.mark.parametrize("mutation", [
    lambda d: d["lines"][0].update(lineTotalCOP=0),
    lambda d: d["lines"][2].update(quantity=1),
    lambda d: d["lines"][0].update(unitPriceCOP=True),
    lambda d: d["lines"][0].update(unitPriceCOP=-1),
    lambda d: d["lines"][0].update(quantity=0),
    lambda d: d["lines"][0].update(quantity=1.5),
    lambda d: d["lines"][1].update(id=d["lines"][0]["id"]),
    lambda d: d["lines"][0].update(itemID=""),
    lambda d: d["lines"][0].update(sourceGroup="1"),
    lambda d: d["lines"][0].update(sourceGroup=True),
    lambda d: d["lines"][0].pop("sourceGroup"),
    lambda d: d["lines"][0].pop("lineTotalCOP"),
    lambda d: d["lines"][0].pop("unitPriceCOP"),
    lambda d: d["groupSubtotalsCOP"].update({"99": 0}),
    lambda d: d["groupSubtotalsCOP"].update({"1": 999}),
    lambda d: d["groupSubtotalsCOP"].update({"1": True}),
    lambda d: d.update(groupSubtotalsCOP={}),
    lambda d: d.update(commercialTotalCOP=d["commercialTotalCOP"]+1),
    lambda d: d.update(commercialTotalCOP=True),
    lambda d: d.update(currency="USD"),
    lambda d: d.update(locale="en-US"),
])
def test_any_commercial_ambiguity_is_rejected(mutation):
    doc=synthetic_quote()
    mutation(doc)
    with pytest.raises(EstimateRejected):
        reconcile_quotation_cop(doc)


def test_legacy_quantity_only_quotation_does_not_break_existing_contract():
    legacy = {
        "status": "draft",
        "lines": [{"id": "line-old", "itemID": "item-old", "quantity": 2}],
    }
    assert reconcile_quotation_cop(legacy) is None
    legacy["lines"][0]["sourceGroup"] = 3
    assert reconcile_quotation_cop(legacy) is None


def test_partial_financial_fields_require_complete_financial_snapshot():
    doc = {"status": "draft", "lines": [{"id": "line", "itemID": "item",
                                          "unitPriceCOP": 100}]}
    with pytest.raises(EstimateRejected):
        reconcile_quotation_cop(doc)


@pytest.mark.skipif(
    not os.getenv("VITRIAL_PRIVATE_VILLA_CAMILA_FIXTURE"),
    reason="private source fixture must be explicitly bound outside repository",
)
def test_private_villa_camila_fixture_source_totals_without_publishing_prices():
    path=Path(os.environ["VITRIAL_PRIVATE_VILLA_CAMILA_FIXTURE"])
    assert path.is_file() and not path.is_symlink()
    private_bytes=path.read_bytes()
    assert hashlib.sha256(private_bytes).hexdigest() == (
        "07d6452d29e175bea666a66f3f76f0c1099a101e2a5cc4ccf00dc1f1e75e97c1"
    )
    data=json.loads(private_bytes.decode("utf-8"))
    assert data["visibility"] == "private-company-acceptance-only"
    assert data["publicationStatus"] == "not-approved"
    assert data["currency"] == "COP"
    assert len(data["templates"]) == 16
    assert len(data["rows"]) == 20
    rows=[]
    groups=defaultdict(int)
    for row in data["rows"]:
        groups[str(row["sourceGroup"])] += row["lineTotalCOP"]
        rows.append({
            "id": row["rowID"],
            "itemID": "synthetic-private-item-"+row["rowID"],
            "sourceGroup": row["sourceGroup"],
            "quantity": row["quantity"],
            "unitPriceCOP": row["unitPriceCOP"],
            "lineTotalCOP": row["lineTotalCOP"],
        })
    commercial={
        "currency": "COP",
        "locale": "es-CO",
        "status": "draft",
        "lines": rows,
        "groupSubtotalsCOP": dict(groups),
        "commercialTotalCOP": data["commercialTotalCOP"],
    }
    checked=reconcile_quotation_cop(commercial)
    assert checked.row_count == 20
    assert len(checked.group_subtotals_cop) == 16
    assert checked.grand_total_cop == 64245000
    altered=copy.deepcopy(commercial)
    altered["lines"][2]["quantity"] = 1  # historical V3 multi-quantity
    with pytest.raises(EstimateRejected):
        reconcile_quotation_cop(altered)
