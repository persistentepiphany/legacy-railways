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
from src.routeing.engine import ValidityVerdict
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
    raw_record: str | None = None


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


class _ChangeShared(BaseModel):
    """Fields common to every ChangeRequest kind. Kind-specific fields live
    on the variants below; FastAPI's discriminated-union parser routes on
    the `kind` literal so unknown kinds surface as 422 at the boundary."""
    corridor_origin_nlc: str
    corridor_dest_nlc: str
    peak_valid: bool
    description: str
    rounding_rule: Literal["near5", "near10", "down10", "none"] | None = None
    min_floor_pct: float | None = None
    cluster_name: str | None = None
    contradiction_choice: dict[str, Literal["A", "B"]] | None = None
    scope: Literal["corridor", "toc"] = "corridor"
    toc_code: str | None = None


class AddRailcardChangeModel(_ChangeShared):
    kind: Literal["add_railcard"]
    railcard_code: str
    discount_pct: float
    discount_categories: list[str]

    def to_dataclass(self) -> ChangeRequest:
        return ChangeRequest(
            kind="add_railcard",
            railcard_code=self.railcard_code,
            discount_pct=self.discount_pct,
            discount_categories=tuple(self.discount_categories),
            corridor_origin_nlc=self.corridor_origin_nlc,
            corridor_dest_nlc=self.corridor_dest_nlc,
            peak_valid=self.peak_valid,
            description=self.description,
            rounding_rule=self.rounding_rule,
            min_floor_pct=self.min_floor_pct,
            cluster_name=self.cluster_name,
            contradiction_choice=self.contradiction_choice,
            scope=self.scope,
            toc_code=self.toc_code,
        )


class RaisePriceChangeModel(_ChangeShared):
    kind: Literal["raise_price"]
    railcard_code: str
    discount_pct: float          # reused as the INCREASE fraction (0.05 = +5%)
    discount_categories: list[str]

    def to_dataclass(self) -> ChangeRequest:
        return ChangeRequest(
            kind="raise_price",
            railcard_code=self.railcard_code,
            discount_pct=self.discount_pct,
            discount_categories=tuple(self.discount_categories),
            corridor_origin_nlc=self.corridor_origin_nlc,
            corridor_dest_nlc=self.corridor_dest_nlc,
            peak_valid=self.peak_valid,
            description=self.description,
            rounding_rule=self.rounding_rule,
            min_floor_pct=self.min_floor_pct,
            cluster_name=self.cluster_name,
            contradiction_choice=self.contradiction_choice,
            scope=self.scope,
            toc_code=self.toc_code,
        )


class ApplyCapChangeModel(_ChangeShared):
    kind: Literal["apply_cap"]
    cap_pct: float

    def to_dataclass(self) -> ChangeRequest:
        return ChangeRequest(
            kind="apply_cap",
            cap_pct=self.cap_pct,
            corridor_origin_nlc=self.corridor_origin_nlc,
            corridor_dest_nlc=self.corridor_dest_nlc,
            peak_valid=self.peak_valid,
            description=self.description,
            rounding_rule=self.rounding_rule,
            min_floor_pct=self.min_floor_pct,
            cluster_name=self.cluster_name,
            contradiction_choice=self.contradiction_choice,
            scope=self.scope,
            toc_code=self.toc_code,
        )


class AdjustFaresChangeModel(_ChangeShared):
    kind: Literal["adjust_fares"]
    tickets: list[str]
    delta_mode: Literal["pct", "pence"]
    delta_value: float

    def to_dataclass(self) -> ChangeRequest:
        return ChangeRequest(
            kind="adjust_fares",
            tickets=tuple(self.tickets),
            delta_mode=self.delta_mode,
            delta_value=self.delta_value,
            corridor_origin_nlc=self.corridor_origin_nlc,
            corridor_dest_nlc=self.corridor_dest_nlc,
            peak_valid=self.peak_valid,
            description=self.description,
            rounding_rule=self.rounding_rule,
            min_floor_pct=self.min_floor_pct,
            cluster_name=self.cluster_name,
            contradiction_choice=self.contradiction_choice,
            scope=self.scope,
            toc_code=self.toc_code,
        )


class WithdrawProductChangeModel(_ChangeShared):
    kind: Literal["withdraw_product"]
    withdraw_ticket: str
    confirmed: bool

    def to_dataclass(self) -> ChangeRequest:
        return ChangeRequest(
            kind="withdraw_product",
            withdraw_ticket=self.withdraw_ticket,
            confirmed=self.confirmed,
            corridor_origin_nlc=self.corridor_origin_nlc,
            corridor_dest_nlc=self.corridor_dest_nlc,
            peak_valid=self.peak_valid,
            description=self.description,
            rounding_rule=self.rounding_rule,
            min_floor_pct=self.min_floor_pct,
            cluster_name=self.cluster_name,
            contradiction_choice=self.contradiction_choice,
            scope=self.scope,
            toc_code=self.toc_code,
        )


