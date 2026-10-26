import math
from typing import Literal

from pydantic import Field, model_validator
from cad_agent.contracts import Record


class AnnotationQuery(Record):
    kind: Literal['axes', 'references', 'leaders', 'elevations', 'storeys']
    layout_id: str
    frame_id: str = ''
    bbox: list[float] | None = Field(default=None, min_length=4, max_length=4)
    query: str = Field(default='', max_length=100)
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=8, ge=1, le=20)

    @model_validator(mode='after')
    def bounds(self):
        if self.bbox is not None:
            a, b, c, d = self.bbox
            if not all(math.isfinite(v) for v in self.bbox) or c <= a or d <= b:
                raise ValueError('bbox 必须为有限、非空 WCS 范围。')
        return self


class AxisLocation(Record):
    layout_id: str
    axis_ids: list[str] = Field(min_length=2, max_length=4)


class DetailTrace(Record):
    reference_id: str
    layout_id: str
    target_snapshot_ids: list[str] = Field(default_factory=list, max_length=4)
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=8, ge=1, le=12)


class SemanticTable(Record):
    table_id: str
    header_rows: list[int] | None = Field(default=None, max_length=4)
    row_offset: int = Field(default=0, ge=0)
    row_limit: int = Field(default=3, ge=1, le=5)

    @model_validator(mode='after')
    def headers(self):
        if self.header_rows is not None and (self.header_rows != list(range(len(self.header_rows)))):
            raise ValueError('表头必须从第 0 行连续指定，不能跳过数据行。')
        return self
