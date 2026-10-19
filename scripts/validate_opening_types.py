"""Real local DXF replay. Explicit scopes; no production/platform/model calls."""
import argparse
from collections import Counter
from hashlib import sha256
import json
import logging
from pathlib import Path
import shutil
import sqlite3

from shapely.geometry import Point, Polygon
from fastapi.testclient import TestClient
from cad_agent.app import create_app
from cad_agent.analysis.session import CadAgentSession
from cad_agent.contracts import ChatRequest, stable_id
from cad_agent.cad.reader import entity_batches, decode_text
from cad_agent.config import Settings
from cad_agent.service import Workspace
from cad_agent.components.service import ComponentService
from cad_agent.components.contracts import RelationQuery
from cad_agent.geometry.source import native_source, scoped_geometry_index
from cad_agent.components.openings import effective_layer
from scripts.validate_components import render_review

CASES=[
 dict(name='property-bath',sid='snap_77b384ae399a2c5c69f5a23a',dwg='(6审)D1-D3楼_t3.dwg',bbox=[14500,-6500,20500,5000],maximum=3),
 dict(name='ligong-east',sid='snap_e316a1345c7a5db25e19458f',dwg='(3审)16-10#楼_t3(1)-理工院改后（建筑已修改完）.dwg',bbox=[112000,-184000,128000,-170000],maximum=3),
 dict(name='d5',sid='snap_1955475d78798490837af14d',dwg='(6审)D5#楼_t3.dwg',bbox=[606000,-160500,622500,-144000],maximum=6),
 dict(name='d7-plan',sid='snap_535fa8a4f077da8bf760182e',dwg='(6审)D7-D8楼_t3.dwg',bbox=[450000,-12000,474000,8000],maximum=6),
 dict(name='d7-elevation-negative',sid='snap_535fa8a4f077da8bf760182e',dwg='(6审)D7-D8楼_t3.dwg',bbox=[451000,-957000,471500,-942000],maximum=6,view_kind='detail'),
 dict(name='d7-elevation-as-plan-negative',sid='snap_535fa8a4f077da8bf760182e',dwg='(6审)D7-D8楼_t3.dwg',bbox=[451000,-957000,471500,-942000],maximum=6),
]


def dump(path,data):path.write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n')


def verify(graph,source):
    # Geometry index deliberately excludes TEXT. Independently rewalk native
    # labels by source path, then verify their values as well as their IDs.
    wanted={t['entity_id']:t['text'] for o in graph['openings'] for t in o.get('classification_evidence',{}).get('labels',[])}
    text_ids=set()
    if wanted:
        for layout in source['doc'].layouts:
            lid=stable_id('layout',graph['snapshot_id'],layout.name)
            if lid!=graph['layout_id']:continue
            for batch in entity_batches(layout,[],layout.name):
                for e,path in batch:
                    if e.dxftype() not in ('TEXT','MTEXT','ATTRIB','ATTDEF'):continue
                    eid=stable_id('ent',graph['snapshot_id'],lid,'/'.join(path))
                    if eid in wanted:
                        assert decode_text(e)==wanted[eid]
                        text_ids.add(eid)
        assert text_ids==wanted.keys()
    valid_ids=source['entities'].keys()|text_ids
    objects={x['object_id'] for key in ('rooms','walls','openings') for x in graph[key]}
    assert all(r['source_id'] in objects and r['target_id'] in objects for r in graph['relations'])
    assert all(set(x['entity_ids'])<=valid_ids for key in ('rooms','walls','openings') for x in graph[key])
    rooms={r['object_id']:r for r in graph['rooms']};checks=[]
    for o in graph['openings']:
        if o['passage_status']=='supported_door_between_two_spaces':
            assert o['opening_type']=='door_candidate' and len(o['room_ids'])==2
            assert len(o['sides'])==2 and all(len(s['room_ids'])==1 for s in o['sides'])
            assert o['sides'][0]['room_ids']!=o['sides'][1]['room_ids']
            sidechecks=[]
            for side in o['sides']:
                room=rooms[side['room_ids'][0]];p=Polygon(room['boundary'],room.get('internal_rings',[]))
                hits=[p.contains(Point(q)) for q in side['probe_points']];assert sum(hits)>=2
                sidechecks.append(dict(room_id=room['object_id'],hits=hits))
            checks.append(dict(opening_id=o['object_id'],bbox=o['bbox'],sides=sidechecks))
        else:
            assert not any(r.get('opening_id')==o['object_id'] and r['kind']=='door_connects_candidate_spaces' for r in graph['relations'])
        for key in ('swing_symbols','double_swing_pairs','sliding_panel_pairs'):
            for ev in o.get('classification_evidence',{}).get(key,[]):assert set(ev['entity_ids'])<=source['entities'].keys()
    return checks


