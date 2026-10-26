"""Route literal business-table reads without treating area labels as arithmetic."""
import re

METRIC_QUERY = re.compile('经济技术指标|技术经济指标|用地面积|总建筑面积|计容|不计容|绿地面积|基底面积|容积率|建筑密度|绿地率|停车位')
SCHEDULE_QUERY = re.compile(r'建[（(]?构[）)]?筑物.*一览表|建筑物一览表|建筑一览表|单体一览表')
COMPUTATION = re.compile('计算|测量|求和|复核|验算|合规|规范|优化|造价|推算|是否满足|大于|小于|不超过|不少于')


def literal_business_read(text):
    return bool(re.search('读取|提取|摘录|列出|查找|查询',text) and
        (METRIC_QUERY.search(text) or SCHEDULE_QUERY.search(text)) and not COMPUTATION.search(text) and
        not re.search('差值|平均|占比|比例|比值|加权|总和|总厚|除以|乘以|算出|比较|对比|排序|高亮',text))


def business_route(plan, session):
    question = session.request.question
    if COMPUTATION.search(question): return None
    kinds=[]
    schedule=SCHEDULE_QUERY.search(question)
    if METRIC_QUERY.search(question) and (not schedule or METRIC_QUERY.search(question[:schedule.start()]) or
                                         re.search('经济技术指标|技术经济指标',question)):
        kinds.append('planning_metrics')
    if schedule: kinds.append('building_schedule')
    if not kinds: return None
    # The full original question/requirements survive: this is a tool-routing
    # hint, not a grammar that drops unfamiliar filters or mixed requirements.
    return plan | dict(route='business_tables', reason='business_source_read',
        business_kinds=kinds, parts=[dict(id=p['id'],text=p['text']) for p in session.task.parts],
        next_actions=[dict(tool='extract_'+k,arguments={}) for k in kinds],
        completion='agent_field_review_required')


def handoff(session, plan):
    import json
    session.source_record_handoff = (
        '本轮包含业务表来源读取。使用以下业务提取入口，先发现候选，再核对表格并逐页读取。'
        '尚未执行任何读取。保留全部原始要求和筛选条件；不得把提取入口的字段完整状态当作全图完整。'
        '字段歧义、单位、设计/控制值和范围须分别交付；算术另用calculate_facts。'
        '路由数据：' + json.dumps(plan,ensure_ascii=False))
