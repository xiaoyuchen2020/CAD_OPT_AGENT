"""Shared tool schemas and capability registry, independent of runtime loops."""
from typing import Literal
from pydantic import Field, field_validator
from cad_agent.contracts import Record, PropertyAssertion
from cad_agent.semantics.contracts import AnnotationQuery, AxisLocation, DetailTrace, SemanticTable
from cad_agent.geometry.contracts import GeometryRead, SpatialQuery, GeometryMeasure, RoomMeasure, MeasurementRead, RoomDiscover, RoomContoursRead, RoomAreaMeasure
from cad_agent.geometry.room_batch import RoomBatchQuery
from cad_agent.analysis.tasks import TaskPlan
from cad_agent.analysis.enclosure_inspection import EnclosureInspection
from cad_agent.analysis.result_sets import PreviousResults, AnalyzeMeasurements
from cad_agent.analysis.focus import ToolsetRequest
from cad_agent.components.contracts import ContextQuery, DimensionQuery, RelationQuery, ComponentRead
from cad_agent.components.dimension_chain import DimensionChainQuery
from cad_agent.geometry.spatial_labels import LocateSpaceLabels
from cad_agent.geometry.connection_review import PrepareConnections, ReviewConnections
from cad_agent.geometry.discovery_review import PrepareDiscoveryReview, ReviewDiscoveredSpaces
from cad_agent.geometry.vision import RenderRegion, BindVisualObjects
from cad_agent.geometry.room_boundary import PrepareBoundaries, SelectBoundaries
from cad_agent.analysis.unit_policy import ResolveRoomUnits
from cad_agent.analysis.grounding import Calculation
from cad_agent.analysis.fact_tools import UserInputRef, CalculateFacts, ReadAssembly, ResolveScope, ProjectRead
from cad_agent.analysis.batch_reads import BatchReadSources
from cad_agent.analysis.scope_objects import InspectScopeObjects
from cad_agent.analysis.evidence_store import EvidenceRecordRead
from cad_agent.analysis.spatial_evidence import BindSpatialObject, SpatialObjectRead
from cad_agent.geometry.spatial_boundaries import InspectSpatialBoundary, MeasureSpatialArea
from cad_agent.geometry.spatial_relations import MeasureSpatialRelation, InspectDoorSweep, MeasureDoorSweepRelation
from cad_agent.geometry.spatial_sections import MeasureSpatialSections, MeasureElevationRelation
from cad_agent.analysis.door_schedule import ReadDoorScheduleTable, TraceDoorSchedule
from cad_agent.analysis.similarity_search import SimilarObjectSearch
from cad_agent.analysis.requirement_sources import RequirementSources

class SearchArgs(Record):
    alternatives: list[str] = Field(default_factory=list,max_length=8,description='显式替代查询词（每词至多150字符），与query按OR匹配并强制字面分页；是待核候选词，不确认同义或对象功能。match=all时该组仍须与各keywords同时命中。')
    keywords: list[str] = Field(default_factory=list, max_length=6)
    match: Literal["any", "all"] = "any"
    layout_id: str = ""
    section_id: str = ""
    query: str = Field(default="", max_length=150)
    frame_id: str = ""
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=8, ge=1, le=12)


class OverviewArgs(Record):
    view: Literal["titles", "layouts", "frames", "sections", "tables"] = "titles"
    layout_id: str = ""
    frame_id: str = ""
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=8, ge=1, le=12)


class ContextArgs(Record):
    region_id: str
    neighbors: int = Field(default=1, ge=0, le=2)
    start: int = Field(default=0, ge=0)


class ExtractArgs(Record):
    extractor_ref: Literal[
        "building.masonry@1", "generic.key_value@1", "generic.table@1"
    ] = "building.masonry@1"
    frame_ids: list[str] = Field(default_factory=list, max_length=10)


