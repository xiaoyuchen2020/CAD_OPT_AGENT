"""Source-bound planning metrics and building schedules, without inferred totals.

Only explicit labels/headers bind fields. No geometry, model calls, cross-table
merging or fill-down. A merged source cell retains its identity and span.
"""
import re
import unicodedata
from decimal import Decimal

from cad_agent.contracts import stable_id
from cad_agent.analysis.general import DrawingIndex
from cad_agent.analysis.source_record_store import verify_sources, read_table_page
from cad_agent.analysis.business_table_contracts import METRICS, BUILDING
from cad_agent.semantics.contracts import SemanticTable
from cad_agent.semantics.tables import grid, TABLE_VERSION

VERSION = 'business-tables-v2-source-grid'
ALIASES = {
    'land_area': ('用地面积', '总用地面积', '建设用地面积', '规划建设用地面积', '净用地面积'),
    'total_building_area': ('总建筑面积', '建筑总面积', '建筑面积', '总面积'),
    'counted_area': ('计容', '计容面积', '计容建筑面积', '计容总建筑面积', '计入容积率建筑面积'),
    'excluded_area': ('不计容', '不计容面积', '不计容建筑面积', '不计入容积率建筑面积'),
    'green_area': ('绿地面积', '绿化面积'),
    'footprint_area': ('基底', '基底面积', '建筑基底面积', '建筑占地面积'),
    'floor_area_ratio': ('容积率',), 'building_density': ('建筑密度',),
    'green_ratio': ('绿地率', '绿化率'),
    'parking_spaces': ('停车位', '停车位数量', '停车位数', '停车泊位', '机动车停车位',
                       '非机动车停车位', '机动车停车位数', '非机动车停车位数', '机动车位', '非机动车位',
                       '机动车位数量','非机动车位数量','地上停车位','地下停车位','装卸停车位'),
    'ordinal': ('序号', '编号', '建筑编号'), 'name': ('名称', '建筑名称', '建筑物名称', '建构筑物名称', '单体名称'),
    'use': ('使用性质', '使用功能', '建筑功能', '建筑性质'),
    'storeys': ('层数', '楼层数', '建筑层数', '地上层数', '地下层数'),
    'height': ('高度', '建筑高度', '建筑物高度'),
}
UNITS = {'㎡': 'm2', 'm²': 'm2', 'm2': 'm2', '平方米': 'm2',
         '万平方米': '万m2', '万㎡': '万m2', 'hm2': 'hm2', '公顷': 'hm2', '亩': '亩',
         'mm': 'mm', '毫米': 'mm', 'cm': 'cm', '厘米': 'cm', 'm': 'm', '米': 'm',
         '%': '%', '％': '%', '个': '个', '辆': '辆', '位': '位', '个车位': '位', '层': '层'}
TITLE = {'planning_metrics': re.compile('经济技术指标|技术经济指标|规划指标|主要技术指标'),
         'building_schedule': re.compile(r'建[（(]?构[）)]?筑物.*(?:一览|明细|汇总)|建筑物.*(?:一览|明细|汇总)|单体.*一览')}
VALUE_HEADERS = {'指标', '数值', '数量', '指标值', '设计值', '设计指标', '规划值', '规划指标',
                 '控制值', '控制指标', '规定值', '要求值', '报建值', '实际值', '合计', '总计',
                 '地上', '地下', '地面', '总数', '总量'}
LABEL_HEADERS = {'项目', '名称', '指标名称', '项目名称', '指标项目', '内容', '类别'}


def clean(text):
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC', text)).replace('（', '(').replace('）', ')')


def label(text):
    return re.sub(r'\([^)]*\)', '', clean(text)).rstrip(':：')


def field_key(path, allowed):
    for text in reversed(path):
        name = label(text)
        for key in allowed:
            if name in ALIASES[key]:
                return key
    return None


def units_in(text):
    value = clean(text)
    units = {clean(k): v for k, v in UNITS.items()}
    parts = re.findall(r'\(([^)]+)\)', value)
    if value.startswith('单位:'): parts.append(value[3:])
    parts.append(value)
    return sorted({units[p] for p in parts if p in units})


def _refs(cells):
    return list({e['evidence_id']: e for c in cells for e in c.get('evidence', [])}.values())


