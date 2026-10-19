"""Independent original-DXF reread of all sources used in this experiment."""
import hashlib
import json
import math
import sqlite3
import logging
import argparse
from pathlib import Path
from collections import defaultdict
import ezdxf
from cad_agent.config import ROOT
from cad_agent.cad.reader import entity_batches, decode_text
from cad_agent.contracts import stable_id
from cad_agent.extensions.table_recovery.bundle import verify_bundle


def main():
    logging.getLogger('ezdxf').setLevel(logging.ERROR)
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, default=ROOT/'artifacts/dynamic-adaptation-20261016')
    parser.add_argument('--run-prefix', default='')
    args=parser.parse_args();out=args.out;data=out/'data'
    connection=sqlite3.connect(data/'index.sqlite3');groups=defaultdict(list)
    selected=None
    if args.run_prefix:
        runs=[json.loads(r[0]) for r in connection.execute("select data from records where kind='adaptation_run'")]
        selected={r.get('input_artifact_id') for r in runs if r['request']['idempotency_key'].startswith(args.run_prefix)}
    for key,text in connection.execute("select id,data from records where kind='adaptation_input'"):
        if selected is not None and key not in selected:continue
        b=json.loads(text);verify_bundle(b);groups[b['snapshot_id']].append(b)
    connection.close()
    reports=[]
    for sid,bundles in groups.items():
        source=data/'drawings'/sid/'source.dxf';source_sha=hashlib.sha256(source.read_bytes()).hexdigest()
        assert all(source_sha==b['source_sha256'] for b in bundles)
        segments={s['segment_id']:s for b in bundles for s in b['segments']}
        texts={t['entity_id']:t for b in bundles for t in b['texts']}
        needed={s['entity_id'] for s in segments.values()}|texts.keys()
        doc=ezdxf.readfile(source);entities={};issues=[]
        for layout in doc.layouts:
            lid=stable_id('layout',sid,layout.name)
            for batch in entity_batches(layout,issues,layout.name):
                for e,path in batch:
                    eid=stable_id('ent',sid,lid,'/'.join(path))
                    if eid in needed:entities[eid]=(e,path)
        assert needed<=entities.keys()
        for s in segments.values():
            e,path=entities[s['entity_id']];assert path==s['entity_path']
            if e.dxftype()=='LINE':points=[e.dxf.start,e.dxf.end]
            elif e.dxftype()=='LWPOLYLINE':
                points=list(e.vertices_in_wcs())
                if e.closed:points.append(points[0])
            else:
                assert e.dxftype()=='POLYLINE';points=list(e.points_in_wcs())
                if e.is_closed:points.append(points[0])
            i=s['segment_index']
            for stored,native in ((s['a'],points[i]),(s['b'],points[i+1])):
                assert math.isclose(stored[0],native.x,rel_tol=0,abs_tol=1e-7)
                assert math.isclose(stored[1],native.y,rel_tol=0,abs_tol=1e-7)
        for eid,t in texts.items():
            e,path=entities[eid];assert path==t['entity_path'] and decode_text(e)==t['text']
        reports.append(dict(snapshot_id=sid,source_sha256=source_sha,bundles=len(bundles),
                            native_segments_verified=len(segments),source_texts_verified=len(texts),passed=True))
        print(sid,len(segments),len(texts),'passed',flush=True)
    (out/'source-binding-audit.json').write_text(json.dumps(reports,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
