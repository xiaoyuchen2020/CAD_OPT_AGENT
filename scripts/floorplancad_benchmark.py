"""Development benchmark with a sealed test boundary and frozen validation runs."""
import argparse
from collections import Counter, defaultdict
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import time

from cad_agent.geometry.vector_recognition import recognize_vectors, RecognitionOptions, VERSION
from scripts.floorplancad_svg import parse_svg, SCHEMA_SOURCE
from scripts.floorplancad_prepare import ARCHIVES

ROOT=Path(__file__).resolve().parents[1]
SOURCES=('cad_agent/geometry/vector_recognition.py','scripts/floorplancad_svg.py',
         'scripts/floorplancad_prepare.py','scripts/floorplancad_benchmark.py')
LIMITS={'development':256,'validation':128}


def sha(raw):return hashlib.sha256(raw).hexdigest()


def write_json(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(value,indent=2,ensure_ascii=False))


def source_hashes():return {p:sha((ROOT/p).read_bytes()) for p in SOURCES}


def read_sample(row,dataset):
    if row['archive'] not in ARCHIVES or row['split'] not in ('development','validation','quarantine'):
        raise ValueError('Only allowlisted training-source samples are readable')
    path=Path(row['path'])
    if not path.resolve().is_relative_to((dataset/'svg').resolve()) or path.is_symlink():
        raise ValueError('Sample escapes training cache')
    raw=path.read_bytes()
    if sha(raw)!=row['sha256']:raise ValueError('Source hash mismatch')
    return raw


def freeze(dataset,output):
    if (output/'freeze.json').exists():raise ValueError('Freeze exists; never overwrite a validation contract')
    manifest=json.loads((dataset/'manifest.json').read_text())
    # Freeze implementation before scanning validation geometry for leakage.
    contract=dict(version=VERSION,source_hashes=source_hashes(),options=asdict(RecognitionOptions()),
        manifest_sha256=sha((dataset/'manifest.json').read_bytes()),
        limits=LIMITS,selection='Round robin filename families in SHA order; at most four samples per family.',
        schema_source=SCHEMA_SOURCE,
        raw_target_ids={'wall':[1],'door':[3,4,5,6,7,8],'window':[9,10,11]},
        test_opened=False,performance_tuning_iterations=0,
        duplicate_policy='Quarantine whole filename families with exact geometry duplicates crossing splits; deduplicate within split.',
        metrics='Primitive-level one-vs-rest micro P/R/F1; unsupported primitives remain in recall denominator; no instance AP or room GT.',
        no_claim='Filename family independence is not verified project independence; near-duplicate drawings remain a limitation.')
    write_json(output/'freeze.json',contract)
    for rel in SOURCES:
        p=output/'frozen_source'/rel;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes((ROOT/rel).read_bytes())
    hashes=defaultdict(list);audit=[]
    for i,row in enumerate(manifest):
        if row['split']=='quarantine':continue
        p,t,diagnostics,h=parse_svg(read_sample(row,dataset))
        hashes[h].append(row)
        audit.append(dict(sample_id=row['sample_id'],geometry_sha256=h,primitives=len(t),
                          parsed=len(p),diagnostics=diagnostics))
        if i%1000==0:print(f'Geometry leakage audit {i}/{len(manifest)}',flush=True)
    bad={r['group_id'] for values in hashes.values() if len({r['split'] for r in values})>1 for r in values}
    eligible=[];duplicates=[]
    for h,values in hashes.items():
        values=sorted((r for r in values if r['group_id'] not in bad),key=lambda r:r['selection_key'])
        if not values:continue
        eligible.append(values[0]);duplicates.extend(r['sample_id'] for r in values[1:])
    selections={}
    for split,limit in LIMITS.items():
        groups=defaultdict(list)
        for row in sorted(eligible,key=lambda r:r['selection_key']):
            if row['split']==split:groups[row['group_id']].append(row)
        ordered=sorted(groups,key=lambda g:sha(g.encode()))
        candidates=[groups[g][k] for k in range(4) for g in ordered if len(groups[g])>k]
        selections[split]=candidates[:limit]
    write_json(output/'geometry_audit.json',dict(samples=audit,quarantined_families=sorted(bad),
        same_geometry_duplicates_removed=duplicates,near_duplicate_audit=False,test_opened=False))
    write_json(output/'selection.json',selections)
    write_json(output/'selection_seal.json',dict(selection_sha256=sha((output/'selection.json').read_bytes()),
        freeze_sha256=sha((output/'freeze.json').read_bytes()),
        geometry_audit_sha256=sha((output/'geometry_audit.json').read_bytes())))
    print({s:len(v) for s,v in selections.items()},flush=True)


