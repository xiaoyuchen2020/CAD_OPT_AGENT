"""Object-scoped progress and repair advice; never an execution budget or executor."""
import hashlib
import json


def fingerprint(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,default=str).encode()).hexdigest()


def record(ledger,name,args,result):
    request=args.model_dump();bid=request.get('bundle_id') or result.get('bundle_id')
    subject=request.get('hypothesis_id','')
    if not subject:
        subject='sources:'+','.join(sorted(request.get('entity_ids',request.get('primitive_ids',[]))))
    previous=ledger.setdefault('investigation_events',[])
    key=fingerprint([name,request]);error=result.get('error')
    signals=[]
    if not error:
        if name in ('measure_local_geometry','measure_geometry_primitives') and result.get('status')=='local_pair_measured':
            signals=[('local_measurement',fingerprint([sorted(result.get('source_entity_ids',[])),result.get('source_primitive_ids'),result.get('measurements')]))]
        elif name=='analyze_geometry_context':
            signals=[('source_relationship',fingerprint([result.get('label_candidates'),result.get('entity_features')]))]
        elif name=='trace_geometry_network':
            signals=[('topology',fingerprint([result.get('network_id')]))]
        elif name=='resolve_geometry_basis':
            signals=[('basis_evidence',fingerprint(e)) for e in result.get('evidence',[])]
            if result.get('unit_resolution',{}).get('state')=='resolved':signals.append(('unit_basis',fingerprint(result['unit_resolution'])))
        elif name in ('measure_geometry_route','measure_geometry_network_route'):
            signals=[('sampled_profile',fingerprint([s.get('source_entity_ids'),s.get('domain'),s.get('sample_minimum_drawing_units'),request.get('obstacle_entity_ids')])) for s in result.get('segments',[]) if s.get('status')=='sampled_candidate_profile']
        elif name=='record_geometry_decision' and result.get('decision')=='exclude_candidate':
            signals=[('model_candidate_exclusion',fingerprint([sorted(result.get('entity_ids',[])),result.get('hypothesis_id')]))]
    seen={(e['bundle_id'],k,v) for e in previous for k,v in e.get('signals',[])}
    additions=[(k,v) for k,v in signals if (bid,k,v) not in seen]
    event=dict(revision=len(previous)+1,tool=name,bundle_id=bid,subject=subject,call_key=key,
        arguments=request,signals=signals,new_signals=additions,
        progress_kind='new_source_or_geometry_evidence' if any(k!='model_candidate_exclusion' for k,v in additions)
            else 'model_candidate_exclusion' if additions else 'tool_error' if error else 'no_new_measured_evidence',
        error=error,reference_ids={k:v for k,v in result.items() if k in ('measurement_id','network_id','context_analysis_id','basis_id','decision_id','route_id','hypothesis_id','validation_id')})
    previous.append(event)
    return event


