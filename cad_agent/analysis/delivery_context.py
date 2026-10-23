"""Bounded synthesis packets, preserving exact whole quotes and explicit omissions."""
from copy import deepcopy
from cad_agent.analysis.efficiency import dumps
from cad_agent.analysis.assembly_delivery import delivery_fields


def _references(value):
    if isinstance(value, list):
        return [_references(v) for v in value]
    if not isinstance(value, dict):
        return value
    if 'evidence_id' in value:
        return {'evidence_id': value['evidence_id']}
    return {k: _references(v) for k, v in value.items()
            if k not in ('bbox', 'source_lines', 'row_separators', 'table_bbox', 'entity_ids')}


def assembly_observation(observation):
    """A source record fits with its citations; geometry stays in the stored record.

    Never project instance applicability from the existence of a source binding.
    Page coverage, external references and source units remain independent limits.
    """
    if observation.get('tool') != 'read_assembly':
        return observation
    data = observation['data']
    binding = data.get('source_binding')
    keys = ('record_id', 'target_region_id', 'snapshot_id', 'heading_start', 'view', 'relation_basis', 'offset',
            'total', 'has_more', 'next_offset', 'checks', 'diagnostics',
            'title_evidence', 'reference_reads', 'source_binding', 'source_scope',
            'default_unit_clauses', 'unit_candidates')
    projected = _references({k: data[k] for k in keys if k in data})
    # The complete header evidence and role mapping suffice here; source geometry
    # is reproducible through observation_id and must not displace the source text.
    if binding:
        projected['source_binding'].pop('headers', None)
    projected['items'] = [_references({k: item[k] for k in (
        'order', 'region_id', 'column_index', 'source_field_roles',
        'source_binding_state', 'text_span', 'evidence', 'truncated') if k in item})
        for item in data.get('items', [])]
    if binding:
        projected['source_binding_state'] = binding['state']
        projected['source_layer_order'] = binding.get('layer_order', 'unverified')
        projected['engineering_applicability_state'] = binding.get('engineering_applicability_state', 'unverified')
        # Old persisted observations can contain these legacy ambiguous fields.
        projected['checks'] = {k: v for k, v in projected.get('checks', {}).items()
                               if k not in ('ownership', 'layer_order')}
    projected['delivery_scope'] = '来源构造记录；逐项提取并引用原文。来源内绑定不等于工程实例适用性；未消费的已读内容属于整理缺口。'
    return {**observation, 'data': projected}


def _reference_ids(value):
    if isinstance(value, dict):
        if 'evidence_id' in value:
            return {value['evidence_id']}
        return set().union(*(_reference_ids(v) for v in value.values())) if value else set()
    if isinstance(value, list):
        return set().union(*(_reference_ids(v) for v in value)) if value else set()
    return set()