class QueryArgs(Record):
    fact_type: Literal[
        "building.masonry.v1", "generic", "generic.key_value.v1", "generic.table.v1"
    ] = "building.masonry.v1"
    subject: str = Field(default="", max_length=150)
    property: str = Field(default="", max_length=150)
    query: str = Field(default="", max_length=150)
    wall_role: Literal["", "inner", "outer"] = ""
    frame_id: str = ""
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=8, ge=1, le=12)


class EvidenceArgs(Record):
    evidence_ids: list[str] = Field(min_length=1, max_length=8)


class PropertyEvidenceQuery(Record):
    subject_id: str = Field(default="", max_length=200)
    property_name: str = Field(default="", max_length=120)
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=30, ge=1, le=100)


class TableArgs(Record):
    table_id: str
    row_offset: int = Field(default=0, ge=0)
    row_limit: int = Field(default=3, ge=1, le=5)


class EntityArgs(Record):
    spatial_relation: Literal['bbox_intersects', 'insertion_point_inside'] = 'bbox_intersects'
    alternatives: list[str] = Field(default_factory=list,max_length=8,description='至多8个显式替代文字查询词，每词至多150字符，与query按OR匹配；按实体ID去重，不合并独立同名实体。')
    layout_id: str = ""
    frame_id: str = ""
    bbox: list[float] | None = Field(default=None, min_length=4, max_length=4)
    layer: str = ""
    kind: str = ""
    query: str = Field(default="", max_length=150)
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=12, ge=1, le=20)


class InspectArgs(Record):
    region_id: str
    padding: float = Field(default=1.0, ge=0, le=3)


class Claim(Record):
    query_target_ids: list[str] = Field(default_factory=list, max_length=20, description="本条结论对应查询意图中的target_id；同一要求包含多个对象时分别填写。")
    property_evidence_ids: list[str] = Field(default_factory=list, max_length=20,
        description="可选：query_property_evidence 已返回的字段证据ID；保留对象、属性口径、单位及冲突，不替代原文或工具观察引用。")
    property_assertions: list[PropertyAssertion] = Field(default_factory=list, max_length=20,
        description="可选字段断言：逐项绑定field_id、subject_id、property_name、basis、unit与value，禁止跨对象或计量口径借值。")
    user_inputs: list[UserInputRef] = Field(default_factory=list,max_length=8)
    observation_kind: Literal['auto', 'geometry_observation', 'measurement_result', 'measurement_diagnostic', 'visual_interpretation'] = 'auto'
    question_part_ids: list[str] = Field(default_factory=list, max_length=10)
    calculations: list[Calculation] = Field(default_factory=list, max_length=4)
    evidence_basis: Literal["source", "tool_observation"] = Field(default="source", description="只能为source或tool_observation；用户条件另放user_inputs，不能新增user_input等枚举。")
    observation_ids: list[str] = Field(default_factory=list, max_length=4)
    evidence_roles: dict[
        Literal["subject", "property", "value", "scope", "relation"], list[str]
    ] = Field(default_factory=dict)

    @field_validator("evidence_roles", mode="before")
    @classmethod
    def normalize_role_ids(cls, value):
        # A single explicit ID is unambiguous; still validated against read evidence.
        if isinstance(value, dict):
            return {k: [v] if isinstance(v, str) else v for k, v in value.items()}
        return value

    text: str = Field(min_length=1, max_length=1500)
    state: Literal["observed", "derived", "unknown", "conflict"]
    evidence_ids: list[str] = Field(default_factory=list, max_length=20)


class RequirementOutcome(Record):
    requirement_id: str
    gap_kind: Literal['none','unread_source','not_found_in_read_scope','ambiguous_scope','missing_input','unsupported_capability','budget_exhausted','out_of_scope','validation_failed'] = 'none'
    status: Literal["fulfilled","partial","unresolved"]
    reason: str = Field(min_length=1,max_length=300)


