"""Pydantic request/response models for the FastAPI surface.

One model per engine dataclass — direct field-for-field mirrors. The
engine dataclasses (frozen, primitives + tuples + dicts) are already
JSON-friendly; these models exist to give the frontend an explicit
contract via FastAPI's OpenAPI schema."""

from __future__ import annotations

from dataclasses import asdict
from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field

from src.impact.change_request import ChangeRequest
from src.impact.report import ImpactReport
from src.resolver.resolve import ResolvedFare
from src.staging.types import (
    Accepted,
    ApprovalCard,
    ProposalOutcome,
    StagingLayer,
)


# --- Resolver --------------------------------------------------------------


class ProvenanceStepModel(BaseModel):
    step: str
    source: str
    detail: dict[str, str]


class ResolvedFareModel(BaseModel):
    origin_nlc: str
    dest_nlc: str
    ticket_code: str
    price_pence: int | None
    status: Literal[
        "resolved", "no_flow", "no_fare", "ambiguous", "suppressed", "contradiction",
    ]
    provenance: list[ProvenanceStepModel]


# --- ChangeRequest (also used as request body) -----------------------------


class ChangeRequestModel(BaseModel):
    kind: Literal["add_railcard"]
    railcard_code: str
    discount_pct: float
    discount_categories: list[str]
    corridor_origin_nlc: str
    corridor_dest_nlc: str
    peak_valid: bool
    description: str

    def to_dataclass(self) -> ChangeRequest:
        return ChangeRequest(
            kind=self.kind,
            railcard_code=self.railcard_code,
            discount_pct=self.discount_pct,
            discount_categories=tuple(self.discount_categories),
            corridor_origin_nlc=self.corridor_origin_nlc,
            corridor_dest_nlc=self.corridor_dest_nlc,
            peak_valid=self.peak_valid,
            description=self.description,
        )


# --- Impact ----------------------------------------------------------------


class RegulationCitationModel(BaseModel):
    section: str
    rule_text: str
    evidence: dict[str, str]


class ComplianceVerdictModel(BaseModel):
    status: Literal["compliant", "breach", "not_regulated"]
    cap_price_2025_pence: int | None
    new_price_pence: int
    citation: RegulationCitationModel | None
    explanation: str


class AffectedFareModel(BaseModel):
    flow_id: str
    ticket_code: str
    route_code: str
    representative_origin_nlc: str
    representative_dest_nlc: str
    status: Literal[
        "resolved", "no_flow", "no_fare", "ambiguous", "suppressed", "contradiction",
    ]
    old_price_pence: int | None
    new_price_pence: int | None
    discount_category: str
    provenance: list[ProvenanceStepModel]
    blast_radius_pairs: list[tuple[str, str]]
    compliance: ComplianceVerdictModel | None = None


class BlastRadiusPairModel(BaseModel):
    origin_nlc: str
    dest_nlc: str
    canonical_index: int
    expansion_reason: Literal[
        "direct", "loc_group_origin", "loc_group_dest", "loc_group_both",
    ]


class FareInversionModel(BaseModel):
    rule: str
    origin_nlc: str
    dest_nlc: str
    higher_ticket: str
    higher_price_pence: int
    lower_ticket: str
    lower_price_pence: int
    explanation: str


class ImpactReportModel(BaseModel):
    change: ChangeRequestModel
    canonical_affected: list[AffectedFareModel]
    skipped: list[AffectedFareModel]
    blast_radius_pairs: list[BlastRadiusPairModel]
    inversions: list[FareInversionModel]
    per_flow_exposure_pence: int
    per_pair_exposure_pence: int
    notes: list[str]
    regulated_count: int
    breach_count: int
    breaches: list[AffectedFareModel]
    regulation_map_notes: list[str]


# --- Staging ---------------------------------------------------------------


class ApprovalCardModel(BaseModel):
    card_id: str
    change: ChangeRequestModel
    impact: ImpactReportModel
    status: Literal["pending", "approved"]


class StagingLayerModel(BaseModel):
    pending: list[ApprovalCardModel]
    approved: list[ApprovalCardModel]
    next_card_seq: int


class ContradictingPairModel(BaseModel):
    flow_id: str
    ticket_code: str
    option_a: dict[str, str]
    option_b: dict[str, str]


class AcceptedModel(BaseModel):
    kind: Literal["accepted"] = "accepted"
    card: ApprovalCardModel
    layer: StagingLayerModel


class EscalationModel(BaseModel):
    kind: Literal["escalation"] = "escalation"
    reason: str
    contradictions: list[ContradictingPairModel]
    proposed: ChangeRequestModel
    existing_card_ids: list[str]


ProposalOutcomeModel = Annotated[
    Union[AcceptedModel, EscalationModel],
    Field(discriminator="kind"),
]


# --- Conversion helpers ----------------------------------------------------


def resolved_to_model(dc: ResolvedFare) -> ResolvedFareModel:
    return ResolvedFareModel.model_validate(asdict(dc))


def impact_to_model(dc: ImpactReport) -> ImpactReportModel:
    return ImpactReportModel.model_validate(asdict(dc))


def card_to_model(dc: ApprovalCard) -> ApprovalCardModel:
    return ApprovalCardModel.model_validate(asdict(dc))


def layer_to_model(dc: StagingLayer) -> StagingLayerModel:
    return StagingLayerModel.model_validate(asdict(dc))


def outcome_to_model(dc: ProposalOutcome) -> AcceptedModel | EscalationModel:
    if isinstance(dc, Accepted):
        d = asdict(dc)
        d["kind"] = "accepted"
        return AcceptedModel.model_validate(d)
    d = asdict(dc)
    d["kind"] = "escalation"
    return EscalationModel.model_validate(d)


__all__ = [
    "AcceptedModel",
    "AffectedFareModel",
    "ApprovalCardModel",
    "BlastRadiusPairModel",
    "ChangeRequestModel",
    "ComplianceVerdictModel",
    "ContradictingPairModel",
    "EscalationModel",
    "FareInversionModel",
    "ImpactReportModel",
    "ProposalOutcomeModel",
    "ProvenanceStepModel",
    "RegulationCitationModel",
    "ResolvedFareModel",
    "StagingLayerModel",
    "card_to_model",
    "impact_to_model",
    "layer_to_model",
    "outcome_to_model",
    "resolved_to_model",
]
