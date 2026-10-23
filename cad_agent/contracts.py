"""Versioned, policy-free contracts shared by all application layers."""

from typing import Annotated, Any, Literal
from pydantic import BaseModel, Field, ConfigDict, FiniteFloat, model_validator
from hashlib import sha256


def stable_id(prefix: str, *parts: str) -> str:
    return prefix + "_" + sha256("\x1f".join(parts).encode()).hexdigest()[:24]


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Diagnostic(Record):
    code: str
    message: str
    source_id: str | None = None


class Entity(Record):
    entity_id: str
    entity_path: list[str]
    layout_id: str
    kind: str
    text: str = ""
    bbox: list[float]
    height: float = 1
    rotation: float = 0
    origin: list[float] = Field(default_factory=list)
    layer: str = ""
    bbox_method: str = "font_estimate"
    block_name: str = ""


class HighlightRegion(Record):
    """Display boundary in DXF WCS; holes remain unfilled, never measurement input."""

    boundary: list[tuple[FiniteFloat, FiniteFloat]] = Field(min_length=3, max_length=16384)
    holes: list[Annotated[list[tuple[FiniteFloat, FiniteFloat]], Field(min_length=3, max_length=16384)]] = Field(default_factory=list, max_length=512)

    @model_validator(mode="after")
    def valid_outline(self):
        if len(self.boundary) + sum(map(len, self.holes)) > 16384:
            raise ValueError("Highlight point limit exceeded")
        from shapely.geometry import Polygon
        shape = Polygon(self.boundary, self.holes)
        if not shape.is_valid or shape.area <= 0:
            raise ValueError("Highlight requires a valid polygon with interior holes")
        return self


class SourcePart(Record):
    entity_id: str
    text_span: list[int]
    entity_text_span: list[int]
    bbox: list[float]
    highlight_regions: list[HighlightRegion] = Field(default_factory=list, max_length=512, exclude_if=lambda value: not value)


class SourceRegion(Record):
    region_id: str
    layout_id: str
    frame_id: str
    frame_name: str
    kind: Literal["paragraph", "table_row", "text"]
    text: str
    entity_ids: list[str]
    bbox: list[float]
    highlight_regions: list[HighlightRegion] = Field(default_factory=list, max_length=512, exclude_if=lambda value: not value)
    context_region_ids: list[str] = Field(default_factory=list)
    parts: list[SourcePart] = Field(default_factory=list)
    column_key: str | None = None
    table_row_key: str | None = None


class Evidence(Record):
    evidence_id: str
    snapshot_id: str
    region_id: str
    layout_id: str
    frame_id: str
    quote: str
    text_span: list[int]
    entity_ids: list[str]
    bbox: list[float]
    component_boxes: list[list[float]] = Field(default_factory=list)
    highlight_regions: list[HighlightRegion] = Field(default_factory=list, max_length=512, exclude_if=lambda value: not value)
    source_parts: list[SourcePart] = Field(default_factory=list)
    coordinate_space: str = "dxf_wcs"
    unit: str
    precision: Literal["entity", "region", "cell"] = "entity"
    binding_method: str = "exact_source_span"
    verification_status: str = "source_bound"
    numeric_readings: dict = Field(default_factory=dict, exclude_if=lambda value: not value)


class FactValue(Record):
    value: Any = None
    state: Literal["observed", "derived", "unknown", "conflict"] = "unknown"
    evidence_ids: list[str] = Field(default_factory=list)
    derivation: str | None = None


class MasonryPractice(Record):
    practice_id: str
    fact_schema: str = "building.masonry.v1"
    snapshot_id: str
    source_occurrence_id: str
    source_region_id: str
    layout_id: str
    frame_id: str
    frame_name: str
    original_text: str
    attributes: dict[str, FactValue]
    evidence_ids: list[str]
    missing_fields: list[str]
    conflicts: list[str] = Field(default_factory=list)
    review_status: Literal["source_bound", "needs_review"]


class Coverage(Record):
    scope: list[str]
    source_units_seen: int = 0
    source_units_resolved: int = 0
    candidate_units: int = 0
    unresolved_region_ids: list[str] = Field(default_factory=list)
    skipped_region_ids: list[str] = Field(default_factory=list)
    completeness: Literal["bounded", "partial", "unknown"] = "bounded"
    note: str = "统计仅覆盖当前解析出的文字区域，不证明整图语义完整。"


