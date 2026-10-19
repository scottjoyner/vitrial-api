"""SCRUM-51: opt-in, text-first, job-specific finished-Item draft.

A template reference is never catalog publication authority. This contract
does not derive a manufacturing BOM or infer supplier pricing/inventory.
Legacy Item payloads without jobItemDraft stay backward-compatible.
"""
from __future__ import annotations

from decimal import Decimal
import math
from typing import Literal

from pydantic import Field, field_validator, model_validator

from app.schemas import StrictModel


class ItemDraftComponent(StrictModel):
    id: str = Field(min_length=1, max_length=128, strict=True)
    kind: Literal["glass", "aluminum", "hardware", "accessory", "steel", "other"]
    name: str = Field(min_length=1, max_length=256, strict=True)
    quantity: str | None = Field(
        default=None,
        pattern=r"^(?:0|[1-9][0-9]{0,5})(?:\.[0-9]{1,6})?$",
        max_length=13,
        strict=True,
    )
    unit: Literal["each", "m", "m2", "kg"] | None = None

    @model_validator(mode="after")
    def positive_component_quantity(self) -> "ItemDraftComponent":
        if (self.quantity is None) != (self.unit is None):
            raise ValueError("material descriptor needs both quantity and unit")
        if self.quantity is not None and Decimal(self.quantity) <= 0:
            raise ValueError("material descriptor quantity must be positive")
        return self


class OpeningDraft(StrictModel):
    kind: Literal["rectangular", "corner", "l_shape"]
    heightCm: float = Field(gt=0, le=100000, allow_inf_nan=False)
    widthCm: float | None = Field(default=None, gt=0, le=100000, allow_inf_nan=False)
    legsCm: list[float] | None = Field(default=None, min_length=2, max_length=2)

    @model_validator(mode="after")
    def legal_manual_geometry(self) -> "OpeningDraft":
        if self.kind == "rectangular":
            if self.widthCm is None or self.legsCm is not None:
                raise ValueError("rectangular opening requires width only")
        else:
            if self.widthCm is not None or self.legsCm is None:
                raise ValueError("corner/L opening requires exactly two manual legs")
            if any(type(x) is bool or x <= 0 or x > 100000
                   or not math.isfinite(x) for x in self.legsCm):
                raise ValueError("manual leg length is not positive/finite")
        return self


class JobItemDraftV1(StrictModel):
    schemaVersion: Literal["vitrial.job-item-draft.v1"]
    itemID: str = Field(min_length=1, max_length=256, strict=True)
    projectID: str = Field(min_length=1, max_length=256, strict=True)
    projectSectorID: str = Field(min_length=1, max_length=256, strict=True)
    nameEsCO: str = Field(min_length=1, max_length=512, strict=True)
    location: str | None = Field(default=None, max_length=256, strict=True)
    reference: str | None = Field(default=None, max_length=128, strict=True)
    quantity: int = Field(strict=True, ge=1, le=100000)
    templateID: str | None = Field(default=None, min_length=1, max_length=256, strict=True)
    templateVersion: int | None = Field(default=None, strict=True, ge=1)
    templateReviewStatus: Literal["pending_customer_review"]
    sourceGroup: int | None = Field(default=None, strict=True, ge=1, le=500)
    opening: OpeningDraft | None = None
    components: list[ItemDraftComponent] = Field(default_factory=list, max_length=32)
    notes: str | None = Field(default=None, max_length=2048, strict=True)

    @model_validator(mode="after")
    def no_bogus_template_identity(self) -> "JobItemDraftV1":
        if (self.templateID is None) != (self.templateVersion is None):
            raise ValueError("draft requires template identity and version together")
        ids = [part.id for part in self.components]
        if len(ids) != len(set(ids)):
            raise ValueError("draft component ids must be unique")
        return self


def validate_job_item_draft(
    payload: dict,
    *,
    item_id: str,
    project_id: str,
    project_sector_id: str,
) -> JobItemDraftV1 | None:
    """Validate optional draft plus its three server-verified owning identities.

    This is NOT an approval or promotion path. In particular, disallow embedded
    prices, supplier SKUs, stock/warehouse quantities and inferred fabrication
    geometry (all are forbidden by the strict nested models).
    """
    raw = payload.get("jobItemDraft")
    if raw is None and "jobItemDraft" not in payload:
        return None
    if not isinstance(raw, dict):
        raise ValueError("jobItemDraft must be an object")
    try:
        result = JobItemDraftV1.model_validate(raw)
    except ValueError as exc:
        raise ValueError("jobItemDraft schema invalid") from exc
    if (result.itemID, result.projectID, result.projectSectorID) != (
        item_id, project_id, project_sector_id,
    ):
        raise ValueError("jobItemDraft ownership does not match canonical Item")

    # The flat Item representation is an older sync contract. When a draft
    # is present it must not contradict the manually entered source geometry.
    # Otherwise V1 or V2 API clients could bypass iOS request validation.
    if "name" in payload and payload["name"] != result.nameEsCO:
        raise ValueError("Item name disagrees with its draft name")
    if "quantity" in payload and (
        type(payload["quantity"]) is not int or payload["quantity"] != result.quantity
    ):
        raise ValueError("Item quantity disagrees with its draft quantity")
    if result.opening is not None:
        def _matches_flat_cm(value: object, expected: float) -> bool:
            if not isinstance(value, dict) or set(value) != {"value", "unit"}:
                return False
            amount = value["value"]
            return (
                type(amount) in (int, float)
                and math.isfinite(amount)
                and value["unit"] == "cm"
                and float(amount) == expected
            )

        if payload.get("height") is not None and not _matches_flat_cm(
            payload["height"], result.opening.heightCm
        ):
            raise ValueError("Item height disagrees with manually entered opening")
        if result.opening.kind == "rectangular":
            if payload.get("width") is not None and not _matches_flat_cm(
                payload["width"], result.opening.widthCm
            ):
                raise ValueError("Item width disagrees with rectangular opening")
        elif payload.get("width") is not None:
            raise ValueError("corner/L Item must not carry a rectangular width")
    return result
