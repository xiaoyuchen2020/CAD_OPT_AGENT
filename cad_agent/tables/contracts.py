"""One contract for native, ruled, borderless and visual table sources."""
from typing import Literal
import math
from pydantic import Field, model_validator
from cad_agent.contracts import Record, FiniteFloat

MAX_CELLS = 4000
MAX_SEGMENTS = 30000
MAX_CANDIDATES = 256


class TableScope(Record):
    snapshot_id: str = ''
    layout_id: str = ''
    frame_id: str = ''
    bbox: list[FiniteFloat] | None = Field(default=None, min_length=4, max_length=4)

    @model_validator(mode='after')
    def bounds(self):
        if self.bbox and not (self.bbox[0] < self.bbox[2] and self.bbox[1] < self.bbox[3]):
            raise ValueError('需要有面积的局部范围。')
        return self


class SearchTables(TableScope):
    query: str = Field(default='', max_length=200)
    level: Literal['all', 'table', 'group', 'field'] = 'all'
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=8, ge=1, le=20)


class InspectTable(Record):
    snapshot_id: str = ''
    table_id: str
    view: Literal['structure', 'diagnostics', 'coverage'] = 'structure'
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=20, ge=1, le=100)


class ReadTableContent(Record):
    snapshot_id: str = ''
    table_id: str
    node_id: str = ''
    fields: list[str] = Field(default_factory=list, max_length=20)
    cursor: str = Field(default='', max_length=300)
    limit: int = Field(default=24, ge=1, le=64)
    read_plan_id: str = ''


class RecoverTableRegion(TableScope):
    candidate_id: str = ''
    method: Literal['source_geometry', 'visual_review'] = 'source_geometry'

    @model_validator(mode='after')
    def target(self):
        if not self.candidate_id and (not self.layout_id or self.bbox is None):
            raise ValueError('需要候选 ID 或已知布局和局部范围。')
        return self


class VisualCell(Record):
    bbox: list[FiniteFloat] = Field(min_length=4, max_length=4)
    text_entity_ids: list[str] = Field(default_factory=list, max_length=200)
    transcription: str = Field(default='', max_length=2000)
    row: int = Field(ge=0, le=3999)
    column: int = Field(ge=0, le=3999)
    row_span: int = Field(default=1, ge=1, le=4000)
    column_span: int = Field(default=1, ge=1, le=4000)


class ReviewTableRegion(Record):
    snapshot_id: str = ''
    recovery_id: str
    cells: list[VisualCell] = Field(min_length=1, max_length=MAX_CELLS)


class Fragment(Record):
    entity_id: str
    raw_text: str
    display_text: str = ''
    assignment_basis: str = 'native_insertion_inside'
    origin: list[FiniteFloat]
    bbox: list[FiniteFloat]
    evidence: list[dict] = Field(default_factory=list)
    source_kind: Literal['native_text', 'native_table', 'image_transcription'] = 'native_text'


class Cell(Record):
    cell_id: str
    row: int
    column: int
    row_span: int = 1
    column_span: int = 1
    bbox: list[FiniteFloat]
    boundary: list[list[FiniteFloat]]
    local_bbox: list[FiniteFloat]
    fragments: list[Fragment] = Field(default_factory=list)
    text: str = ''
    edge_sources: list[list[str]] = Field(default_factory=list)
    source_state: str = 'source_bound'
    structure_state: str = 'candidate'
    binding_state: str = 'unresolved'
    diagnostics: list[str] = Field(default_factory=list)


class TableNode(Record):
    node_id: str
    kind: Literal['table', 'group', 'subtable', 'cell', 'note', 'header']
    parent_id: str | None = None
    title: str = ''
    cell_ids: list[str] = Field(default_factory=list)
    children: list[str] = Field(default_factory=list)
    binding_state: str = 'candidate'


class TableDocument(Record):
    version: str
    table_id: str
    snapshot_id: str
    source_sha256: str
    signature: str
    layout_id: str
    frame_id: str = ''
    instance_path: list[str] = Field(default_factory=list)
    source_kind: str
    title: str
    title_evidence: list[dict] = Field(default_factory=list)
    bbox: list[FiniteFloat]
    boundary: list[list[FiniteFloat]]
    transform: dict
    cells: list[Cell]
    nodes: list[TableNode]
    fields: list[dict] = Field(default_factory=list)
    source_state: str = 'source_bound'
    structure_state: str = 'candidate'
    binding_state: str = 'unresolved'
    diagnostics: list[str] = Field(default_factory=list)
    unassigned_entity_ids: list[str] = Field(default_factory=list)
    source_inventory: list[str] = Field(default_factory=list)
    scope_exhaustive: bool = False