class FinalAnswer(Record):
    user_inputs: list[UserInputRef] = Field(default_factory=list,max_length=20)
    requirement_outcomes: list[RequirementOutcome] = Field(default_factory=list,max_length=10)
    claims: list[Claim] = Field(min_length=1, max_length=12)
    missing_information: list[str] = Field(default_factory=list, max_length=12, description="字符串数组；每项完整说明对象与缺口，不返回对象或嵌套字段。")


from cad_agent.analysis.source_record_selection import SelectSourceRecords

from cad_agent.analysis.source_candidates import SearchSourceRecords

TOOL_SPECS = {
    "search_source_records": (SearchSourceRecords, "按已登记query_intent的target_id发现做法标题候选，精确编号优先；范围独立判断。只列标题、位置、歧义及真实read_assembly参数，不读正文、不扩展子类别。alternatives必须附expansion_reason且不能改变原目标。"),
    "select_source_records": (SelectSourceRecords, "对本轮已读完整来源记录执行用户明确字面筛选；分别保留纳入和排除记录，不证明全图覆盖或工程适用性。"),
    "read_requirement_sources": (
        RequirementSources,
        "按当前 requirement_id 的原始问题跨条款补读原文（有界、本地字面排序），用于通则、特殊条件、不同目标漏读。返回可引用原文及续读入口；排名不证明对象归属，不替代完整表行核查。"),
    "query_property_evidence": (PropertyEvidenceQuery, "查看本任务已读取对象的字段证据；按对象和属性筛选，分别保留表列值、标注值、几何测量、计算、单位和冲突。仅整理已读记录，不增加读取覆盖或确认对象身份；field_id可用于最终字段断言。"),
    "measure_spatial_sections": (MeasureSpatialSections, "按绑定源对象的闭合区域或显式对边，沿原生边支持的轴做局部截面并扣除已绑定障碍。轴和断面参数必须核对源几何；返回所采样截面宽度，不等于全路径最小通行净宽，不假定障碍枚举完整。"),
    "measure_elevation_relation": (MeasureElevationRelation, "核查绑定源标签的两个标高原文、单位、共同基准和视图，再计算条件成立的差值。用户假设必须逐字来自本轮问题；不同完成面/结构面、视图或基准未证实不能把相邻负标高直接认作层高。"),
    "inspect_spatial_boundary": (InspectSpatialBoundary, "核查已绑定object_id的原生线段闭合拓扑、逐边来源和开放缺口；不补缝、不用包络框替代边界。返回contour_id供明确选择面积口径；身份和全图枚举仍未确认。"),
    "measure_spatial_area": (MeasureSpatialArea, "按已核查object_id及contour_id计算topological_face、outer_shell或selected_exclusions的面积与周长；扣除项须已绑定真实闭合源路径。未知单位只报告原图单位量，条件单位保留条件；不自动确认主体外轮廓或净面积。"),
    "measure_spatial_relation": (MeasureSpatialRelation, "计算同范围两个已绑定对象的原生闭合区域重叠、包含、接触和距离，或显式source_paths源线距离。图例不代表实际布置；源几何关系不确认工程用途或整图完整性。"),
    "inspect_door_sweep": (InspectDoorSweep, "核查已绑定对象内原生ARC及径向叶线的共同端点、半径和角度，提供可溯源旋转扇形候选。缺弧或径向来源不构造假想扇形；几何支持不确认门身份。"),
    "measure_door_sweep_relation": (MeasureDoorSweepRelation, "比较有原生弧和径向叶线支持的扇形与另一闭合源区域，返回距离/重叠上下界和近似精度；不能用弦近似确认精确接触，不能将图例当实际布置。"),
    "read_evidence_record": (EvidenceRecordRead, "按已返回record_id/start/length分页回读完整本地工具结果、原文或几何JSON。完整性校验绑定选定图纸版本。返回JSON字符片段和next_start；存储记录、摘要或未读分页不证明整图覆盖或对象身份，不能绕过来源与测量校验。"),
    "bind_spatial_object": (BindSpatialObject, "把已读取的原生实体和标签绑定为同图纸、同范围的持久对象。返回object_id供空间方法复用；核验来源、包含关系和图例排除，角色仍是待核声明，不能把绑定成功等同净面积或工程身份确认。"),
    "get_spatial_object": (SpatialObjectRead, "读取本轮持久对象的来源、标签关系、范围、单位及扣除项缺口，重新核验快照与源文件；按object_id复用，不接受新坐标或模型自报面积。"),
    "inspect_scope_objects": (InspectScopeObjects, "在已绑定scope_id内列出子图标题与局部窗口，或按window_id读取真实源几何/闭合边界候选。单位未知仍审查原始坐标，不换算米制。next_calls有界续读或分块，review_calls供源路径和局部图核验；文字/图框/窗口都不是建筑边界，候选不确认身份或净宽。"),
    "batch_read_sources": (BatchReadSources, "一次提交至多4个已知、相互独立的 read_source_context/read_assembly 读取，reads每项为tool/arguments/task_ids。逐项执行与扣除原工具额度，保留来源校验和失败隔离；不得包含依赖前项结果的请求或状态操作。各项分页照原工具续读；pending_reads尚未执行。大页面使用原工具。"),
    "read_assembly": (ReadAssembly, "从做法标题/编号恢复源线约束的跨栏正文/备注候选及逐行原文、括号替代、单位与坐标。按 next_actions 续读全部字符/行；checks 区分读取穷尽和对象/归属未确认。邻近不证明适用范围，缺单位不填空。"),
    "calculate_facts": (CalculateFacts, "计算要求必须实际执行：明确target与操作数label/value/unit/来源，原文source_quote须绑定数值及单位，不从首个数字或邻近文字猜值。缺操作数用missing_operands逐项记录具体缺口（可quantities=[]），补齐后沿用calculation_key重试。全库合计填coverage_scope=aggregate，不能替代逐分区/逐做法计算。保留约数和口径。对已读原文、用户原句、已读测量做本地算术。origin区分source/user_input/measurement/calculation（前一步calculate_facts结果可链式引用）；单位须在各自原文明确，缺单位先读取依据。支持sum/subtract/mean/multiply/divide/ceil/floor/rank，返回推导值与输入引用；只算事实，不判优化合规。"),
    "resolve_drawing_scope": (ResolveScope, "诊断真实标题与图框候选，view默认plan；剖面或详图显式选section/detail，并用floor_title传原图标题。核验原生标题、闭合图框与冲突；范围只用于检索，不确认共同基准或视图等价。非平面范围不能用于房间业务；有歧义仍可交付名称线索。"),
    "read_project_sources": (ProjectRead, "catalog核对本轮选定清单；query可指定待核对资料，reference_kind区分external_file/internal_section/unspecified；引用须附已读reference_evidence_id及逐字reference_quote。区分未选、未读、引用与版本关系未确认；图内专篇不是缺独立文件。按next_calls在白名单内search/read。标签不证明同项目版本，缺外部资料仍继续本图事实。"),
    "locate_space_labels": (LocateSpaceLabels, "按楼层标题或图框一次定位最多4种精确空间名称，保留表格候选、全部计数与来源ID；仅定位，不能凭文字相邻认定连通。"),
    "prepare_space_connections": (PrepareConnections, "对已定位的每对空间批量生成局部图、横穿边线、门垛源段实测和关联尺寸。不按文字相邻判连通。下一轮必须逐处review_space_connections。"),
    "review_space_connections": (ReviewConnections, "看实际配对图逐处判open/door/wall/uncertain/not_plan_space；door需同图开口ID和双侧证据。自动交付源边线和测距高亮，不得遗漏配对。"),
    "prepare_discovery_review": (PrepareDiscoveryReview, "对discover_rooms结果按面积降序生成最多12格局部原图及轮廓，供排除家具/表格/图框/墙带。无需房间名称。"),
    "review_discovered_spaces": (ReviewDiscoveredSpaces, "实际看图后逐项筛选space/furniture/table/frame/wall_band/annotation/boundary_mismatch/duplicate/uncertain，本地计算面积周长、验证无重叠并交付前N候选高亮；未审大候选阻止前三完整声明。"),
    "inspect_enclosure": (EnclosureInspection, "读取已选择房间轮廓内部闭合对象及邻近文字候选，逐项提供来源实体和候选面积。仅调查，绝不自动把设备/基础/家具视为洞口扣除；保留覆盖缺口，进一步用局部图和原文验证用途。"),
    "check_dimension_chain": (DimensionChainQuery, "在指定范围读取原生尺寸并核查一条水平/垂直连续尺寸链。按尺寸线和界点选择无重复分段及对应总尺寸，计算文字值之和、差值及几何距离。保留原始显示文本和样式值区别；无法建立链时明确缺口。"),
    "get_analysis_results": (PreviousResults, "恢复父会话同源版本的结果集合、原编号、轮廓与单位。返回批次供本轮继续测量/筛选；不重新解释历史图像。"),
    "analyze_measurements": (AnalyzeMeasurements, "对已读取批次的所选轮廓本地计算面积或外环周长；rank排序、range最大最小差、sum和mean仅聚合互不重叠的完整已测集合。limit按值及原编号稳定筛选；replace_highlights=true实际替换当前高亮集合。结果保留候选口径。"),
    'resolve_room_units': (ResolveRoomUnits, '检查房间批次的图纸单位、原生尺寸、可能炸开标注及用户条件性单位约定。用户已明确授权时自动应用；不接受模型传入单位。未知单位不阻止轮廓分析。'),
    "finish_analysis": (FinalAnswer, "证据足够后提交最终答案并立即结束；每个未由批次交付的要求须有结论或unknown。服务端校验引用、数值、单位和逐项覆盖；失败只返回纠错反馈，不发布答案。"),
    "request_toolset": (ToolsetRequest, "当前工具不足或交付检查发现明确缺口时，按family加载注册工具。说明具体缺失的对象、属性、范围、表头或冲突；不允许代码执行。"),
    "plan_task": (TaskPlan, "为所有原始问题分项建立一次有向无环计划；逐项填写 requirement_id、kind、depends_on、acceptance。不能标记完成或添加原文不存在的对象ID。"),
    "analyze_room_batch": (
        RoomBatchQuery,
        "按楼层平面图标题或图框、房间名称批量枚举文字候选并测量，返回逐项证据与高亮；无单位则等待界面确认，不允许模型猜单位。同名范围需确认；分页不代表整图房间总数。",
    ),
    "query_annotations": (AnnotationQuery, "查询指定布局的 axes轴网、references索引、leaders引线、elevations标高或storeys楼层候选。支持bbox/图框/文字分页筛选，返回实体ID和原文证据；关系仍待核查。"),
    "locate_by_axes": (AxisLocation, "用query_annotations返回的2条相交轴线定位交点，或4条成两组平行的轴线定位轴区。必须选择当前布局的明确axis_ids，重复轴号不能自动择一。"),
    "trace_detail": (DetailTrace, "追踪references返回的reference_id至详图标题候选。默认当前快照，可显式给出最多4个已知target_snapshot_ids；不得猜ID。区分无目标、图号未解和多个目标；跨图来源保留自身快照。"),
    "read_table_semantics": (SemanticTable, "增强读取既有table_id：恢复矩形合并格、多级表头候选、明确单位和编号关联。可用连续header_rows选择表头，默认保守识别。保留单位冲突、空白、跨度及原文证据。"),
    "read_door_schedule_table": (ReadDoorScheduleTable, "读取本轮已选关联图纸中的已知门表table_id。保留实际来源快照、表头和单位；选择图纸不等于确认版本或适用性。"),
    "trace_door_schedule": (TraceDoorSchedule, "连接已绑定门实例、已读且绑定实例的型号标注与已读门表行。保留同型号多行、项目/楼层/版本缺口，仅返回候选来源链，不确认工程身份或自动借值。"),
    "search_similar_objects": (SimilarObjectSearch, "在明确布局/图框内，以WCS样本框搜索原生形状相似候选。有限扫描与几何描述符，返回实际尺寸、实例路径及高亮；相似度不是工程身份置信度，不作整图数量结论。"),
    "get_drawing_context": (ContextQuery, "读取当前布局的图框/图签字段、子图标题、楼层/比例候选及边界依据。支持 frame_id/bbox 和分页；比例不换算图纸单位。"),
    "read_dimensions": (DimensionQuery, "读取原生尺寸标注：文字覆盖、原始值、样式缩放、几何复算及端点对象候选。爆炸标注不重建，未知单位不推算米。"),
    "query_building_relations": (RelationQuery, "复用房间围合与墙面门垛证据，查询有界范围的房间/墙面/门洞/关系候选。门洞不确认是门；双侧关联可能未解决。分页可能按响应大小或剩余预算调整，继续读取必须使用返回的next_offset，不按请求limit推算。"),
    "get_component_evidence": (ComponentRead, "分页读取当前快照公共组件记录及不同视图，保留来源实体、哈希、版本和候选状态。实际页大小见effective_limit；继续读取必须使用next_offset，预算不足时明确未读部分。"),
    "prepare_room_boundaries": (PrepareBoundaries, "对已枚举批次生成来源可查的墙面/家具轮廓候选，并发送至多两张局部图拼图；不要求单位，不计算面积。下一轮用select_room_boundaries逐项看图选择或uncertain。"),
    "select_room_boundaries": (SelectBoundaries, "逐项绑定已收到的局部图轮廓候选，本地校验几何/家具排除/重叠后保存高亮；选择只表示视觉候选，开口和净边界仍待核查。不接受新坐标或面积。"),
    "render_region": (RenderRegion, "读取局部PNG图像并在下一轮发送给模型；WCS bbox来自用户选择/已读图元/区域。返回E编号与实体对应、坐标映射和证据链接。最多两图，不以像素猜尺寸。仅订阅自定义/Codex桥支持。"),
    "bind_visual_objects": (BindVisualObjects, "把已收到图像中的候选对象绑定到该图像提供的实体ID，记录候选视觉解释。身份仍待核查；返回binding_id供后续几何测量引用。"),
    "read_geometry": (GeometryRead, "读取当前快照图元的计算几何、原始参数与块来源；大数组有截断标记。ID 来自 query_entities 或 query_spatial。用户已选原生句柄但旧索引缺失时，传layout_id和entity_paths（逐级句柄数组）直接核验源DXF；与entity_ids二选一。"),
    "query_spatial": (SpatialQuery, "按布局和 WCS bbox 查询实际线形相交或完全包含关系，曲线使用有界细分。返回几何实体 ID。"),
    "measure_geometry": (GeometryMeasure, "测量两个图元最短距离或单图元长度/闭合面积。跨布局、无单位和无效轮廓拒绝；结果不证明房间净面积。"),
    "measure_room": (RoomMeasure, "房间净边界与净面积试点：提供房间文字 region_id 或 layout_id+seed。以成对墙面与门垛证据寻找局部边界，返回测量状态、证据ID及预览。无可靠边界时只返回候选/待复核，不执行优化规则。"),
    "discover_rooms": (RoomDiscover, "在布局与 WCS bbox 范围内发现通用房间/围合空间候选，无房间名称或图层白名单，无需预选室内点，支持未命名空间。返回分页候选、可追溯证据及后续测量请求；身份和净面积尚未确认，不代表全图房间总数。"),
    "get_room_contours": (RoomContoursRead, "读取 discover_rooms 返回候选的外轮廓、内部闭合轮廓、严格包含层级、拓扑内环和逐边实体证据。内部轮廓分页；空洞/柱/设备语义待确认，不能自动扣除。"),
    "measure_room_area": (RoomAreaMeasure, "按已读取的候选轮廓计算指定口径：outer_shell 外轮廓、topological_face 拓扑面、selected_exclusions 指定内部轮廓并集扣除。返回候选口径面积及原图叠加证据，不确认净面积或构件语义。"),
    "get_measurement_evidence": (MeasurementRead, "读取当前快照已生成测量的来源、边界、逐边支持、推断门洞和诊断；完整记录可通过 evidence_url 查看。"),
    "read_table": (
        TableArgs,
        "按行读取通用有边线表格。用 get_drawing_overview(view=tables) 找 table_id。返回单元格、合并列跨度及原文证据；表头/单位关系不自动确认。",
    ),
    "query_entities": (
        EntityArgs,
        "查询当前图纸文字和几何图元，可按布局、图框、原坐标 bbox、图层、类型或文字筛选。空间邻近不代表语义关联。",
    ),
    "inspect_region": (
        InspectArgs,
        "取得指定原文区域的局部SVG预览链接、文字证据和附近图元。预览供用户查看，当前模型未接收图片。",
    ),
    "get_drawing_overview": (
        OverviewArgs,
        "读取当前图纸概览，view=titles 返回标题候选及证据；layouts/frames/sections/tables 分页浏览结构入口。图框和表格为候选，不代表确认图纸张数。",
    ),
    "search_sources": (
        SearchArgs,
        "检索原文；检索方式由用户选择，支持自然语言query和keywords、布局/图框/章节范围。match=all要求所有输入字面命中。候选仅是阅读入口，按snippet_start读取上下文后才能引用。",
    ),
    "read_source_context": (
        ContextArgs,
        "读取原文区域及同图框同列的邻近上下文，返回可引用证据。分离上标以unit_candidates返回多实体原文及单位候选，不能据此确认面积口径。邻近不是适用关系证明。start 用于长区域分页。",
    ),
    "extract_facts": (
        ExtractArgs,
        "执行已注册的本地抽取器：building.masonry@1 砌体；generic.key_value@1 属性键值；generic.table@1 通用表格候选。返回事实集合和覆盖情况，不执行生成代码。",
    ),
    "query_facts": (
        QueryArgs,
        "查询事实。fact_type 默认 building.masonry.v1；generic 查询通用候选，支持 subject/property。返回属性及证据ID，需 get_evidence 读取后引用。",
    ),
    "get_evidence": (
        EvidenceArgs,
        "读取当前图纸事实的原文证据，返回可用于最终回答的 evidence_ids。",
    ),
}


