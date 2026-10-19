"""Shared HTTP/tool inputs. All lengths in options are metres, points are WCS."""
from typing import Literal

from pydantic import Field, model_validator
from cad_agent.contracts import Record


class GeometryRead(Record):
    entity_ids: list[str] = Field(min_length=1, max_length=12)


class SpatialQuery(Record):
    layout_id: str
    bbox: list[float] = Field(min_length=4, max_length=4)
    relation: Literal["intersects", "within"] = "intersects"
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=12, ge=1, le=20)


class GeometryMeasure(Record):
    binding_id: str = ''
    unit_override: Literal["mm", "cm", "m", "in", "ft"] | None = None
    operation: Literal["distance", "length", "area"]
    entity_ids: list[str] = Field(min_length=1, max_length=2)


class RoomMeasure(Record):
    binding_id: str = ''
    unit_override: Literal["mm", "cm", "m", "in", "ft"] | None = None
    region_id: str = ""
    layout_id: str = ""
    frame_id: str = ""
    seed: list[float] | None = Field(default=None, min_length=2, max_length=2)
    radius_m: float = Field(default=15, ge=1, le=50, allow_inf_nan=False)
    tolerance_m: float = Field(default=0.001, ge=0.0001, le=0.005, allow_inf_nan=False)
    wall_min_m: float = Field(default=0.06, ge=0.02, le=0.5, allow_inf_nan=False)
    wall_max_m: float = Field(default=0.5, ge=0.04, le=1, allow_inf_nan=False)
    close_openings: bool = True
    max_opening_m: float = Field(default=3, ge=0.3, le=6, allow_inf_nan=False)
    other_room_region_ids: list[str] = Field(default_factory=list, max_length=12)
    void_entity_ids: list[str] = Field(default_factory=list, max_length=12)

    @model_validator(mode="after")
    def check(self):
        if not self.region_id and (self.seed is None or not self.layout_id):
            raise ValueError("提供房间文字 region_id，或布局 layout_id 与室内 seed 点。")
        if self.wall_min_m >= self.wall_max_m:
            raise ValueError("墙面间距范围无效。")
        return self


class MeasurementRead(Record):
    measurement_id: str


class RoomContoursRead(Record):
    discovery_id: str
    candidate_id: str
    offset: int = Field(default=0, ge=0, le=4000)
    limit: int = Field(default=8, ge=1, le=20)


class RoomAreaMeasure(Record):
    discovery_id: str
    candidate_id: str
    basis: Literal["outer_shell", "topological_face", "selected_exclusions"]
    exclusion_contour_ids: list[str] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def check(self):
        if len(set(self.exclusion_contour_ids)) != len(self.exclusion_contour_ids):
            raise ValueError("扣除轮廓 ID 不得重复。")
        if self.basis != "selected_exclusions" and self.exclusion_contour_ids:
            raise ValueError("只有 selected_exclusions 口径允许指定扣除轮廓。")
        return self


class RoomDiscover(Record):
    layout_id: str
    view_kind: Literal["unknown", "floor_plan", "section", "elevation", "detail", "table", "legend"] = "unknown"
    bbox: list[float] = Field(min_length=4, max_length=4)
    frame_id: str = ""
    unit_override: Literal["mm", "cm", "m", "in", "ft"] | None = None
    tolerance_m: float = Field(default=0.001, ge=0.0001, le=0.005, allow_inf_nan=False)
    wall_min_m: float = Field(default=0.06, ge=0.02, le=0.5, allow_inf_nan=False)
    wall_max_m: float = Field(default=0.5, ge=0.04, le=1, allow_inf_nan=False)
    close_openings: bool = True
    max_opening_m: float = Field(default=3, ge=0.3, le=6, allow_inf_nan=False)
    offset: int = Field(default=0, ge=0, le=2000)
    limit: int = Field(default=8, ge=1, le=20)

    @model_validator(mode="after")
    def check(self):
        from .source import finite_box
        finite_box(self.bbox)
        if self.wall_min_m >= self.wall_max_m:
            raise ValueError("墙面间距范围无效。")
        return self
