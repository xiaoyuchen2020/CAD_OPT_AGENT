"""Exact model-side output contract; validation remains owned by delivery.py."""
import json
from cad_agent.analysis.tool_registry import FinalAnswer

GUIDANCE = '''最终输出必须符合下方真实JSON Schema，禁止用自然语言猜测字段结构。
原文摘录及定位的最小完整交付：一条source结论写自足的逐字原文，evidence_ids引用支持该全文的已读原生片段，question_part_ids同时覆盖内容和引用定位分项，query_target_ids覆盖对应真实目标。原图位置由该引用自带的来源锚点交付，无须再添加一条布局/坐标/序号的工具观测结论。若内容已足够回答，不为补写位置说明反复检索。只引用原文时 observation_ids 必须为空；不能因为原文是工具返回的，就把 evidence_basis 改成 tool_observation。不要给未请求的条文号或坐标增加数值断言。独立数字图元与正文相邻不证明“第几项/条”的序号关系，不能将其拼接进原文或用包含相同数字的引用代替关系证据。原文摘录应逐字采用完整原生正文片段；用户要求的定位由该片段的原图锚点交付。
plan_task的顶层只接收steps及工具允许的query_intent；unresolved_requirements只能放query_intent内。普通单属性原文查找、完整引用和来源定位，计划用operations=[]、coverage=[]并以actions描述读取核查；这不省略requirement_id或最终逐项交付。selection是按条件筛选集合，不能用它表示选择读取工具或选引用。coverage仅用于用户显式的对象×属性清单，不把“如何表述”等疑问措辞当成原图必须逐字包含的属性。真正的筛选、运算和显式枚举仍须保留操作与检查项。
evidence_roles可省略；若提供，只能用subject/property/value/scope/relation，值为已读取证据ID数组，绝不能填对象名称、数值或“完整原文”。
missing_information为字符串数组。每个原始要求都必须有requirement_outcomes，并在claims.question_part_ids中覆盖；未完成必须明确partial/unresolved和真实缺口，不能补造fulfilled。
定位由evidence_ids生成的原图引用链接交付。正文不写r1/e1/内部区域ID、阅读顺序号；只有用户另行要求布局/坐标等工具元数据时，才将纯元数据另用tool_observation和实际observation_ids交付，不能在该条重述原文事实或原图数值。每条结论完整自足，不用上述/同上。
引用内容应是已读证据中确实存在的原文。区域若串入邻栏文字，应查看原生实体或局部视图核实条文边界，不能把相邻无关条款当成用户要求的完整条文。未知边界必须保留缺口。
read_source_context的native_fragments提供原生图元的逐字引用及位置；有跨栏混入时，核对这些片段并选择真正支持该条结论的证据ID，不能继续引用包含无关旁栏的整个区域。单图原文查找不自动要求证明全图唯一或确认跨图工程归属；只交付用户要求的原文和可核查定位，不增造穷尽性结论。
证据内容与用户要求分开：用户问题中的value必须是question_quote内连续原字串，question_quote必须是用户原句连续子串；不改写否定、不拼接简称。后续工具省略query_intent以复用已登记意图，不重新发明intent_id。'''

def contract_message():
    return {'role':'system','content':GUIDANCE+'\n最终答案JSON Schema：'+json.dumps(FinalAnswer.model_json_schema(),ensure_ascii=False)}
