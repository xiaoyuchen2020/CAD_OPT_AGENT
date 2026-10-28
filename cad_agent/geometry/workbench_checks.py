"""Per-claim checks over immutable geometry results; no expected answer values."""
import math
import itertools
from typing import Literal
from pydantic import Field
from cad_agent.contracts import Record, FiniteFloat, stable_id


def view_conflicts(workset):
    """Advisory overlap, not a ban on legitimate cross-view shared evidence."""
    rows=[r for hid,r in workset.get('hypothesis_ids',{}).items() if hid not in workset.get('superseded_hypotheses',{}) and 'view_kind' in r.get('request',{})]
    out=[]
    for a,b in itertools.combinations(rows,2):
        ar,br=a['request'],b['request'];kinds=[ar['view_kind'],br['view_kind']]
        if kinds[0]==kinds[1] or 'unknown' in kinds or a['bundle_id']!=b['bundle_id']:continue
        shared=sorted(set(ar['entity_ids']) & set(br['entity_ids']))
        if shared:out.append(dict(check_id='CROSS_VIEW_SOURCE_REUSE',hypothesis_ids=[a['hypothesis_id'],b['hypothesis_id']],
            shared_entity_ids=shared,view_kinds=kinds,disposition='review_mapping_not_block_geometry',
            note='Same source geometry appears under different view hypotheses. Verify the relationship; shared frame/title proximity is not proof.'))
    return out


class GeometryClaim(Record):
    claim_id: str
    reference_id: str
    kind: Literal['local_distance', 'converted_local_distance', 'sampled_boundary_minimum', 'whole_route_minimum',
                  'finished_width', 'confirmed_object_identity']
    value: FiniteFloat | None = None
    unit: str = 'drawing_unit'


class CheckClaims(Record):
    bundle_id: str
    claims: list[GeometryClaim] = Field(min_length=1, max_length=128)


def check_claim(claim, result):
    issues=[]; expected=None
    def issue(code, detail): issues.append(dict(check_id=code, detail=detail, impact='this_claim_only'))
    if claim.kind=='local_distance':
        if result.get('status')!='local_pair_measured':issue('LOCAL_MEASUREMENT_REQUIRED','Reference is not a successful local pair measurement.')
        else:expected=result['measurements']['width_drawing_units']
    elif claim.kind=='converted_local_distance':
        conversion=result.get('conversion',{})
        if conversion.get('value') is None or result.get('unit_resolution',{}).get('state')!='resolved':
            issue('UNIT_EVIDENCE_REQUIRED','A successful explicit source-unit conversion is required.')
        else:
            expected=conversion['value']
            if claim.unit!=conversion['unit']:issue('UNIT_EVIDENCE_REQUIRED','Claim unit must match the converted value unit.')
    elif claim.kind=='sampled_boundary_minimum':
        values=[s.get('sample_minimum_drawing_units') for s in result.get('segments',[])]
        values=[v for v in values if v is not None]
        if not values:issue('SAMPLED_PROFILE_REQUIRED','Reference has no measured profile samples.')
        else:expected=min(values)
    elif claim.kind=='whole_route_minimum':
        issue('WHOLE_ROUTE_PROOF_REQUIRED','Selected pair/profile samples do not prove the complete route or its minimum.')
    elif claim.kind=='finished_width':
        issue('FINISHED_SURFACE_REQUIRED','Selected source faces have no confirmed finished-surface interpretation.')
    else:issue('OBJECT_IDENTITY_UNCONFIRMED','Source existence and model hypotheses do not independently confirm engineering identity.')
    unit=result.get('unit',{})
    unit=unit.get('unit') if isinstance(unit,dict) else unit
    if claim.unit!='drawing_unit' and (unit in (None,'unknown','drawing_unit') or claim.unit!=unit):
        issue('UNIT_EVIDENCE_REQUIRED','Keep drawing_unit unless this physical unit is established by source metadata; conversions require separate evidence.')
    if expected is not None and (claim.value is None or not math.isclose(claim.value,expected,rel_tol=1e-8,abs_tol=1e-9)):
        issue('VALUE_MISMATCH','Claim value differs from the referenced deterministic result.')
    return dict(claim_id=claim.claim_id,reference_id=claim.reference_id,kind=claim.kind,
                supported=not issues,issues=issues,expected_value=expected,
                scope='selected_sources_only',engineering_identity_confirmed=False)


def validate(service,args):
    b=service.bundle(args.bundle_id);mode=service.w.settings.geometry_validation_mode
    rows=[]
    if len({c.claim_id for c in args.claims})!=len(args.claims):raise ValueError('DUPLICATE_CLAIM_ID')
    for claim in args.claims:
        result=None
        for kind in ('local_measurement','geometry_route','geometry_hypothesis','geometry_basis'):
            try:result=service.w.store.get(kind,claim.reference_id);break
            except KeyError:pass
        if result is None or result.get('bundle_id')!=args.bundle_id:
            row=dict(claim_id=claim.claim_id,reference_id=claim.reference_id,supported=False,
                     issues=[dict(check_id='SOURCE_REFERENCE_INVALID',detail='Use an existing result from this bundle.',impact='this_claim_only')])
        else:row=check_claim(claim,result)
        # Invalid references remain invalid even in shadow mode; no forged receipt.
        row['disposition']='supported_local' if row['supported'] else 'blocked_claim' if mode=='enforce' else 'flagged_candidate'
        from cad_agent.geometry.investigation_progress import repairs
        row['repair_actions']=repairs(row['issues'],args.bundle_id,claim.reference_id)
        row['unit']=claim.unit
        rows.append(row)
    vid=stable_id('gc',service.sid,b['source_sha256'],'checks-v2',mode,args.model_dump_json())
    record=dict(validation_id=vid,bundle_id=args.bundle_id,snapshot_id=service.sid,mode=mode,
        claims=rows,request=args.model_dump(),source_sha256=b['source_sha256'],
        supported_claim_ids=[r['claim_id'] for r in rows if r['supported']],
        blocked_claim_ids=[r['claim_id'] for r in rows if r['disposition']=='blocked_claim'],
        unit=dict(unit=b['unit']),
        measurements={r['claim_id']:dict(width_drawing_units=r['expected_value']) for r in rows if r['supported'] and r.get('expected_value') is not None and r.get('kind')!='converted_local_distance'},
        measurement_evidence=[dict(unit=dict(unit=r['unit']),measurements=dict(width_drawing_units=r['expected_value'])) for r in rows if r['supported'] and r.get('kind')=='converted_local_distance'],
        spatial_fact_scope=dict(identity=dict(engineering_confirmed=False),extent='validated_local_claims_only'),
        note='Validation is per claim. Neither mode confirms semantic identity or blocks further exploration.')
    service.w.store.put('geometry_validation',vid,record)
    return record


SPECS={'validate_geometry_claims':(CheckClaims,'逐项核对已有局部/采样结果的数值、单位与声明范围。宿主配置shadow只记录问题，enforce拒绝该项声明；有效局部结果保留。')}
HANDLERS={'validate_geometry_claims':validate}