def _headers(index, table, kind):
    cache=getattr(index,'_semantic_grids',{})
    cache_key=(TABLE_VERSION,table['table_id'],tuple(table['bbox']))
    if cache_key not in cache: cache[cache_key]=grid(index,table)
    index._semantic_grids=cache
    cells, n, m, diagnostics = cache[cache_key]
    rows = []
    for r in range(min(n, 4)):
        row = [c for c in cells if c['row_index'] == r]
        texts = [label(c['text']) for c in row if c['text'].strip()]
        if not texts or any(re.match(r'^[+\-]?\d', t) for t in texts): break
        title = len(row) == 1 and row[0]['column_span'] == m and TITLE[kind].search(row[0]['text'])
        known = [t in LABEL_HEADERS | VALUE_HEADERS | {'单位', '序号', '备注', '计容', '不计容', '基底'}
                 or field_key([t], BUILDING if kind=='building_schedule' else METRICS) for t in texts]
        schedule_header = (kind=='building_schedule' and
            any(field_key([t],BUILDING)=='name' for t in texts) and sum(bool(k) for k in known)>=3
            and sum(bool(k) for k in known)*2>=len(texts))
        if title or schedule_header or (len(texts) >= 2 and all(known)):
            rows.append(r)
        else: break
    return rows


def _scope(index, table, args):
    index.scope(args.layout_id, args.frame_id)
    if args.layout_id and table['layout_id'] != args.layout_id:
        raise ValueError('表格不属于所选布局。')
    frames = [f for f in index.drawing['frames'] if f['layout_id'] == table['layout_id'] and
              f['bbox'][0] <= table['bbox'][0] and f['bbox'][1] <= table['bbox'][1] and
              f['bbox'][2] >= table['bbox'][2] and f['bbox'][3] >= table['bbox'][3]]
    if args.frame_id and not any(f['frame_id'] == args.frame_id for f in frames):
        raise ValueError('表格不完全属于所选图框。')
    return dict(snapshot_id=index.snapshot, layout_id=table['layout_id'], table_id=table['table_id'],
                frame_id=args.frame_id or (frames[0]['frame_id'] if len(frames) == 1 else None),
                bbox=table['bbox'], project_identity_state='unverified')


def _discover(index, args, kind):
    candidates = []
    allowed = METRICS if kind == 'planning_metrics' else BUILDING
    for table in index.tables:
        try: scope = _scope(index, table, args)
        except ValueError: continue
        # Existing table envelope only; nearby titles never establish ownership.
        cells = [c for row in table['rows'] for c in row['cells']]
        keys = {field_key([c['text']], allowed) for c in cells} - {None}
        titled = any(TITLE[kind].search(c['text']) for c in cells)
        matches = len(keys) >= 2 if kind == 'planning_metrics' else 'name' in keys and len(keys) >= 3
        if not (matches or titled): continue
        candidates.append(dict(table_id=table['table_id'], source_scope=scope,
            state='candidate', matched_fields=sorted(keys), evidence=_refs([c for c in cells if field_key([c['text']],allowed) or TITLE[kind].search(c['text'])][:30]),
            suggested_header_rows=_headers(index, table, kind),
            next_action=dict(tool='extract_'+kind, arguments=dict(table_id=table['table_id'],snapshot_id=index.snapshot,
                layout_id=args.layout_id, frame_id=args.frame_id, requested_fields=args.requested_fields))))
    page = candidates[args.offset:args.offset + args.limit]
    more = args.offset + len(page) < len(candidates)
    return dict(version=VERSION, snapshot_id=index.snapshot, kind=kind, mode='discovery',
        candidates=page, total=len(candidates), has_more=more,
        next_offset=args.offset + len(page) if more else None,
        next_actions=[dict(tool='extract_'+kind, arguments=args.model_dump() | {'offset':args.offset+len(page)})] if more else [],
        coverage='indexed_ruled_table_candidates_only', scope_exhaustive=False,
        note='候选表不等于目标表；未匹配不证明图纸不存在该数据。')


def parse_value(raw, key):
    """Lossless scalar parsing; comparators remain separate from the operand."""
    raw = raw.strip()
    text = clean(raw)
    # DXF TEXT uses %%% for a literal percent; keep raw_text and evidence intact.
    if re.fullmatch(r'[约≤≥<>~≈+\-\d.,]+%%%',text):
        from ezdxf.tools.text import plain_text
        text=plain_text(text)
    if key=='storeys':
        if re.fullmatch(r'\d+F',text,re.I): return text[:-1], '', ['层']
        if re.fullmatch(r'(?:-|\d+F)/(?:-|-[1-9]\d*F)',text,re.I):
            return text, 'storey_pair', ['层']
    # Strict grouping prevents 1,2 or 3/4 from silently becoming a scalar.
    match = re.fullmatch(r'(约)?([≤≥<>~≈]?)([+\-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)(.*)', text)
    value = None; qualifier = ''; own_units = []
    textual = key in ('name', 'use', 'ordinal')
    if textual: value = raw or None
    elif match:
        own_units = units_in(match[4])
        if not match[4] or own_units:
            value = format(Decimal(match[3].replace(',', '')), 'f')
            qualifier = (match[1] or '') + (match[2] or '')
    return value, qualifier, own_units


