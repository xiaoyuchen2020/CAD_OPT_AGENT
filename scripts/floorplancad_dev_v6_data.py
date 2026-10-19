"""Phase allowlists: only previous analysis families and fresh development checks."""
import hashlib,json,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
BASE=ROOT/'artifacts/floorplancad-vector-v1'
OLD=ROOT/'artifacts/floorplancad-vector-v2'
PREV=ROOT/'artifacts/floorplancad-vector-v4'
PREV5=ROOT/'artifacts/floorplancad-vector-v5'
OUT=ROOT/'artifacts/floorplancad-vector-v6'
def sha(b):return hashlib.sha256(b).hexdigest()
def save(p,d):p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(d,ensure_ascii=False,indent=2))
def prepare():
    if (OUT/'protocol.json').exists():raise ValueError('Immutable protocol exists')
    old=json.loads((OLD/'development_roster.json').read_text())
    analysis=[r for r in old if r['fold']<3]
    consumed=json.loads((PREV/'fresh_checks_roster.json').read_text())
    consumed += json.loads((PREV5/'fresh_checks_roster.json').read_text())
    old=old+consumed;used={r['group_id'] for r in old}
    assert len(used)==448
    manifest=json.loads((BASE/'dataset/manifest.json').read_text())
    audit=json.loads((BASE/'baseline/geometry_audit.json').read_text())
    banned=set(audit['quarantined_families']);dups=set(audit['same_geometry_duplicates_removed'])
    geometry={r['sample_id']:r['geometry_sha256'] for r in audit['samples']}
    used_geoms={geometry[r['sample_id']] for r in old}
    eligible=[r for r in manifest if r['split']=='development' and r['group_id'] not in used|banned and r['sample_id'] not in dups and geometry.get(r['sample_id']) not in used_geoms]
    families={}
    for r in sorted(eligible,key=lambda r:sha(('v6-fresh-sample:'+r['sample_id']).encode())):
        families.setdefault(r['group_id'],r)
    checks=[];seen=set(used_geoms)
    for fam in sorted(families,key=lambda f:sha(('v6-fresh-family:'+f).encode())):
        r=families[fam];gh=geometry[r['sample_id']]
        if gh in seen:continue
        seen.add(gh);checks.append(r|{'fold':len(checks)%2,'geometry_sha256':gh})
        if len(checks)==96:break
    assert len(analysis)==154 and len(checks)==96
    save(OUT/'analysis_roster.json',analysis);save(OUT/'fresh_checks_roster.json',checks)
    save(OUT/'excluded_used_families.json',sorted(used))
    save(OUT/'protocol.json',dict(analysis_samples=154,analysis_families=154,fresh_check_samples=96,fresh_check_families=96,
        fresh_check_cohorts=2,eligible_fresh_families=len(families),previous_check_families_excluded=294,
        original_validation_access=False,test_access=False,previous_checks_tuning=False,
        analysis='Only old folds 0,1,2; all 448 previously used families excluded from fresh checks.',
        freshness='Original manifest split + geometry quarantine + exact geometry duplicate exclusion; filename families are proxies, not verified project identities. No near duplicate independence claim.',
        metadata='Existing manifest and geometry-audit hashes used only to construct isolation roster; no held-out labels, drawings or metrics consulted.',
        batch='Finite analysis-only hypotheses about candidate part ownership and wall versus window geometry. Keep seeded door-part completion independent from hard host constraints. No threshold search or single-file exceptions; freeze code before scoring, seal selection before fresh checks.',
        gates=dict(wall_as_window_fp_reduction_vs_v5_min=.2,window_tp_retention_vs_v5_min=.95,window_precision_vs_v5_nondecreasing=True,window_f1_vs_v5_nondecreasing=True,door_roles_unchanged_from_independent_completion=True,wall_tp_vs_v5_nondecreasing=True,wall_fp_vs_v5_nonincrease=True,complete=True),
        component_selection='Door completion is fixed to v5 door_parts_only, without host hard gate; no component reselection on checks.',
        selection='Select highest mean class F1 among variants passing every analysis gate; none means no promotion. Check all frozen ablations for diagnostic reproducibility, never select on checks.',
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
    if s.startswith(str(OLD)) or s.startswith(str(PREV)) or s.startswith(str(PREV5)):
        # Only exact analysis prediction paths may be read by the diagnosis.
        return s not in allowed
    if s.startswith(str(BASE/'baseline')):return True
    return False

def install_guard(rows,extra=()):
    allowed={str(Path(r['path']).resolve()) for r in rows}|{str(Path(p).resolve()) for p in extra}
    def guard(event,args):
        if event=='open' and isinstance(args[0],(str,bytes)) and forbidden(args[0],allowed):
            raise PermissionError('v6 split isolation blocks '+str(args[0]))
    sys.addaudithook(guard)
def read(row):
    from scripts.floorplancad_svg import parse_svg
    if row['split']!='development' or row['archive'] not in ('train-00.tar.xz','train-01.tar.xz'):raise ValueError('Development only')
    raw=Path(row['path']).read_bytes()
    if sha(raw)!=row['sha256']:raise ValueError('Source hash changed')
    return parse_svg(raw)
if __name__=='__main__':prepare()