ChangeRequestModel = Annotated[
    Union[
        AddRailcardChangeModel,
        RaisePriceChangeModel,
        ApplyCapChangeModel,
        AdjustFaresChangeModel,
        WithdrawProductChangeModel,
    ],
    Field(discriminator="kind"),
]


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
    representative_origin_name: str = ""
    representative_dest_name: str = ""
    blast_station_nlcs: list[str] = []


class BlastRadiusPairModel(BaseModel):
    origin_nlc: str
    dest_nlc: str
    canonical_index: int
    expansion_reason: Literal[
        "direct", "loc_group_origin", "loc_group_dest", "loc_group_both",
        "fsc_cluster_origin", "fsc_cluster_dest", "fsc_cluster_both",
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
    partial: bool = False  # True when joined over retained top-N rows only


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
    adoption_share: float | None = None
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


class ScopeStatsModel(BaseModel):
    """Scale bookkeeping for the affected set (see ScopeStats dataclass):
    at operator scope aggregates run over the full set but only top-N
    detailed rows are returned — these counters expose the cut."""
    scope: Literal["corridor", "toc"]
    toc_code: str | None
    flows_total: int
    flows_actual: int
    flows_generated_skipped: int
    canonical_total: int
    canonical_returned: int
    blast_pairs_total: int
    blast_pairs_returned: int
    truncated: bool
    toc_station_nlcs: list[str] = []


class ImpactReportModel(BaseModel):
    change: ChangeRequestModel
    canonical_affected: list[AffectedFareModel]
    skipped: list[AffectedFareModel]
    blast_radius_pairs: list[BlastRadiusPairModel]
    notes: list[str]
    scope_stats: ScopeStatsModel | None = None
    compliance: ComplianceBlockModel | None = None
    anomalies: AnomaliesBlockModel | None = None
    revenue: RevenueBlockModel | None = None
    revenue_odm: ODMRevenueBlockModel | None = None
    splits: SplitOpportunityResultModel | None = None
    performance: PerformanceBlockModel | None = None
    demand: DemandBlockModel | None = None
    carbon: CarbonBlockModel | None = None


# --- Corridor statistics ----------------------------------------------------


class CorridorStatsModel(BaseModel):
    """Route fact sheet for the Statistics tab. Every figure carries its
    basis in `notes`; absent data is None + a note, never a guess."""
    origin_crs: str
    dest_crs: str
    # Timetable (RSPS5046 CIF) — distinct schedules, ever-calls semantics.
    train_count: int | None = None
    electric_pct: float | None = None
    diesel_pct: float | None = None
    intermediate_call_count: int | None = None
    timetable_source: str | None = None
    # ODM (ORR origin-destination matrix) — journeys per publication period.
    odm_journeys_out: int | None = None
    odm_journeys_back: int | None = None
    odm_period_label: str | None = None
    implied_yield_pence: int | None = None
    # Geometry + per-passenger carbon (independent of any change).
    distance_km: float | None = None
    distance_method: str | None = None
    rail_kgco2e_per_journey: float | None = None
    car_kgco2e_per_journey: float | None = None
    carbon_saving_per_journey_kg: float | None = None
    notes: list[str]


class OverviewKeyFareModel(BaseModel):
    """One headline baseline fare on an overview row."""
    ticket_code: str
    description: str
    price_pence: int
    label: str  # "default" | "cheapest" | "dearest"


class OverviewCorridorModel(BaseModel):
    """One row of the network overview: baseline pricing, structural
    aberrations, service level and in-flight staged activity for a curated
    corridor. Baseline figures are computed once at startup (the baseline is
    immutable within a session); staging counts are overlaid per request."""
    id: str
    name: str
    sub: str | None = None
    toc: str | None = None
    origin_crs: str
    dest_crs: str
    origin_nlc: str
    dest_nlc: str
    key_fares: list[OverviewKeyFareModel]
    fares_scanned: int
    aberration_count: int
    aberrations: list[FareInversionModel]
    train_count: int | None = None
    odm_journeys_out: int | None = None
    odm_journeys_back: int | None = None
    pending_changes: int = 0
    approved_changes: int = 0
    notes: list[str]


class OverviewModel(BaseModel):
    ready: bool
    computed_at: str | None = None
    odm_period_label: str | None = None
    timetable_source: str | None = None
    corridors: list[OverviewCorridorModel]
    notes: list[str]


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


# --- Metadata surface (snapshot / corridors / stations / railcards) --------


class SnapshotModel(BaseModel):
    id: str
    date: str            # YYYY-MM-DD, best-effort from the FFL header
    feed: str            # e.g. "RJFAF"
    sequence: str        # e.g. "805"
    records: int         # wc -l of the .FFL
    generated_at: str    # from feed header; empty string if not parseable
    set_kind: str        # "full refresh (F)" | "changes only (C)" | ""


class CorridorModel(BaseModel):
    id: str
    name: str
    sub: str
    origin_crs: str
    origin_nlc: str
    dest_crs: str
    dest_nlc: str
    default_ticket: str
    toc: str
    path_crs: list[str]


class RouteModel(BaseModel):
    """Timetable-derived route for a free-form OD pair. `found=False` is a
    typed miss (no through service / no fares NLC) — never a fabricated path."""
    found: bool
    reason: str | None = None
    origin_crs: str
    dest_crs: str
    origin_nlc: str | None = None
    dest_nlc: str | None = None
    name: str = ""
    sub: str = ""
    path_crs: list[str] = []
    direct_trains: int = 0
    reversed_path: bool = False   # path taken from the opposite-direction service
    source: str = ""


class StationModel(BaseModel):
    crs: str
    nlc: str | None       # None when MSN carries the station but LOC has no matching CRS
    name: str
    x: float
    y: float
    easting: int
    northing: int


class TocModel(BaseModel):
    """One fare-TOC for the operator-scope picker. Counts and station list
    are None until the FFL indexes are warm — the endpoint never triggers
    the ~30s parse on the request thread."""
    code: str                        # 3-char fare-TOC code from .FFL (e.g. 'NTH')
    toc_2char: str | None            # 2-char timetable code from .TOC, if known
    name: str | None                 # operator name from .TOC, if known
    flow_count: int | None           # all flows carrying this code
    actual_flow_count: int | None    # usage_code 'A' only (what impact iterates)
    station_nlcs: list[str] = []     # deduped O/D NLCs of 'A' flows, capped


class TicketMetaModel(BaseModel):
    """One .TTY row surfaced to the Author's Adjust-fares / Withdraw-product
    forms. `tkt_class` / `tkt_type` / `tkt_group` are the 1-char discriminators
    the UI uses to group tickets (First vs Standard, single vs return vs
    season, …). `discount_category` is included so a caller who is planning an
    add_railcard proposal can cross-reference the same list."""
    code: str
    description: str
    tkt_class: str
    tkt_type: str
    tkt_group: str
    discount_category: str


class RailcardMetaModel(BaseModel):
    code: str
    display: str
    hint_pct: float       # suggested discount percentage 0..100
    off_peak_only: bool
    sub: str
    national: bool
    in_feed: bool         # True if the code appears in the current .RLC snapshot


# --- Conversion helpers ----------------------------------------------------


def resolved_to_model(dc: ResolvedFare) -> ResolvedFareModel:
    return ResolvedFareModel.model_validate(asdict(dc))


def impact_to_model(dc: ImpactReport) -> ImpactReportModel:
    return ImpactReportModel.model_validate(asdict(dc))


def perf_to_model(dc) -> PerformanceResultModel:
    """Wrap a PerformanceResult dataclass as its Pydantic mirror for /api/performance."""
    return PerformanceResultModel.model_validate(asdict(dc))


# --- Routeing / Validity ---------------------------------------------------


class RouteingProvenanceLineModel(BaseModel):
    step: str
    source: str
    detail: dict[str, str]


class PermittedRouteModel(BaseModel):
    start_routeing_point: str
    end_routeing_point: str
    map_sequence: list[str]


class EasementMatchModel(BaseModel):
    easement_ref: str
    outcome: Literal["match", "no_match", "excepted", "outside_window"]
    is_positive: bool
    reasons: list[str]


class ValidityVerdictModel(BaseModel):
    """Return shape of GET /api/validity.

    `status` is the engine's typed verdict — the frontend switches the UI
    off this (green/red/amber/escalate), never a naked string comparison."""
    status: Literal[
        "permitted",
        "permitted_by_easement",
        "not_permitted",
        "denied_by_easement",
        "contradiction",
        "unknown_no_data",
        "unknown_origin",
        "unknown_dest",
    ]
    query: dict
    origin_routeing_points: list[str]
    dest_routeing_points: list[str]
    permitted_routes: list[PermittedRouteModel]
    firing_positive: list[EasementMatchModel]
    firing_negative: list[EasementMatchModel]
    considered_easement_refs: list[str]
    easement_texts: dict[str, str]
    provenance: list[RouteingProvenanceLineModel]
    notes: list[str]


def validity_to_model(vv: ValidityVerdict) -> ValidityVerdictModel:
    return ValidityVerdictModel.model_validate(asdict(vv))


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
    "CorridorStatsModel",
    "OverviewCorridorModel",
    "OverviewKeyFareModel",
    "OverviewModel",
    "EasementMatchModel",
    "EscalationModel",
    "FareInversionModel",
    "ImpactReportModel",
    "ODMRevenueBlockModel",
    "ODMRevenueEstimateModel",
    "PerformanceBlockModel",
    "PerformanceResultModel",
    "PermittedRouteModel",
    "ProposalOutcomeModel",
    "ProvenanceStepModel",
    "RegulationCitationModel",
    "ResolvedFareModel",
    "RevenueBlockModel",
    "RouteingProvenanceLineModel",
    "ScopeStatsModel",
    "ServicePerformanceModel",
    "ServiceToleranceModel",
    "SplitCandidateModel",
    "SplitOpportunityResultModel",
    "StagingLayerModel",
    "TocModel",
    "ValidityVerdictModel",
    "card_to_model",
    "impact_to_model",
    "layer_to_model",
    "outcome_to_model",
    "perf_to_model",
    "resolved_to_model",
    "validity_to_model",
]