def _keyvalue_column(index, table):
    """Accept headerless key/value rows only with unique, aligned scalars.

    Labels and explicit units bind each row. Two agreeing rows establish the
    value column; arbitrary numeric adjacency or conflicting columns do not.
    """
    cells,n,m,_=grid(index,table)
    columns=[]
    for r in range(n):
        row=[c for c in cells if c['row_index']==r]
        labels=[c for c in row if field_key([c['text']],METRICS)]
        if not labels: continue
        key=field_key([labels[-1]['text']],METRICS)
        values=[c for c in row if c not in labels and parse_value(c['text'],key)[0] is not None]
        if len(values)!=1: continue
        # Area units cannot come from drawing coordinate units or a guess.
        if key.endswith('_area') and not (parse_value(values[0]['text'],key)[2] or
            any(units_in(c['text']) for c in row)): continue
        columns.append(values[0]['column_index'])
    return columns[0] if len(columns)>=2 and len(set(columns))==1 else None


def _value(cell, key, labels=(), unit_cells=(), diagnostics=()):
    raw=cell['text'].strip()
    value,qualifier,own_units=parse_value(raw,key)
    textual=key in ('name','use','ordinal')
    header_units = {u for t in cell['header_path'] for u in units_in(t)}
    label_units = {u for c in labels for u in units_in(c['text'])}
    row_units = {u for c in unit_cells for u in units_in(c['text'])}
    units = sorted(header_units | label_units | row_units | set(own_units))
    count_basis = []
    if not units and key in ('storeys','parking_spaces'):
        count_basis = _refs(labels) if labels else cell['header_evidence']
        if count_basis: units = ['层' if key=='storeys' else '位']
    errors = list(diagnostics)
    if len(units) > 1: errors.append('UNIT_CONFLICT')
    if not textual and value is not None and not units and key != 'floor_area_ratio': errors.append('UNIT_UNKNOWN')
    allowed_units = ({'m2','万m2','hm2','亩'} if key.endswith('_area') else
                     {'m','cm','mm'} if key=='height' else {'%'} if key in ('green_ratio','building_density') else
                     {'层'} if key=='storeys' else {'个','辆','位'} if key=='parking_spaces' else set())
    if not textual and units and not set(units)<=allowed_units: errors.append('UNEXPECTED_UNIT')
    if raw and value is None: errors.append('NON_SCALAR_VALUE')
    if cell['column_span'] > 1: errors.append('MERGED_VALUE_COLUMNS')
    if not cell['evidence']: errors.append('VALUE_SOURCE_MISSING')
    state = 'missing' if not raw else 'conflict' if 'UNIT_CONFLICT' in errors else 'ambiguous' if errors else 'observed'
    display=raw
    normalizations=[]
    if own_units==['%'] and raw.endswith('%%%') and value is not None:
        display=qualifier+value+'%'
        normalizations.append('DXF_LITERAL_PERCENT')
    return dict(field=key, raw_text=raw, value=value, qualifier=qualifier,
        display_text=display, normalizations=normalizations,
        unit=units[0] if len(units) == 1 else None, unit_candidates=units,
        unit_state='explicit' if len(units) == 1 else 'dimensionless' if not units and key=='floor_area_ratio' else 'conflict' if units else 'unknown',
        unit_basis='count_label' if count_basis else 'source_declaration',
        state=state, cell_id=cell['cell_id'], row_index=cell['row_index'], column_index=cell['column_index'],
        row_span=cell['row_span'], column_span=cell['column_span'], continuation_of=cell['continuation_of'],
        bbox=cell['bbox'], header_path=cell['header_path'],
        source_labels=[c['text'] for c in labels], evidence=cell['evidence'],
        header_evidence=cell['header_evidence'], label_evidence=_refs(labels),
        unit_evidence=list({e['evidence_id']:e for e in cell.get('unit_evidence', []) +
            (cell['evidence'] if own_units else []) + (_refs(labels) if label_units else []) +
            (_refs(unit_cells) if row_units else []) + (cell['header_evidence'] if header_units else []) + count_basis}.values()),
        diagnostics=sorted(set(errors)))