def tool_definitions(efficient=False, session=None):
    result = [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": desc,
                "parameters": schema.model_json_schema(),
            },
        }
        for name, (schema, desc) in TOOL_SPECS.items()
        if name != 'batch_read_sources' or (session is not None and session.request.source_read_batching)
        if session is None or runtime_supports(name, session.request.runtime, session.w.settings.provider)
        if name not in ('finish_analysis','request_toolset') or (session is not None and session.focused_enabled)
    ]
    if session is not None:
        from cad_agent.analysis.query_intent import enabled as intent_enabled, tool_schema
        if intent_enabled(session):
            for tool in result:
                if tool["function"]["name"] in {"plan_task", "search_source_records"}:
                    tool_schema(tool["function"]["parameters"])
    if efficient:
        for tool in result:
            props = tool["function"]["parameters"].get("properties", {})
            if "limit" in props:
                props["limit"]["default"] = 12 if tool["function"]["name"] in {"analyze_room_batch", "read_assembly"} else 5
    if session and session.request.source_read_batching:
        from cad_agent.analysis.batch_reads import batch_schema
        for tool in result:
            if tool['function']['name'] == 'batch_read_sources':
                tool['function']['parameters'] = batch_schema(session, TOOL_SPECS)
    if session and (session.task.complex or getattr(session, "stable_prefix_enabled", False)):
        for tool in result:
            if tool['function']['name'] in ('plan_task','finish_analysis','batch_read_sources'):continue
            schema=tool['function']['parameters']
            schema['properties']['task_ids']={'type':'array','items':({'type':'string'} if getattr(session, "stable_prefix_enabled", False) else {'type':'string','enum':[p['id'] for p in session.task.parts]}),'minItems':1,'maxItems':10,
                'description':'本次调用负责的原始任务分项；批量工具可以同时交付枚举、面积和高亮。'}
            if not getattr(session, "stable_prefix_enabled", False):
                schema.setdefault('required',[]).append('task_ids')
    if session is not None:
        for tool in result:
            name = tool['function']['name']
            if name in CONTROL or name == 'batch_read_sources':
                continue
            tool['function']['parameters']['properties']['result_view'] = {
                'type': 'string', 'enum': ['auto', 'summary', 'full'], 'default': 'auto',
                'description': '结果视图：auto按当前任务保留必要几何，summary省略冗余坐标，full保留完整返回；摘要不代表省略字段已读取，可按返回record_id回读。',
            }
    return result