def main():
    p=argparse.ArgumentParser();p.add_argument('--archive',type=Path,required=True);p.add_argument('--output',type=Path,required=True);args=p.parse_args()
    out=args.output.resolve();out.mkdir(parents=True,exist_ok=True);archive=args.archive.resolve()
    logging.getLogger('ezdxf').setLevel(logging.ERROR)
    with sqlite3.connect(f"file:{archive/'data/index.sqlite3'}?mode=ro",uri=True) as db:
        drawings={d['id']:d for raw, in db.execute("select data from records where kind='drawing'") if (d:=json.loads(raw))}
    summary=[]
    w=Workspace(Settings(data_dir=out/'data'))
    for case in CASES:
        sid=case['sid'];d=drawings[sid];origin=archive/'data/drawings'/sid
        digest=sha256((origin/'source.dxf').read_bytes()).hexdigest();assert digest==d['sha256']
        try:
            for fn in ('source.dxf','scene.json','entities.json','regions.json'):shutil.copy2(origin/fn,w.store.artifact(sid,fn))
            w.store.put('drawing',sid,d);lid=next(x['layout_id'] for x in d['layouts'] if x['name']=='Model')
            query=RelationQuery(layout_id=lid,bbox=case['bbox'],view_kind=case.get('view_kind','floor_plan'),max_opening_m=case['maximum'])
            print(case['name'],'build',flush=True);g=ComponentService(w,sid).build('relations',query)
            dump(out/f"{case['name']}.json",g);g=json.loads((out/f"{case['name']}.json").read_text())
            source=native_source(w,sid);checks=verify(g,source);dump(out/f"{case['name']}-checks.json",checks)
            # Direct service, Agent tool (no model), and HTTP must read the same
            # immutable graph and expose the same classification evidence.
            params=query.model_dump()|dict(view='openings',limit=20)
            agent=CadAgentSession(w,ChatRequest(snapshot_id=sid,question='核对门窗洞口'))
            observation=json.loads(agent.invoke_tool('query_building_relations',params))
            assert observation['record_id']==g['record_id']
            with TestClient(create_app(w.settings)) as client:
                response=client.post(f'/api/drawings/{sid}/components/relations',json=params)
                assert response.status_code==200,response.text
                page=response.json();assert page['record_id']==g['record_id']
                assert page['items']==observation['items']
                readback=client.get(g['evidence_url']);assert readback.status_code==200
                assert readback.json()==g
            dump(out/f"{case['name']}-api-agent.json",dict(http_record_id=page['record_id'],agent_record_id=observation['record_id'],observation_id=observation['observation_id'],same_items=True,full_readback_equal=True))
            records=scoped_geometry_index(w,sid,lid,case['bbox'],'mm').query(lid,case['bbox'])
            for r in records:r['effective_layer']=effective_layer(r,source)
            dump(out/f"{case['name']}-source.json",records)
            render_review(w,sid,case['bbox'],g,out/f"{case['name']}.png")
            if 'negative' in case['name']:assert not g['openings'] and not g['rooms']
            assert sha256((origin/'source.dxf').read_bytes()).hexdigest()==digest
            row=case|dict(source_sha256=digest,source_unchanged=True,record_id=g['record_id'],rooms=len(g['rooms']),openings=len(g['openings']),types=dict(Counter(o.get('opening_type','unknown') for o in g['openings'])),operations=dict(Counter(o.get('operation_type','unknown') for o in g['openings'])),two_sided_door_connections=len(checks))
            summary.append(row);dump(out/'summary.json',summary);print(json.dumps(row,ensure_ascii=False),flush=True)
        finally:pass
    w.close()


if __name__=='__main__':main()