def _completion(fields, requested):
    observed = {f['field'] for f in fields if f['state']=='observed'}
    present = {f['field'] for f in fields}
    return dict(requested_fields=list(requested), missing_fields=sorted(set(requested)-present),
        unresolved_fields=sorted({f['field'] for f in fields if f['state']!='observed'}),
        fields_complete=set(requested)<=observed and all(f['state']=='observed' for f in fields))


def _conflicts(fields):
    groups = {}
    for f in fields:
        key = (f['field'], tuple(f.get('source_labels',[])), tuple(f.get('qualifiers',[])), f.get('value_kind'))
        groups.setdefault(key, []).append(f)
    for group in groups.values():
        if len({(f['value'], f['unit'], f['qualifier']) for f in group if f['value'] is not None}) > 1:
            for f in group:
                f['state']='conflict'; f['diagnostics'].append('MULTIPLE_VALUES_SAME_SOURCE_KEY')


def _metrics(page, requested):
    fields=[]; unresolved=[]
    for row in page['items']:
        cells=[c for c in row['cells'] if not c['is_header']]
        labels=[c for c in cells if field_key([c['text']], METRICS)]
        if not labels:
            mapped=[(c,field_key(c['header_path'],METRICS)) for c in cells]
            recognised=[(c,k) for c,k in mapped if k]
            if recognised:
                scope_cells=[c for c,k in mapped if not k and c['text'].strip()]
                for cell,key in recognised:
                    if key not in requested: continue
                    f=_value(cell,key,diagnostics=page['diagnostics'])
                    f['value_kind']='unspecified'
                    f['qualifiers']=[c['text'] for c in scope_cells]
                    f['scope_evidence']=_refs(scope_cells)
                    f['record_id']=stable_id('metric-source',page['snapshot_id'],page['table_id'],cell['cell_id'],key)
                    fields.append(f)
            elif any(c['text'].strip() for c in cells): unresolved.append(row)
            continue
        keys=[field_key([c['text']], METRICS) for c in labels]
        # A parent total-area cell may explicitly own counted/excluded subrows.
        if len(set(keys))>1 and not (set(keys)<={'total_building_area','counted_area'} or set(keys)<={'total_building_area','excluded_area'}):
            unresolved.append(row); continue
        key=next((k for k in keys if k in ('counted_area','excluded_area')), keys[-1])
        if key not in requested: continue
        unit_cells=[c for c in cells if any(label(h)=='单位' for h in c['header_path']) or
                    (page.get('keyvalue_column') is not None and units_in(c['text']))]
        values=[c for c in cells if c not in labels and any(label(h) in VALUE_HEADERS for h in c['header_path'])]
        if not values and page.get('keyvalue_column') is not None:
            values=[c for c in cells if c not in labels and c['column_index']==page['keyvalue_column'] and c['column_span']==1]
        if not values: unresolved.append(row); continue
        for cell in values:
            f=_value(cell,key,labels,unit_cells,page['diagnostics'])
            headers=[label(h) for h in cell['header_path']]
            f['value_kind']=next((h for h in reversed(headers) if h not in {'指标','指标值','数值','数量'}), 'unspecified')
            f['qualifiers']=[c['text'] for c in cells if c not in labels+values+unit_cells and c['text'].strip()]
            f['record_id']=stable_id('metric-source',page['snapshot_id'],page['table_id'],cell['cell_id'],key)
            fields.append(f)
    _conflicts(fields)
    return fields, [], [], unresolved