# The registry describes actual implemented transports, not model marketing names.
# Runtime selection is still independently checked by runtimes.registry.
from cad_agent.analysis.tool_metadata import GROUPS, READS, CONTROL, OPERATION_TOOLS

VISUAL_TOOLS = frozenset({
    'render_region', 'bind_visual_objects', 'prepare_room_boundaries',
    'select_room_boundaries', 'prepare_space_connections', 'review_space_connections',
    'prepare_discovery_review', 'review_discovered_spaces',
})
MUTATING_TOOLS = frozenset({
    'trace_door_schedule',
    'inspect_spatial_boundary', 'measure_spatial_area', 'measure_spatial_relation',
    'inspect_door_sweep', 'measure_door_sweep_relation',
    'measure_spatial_sections', 'measure_elevation_relation',
    'bind_spatial_object',
    'extract_facts', 'analyze_room_batch', 'resolve_room_units',
    'prepare_room_boundaries', 'select_room_boundaries', 'bind_visual_objects',
    'prepare_space_connections', 'review_space_connections',
    'prepare_discovery_review', 'review_discovered_spaces', 'analyze_measurements',
    'get_analysis_results',
}) | CONTROL
RUNTIMES = frozenset({'native', 'responses_subscription', 'codex_subscription', 'codex', 'deepseek_harness'})


