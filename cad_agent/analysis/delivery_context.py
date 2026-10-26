"""Bounded synthesis packets, preserving exact whole quotes and explicit omissions."""
from copy import deepcopy
from cad_agent.analysis.efficiency import dumps
from cad_agent.analysis.assembly_delivery import delivery_fields
from cad_agent.analysis.business_table_context import TOOLS as BUSINESS_TOOLS, project as business_page, unique_pages
from cad_agent.analysis.context_policy import (resolve_context_policy, project_observation,
    reference_dependencies, observation_closure)


def _references(value):
    if isinstance(value, list):
        return [_references(v) for v in value]
    if not isinstance(value, dict):
        return value
    if 'evidence_id' in value:
        return {'evidence_id': value['evidence_id']}
    return {k: _references(v) for k, v in value.items()
            if k not in ('bbox', 'source_lines', 'row_separators', 'table_bbox', 'entity_ids')}


def assembly_observation(observation, policy=None):
    """A source record fits with its citations; geometry stays in the stored record.

    Never project instance applicability from the existence of a source binding.
    Page coverage, external references and source units remain independent limits.
    """
    if observation.get('tool') != 'read_assembly' or (policy is not None and policy.full_numbers):
        return observation
    data = observation['data']
    binding = data.get('source_binding')
    # Keep unknown/new semantic fields. Only known CAD display/geometry copies
    # are removable from this source-only view; headers retain their source refs.
    projected = _references({k: v for k, v in data.items()
                             if k not in ('items', 'preview_url', 'highlight_action')})
    projected['items'] = [_references(item) for item in data.get('items', [])]
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
    return reference_dependencies(value)[0]


def bounded_packet(packet, limit, steps=(), *, include_templates=True):
    out=deepcopy(packet)
    sources = {e['evidence_id']: e for e in out.get('evidence', [])}
    from cad_agent.analysis.typed_requirements import requirement_type
    policy = resolve_context_policy(
        [requirement_type(c, step) for step in steps for c in step.get('coverage', [])],
        [op for step in steps for op in step.get('operations', [])],
        [o.get('tool') for o in out.get('observations', [])])
    observations = []
    projection_diagnostics = []
    for observation in out.get('observations', []):
        view = project_observation(observation, policy)
        projection_diagnostics.extend(view.diagnostics)
        projected = assembly_observation(view.payload, policy)
        if observation.get("tool") in BUSINESS_TOOLS and not policy.spatial:
            projected={**projected,"data":business_page(projected["data"])}
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
    referenced_observations = set().union(*(reference_dependencies(o)[1] for o in observations)) if observations else set()
    observations=unique_pages(observations, protected=referenced_observations)
    out['observations']=observations
    for observation in out['observations']:
        if observation.get('tool') == 'read_assembly':
            observation['question_part_ids'] = [s['id'] for s in steps
                if observation['observation_id'] in s.get('observation_ids', []) and 'id' in s]
    # Validate dependency closure even below the budget. An archived ID without
    # its source definition cannot support a visible field or calculation.
    by_observation = {o['observation_id']: o for o in observations}
    missing_dependencies = []
    valid = []
    for observation in observations:
        linked, refs, missing = observation_closure(observation, by_observation)
        absent = (refs - sources.keys()) | missing
        if absent:
            missing_dependencies.append(dict(observation_id=observation['observation_id'],
                                             missing_ids=sorted(absent)))
        else:
            valid.append(observation)
    out['observations'] = valid
    if missing_dependencies:
        out['context_dependency_gaps'] = missing_dependencies
        invalid_ids = {item['observation_id'] for item in missing_dependencies}
        invalid_refs = set().union(*(_reference_ids(o) for o in observations if o['observation_id'] in invalid_ids))
        valid_refs = set().union(*(_reference_ids(o) for o in valid)) if valid else set()
        # Keep a source if a surviving complete record also needs it.
        removed_refs = invalid_refs - valid_refs
        out['evidence'] = [e for e in out.get('evidence', []) if e['evidence_id'] not in removed_refs]
        omissions = out.setdefault('context_omissions', {})
        omissions['observation_count'] = omissions.get('observation_count', 0) + len(missing_dependencies)
        omissions['evidence_count'] = omissions.get('evidence_count', 0) + len(removed_refs & sources.keys())
    if projection_diagnostics:
        out['context_projection_diagnostics'] = sorted(set(projection_diagnostics))
    if len(dumps(out)) <= limit:
        return out
    evidence=out.pop('evidence',[]);observations=out.pop('observations',[])
    contexts=out.pop('source_contexts',[])
    out['evidence']=[];out['observations']=[];out['source_contexts']=[]
    previous_omissions=out.get('context_omissions', {})
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
    # A field/record and all header/unit/calculation dependencies are a single
    # budget unit. Never leave their numeric sources as unlabelled leftovers.
    owned = set().union(*(_reference_ids(o) for o in observations)) if observations else set()
    retained = set()
    selected_observations = set()
    priority = {'read_assembly', 'calculate_facts', 'read_dimensions',
                'extract_planning_metrics', 'extract_building_schedule'}
    for observation in sorted(observations, key=lambda o: o['tool'] not in priority):
        if observation['observation_id'] in selected_observations:
            continue
        linked, refs, missing = observation_closure(observation, by_observation)
        if missing or not refs <= byid.keys():
            continue
        before_e, before_o = len(out['evidence']), len(out['observations'])
        out['evidence'].extend(byid[eid] for eid in byid if eid in refs and eid not in retained)
        out['observations'].extend(o for o in linked if o['observation_id'] not in selected_observations)
        if len(dumps(out)) > limit - 500:
            del out['evidence'][before_e:]
            del out['observations'][before_o:]
        else:
            retained.update(refs)
            selected_observations.update(o['observation_id'] for o in linked)
    for eid in order:
        if eid not in retained and eid not in owned:
            if add('evidence', byid[eid], limit - 500):
                retained.add(eid)
    for c in reversed(contexts):add('source_contexts',c,limit-500)
    out['context_omissions'].update(evidence_count=len(evidence)-len(out['evidence']),
        observation_count=len(observations)-len(out['observations']),source_context_count=len(contexts)-len(out['source_contexts']))
    for key, count in previous_omissions.items():
        if key.endswith('_count') and isinstance(count, int):
            out['context_omissions'][key] = out['context_omissions'].get(key, 0) + count
    # The immutable question and task requirements have small schema bounds;
    # if metadata nevertheless expands, retain the actual task text and gaps.
    if len(dumps(out))>limit:
        out={k:v for k,v in out.items() if k in ('question','task','evidence','observations','context_omissions','context_dependency_gaps','context_projection_diagnostics','instruction','validation_issue','answer_contract','field_issues','validated_claims','validated_user_inputs','user_conditions','repair_targets')}
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