class ExtractionRequest(Record):
    snapshot_id: str
    extractor_ref: Literal["building.masonry@1"] = "building.masonry@1"
    mode: Literal["local", "model"] = "local"
    layout_ids: list[str] = Field(default_factory=list)
    frame_ids: list[str] = Field(default_factory=list)


class RelatedDrawing(Record):
    snapshot_id: str
    project: str = Field(default='',max_length=100)
    discipline: str = Field(default='',max_length=50)
    floor: str = Field(default='',max_length=80)
    revision: str = Field(default='',max_length=80)
    relationship: str = Field(default='',max_length=160)


class ChatRequest(Record):
    related_drawings: list[RelatedDrawing] = Field(default_factory=list,max_length=8)

    snapshot_id: str
    question: str = Field(min_length=1, max_length=3000)
    fact_set_id: str | None = None
    selected_practice_id: str | None = None
    mode: Literal["local", "agent"] = "local"
    parent_run_id: str | None = None
    resume_task_id: str | None = None
    adaptation_policy_id: str | None = Field(default=None, max_length=100)
    runtime: Literal["native", "deepseek_harness", "codex", "codex_subscription", "responses_subscription"] = "native"
    allow_fallback: bool = False
    retrieval_mode: Literal["keyword", "lexical", "hybrid"] = "keyword"
    agent_policy: Literal["efficient", "legacy"] = "efficient"
    source_read_batching: bool = Field(default=False, strict=True, description="手动启用独立来源批量读取；默认逐项读取。")
    model_profile: (
        Literal["default", "qwen_large", "qwen_medium", "qwen_small", "codex_sol_56", "codex_sol", "codex_astra", "siwc_sol", "siwc_sol_high", "siwc_luna_high"] | None
    ) = None


class GenericFact(Record):
    fact_id: str
    snapshot_id: str
    fact_schema: str
    subject: FactValue
    property: FactValue
    value: FactValue
    scope_evidence_ids: list[str] = Field(default_factory=list)
    evidence_roles: dict[str, list[str]] = Field(default_factory=dict)
    evidence_ids: list[str]
    extractor_ref: str
    relation_state: Literal["derived", "unknown"] = "unknown"
    review_status: Literal["needs_review"] = "needs_review"
    producer: Literal["deterministic_parser"] = "deterministic_parser"


PropertyBasis = Literal["source_nominal", "annotation", "measurement", "calculation"]


class PropertySourceRef(Record):
    """A locator into an existing registered source/observation, not new evidence."""

    kind: Literal["evidence", "observation"]
    ref_id: str
    role: Literal["subject", "property", "value", "unit", "scope"]
    path: list[str | int] = Field(default_factory=list)


class PropertyEvidence(Record):
    field_id: str
    snapshot_id: str
    subject_id: str
    subject_label: str = ""
    subject_kind: Literal["source_record", "dimension", "geometry_object", "calculation"]
    property_name: str
    property_label: str
    value: Any = None
    basis: PropertyBasis
    measurement_basis: str = ""
    unit: str | None = None
    unit_state: Literal["explicit", "conditional", "unknown", "conflict"] = "unknown"
    state: Literal["observed", "derived", "unknown", "conflict"] = "unknown"
    binding_state: Literal["source_bound", "candidate", "unresolved"] = "unresolved"
    object_instance_state: Literal["unverified"] = "unverified"
    scope: dict[str, Any] = Field(default_factory=dict)
    source_refs: list[PropertySourceRef] = Field(default_factory=list)
    diagnostics: list[str] = Field(default_factory=list)
    conflicting_field_ids: list[str] = Field(default_factory=list)
    comparison_state: Literal["uncompared", "consistent", "conflict", "different_bases", "unit_unresolved"] = "uncompared"


class PropertyAssertion(Record):
    """Optional typed final-answer binding; every component must match a read field."""

    field_id: str
    subject_id: str
    property_name: str
    basis: PropertyBasis
    unit: str | None
    value: Any
