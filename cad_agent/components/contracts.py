from typing import Literal

from pydantic import Field, model_validator

from cad_agent.contracts import Record
from cad_agent.geometry.contracts import RoomDiscover


class ComponentScope(Record):
    layout_id: str
    frame_id: str = ""
    bbox: list[float] | None = Field(default=None, min_length=4, max_length=4)
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=8, ge=1, le=20)

    @model_validator(mode="after")
    def bounds(self):
        if self.bbox is not None:
            from cad_agent.geometry.source import finite_box
            finite_box(self.bbox)
        return self


class ContextQuery(ComponentScope):
    title: str = Field(default="",max_length=150,description="可按子图标题、图签图号或字段值筛选；保留同名歧义，不用标题框代替范围。")
    view: Literal["frames", "subframes"] = "subframes"


class DimensionQuery(ComponentScope):
    unit_override: Literal["mm", "cm", "m", "in", "ft"] | None = None


class RelationQuery(RoomDiscover):
    view: Literal["rooms", "walls", "openings", "relations"] = "relations"
    linework_mode: Literal["auto", "all"] = "auto"


class ComponentRead(Record):
    record_id: str
    view: Literal["frames", "subframes", "dimensions", "rooms", "walls", "openings", "relations"]
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=8, ge=1, le=20)