def _buildings(page, requested):
    records=[]; summaries=[]; unresolved=[]
    for row in page['items']:
        cells=[c for c in row['cells'] if not c['is_header']]
        if not any(c['text'].strip() for c in cells): continue
        mapped=[(c,field_key(c['header_path'],BUILDING)) for c in cells]
        identity=[c for c,k in mapped if k in ('name','ordinal')]
        is_total=any(label(c['text']) in ('合计','总计','小计') for c in identity)
        fields=[]
        for c,key in mapped:
            if key not in requested: continue
            f=_value(c,key,diagnostics=page['diagnostics'])
            f['qualifiers']=[h for h in c['header_path'] if label(h) not in ALIASES[key]]
            fields.append(f)
        _conflicts(fields)
        record=dict(record_id=stable_id('building-source',page['snapshot_id'],page['table_id'],str(row['row_index'])),
            row_index=row['row_index'], record_type='summary' if is_total else 'building_schedule_row',
            identity=[dict(text=c['text'],cell_id=c['cell_id'],row_span=c['row_span'],continuation_of=c['continuation_of'],evidence=c['evidence']) for c in identity],
            identity_state='source_bound' if any(c['text'].strip() for c,k in mapped if k=='name') else 'unresolved',
            engineering_identity_state='unverified', fields=fields,
            unmapped_cells=[c for c,k in mapped if k is None], **_completion(fields,requested))
        if is_total: summaries.append(record)
        elif record['identity_state']=='unresolved': unresolved.append(row)
        else: records.append(record)
    return [f for r in records for f in r['fields']], records, summaries, unresolved


def extract(workspace, snapshot_id, args, kind, index=None):
    if args.snapshot_id and args.snapshot_id!=snapshot_id: raise ValueError('请求快照与入口快照不匹配。')
    index=index or DrawingIndex(workspace,snapshot_id)
    if index.snapshot != snapshot_id: raise ValueError('索引快照不匹配。')
    index.scope(args.layout_id,args.frame_id)
    if not args.table_id: return _discover(index,args,kind)
    table=next((t for t in index.tables if t['table_id']==args.table_id),None)
    if table is None: raise ValueError('表格不属于当前快照。')
    scope=_scope(index,table,args)
    headers=args.header_rows if args.header_rows is not None else _headers(index,table,kind)
    keyvalue_column=_keyvalue_column(index,table) if not headers and kind=='planning_metrics' else None
    if not headers and keyvalue_column is None:
        return dict(version=VERSION,snapshot_id=snapshot_id,kind=kind,mode='extraction',source_scope=scope,
            table_id=args.table_id,fields=[],records=[],diagnostics=['HEADER_UNRESOLVED'],
            page_state='unread',fields_complete=False,scope_exhaustive=False,
            next_actions=[dict(tool='read_table',arguments=dict(table_id=args.table_id))])
    page, cache_hit=read_table_page(workspace,index,SemanticTable(table_id=args.table_id,header_rows=headers,
        row_offset=args.row_offset,row_limit=args.row_limit))
    if keyvalue_column is not None:
        page['keyvalue_column']=keyvalue_column
        page['diagnostics']=[d for d in page['diagnostics'] if d!='HEADER_UNRESOLVED']
    verify_sources(page,index)
    requested=args.requested_fields or (METRICS if kind=='planning_metrics' else BUILDING)
    fields,records,summaries,unresolved=(_metrics if kind=='planning_metrics' else _buildings)(page,requested)
    next_offset=page['next_offset']
    complete=_completion(fields,requested)
    if records: complete['fields_complete']=all(r['fields_complete'] for r in records)
    complete['fields_complete']=bool(fields) and complete['fields_complete'] and not unresolved
    row_end = next_offset if page['has_more'] else page['row_count']
    return dict(version=VERSION,snapshot_id=snapshot_id,source_sha256=index.drawing['sha256'],kind=kind,
        mode='extraction',source_scope=scope,table_id=args.table_id,header_rows=headers,
        header_selection='row_labels_and_units' if keyvalue_column is not None else 'explicit' if args.header_rows is not None else 'inferred_from_labels',
        fields=fields if kind=='planning_metrics' else [], records=records,summary_rows=summaries,
        unresolved_rows=unresolved,diagnostics=page['diagnostics'],cache_hit=cache_hit,
        row_count=page['row_count'],row_offset=args.row_offset,row_end=row_end,has_more=page['has_more'],next_offset=next_offset,
        page_state='complete', table_read_complete=not page['has_more'] and args.row_offset==0,
        completeness_scope='returned_page_only',scope_exhaustive=False, **complete,
        next_actions=[dict(tool='extract_'+kind,arguments=args.model_dump()|{'snapshot_id':snapshot_id,'row_offset':next_offset})] if page['has_more'] else [],
        note='只提取表中明确字段；字段完整性仅针对当前页。合并格保留同一来源，空白不填充；不计算缺项，不确认工程身份或项目总量。')


def extract_planning_metrics(workspace,snapshot_id,args,index=None):
    return extract(workspace,snapshot_id,args,'planning_metrics',index)


def extract_building_schedule(workspace,snapshot_id,args,index=None):
    return extract(workspace,snapshot_id,args,'building_schedule',index)
