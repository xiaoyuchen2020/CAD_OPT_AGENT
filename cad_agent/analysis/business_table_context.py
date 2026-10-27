"""Lossless business-page projection: keep bindings and quote each source once."""
import json

TOOLS = {'extract_planning_metrics', 'extract_building_schedule'}


def citation_ids(value):
    """Bundle existing cell sources, without inventing or broadening evidence."""
    if isinstance(value, list):
        return list(dict.fromkeys(eid for item in value for eid in citation_ids(item)))
    if not isinstance(value, dict):return []
    if 'evidence_id' in value:return [value['evidence_id']]
    return citation_ids(list(value.values()))


def unique_pages(observations, *, protected=()):
    """Repeated reads must not displace different pages in final context.

    Compare all projected source content, excluding only execution metadata;
    keep the latest read coverage. Different sources, fields or cells never
    coalesce, even when the page coordinates match.
    """
    seen=set(); kept=[]
    transient={'observation_id','evidence_record','cache_hit','read_coverage','next_actions'}
    for observation in reversed(observations):
        data=observation.get('data',{})
        if observation.get('tool') in TOOLS and data.get('mode')=='extraction':
            key=json.dumps({k:v for k,v in data.items() if k not in transient},
                           ensure_ascii=False,sort_keys=True)
            if key in seen and observation.get("observation_id") not in protected:continue
            seen.add(key)
        kept.append(observation)
    return list(reversed(kept))


def pack_cells(value):
    """Factor repeated raw-cell keys; preserve every value and source role."""
    if isinstance(value,list):return [pack_cells(v) for v in value]
    if not isinstance(value,dict):return value
    out={k:pack_cells(v) for k,v in value.items()}
    for key in ('cells','unmapped_cells'):
        cells=out.get(key)
        if not isinstance(cells,list) or not cells or not all(isinstance(c,dict) for c in cells):continue
        columns=list(dict.fromkeys(k for cell in cells for k in cell))
        # Include explicit key sets: absent and present-null remain distinct.
        out[key]={'columns':columns,'rows':[[c.get(k) for k in columns] for c in cells],
                  'present_columns':[[i for i,k in enumerate(columns) if k in c] for c in cells]}
    return out


def project(data, *, include_sources=False):
    if data.get('kind') not in ('planning_metrics','building_schedule') or data.get('mode')!='extraction':
        return data
    sources={}
    def visit(value):
        if isinstance(value,list):return [visit(v) for v in value]
        if not isinstance(value,dict):return value
        if 'evidence_id' in value:
            if 'quote' in value:
                sources[value['evidence_id']]={k:value[k] for k in
                    ('evidence_id','snapshot_id','region_id','quote','text_span') if k in value}
            return {'evidence_id':value['evidence_id']}
        return {k:visit(v) for k,v in value.items() if k not in
                ('bbox','entity_ids','region_ids','source_parts','component_boxes')}
    packet=visit(data)
    # A numerical row includes its ordinal, name, values and unit headers.
    # Keep an explicit ready-to-cite bundle so delivery does not accidentally
    # cite only area cells while dropping the ordinal's independent source.
    for field in packet.get('fields',[]):
        field['evidence_ids']=citation_ids(field)
    for row in packet.get('records',[])+packet.get('summary_rows',[]):
        row['evidence_ids']=citation_ids([row.get('identity',[]),row.get('fields',[])])
    packet=pack_cells(packet)
    if include_sources:packet['source_evidence']=list(sources.values())
    packet['result_projection']='business_source_page'
    packet['delivery_scope']=('字段state=observed表示原表行内标签、值和单位已绑定，可直接摘录，'
        '不等于工程身份已确认。优先使用value/unit及display_text，raw_text和normalizations保留源编码。'
        '分页按next_actions继续，缺列和歧义如实保留；不要把已绑定字段重标为未读取。'
        '逐行交付records；整行摘录须引用该行evidence_ids，包括序号和名称的独立来源，'
        '不能只引用面积数值。单字段摘录引用该字段的来源、表头和单位。')
    packet['cell_matrix_format']='原始cells/unmapped_cells以columns列名和rows值矩阵表示；present_columns区分不存在的键和显式null。所有原文、跨度和引用均保留。'
    return packet
