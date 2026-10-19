"""Development-only diagnostics; original validation and test are forbidden."""
import argparse
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import sys

from scripts.floorplancad_svg import parse_svg

ROOT=Path(__file__).resolve().parents[1]
BASE=ROOT/'artifacts/floorplancad-vector-v1'
OUT=ROOT/'artifacts/floorplancad-vector-v2'


def sha(raw):return hashlib.sha256(raw).hexdigest()


def save(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(value,indent=2,ensure_ascii=False))


def forbidden(path):
    p=Path(path)
    return 'validation' in p.parts or p.name.startswith('test-')


def install_guard(rows):
    allowed={str(Path(r['path']).resolve()) for r in rows}
    def audit(event,args):
        if event!='open' or not isinstance(args[0],(str,bytes,Path)):return
        path=Path(args[0].decode() if isinstance(args[0],bytes) else args[0]).resolve()
        if forbidden(path) or (path.suffix.lower()=='.svg' and str(path) not in allowed):
            raise PermissionError('Development-only read boundary: '+str(path))
    sys.addaudithook(audit)


def load_rows():
    rows=json.loads((OUT/'development_roster.json').read_text())
    for r in rows:
        if r['split']!='development' or r['archive'] not in ('train-00.tar.xz','train-01.tar.xz') or forbidden(r['path']):
            raise ValueError('Non-development row rejected')
    return rows


def read(row):
    if row['split']!='development' or forbidden(row['path']):raise ValueError('Non-development input')
    raw=Path(row['path']).read_bytes()
    if sha(raw)!=row['sha256']:raise ValueError('Sample hash mismatch')
    return parse_svg(raw)


def prepare():
    if (OUT/'protocol.json').exists():raise ValueError('Do not overwrite protocol')
    # Only the split roster metadata is read, never validation samples or scores.
    rows=json.loads((BASE/'baseline/selection.json').read_text())['development']
    families=sorted({r['group_id'] for r in rows},key=lambda g:sha(('dev-v2-folds:'+g).encode()))
    folds={g:i%5 for i,g in enumerate(families)}
    rows=[r|{'fold':folds[r['group_id']]} for r in rows]
    save(OUT/'protocol.json',dict(source='previous frozen development roster only',
        analysis_folds=[0,1,2],internal_check_folds=[3,4],original_validation_access=False,test_access=False,
        family_split_seed='dev-v2-folds',grouping='filename families, not verified project identity',
        selection='same 256 development samples; family-balanced hash assignment to five folds',
        method='Analyze folds 0-2 only; freeze algorithms and all ablations before folds 3-4.',
        selection_rule='Use analysis folds only to select variant; internal checks are reported without retuning.',
        constraints='No per-sample, filename, source ID, layer, color or label feature rules.',
        historical_exposure='v1 aggregate results for the development roster existed before this split; not a new blind test.',
        room_claim=False))
    save(OUT/'development_roster.json',rows)
    print(dict(Counter(r['fold'] for r in rows)))


def analyze():
    rows=load_rows();install_guard(rows)
    stats=defaultdict(Counter);families=defaultdict(set);examples=defaultdict(list)
    sample_results=[]
    for row in rows:
        if row['fold'] not in (0,1,2):continue
        primitives,truth,diag,h=read(row);byid={p.entity_id:p for p in primitives}
        pred=json.loads((BASE/'baseline/development/predictions'/(row['sample_id'].replace('/','__')+'.json')).read_text())
        roles=pred['entity_roles']
        instances=defaultdict(list)
        for eid,t in truth.items():
            if t['category'] in ('door','window') and t['instance_id']!='-1':
                instances[t['category'],t['instance_id']].append(eid)
            label=t['category'];role=roles.get(eid,'unknown');p=byid.get(eid)
            if label in ('door','window'):
                key=label+'_total_kind';stats[key][p.kind if p else 'unsupported']+=1
                if role!=label:
                    key=label+'_miss_kind';stats[key][p.kind if p else 'unsupported']+=1
                    stats[label+'_miss_raw_class'][t['raw_semantic_id']]+=1
                    if p and p.kind=='arc':stats[label+'_miss_arc_sweep'][round(abs(p.sweep_degrees or 0))]+=1
                    families[key].add(row['group_id'])
            if role=='wall' and label!='wall':
                stats['wall_false_positive_raw_class'][t['raw_semantic_id']]+=1
                families['wall_false_positive_raw_class'].add(row['group_id'])
        for (cls,iid),ids in instances.items():
            n=sum(roles.get(eid)==cls for eid in ids)
            key='no_hit' if n==0 else 'partial_below_half' if n/len(ids)<.5 else 'at_least_half'
            stats[cls+'_instance_coverage'][key]+=1
            if n:
                stats[cls+'_missed_entities_in_hit_instances']['missed']+=len(ids)-n
        # Candidate geometry distributions identify mechanisms, not individual exceptions.
        for c in pred['candidates']:
            if c['kind']!='wall':continue
            good=sum(truth[e]['category']=='wall' for e in c['entity_ids'])==len(c['entity_ids'])
            e=c['evidence'];stats['wall_pair_aspect_'+('correct' if good else 'incorrect')][int(e['overlap']/e['spacing']//10)*10]+=1
        sample_results.append(dict(sample_id=row['sample_id'],fold=row['fold'],group_id=row['group_id'],primitives=len(truth)))
    result=dict(analysis_samples=len(sample_results),analysis_families=len({r['group_id'] for r in sample_results}),
        stats={k:dict(v) for k,v in stats.items()},families_affected={k:len(v) for k,v in families.items()},
        samples=sample_results,original_validation_read=False,test_read=False)
    save(OUT/'development_diagnosis.json',result)
    print(json.dumps({k:v for k,v in result.items() if k!='samples'},indent=2))


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('command',choices=['prepare','analyze']);args=p.parse_args()
    prepare() if args.command=='prepare' else analyze()