def bounded_packet(packet, limit, steps=(), *, include_templates=True):
    out=deepcopy(packet)
    sources = {e['evidence_id']: e for e in out.get('evidence', [])}
    observations = []
    for observation in out.get('observations', []):
        projected = assembly_observation(observation)
        if observation.get('tool') == 'read_assembly' and include_templates:
            owners = [s['id'] for s in steps if observation['observation_id'] in s.get('observation_ids', []) and 'id' in s]
            fields = delivery_fields(observation['data'], sources, owners)
            if fields:
                projected['data']['delivery_fields'] = fields
                projected['data']['delivery_contract'] = (
                    '按实际问题选择字段模板并填写 question_part_ids，再经过 finish 校验。'
                    'candidate_requirement_ids 仅说明读取关联，不是字段归属或完成证明；'
                    '未读分页、外部引用和工程实例适用性继续保留缺口。')
        observations.append(projected)
    out['observations']=observations
    for observation in out['observations']:
        if observation.get('tool') == 'read_assembly':
            observation['question_part_ids'] = [s['id'] for s in steps
                if observation['observation_id'] in s.get('observation_ids', []) and 'id' in s]
    # Deduplicate repeated geometry/quotes before deciding a whole record cannot fit.
    # This projection also applies below the cap, avoiding contradictory old fields.
    if len(dumps(out))<=limit:return out
    evidence=out.pop('evidence',[]);observations=out.pop('observations',[])
    contexts=out.pop('source_contexts',[])
    out['evidence']=[];out['observations']=[];out['source_contexts']=[]
    out['context_omissions']={'evidence_count':len(evidence),'observation_count':len(observations),
        'source_context_count':len(contexts),'note':'完整记录已保存。未装入本次整理包不等于不存在；不得声称穷尽来源或完整完成。'}
    # Menus/attempt histories are not source evidence. Keep the newest attempt
    # separately in the runtime's paired tool exchange.
    for key in ('reads','attempts','last_search','pending_visual_objects'):
        out.pop(key,None)
    byid={e['evidence_id']:e for e in evidence};order=[]
    # Prefer the containing whole source span to its later token-sized table cells.
    # IDs remain unchanged, and smaller citations may still be included afterwards.
    def enclosing(eid):
        e=byid[eid];span=e.get('text_span')
        if not span:return eid
        candidates=[v for v in evidence if v.get('region_id')==e.get('region_id')
                    and v.get('snapshot_id')==e.get('snapshot_id') and v.get('text_span')
                    and v['text_span'][0]<=span[0] and v['text_span'][1]>=span[1]
                    and len(v.get('quote',''))>=len(e.get('quote',''))]
        return max(candidates,key=lambda v:len(v.get('quote','')))['evidence_id'] if candidates else eid
    queues=[[eid for eid in reversed(s.get('evidence_ids',[])) if eid in byid] for s in steps]
    while any(queues):
        for q in queues:
            if q:
                eid=q.pop(0)
                if enclosing(eid) not in order:order.append(enclosing(eid))
    order.extend(e['evidence_id'] for e in reversed(evidence) if e['evidence_id'] not in order)
    def add(key,item,budget):
        out[key].append(item)
        if len(dumps(out))>budget:out[key].pop();return False
        return True
    # Keep source binding and its whole citations atomic. A reference-only record
    # whose text was omitted cannot be used to assert any construction parameter.
    retained=set()
    for o in observations:
        if o.get('tool')!='read_assembly':continue
        refs=_reference_ids(o)
        if not refs<=byid.keys():continue
        linked=[byid[eid] for eid in byid if eid in refs and eid not in retained]
        before=len(out['evidence'])
        out['evidence'].extend(linked)
        if add('observations',o,limit-1000):retained.update(refs)
        else:del out['evidence'][before:]
    for eid in order:
        if eid not in retained and add('evidence',byid[eid],limit*.72 if observations else limit-1000):
            retained.add(eid)
    priority={'calculate_facts','analyze_measurements','measure_geometry','measure_room_area',
        'select_room_boundaries','review_space_connections','review_discovered_spaces','read_dimensions'}
    obs=sorted(reversed(observations),key=lambda o:o['tool'] not in priority)
    for o in obs:
        if o['tool']!='read_assembly':add('observations',o,limit-500)
    included={e['evidence_id'] for e in out['evidence']}
    for eid in order:
        if eid not in included:add('evidence',byid[eid],limit-500)
    for c in reversed(contexts):add('source_contexts',c,limit-500)
    out['context_omissions'].update(evidence_count=len(evidence)-len(out['evidence']),
        observation_count=len(observations)-len(out['observations']),source_context_count=len(contexts)-len(out['source_contexts']))
    # The immutable question and task requirements have small schema bounds;
    # if metadata nevertheless expands, retain the actual task text and gaps.
    if len(dumps(out))>limit:
        out={k:v for k,v in out.items() if k in ('question','task','evidence','observations','context_omissions','instruction','validation_issue','answer_contract','field_issues','validated_claims','validated_user_inputs','user_conditions','repair_targets')}
    if len(dumps(out))>limit:
        out['task']={'subtasks':[{k:s[k] for k in ('id','requirement','status','reason') if k in s} for s in steps]}
    # Previously validated clauses remain cached in the session even when the
    # repair prompt cannot carry every one. Report omissions, never truncate text.
    for key in ('validated_claims', 'repair_targets', 'field_issues'):
        while out.get(key) and len(dumps(out)) > limit:
            out[key].pop()
            count_key = key + '_count'
            out['context_omissions'][count_key] = out['context_omissions'].get(count_key, 0) + 1
    return out
