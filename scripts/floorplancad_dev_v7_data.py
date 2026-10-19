"""Phase allowlists: only previous analysis families and fresh development checks."""
import hashlib,json,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
BASE=ROOT/'artifacts/floorplancad-vector-v1'
OLD=ROOT/'artifacts/floorplancad-vector-v2'
PREV=ROOT/'artifacts/floorplancad-vector-v4'
PREV5=ROOT/'artifacts/floorplancad-vector-v5'
PREV6=ROOT/'artifacts/floorplancad-vector-v6'
OUT=ROOT/'artifacts/floorplancad-vector-v7'
def sha(b):return hashlib.sha256(b).hexdigest()
def save(p,d):p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(d,ensure_ascii=False,indent=2))
def prepare():
    if (OUT/'protocol.json').exists():raise ValueError('Immutable protocol exists')
    old=json.loads((OLD/'development_roster.json').read_text())
    analysis=[r for r in old if r['fold']<3]
    consumed=json.loads((PREV/'fresh_checks_roster.json').read_text())
    consumed += json.loads((PREV5/'fresh_checks_roster.json').read_text())
    consumed += json.loads((PREV6/'fresh_checks_roster.json').read_text())
    old=old+consumed;used={r['group_id'] for r in old}
    assert len(used)==544
    manifest=json.loads((BASE/'dataset/manifest.json').read_text())
    audit=json.loads((BASE/'baseline/geometry_audit.json').read_text())
    banned=set(audit['quarantined_families']);dups=set(audit['same_geometry_duplicates_removed'])
    geometry={r['sample_id']:r['geometry_sha256'] for r in audit['samples']}
    used_geoms={geometry[r['sample_id']] for r in old}
    eligible=[r for r in manifest if r['split']=='development' and r['group_id'] not in used|banned and r['sample_id'] not in dups and geometry.get(r['sample_id']) not in used_geoms]
    families={}
    for r in sorted(eligible,key=lambda r:sha(('v7-fresh-sample:'+r['sample_id']).encode())):
        families.setdefault(r['group_id'],r)
    checks=[];seen=set(used_geoms)
    for fam in sorted(families,key=lambda f:sha(('v7-fresh-family:'+f).encode())):
        r=families[fam];gh=geometry[r['sample_id']]
        if gh in seen:continue
        seen.add(gh);checks.append(r|{'fold':len(checks)%2,'geometry_sha256':gh})
        if len(checks)==96:break
    assert len(analysis)==154 and len(checks)==96
    save(OUT/'analysis_roster.json',analysis);save(OUT/'fresh_checks_roster.json',checks)
    save(OUT/'excluded_used_families.json',sorted(used))
    save(OUT/'protocol.json',dict(analysis_samples=154,analysis_families=154,fresh_check_samples=96,fresh_check_families=96,
        fresh_check_cohorts=2,eligible_fresh_families=len(families),previous_check_families_excluded=390,
        original_validation_access=False,test_access=False,previous_checks_tuning=False,
        analysis='Only old folds 0,1,2; all 544 previously used families excluded from fresh checks.',
        freshness='Original manifest split + geometry quarantine + exact geometry duplicate exclusion; filename families are proxies, not verified project identities. No near duplicate independence claim.',
        metadata='Existing manifest and geometry-audit hashes used only to construct isolation roster; no held-out labels, drawings or metrics consulted.',
        batch='Only 154 analysis families for causal diagnosis of door-part graph growth and shared wall/window ownership. Independent host-free door component; no numeric search or per-file exceptions. Freeze finite ablations, all sources, gates and roster before scoring. Seal selection before fresh checks.',
        gates=dict(door_fp_reduction_vs_v5_min=.2,door_tp_retention_vs_v5_min=.95,door_precision_and_f1_vs_v5_nondecreasing=True,door_precision_vs_v3_max_drop=.02,door_window_and_wall_roles_unchanged=True,
            wall_as_window_fp_reduction_vs_v5_min=.2,window_tp_retention_vs_v5_min=.95,window_precision_and_f1_vs_v5_nondecreasing=True,window_door_roles_unchanged=True,wall_tp_nondecreasing=True,wall_fp_nonincrease=True,complete=True),
        component_selection='Select door and window components independently from analysis gates only. Combined variants are diagnostic, not a reason to relax component gates. None passing means no promotion.',
        selection='Highest analysis target-class F1 among passing component variants. All frozen variants checked once for reproducibility, never selected or tuned on checks.',
        analysis_roster_sha256=sha((OUT/'analysis_roster.json').read_bytes()),checks_roster_sha256=sha((OUT/'fresh_checks_roster.json').read_bytes())))
    print(json.dumps(dict(analysis=len(analysis),fresh_checks=len(checks),eligible_families=len(families))))
def roster(phase):
    if phase not in ('analysis','fresh_checks'):raise ValueError('Invalid phase')
    return json.loads((OUT/(phase+'_roster.json')).read_text())
def forbidden(path,allowed):
    p=Path(path.decode() if isinstance(path,bytes) else path).resolve();s=str(p)
    if 'validation' in p.parts or 'test' in p.parts or p.name.startswith('test-'):return True
    if p.suffix in ('.xz','.tar','.zip') and 'floorplancad' in s:return True
    if p.suffix=='.svg' and s not in allowed:return True
    if any(s.startswith(str(root)+'/') for root in (OLD,ROOT/'artifacts/floorplancad-vector-v3',PREV,PREV5,PREV6)):
        # Only exact analysis prediction paths may be read by the diagnosis.
        return s not in allowed
    if s.startswith(str(BASE)) and s not in allowed:return True
    if '/predictions/fresh_checks/' in s and s not in allowed:return True
    return False

def install_guard(rows,extra=()):
    allowed={str(Path(r['path']).resolve()) for r in rows}|{str(Path(p).resolve()) for p in extra}
    observed=[]
    def guard(event,args):
        reading=event=='open' and (args[1] is None and (args[2]&3)!=1 or isinstance(args[1],str) and ('r' in args[1] or '+' in args[1]))
        if reading and isinstance(args[0],(str,bytes)) and forbidden(args[0],allowed):
            raise PermissionError('v7 split isolation blocks '+str(args[0]))
        if reading and isinstance(args[0],(str,bytes)):
            path=Path(args[0].decode() if isinstance(args[0],bytes) else args[0]).resolve()
            if path.suffix=='.svg' or str(path).startswith(str(ROOT/'artifacts/floorplancad-vector')):observed.append(str(path))
    sys.addaudithook(guard)
    return observed
def read(row):
    from scripts.floorplancad_svg import parse_svg
    if row['split']!='development' or row['archive'] not in ('train-00.tar.xz','train-01.tar.xz'):raise ValueError('Development only')
    path=Path(row['path'])
    if path.is_symlink() or not path.resolve().is_relative_to(BASE/'dataset/svg'):raise ValueError('Source escapes training cache')
    raw=path.read_bytes()
    if sha(raw)!=row['sha256']:raise ValueError('Source hash changed')
    return parse_svg(raw)
if __name__=='__main__':prepare()
