"""Real-DXF regression of investigation tools and independent native lengths.

This is a tool/kernel replay, not autonomous object recognition or business gold.
Freeze inputs before executing; no expected widths are supplied to tools.
"""
from pathlib import Path
import argparse,hashlib,json,math,sys,time
from dataclasses import replace
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import ezdxf
from cad_agent.config import Settings
from cad_agent.service import Workspace
from cad_agent.contracts import ChatRequest
from cad_agent.analysis.session import CadAgentSession
from cad_agent.geometry.workbench import GeometryWorkbench


def resolve(doc,path):
    entity=doc.entitydb[path[0]]
    for token in path[1:]:
        idx,kind=token.split(':',1);entity=list(entity.virtual_entities())[int(idx)]
        if entity.dxftype()!=kind:raise ValueError('INSTANCE_PATH_MISMATCH')
    return entity


def native_lengths(entity):
    children=list(entity.virtual_entities()) if entity.dxftype() in ('LWPOLYLINE','POLYLINE') else [entity]
    result=[]
    for e in children:
        if e.dxftype()=='LINE':length=e.dxf.start.distance(e.dxf.end)
        elif e.dxftype()=='ARC':length=e.dxf.radius*math.radians((e.dxf.end_angle-e.dxf.start_angle)%360)
        else:continue
        if length>0:result.append(length)
    return sorted(result)


def main():
    p=argparse.ArgumentParser();p.add_argument('manifest');p.add_argument('output');a=p.parse_args()
    manifest=json.loads(Path(a.manifest).read_text());root=Path(a.output);root.mkdir(parents=True,exist_ok=False)
    (root/'input-manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2))
    code_root=Path(__file__).resolve().parents[1]/'cad_agent'
    (root/'code-manifest.json').write_text(json.dumps({str(p.relative_to(code_root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in code_root.rglob('*.py')},indent=2))
    results=[]
    for case in manifest['cases']:
        path=Path(case['source']);assert hashlib.sha256(path.read_bytes()).hexdigest()==case['source_sha256']
        start=time.monotonic();w=Workspace(replace(Settings.load(),data_dir=root/case['case_id'],max_upload_bytes=max(32*1024*1024,path.stat().st_size),llm_query_intent=False,source_record_workflow=False))
        try:
            drawing=w.ingest(path.name,path.read_bytes());sid=drawing['snapshot_id'];lid=next(l['layout_id'] for l in drawing['layouts'] if l['name']==case.get('layout','Model'))
            s=CadAgentSession(w,ChatRequest(snapshot_id=sid,question='识别所选局部对象、追踪边界并测量，分别记录来源与未知条件。',mode='agent',agent_policy='legacy'))
            def call(name,**args):
                result=s.execute(name,args,bounded=False);s.register_observation(name,result);return result
            b=call('prepare_geometry_bundle',layout_id=lid,bbox=case['bbox']);bid=b['bundle_id']
            full=GeometryWorkbench(w,sid).bundle(bid)
            rows=[r for r in full['entities'] if r['status']=='ready' and r['kind'] in ('LINE','ARC','LWPOLYLINE','POLYLINE')][:case.get('max_sources',25)]
            ids=[r['entity_id'] for r in rows]
            c=call('analyze_geometry_context',bundle_id=bid,entity_ids=ids)
            n=call('trace_geometry_network',bundle_id=bid,entity_ids=ids,seed_entity_ids=ids[:1],census_entity_ids=ids,tolerance_drawing_units=case['tolerance'])
            doc=ezdxf.readfile(path);net=w.store.get('geometry_network',n['network_id']);audit=[]
            for r in rows:
                expected=native_lengths(resolve(doc,r['entity_path']))
                actual=sorted(e['length_drawing_units'] for e in net['primitives'] if e['source_entity_id']==r['entity_id'])
                ok=len(expected)==len(actual) and all(math.isclose(x,y,rel_tol=1e-9,abs_tol=1e-6) for x,y in zip(expected,actual))
                audit.append(dict(source_path=r['entity_path'],kind=r['kind'],expected_lengths=expected,actual_lengths=actual,agrees=ok))
            physical=call('resolve_geometry_basis',bundle_id=bid)
            investigation=call('plan_geometry_investigation',bundle_id=bid)
            paged=call('read_geometry_artifact',bundle_id=bid,collection='geometry_network',reference_id=n['network_id'],field='primitives',limit=2)
            result=dict(case_id=case['case_id'],source_sha256=case['source_sha256'],classification='real_source_tool_replay_not_autonomous_accuracy',
                source_count=len(rows),primitive_count=n['primitive_count'],native_audit=audit,
                agrees=sum(r['agrees'] for r in audit),context_analysis=c,network=n,basis=physical,investigation=investigation,
                pagination= dict(total=paged['total'],has_more=paged['has_more']),elapsed_seconds=time.monotonic()-start)
            results.append(result);(root/'results.json').write_text(json.dumps(results,ensure_ascii=False,indent=2))
            print(json.dumps({k:result[k] for k in ('case_id','source_count','primitive_count','agrees','elapsed_seconds')},ensure_ascii=False),flush=True)
        finally:w.close()
    if any(r['agrees']!=r['source_count'] for r in results):raise SystemExit(1)

if __name__=='__main__':main()