def runtime_supports(name, runtime, provider):
    if name not in TOOL_SPECS or runtime not in RUNTIMES:
        return False
    if name in {'finish_analysis', 'request_toolset'}:
        return runtime in {'native', 'responses_subscription'}
    if name in VISUAL_TOOLS:
        return provider == 'responses_subscription' and runtime in {'responses_subscription', 'codex'}
    return True


def require_supported(name, runtime, provider):
    if not runtime_supports(name, runtime, provider):
        raise ValueError(f'当前运行时或模型通道不支持工具 {name}；请查看工具能力列表。')


def capability_manifest(runtime='native', provider='deepseek'):
    if runtime not in RUNTIMES:
        raise ValueError('未知 AgentRuntime。')
    return [dict(
        name=name, description=description,
        input_schema=schema.model_json_schema(),
        families=sorted(group for group, names in GROUPS.items() if name in names),
        operations=sorted(op for op, names in OPERATION_TOOLS.items() if name in names),
        evidence_kind=('source' if name in READS else 'control' if name in CONTROL else 'candidate_or_tool_observation'),
        requires_vision=name in VISUAL_TOOLS,
        changes_session_state=name in MUTATING_TOOLS,
        retry_policy='checkpoint_then_resume' if name in MUTATING_TOOLS else 'explicit_retry',
        pagination_fields=[key for key in ('offset', 'limit', 'row_offset', 'row_limit', 'start') if key in schema.model_fields],
        timeout_policy='session_deadline_cooperative',
        response_character_limit=50000,
        supported=runtime_supports(name, runtime, provider),
        runtime=runtime, provider=provider,
    ) for name, (schema, description) in TOOL_SPECS.items()]
