from typing import Literal

from pydantic import Field

from cad_agent.contracts import Record, FiniteFloat

VERSION = 'table-recovery-v1'
METHOD = 'anchored-lines@1'
VALIDATOR = 'table-recovery-validator@2'
MAX_INPUT = 8 * 1024 * 1024
MAX_OUTPUT = 2 * 1024 * 1024


class Diagnose(Record):
    table_id: str = Field(min_length=1, max_length=100)


class ReadInput(Record):
    input_id: str
    view: Literal['texts', 'segments', 'excluded_geometry'] = 'texts'
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=20, ge=1, le=100)


class RunFixed(Record):
    diagnostic_id: str
    input_id: str
    method_ref: Literal['anchored-lines@1'] = METHOD
    idempotency_key: str = Field(min_length=1, max_length=100)


class Cell(Record):
    row_index: int = Field(ge=0, le=3999)
    column_index: int = Field(ge=0, le=3999)
    row_span: int = Field(ge=1, le=4000)
    column_span: int = Field(ge=1, le=4000)
    text_entity_ids: list[str] = Field(max_length=5000)
    # bottom, right, top, left; IDs must resolve to input segments.
    edge_segment_ids: list[list[str]] = Field(min_length=4, max_length=4)


class Candidate(Record):
    input_id: str
    status: Literal['candidate', 'unresolved']
    reason: str = Field(default='', max_length=500)
    xs: list[FiniteFloat] = Field(default_factory=list, max_length=4001)
    ys: list[FiniteFloat] = Field(default_factory=list, max_length=4001)
    cells: list[Cell] = Field(default_factory=list, max_length=4000)


class ResultPage(Record):
    result_id: str
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=20, ge=1, le=100)
