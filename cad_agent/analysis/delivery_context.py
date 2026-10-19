"""Bounded synthesis packets, preserving exact whole quotes and explicit omissions."""
from copy import deepcopy
from cad_agent.analysis.efficiency import dumps


def bounded_packet(packet, limit, steps=()):
    if len(dumps(packet))<=limit:return packet
    out=deepcopy(packet)
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
    queues=[[eid for eid in reversed(s.get('evidence_ids',[])) if eid in byid] for s in steps]
    while any(queues):
        for q in queues:
            if q:
                eid=q.pop(0)
                if eid not in order:order.append(eid)
    order.extend(e['evidence_id'] for e in reversed(evidence) if e['evidence_id'] not in order)
    def add(key,item,budget):
        out[key].append(item)
        if len(dumps(out))>budget:out[key].pop();return False
        return True
    # Reserve room for geometric/calculated results, not only textual evidence.
    for eid in order:add('evidence',byid[eid],limit*.62 if observations else limit-1000)
    priority={'calculate_facts','analyze_measurements','measure_geometry','measure_room_area',
        'select_room_boundaries','review_space_connections','review_discovered_spaces','read_dimensions'}
    obs=sorted(reversed(observations),key=lambda o:o['tool'] not in priority)
    for o in obs:add('observations',o,limit-500)
    included={e['evidence_id'] for e in out['evidence']}
    for eid in order:
        if eid not in included:add('evidence',byid[eid],limit-500)
    for c in reversed(contexts):add('source_contexts',c,limit-500)
    out['context_omissions'].update(evidence_count=len(evidence)-len(out['evidence']),
        observation_count=len(observations)-len(out['observations']),source_context_count=len(contexts)-len(out['source_contexts']))
    # The immutable question and task requirements have small schema bounds;
    # if metadata nevertheless expands, retain the actual task text and gaps.
    if len(dumps(out))>limit:
        out={k:v for k,v in out.items() if k in ('question','task','evidence','observations','context_omissions','instruction','validation_issue')}
    if len(dumps(out))>limit:
        out['task']={'subtasks':[{k:s[k] for k in ('id','requirement','status','reason') if k in s} for s in steps]}
    return out