def check_frozen(dataset,output):
    contract=json.loads((output/'freeze.json').read_text());seal=json.loads((output/'selection_seal.json').read_text())
    if source_hashes()!=contract['source_hashes']:raise ValueError('Implementation changed after freeze; validation refused')
    if sha((dataset/'manifest.json').read_bytes())!=contract['manifest_sha256']:raise ValueError('Manifest changed')
    for file,key in (('freeze.json','freeze_sha256'),('selection.json','selection_sha256'),('geometry_audit.json','geometry_audit_sha256')):
        if sha((output/file).read_bytes())!=seal[key]:raise ValueError('Frozen contract changed')
    return contract


def counts(truth,roles):
    out={}
    for cls in ('door','window','wall'):
        actual={eid for eid,t in truth.items() if t['category']==cls}
        predicted={eid for eid,v in roles.items() if v==cls}
        out[cls]=dict(tp=len(actual&predicted),fp=len(predicted-actual),fn=len(actual-predicted))
    return out


def summarize(totals):
    out={}
    for cls,c in totals.items():
        tp,fp,fn=(c[k] for k in ('tp','fp','fn'))
        out[cls]=dict(c,precision=tp/(tp+fp) if tp+fp else None,recall=tp/(tp+fn) if tp+fn else None,
                     f1=2*tp/(2*tp+fp+fn) if 2*tp+fp+fn else None)
    return out


def run(dataset,output,split):
    if split not in LIMITS:raise ValueError('Test evaluation is not implemented in this development command')
    check_frozen(dataset,output)
    dest=output/split
    if dest.exists():raise ValueError('Run already exists; do not overwrite validation evidence')
    dest.mkdir(parents=True)
    selected=json.loads((output/'selection.json').read_text())[split]
    totals={c:Counter(tp=0,fp=0,fn=0) for c in ('door','window','wall')};rows=[];group_totals={}
    for i,row in enumerate(selected):
        if row['split']!=split:raise ValueError('Split mismatch')
        g,truth,diagnostics,gh=parse_svg(read_sample(row,dataset))
        # Labels never cross the recognizer call boundary.
        start=time.perf_counter();result=recognize_vectors(g);elapsed=time.perf_counter()-start
        count=counts(truth,result['entity_roles'])
        for cls,c in count.items():totals[cls].update(c)
        group=group_totals.setdefault(row['group_id'],{c:Counter(tp=0,fp=0,fn=0) for c in totals})
        for cls,c in count.items():group[cls].update(c)
        rec=dict(sample_id=row['sample_id'],source_sha256=row['sha256'],geometry_sha256=gh,
            group_id=row['group_id'],primitives=len(truth),parsed=len(g),parser_diagnostics=diagnostics,
            recognition_complete=result['complete'],candidate_counts=dict(Counter(c['kind'] for c in result['candidates'])),
            metrics=summarize(count),elapsed_seconds=elapsed)
        rows.append(rec)
        write_json(dest/'predictions'/(row['sample_id'].replace('/','__')+'.json'),result)
        if i%25==0:print(f'{split} {i+1}/{len(selected)}',flush=True)
    groups={g:summarize(t) for g,t in group_totals.items()}
    macro={c:{m:sum(v[c][m] for v in groups.values() if v[c][m] is not None)/sum(v[c][m] is not None for v in groups.values())
        if any(v[c][m] is not None for v in groups.values()) else None for m in ('precision','recall','f1')} for c in totals}
    report=dict(split=split,source='official training archives only',test_opened=False,samples=len(rows),
        groups=len(groups),primitives=sum(r['primitives'] for r in rows),parsed=sum(r['parsed'] for r in rows),
        incomplete_samples=sum(not r['recognition_complete'] for r in rows),micro=summarize(totals),
        family_macro=macro,family_metrics=groups,sample_metrics=rows,
        frozen_source_hashes=source_hashes(),elapsed_seconds=sum(r['elapsed_seconds'] for r in rows),
        room_boundary_accuracy=None,instance_AP=None,
        limitation='Candidate primitive semantics only; no exhaustive symbol instances or room polygons evaluated.')
    write_json(dest/'metrics.json',report)
    print(json.dumps({k:report[k] for k in ('split','samples','groups','primitives','parsed','incomplete_samples','micro','elapsed_seconds')},indent=2),flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('command',choices=['freeze','run'])
    parser.add_argument('--dataset',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--split',choices=list(LIMITS),default='development')
    args=parser.parse_args()
    if args.command=='freeze':freeze(args.dataset,args.output)
    else:run(args.dataset,args.output,args.split)
