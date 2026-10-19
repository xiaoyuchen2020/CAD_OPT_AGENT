"""Map successful native tool reads to host-authorized method scopes, out of band."""
from cad_agent.contracts import stable_id
from .capabilities import TRIGGERS, READ_TOOLS
from .shadow import ShadowService, target_key, observation_key


def _error(exc):
    code=str(exc)
    return code if code.isupper() and len(code)<100 else 'SHADOW_INTERNAL_ERROR'


def _matches(index,t,name,args):
    if name not in (t.get('trigger_tools') or TRIGGERS[t.get('method','table_grid')]):return False
    method=t.get('method','table_grid')
    if name=='measure_geometry':
        wanted={'segment_measurements':'length','segment_distances':'distance','closed_contour':'area'}
        if args.get('operation')!=wanted.get(method):return False
    if t.get('table_id'):return args.get('table_id')==t['table_id']
    bounds=t['bbox'];layout=t['layout_id']
    if args.get('bbox') is not None:
        # Reading a different/larger scope must not silently authorize a region.
        return args.get('layout_id')==layout and args['bbox']==bounds
    if args.get('region_id'):
        r=index.by_id.get(args['region_id'])
        return bool(r and r.layout_id==layout and bounds[0]<=r.bbox[0]<=r.bbox[2]<=bounds[2]
                    and bounds[1]<=r.bbox[1]<=r.bbox[3]<=bounds[3])
    if args.get('entity_ids'):
        from ..table_recovery.bundle import prepare_region
        bundle=prepare_region(index,layout,bounds)
        known={v['entity_id'] for k in ('texts','segments') for v in bundle[k]}
        return bool(set(args['entity_ids'])<=known)
    return False


def observe_read(workspace,policy_id,sid,name,args):
    service=ShadowService(workspace);p=service.get(policy_id)
    if not p['spec']['agent_shadow_enabled']:return []
    from cad_agent.analysis.general import DrawingIndex
    index=None;events=[]
    for t in p['targets']:
        if t['snapshot_id']!=sid or name not in (t.get('trigger_tools') or TRIGGERS[t.get('method','table_grid')]):continue
        try:
            if index is None:index=DrawingIndex(workspace,sid)
            if not _matches(index,t,name,args):continue
            event=service.observe(policy_id,sid,target_id=target_key(t),background=False)
        except Exception as exc:
            event=dict(status='refused',reason=_error(exc),target_id=target_key(t))
        workspace.store.put('adaptation_shadow_observation',observation_key(policy_id,t),event)
        events.append(event)
    return events


def enqueue_read(workspace,policy_id,sid,name,args):
    if name not in READ_TOOLS:return
    def failure(exc):
        # Legacy table observation keys remain readable, even for queue failures.
        key=stable_id('adobserve',policy_id,sid,args['table_id']) if args.get('table_id') else stable_id('adread',policy_id,sid,name)
        workspace.store.put('adaptation_shadow_observation',key,dict(status='refused',reason=_error(exc)))
    def work():
        try:observe_read(workspace,policy_id,sid,name,args)
        except Exception as exc:failure(exc)
    try:workspace.executor.submit(work)
    except RuntimeError:failure(ValueError('SHADOW_EXECUTOR_UNAVAILABLE'))
