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


def unique_pages(observations):
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
            if key in seen:continue
            seen.add(key)
        kept.append(observation)
    return list(reversed(kept))


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
    if include_sources:packet['source_evidence']=list(sources.values())
    packet['result_projection']='business_source_page'
    packet['delivery_scope']=('字段state=observed表示原表行内标签、值和单位已绑定，可直接摘录，'
        '不等于工程身份已确认。优先使用value/unit及display_text，raw_text和normalizations保留源编码。'
        '分页按next_actions继续，缺列和歧义如实保留；不要把已绑定字段重标为未读取。'
        '逐行交付records；整行摘录须引用该行evidence_ids，包括序号和名称的独立来源，'
        '不能只引用面积数值。单字段摘录引用该字段的来源、表头和单位。')
    return packet
