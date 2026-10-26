"""Executable acceptance contracts shared by planning, routing and delivery.

Operations are capabilities, never a match against an evaluation question. The
model can add operations; lexical lower bounds prevent omission of explicit asks.
Receipts record attempted work separately from successful fulfillment.
"""
import re
from typing import Literal

Operation = Literal['area','perimeter','arithmetic','ranking','selection','highlight',
    'discovery','contours','exclusions','relations','dimensions','geometry','vision','units']

from cad_agent.analysis.tool_metadata import OPERATION_TOOLS as TOOLS


def infer_operations(text):
    if re.match(r'^(不要|不得|不能|不应|勿)',text.strip()):return []
    rules = {
        'perimeter': r'周长|周界|perimeter',
        'arithmetic': r'差值|厚度差|面积差|之差|求和|计算.{0,8}(和|差)|算出.{0,15}差|平均|合计|总和|总厚度|相差|比值|占比|比例|加权指标|面积\s*[/／]\s*(车位|停车)|除以|乘以|ceil\s*\(|floor\s*\(|数量.{0,5}差异',
        'ranking': r'排序|从大到小|从小到大|最大.{0,6}[三四五六七八九十两二一123456789]|最小.{0,6}[三四五六七八九十两二一123456789]|前[三四五六七八九十两二一123456789]',
        'selection': r'只保留|取消高亮|筛选|只显示',
        'discovery': r'不依赖.{0,8}名称|未命名|无名称|几何围合查找|发现空间',
        'exclusions': r'扣除|扣洞',
        'relations': r'连通|隔墙分开|通向|相邻关系',
        'dimensions': r'尺寸链|分尺寸|尺寸标注对照|尺寸界线|总尺寸',
        'geometry': r'测距|净宽|几何距离|测量.{0,12}(宽|长|厚|距离|间距)|(?:间距|宽度|长度|厚度).{0,8}(测量|量出)|量.{0,4}出来',
        'vision': r'看.{0,3}图|局部图|图像核对',
    }
    result={k for k,v in rules.items() if re.search(v,text,re.I)}
    from cad_agent.analysis.business_table_routing import literal_business_read
    if literal_business_read(text): result.discard('arithmetic')
    return sorted(result)


def planned_operations(text, proposed):
    """Dimensions are source annotations; geometric lengths have another route.

    Preserve annotated comparisons even when they also ask for measurement.
    This only resolves the common vocabulary collision for explicit measuring.
    """
    operations=set(proposed)|set(infer_operations(text))
    measuring=re.search(r'测量|测距|量出|量.{0,4}出来|净宽|间距|距离|宽度|长度|厚度',text)
    annotation=re.search(r'尺寸|标注|标高|注记|原文|对照|分段|总长',text)
    if 'dimensions' in operations and measuring and not annotation:
        operations.remove('dimensions');operations.add('geometry')
    annotation_read=re.search(r'(?:读取|提取|对照|查看).{0,8}(?:尺寸|标注)|(?:尺寸|标注).{0,8}(?:读取|提取|对照|宽度|净宽)',text)
    if 'geometry' in operations and annotation_read and not re.search(r'测量|测距|量出|量.{0,4}出来|几何', text):
        operations.remove('geometry');operations.add('dimensions')
    return sorted(operations)


def receipts(name, result):
    """Tool-owned successful results only; planning prose cannot grant receipts."""
    if result.get('error'): return []
    declared=result.get('operation_receipts',[])
    from cad_agent.analysis.tool_metadata import SPATIAL_METHODS
    if name in SPATIAL_METHODS:
        return declared
    if name in ('select_source_records','get_analysis_results','analyze_measurements','calculate_measurements','calculate_facts','check_dimension_chain','inspect_enclosure','review_space_connections','review_discovered_spaces'):
        return declared
    out=[]
    def add(op, fulfilled=True):
        out.append(dict(operation=op,fulfilled=fulfilled,tool=name,
                        reference=result.get('record_id') or result.get('measurement_id') or result.get('batch_id')))
    if name=='inspect_scope_objects':add('geometry',False)
    if name=='prepare_space_connections':
        for op in ('relations','geometry','dimensions','vision'):add(op,False)
    if name=='prepare_discovery_review':
        for op in ('vision','exclusions','contours'):add(op,False)
    if name=='select_room_boundaries':
        items=result.get('measurement',{}).get('items',[])
        add('contours',bool(items) and all(i.get('highlight_kind')=='candidate_boundary' or i.get('candidate_area_m2') is not None or i.get('area_m2') is not None for i in items))
        add('area',bool(items) and all(i.get('candidate_area_m2') is not None or i.get('area_m2') is not None for i in items))
        add('highlight',bool(items));add('vision');add('selection',bool(items))
    if name in ('analyze_room_batch','resolve_room_units') and result.get('unit_resolution'):
        add('units',result['unit_resolution']['status']=='resolved')
    if name=='discover_rooms':
        add('discovery',bool(result.get('items')));add('geometry',bool(result.get('items')))
        if result.get('applied_unit') not in (None,'unknown'):add('units')
    if name=='get_room_contours':add('contours')
    if name=='measure_room_area':
        add('area',result.get('basis_area_m2') is not None)
        add('perimeter',result.get('perimeter_m') is not None)
        if result.get('measurement_basis')=='selected_exclusions':add('exclusions',bool(result.get('selected_contour_ids')))
    if name=='query_building_relations':add('relations',False) # candidate relations need explicit interpretation
    if name=='read_dimensions':add('dimensions',bool(result.get('items'))) # annotations do not measure a requested object's clear width
    if name=='measure_geometry':add('geometry',result.get('status')=='measured')
    if name=='render_region':add('vision');add('highlight',bool(result.get('highlight_entity_ids')))
    return out
