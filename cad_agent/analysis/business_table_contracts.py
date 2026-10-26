"""Read-only business table requests; discovery and extraction share an entry."""
from pydantic import Field, model_validator
from cad_agent.contracts import Record

METRICS = ('land_area', 'total_building_area', 'counted_area', 'excluded_area',
           'green_area', 'footprint_area', 'floor_area_ratio', 'building_density',
           'green_ratio', 'parking_spaces')
BUILDING = ('ordinal', 'name', 'use', 'storeys', 'height', 'total_building_area',
            'counted_area', 'excluded_area', 'footprint_area')


class BusinessTableRequest(Record):
    snapshot_id: str = Field(default='', description='默认当前图纸；Agent 仅可指定本轮用户已选关联快照。')
    table_id: str = Field(default='', description='已发现的表格 ID；留空只列候选，不自动选表。')
    layout_id: str = ''
    frame_id: str = ''
    header_rows: list[int] | None = Field(default=None, min_length=1, max_length=4)
    row_offset: int = Field(default=0, ge=0)
    row_limit: int = Field(default=5, ge=1, le=5)
    offset: int = Field(default=0, ge=0, description='候选表发现分页偏移。')
    limit: int = Field(default=8, ge=1, le=12)
    requested_fields: list[str] = Field(default_factory=list, max_length=10)

    @model_validator(mode='after')
    def validate_request(self):
        if self.header_rows is not None and self.header_rows != list(range(len(self.header_rows))):
            raise ValueError('表头须从第 0 行连续指定。')
        allowed = METRICS if isinstance(self, PlanningMetricsRequest) else BUILDING
        if set(self.requested_fields) - set(allowed):
            raise ValueError('存在不支持的业务字段。')
        if len(set(self.requested_fields)) != len(self.requested_fields):
            raise ValueError('业务字段不能重复。')
        return self


class PlanningMetricsRequest(BusinessTableRequest):
    pass


class BuildingScheduleRequest(BusinessTableRequest):
    pass
