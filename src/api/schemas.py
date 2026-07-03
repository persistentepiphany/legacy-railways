"""Pydantic request/response models for the FastAPI surface.

One model per engine dataclass — direct field-for-field mirrors. The
engine dataclasses (frozen, primitives + tuples + dicts) are already
JSON-friendly; these models exist to give the frontend an explicit
contract via FastAPI's OpenAPI schema.

The impact report is modular: compliance / anomalies / revenue / splits
are independently-computed blocks, each Optional. The frontend renders
whichever blocks are present in the response."""

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


# --- Modular blocks -------------------------------------------------------


class ComplianceBlockModel(BaseModel):
    regulated_count: int
    breach_count: int
    breaches: list[AffectedFareModel]
    regulation_map_notes: list[str]


class AnomaliesBlockModel(BaseModel):
    inversions: list[FareInversionModel]


class RevenueBlockModel(BaseModel):
    per_flow_exposure_pence: int
    per_pair_exposure_pence: int


class ODMRevenueEstimateModel(BaseModel):
    flow_id: str
    ticket_code: str
    matched_pair_count: int
    unmatched_pair_count: int
    journeys_per_period: int
    delta_pence_per_journey: int
    revenue_delta_pence: int


class ODMRevenueBlockModel(BaseModel):
    estimates: list[ODMRevenueEstimateModel]
    total_revenue_delta_pence: int
    matched_flow_count: int
    unmatched_flow_count: int
    period_label: str
    notes: list[str]


class SplitCandidateModel(BaseModel):
    intermediate_nlc: str
    ticket_code: str
    route_code: str | None
    through_price_pence: int | None
    leg1_price_pence: int | None
    leg2_price_pence: int | None
    split_total_pence: int | None
    saving_pence: int
    status: Literal["opportunity", "no_saving", "unresolvable"]
    provenance: list[ProvenanceStepModel]
    explanation: str


class SplitOpportunityResultModel(BaseModel):
    corridor_origin_nlc: str
    corridor_dest_nlc: str
    ticket_code: str
    route_code: str | None
    pre_change: list[SplitCandidateModel]
    post_change: list[SplitCandidateModel]
    created: list[SplitCandidateModel]
    closed: list[SplitCandidateModel]
    notes: list[str]


class ServicePerformanceModel(BaseModel):
    gbtt_ptd: str
    gbtt_pta: str
    origin_crs: str
    dest_crs: str
    toc_code: str
    matched_services: int
    rids: list[str]


class ServiceToleranceModel(BaseModel):
    service: ServicePerformanceModel
    percent_tolerance: list[tuple[int, float]]
    num_tolerance: list[tuple[int, int]]
    num_not_tolerance: list[tuple[int, int]]


class PerformanceResultModel(BaseModel):
    corridor_from_crs: str
    corridor_to_crs: str
    from_date: str
    to_date: str
    days: Literal["WEEKDAY", "SATURDAY", "SUNDAY"]
    services: list[ServiceToleranceModel]
    mode: Literal["live", "cached", "fixture"]
    fetched_at: str
    source_url: str | None
    notes: list[str]


class PerformanceBlockModel(BaseModel):
    result: PerformanceResultModel


class DemandEstimateModel(BaseModel):
    flow_id: str
    ticket_code: str
    flow_type: str
    ticket_segment: str
    direction: str
    elasticity: float
    elasticity_source: str
    elasticity_derived: bool
    price_base_pence: int
    yield_basis: str
    price_ratio: float
    price_change_pct: float
    gross_demand_change_pct: float
    within_validity: bool
    distance_km: float | None
    distance_method: str | None
    odm_journeys_per_period: int | None
    eligible_base_journeys: int | None
    gross_product_journeys: int | None
    abstracted_journeys: int | None
    net_new_journeys: int | None
    notes: list[str]


class DemandBlockModel(BaseModel):
    estimates: list[DemandEstimateModel]
    total_net_new_journeys: int | None
    flows_with_volume: int
    flows_percent_only: int
    validity_warnings: int
    eligible_share_assumption: float
    notes: list[str]


class CarbonEstimateModel(BaseModel):
    flow_id: str
    ticket_code: str
    net_new_journeys: int
    distance_km: float
    distance_method: str
    rail_kgco2e_per_pkm: float
    car_kgco2e_per_pkm: float
    carbon_saving_kg: float
    notes: list[str]


class CarbonBlockModel(BaseModel):
    estimates: list[CarbonEstimateModel]
    total_carbon_saving_kg: float | None
    corridor_distance_km: float | None
    corridor_distance_method: str | None
    corridor_rail_kg_per_passenger: float | None
    corridor_car_kg_per_passenger: float | None
    corridor_saving_kg_per_passenger: float | None
    rail_factor_kgco2e_per_pkm: float
    rail_factor_description: str
    car_factor_kgco2e_per_pkm: float
    traction_electric_pct: float | None
    traction_diesel_pct: float | None
    notes: list[str]


class ImpactReportModel(BaseModel):
    change: ChangeRequestModel
    canonical_affected: list[AffectedFareModel]
    skipped: list[AffectedFareModel]
    blast_radius_pairs: list[BlastRadiusPairModel]
    notes: list[str]
    compliance: ComplianceBlockModel | None = None
    anomalies: AnomaliesBlockModel | None = None
    revenue: RevenueBlockModel | None = None
    revenue_odm: ODMRevenueBlockModel | None = None
    splits: SplitOpportunityResultModel | None = None
    performance: PerformanceBlockModel | None = None
    demand: DemandBlockModel | None = None
    carbon: CarbonBlockModel | None = None


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


def perf_to_model(dc) -> PerformanceResultModel:
    """Wrap a PerformanceResult dataclass as its Pydantic mirror for /api/performance."""
    return PerformanceResultModel.model_validate(asdict(dc))


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
    "AnomaliesBlockModel",
    "ApprovalCardModel",
    "BlastRadiusPairModel",
    "ChangeRequestModel",
    "ComplianceBlockModel",
    "ComplianceVerdictModel",
    "ContradictingPairModel",
    "EscalationModel",
    "FareInversionModel",
    "ImpactReportModel",
    "ODMRevenueBlockModel",
    "ODMRevenueEstimateModel",
    "PerformanceBlockModel",
    "PerformanceResultModel",
    "ProposalOutcomeModel",
    "ProvenanceStepModel",
    "RegulationCitationModel",
    "ResolvedFareModel",
    "RevenueBlockModel",
    "ServicePerformanceModel",
    "ServiceToleranceModel",
    "SplitCandidateModel",
    "SplitOpportunityResultModel",
    "StagingLayerModel",
    "card_to_model",
    "impact_to_model",
    "layer_to_model",
    "outcome_to_model",
    "perf_to_model",
    "resolved_to_model",
]