def plan(ledger,bid,hid=''):
    events=[e for e in ledger.get('investigation_events',[]) if e['bundle_id']==bid and (not hid or e['arguments'].get('hypothesis_id')==hid or not e['arguments'].get('hypothesis_id'))]
    # Advisory never changes task budgets or skips a required operation.
    relevant=[e for e in events if e['tool'] not in ('plan_geometry_investigation','read_geometry_workset','read_geometry_artifact')]
    stagnant=0
    for e in reversed(relevant):
        if e['new_signals']:break
        stagnant+=1
    decisions=[d for d in ledger.get('decision_ids',{}).values() if d['bundle_id']==bid and (not hid or d.get('hypothesis_id')==hid)]
    superseded={d.get('supersedes') for d in decisions if d.get('supersedes')}
    exclusions=[d for d in decisions if d['decision_id'] not in superseded and d['decision']=='exclude_candidate']
    calls=[];gaps=[]
    def propose(gap,tool,args,reason):
        gaps.append(gap)
        args={'bundle_id':bid,**args}
        # Defaults may differ across schema versions: compare supplied arguments semantically.
        repeated=any(e['tool']==tool and all(e['arguments'].get(k)==v for k,v in args.items()) for e in events)
        calls.append(dict(gap=gap,tool=tool,arguments=args,already_attempted=repeated,reason=reason,
            execution='explicit_model_call',automatic_replay=False))
    hypotheses=[h for h in ledger.get('hypothesis_ids',{}).values() if h['bundle_id']==bid and
                (not hid or h['hypothesis_id']==hid) and h['hypothesis_id'] not in ledger.get('superseded_hypotheses',{})]
    if not hypotheses:propose('object_mapping','analyze_geometry_context',{},'先比较原生标题和局部图示，再登记对象假设。')
    for h in hypotheses:
        ids=h['request']['entity_ids'];hid0=h['hypothesis_id']
        if not ids:
            propose('geometry_binding','read_geometry_bundle',dict(view='summary',limit=12),
                '当前假设只有标签；先选取源实体并显式修订假设，再请求连通追踪。禁止提交空实体集合。')
        propose('boundary_role','analyze_geometry_context',dict(entity_ids=ids,label_region_ids=h['request']['label_region_ids']),
            '核对尺寸端点、轴网/墙带/符号反证；候选身份不能由数值大小确认。')
        networks=[n for n in ledger.get('network_ids',{}).values() if n['bundle_id']==bid and set(ids)<=set(n['request']['entity_ids'])]
        if not networks and ids:
            gaps.append('route_connectivity')
            calls.append(dict(gap='route_connectivity',tool='trace_geometry_network',arguments=dict(bundle_id=bid,entity_ids=ids,seed_entity_ids=ids[:1]),
                required_parameters=['tolerance_drawing_units','census_entity_ids (independent scope if coverage requested)'],
                already_attempted=False,reason='先从源分辨率确定容差，独立列出调查全集，再追踪分支和开口端点。',execution='explicit_model_call',automatic_replay=False))
        for n in networks:
            for field,count in [('branches',n.get('branch_count',0)),('open_endpoints',n.get('open_endpoint_count',0))]:
                if count:propose('route_'+field,'read_geometry_artifact',dict(collection='geometry_network',reference_id=n['network_id'],field=field),
                    '调查未决分支或断点；不跨缺口自动闭合。')
    bases=[b for b in ledger.get('basis_ids',{}).values() if b['bundle_id']==bid]
    if not bases:propose('physical_basis','resolve_geometry_basis',{},'先核对源单位；继续补读与所选表面和障碍直接相关的说明/剖面引文。')
    elif any(b['unit_resolution']['state']!='resolved' for b in bases):gaps.append('unit_evidence_unresolved')
    gaps.extend(['surface_applicability_unconfirmed','obstacle_height_mapping_unconfirmed','full_route_extrema_unproven'])
    alternatives=[c for c in calls if not c['already_attempted']]
    return dict(bundle_id=bid,hypothesis_id=hid,gaps=list(dict.fromkeys(gaps)),next_calls=alternatives,
        previously_attempted=[c for c in calls if c['already_attempted']],
        active_model_exclusions=[dict(decision_id=d['decision_id'],entity_ids=d['entity_ids'],reason=d['reason']) for d in exclusions],
        progress=dict(new_source_or_geometry_events=sum(e['progress_kind']=='new_source_or_geometry_evidence' for e in events),
            model_exclusions=sum(e['progress_kind']=='model_candidate_exclusion' for e in events),consecutive_without_new_evidence=stagnant),
        recommendation='change_strategy' if stagnant>=3 and alternatives else 'publish_partial_with_specific_gaps' if stagnant>=3 else 'continue_targeted_investigation',
        stop_is_advisory=True,budget_changed=False)


REPAIRS={
 'UNIT_EVIDENCE_REQUIRED':('resolve_geometry_basis','Read actual unit evidence; retain drawing_unit while unresolved.'),
 'WHOLE_ROUTE_PROOF_REQUIRED':('plan_geometry_investigation','Investigate connectivity, independent coverage and extrema; retain local result.'),
 'FINISHED_SURFACE_REQUIRED':('resolve_geometry_basis','Read directly applicable surface specification; retain unconfirmed source-face distance.'),
 'OBJECT_IDENTITY_UNCONFIRMED':('analyze_geometry_context','Compare competing title/geometry mappings and record counterevidence.'),
 'VALUE_MISMATCH':('read_geometry_artifact','Read the original deterministic value; do not recompute or rewrite evidence.'),
 'SOURCE_REFERENCE_INVALID':('read_geometry_workset','Find a real result from the current bundle; never construct an ID.'),
 'LOCAL_MEASUREMENT_REQUIRED':('read_geometry_workset','Choose a successful local measurement, retaining any unsupported attempt.'),
 'SAMPLED_PROFILE_REQUIRED':('read_geometry_workset','Find an actual sampled profile; selected sources alone are insufficient.'),
}


def repairs(issues,bid,reference_id):
    out=[]
    for issue in issues:
        tool,reason=REPAIRS.get(issue['check_id'],('plan_geometry_investigation','Inspect this claim and its evidence gap.'))
        arguments={} if tool=='read_geometry_workset' else dict(bundle_id=bid)
        if tool=='read_geometry_artifact':
            # The stored result may be a route; caller supplies collection explicitly after navigation.
            arguments.update(reference_id=reference_id)
        out.append(dict(check_id=issue['check_id'],tool=tool,arguments=arguments,reason=reason,
            required_parameters=['collection'] if tool=='read_geometry_artifact' else [],automatic_replay=False))
    return out
