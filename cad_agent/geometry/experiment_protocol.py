"""Frozen, object-level evaluation and supervised intervention accounting.

This module does not infer gold from model answers. Unannotated dimensions are
reported as unevaluated, and assisted selections cannot count as autonomous.
"""
from pathlib import Path
from typing import Literal
import hashlib
import json
import math
from pydantic import Field, model_validator
from cad_agent.contracts import Record, FiniteFloat

VERSION='geometry-object-evaluation-v1'


def digest(value):return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()


class Guidance(Record):
    category: Literal['runtime_repair','investigation_method','source_fact','direct_selection']
    text: str = Field(min_length=1)
    object_keys: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    source: Literal['human','supervisor_agent'] = 'supervisor_agent'
    @model_validator(mode='after')
    def require_sources(self):
        if self.category=='source_fact' and not self.evidence_refs:raise ValueError('Source facts require inspectable evidence references.')
        return self


class GoldMeasurement(Record):
    source_paths: list[list[str]] = Field(min_length=2,max_length=2)
    segment_indices: list[int | None] | None = Field(default=None,min_length=2,max_length=2)
    value: FiniteFloat
    absolute_tolerance: FiniteFloat = Field(ge=0)
    unit: str = 'drawing_unit'


class GoldObject(Record):
    object_key: str
    aliases: list[str] = Field(default_factory=list)
    view_kind: str | None = None
    source_paths: list[list[str]] | None = None
    measurements: list[GoldMeasurement] = Field(default_factory=list)
    route_segment_keys: list[str] | None = None
    physical_unit: str | None = None


class GoldCase(Record):
    case_id: str
    source_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    split: Literal['development','heldout']
    question: str = Field(min_length=1)
    scope: dict = Field(min_length=1)
    objects: list[GoldObject]
    census_complete: bool = False
    annotation_basis: str = Field(min_length=1)
    @model_validator(mode='after')
    def unique_keys(self):
        names=[n for o in self.objects for n in [o.object_key,*o.aliases]]
        if len(names)!=len(set(names)):raise ValueError('Gold object labels and aliases must be unique within a case.')
        for o in self.objects:
            if o.route_segment_keys is not None and len(o.route_segment_keys)!=len(set(o.route_segment_keys)):
                raise ValueError('Duplicate route segment in independent census.')
        return self


def freeze(cases,path):
    rows=[GoldCase.model_validate(c).model_dump() for c in cases]
    if len({c['case_id'] for c in rows})!=len(rows):raise ValueError('DUPLICATE_CASE_ID')
    dev={c['source_sha256'] for c in rows if c['split']=='development'}
    held={c['source_sha256'] for c in rows if c['split']=='heldout'}
    if dev & held:raise ValueError('DRAWING_LEAKAGE_ACROSS_SPLITS')
    payload=dict(version=VERSION,cases=rows);payload['sha256']=digest(payload)
    with Path(path).open('x') as f:json.dump(payload,f,ensure_ascii=False,indent=2,allow_nan=False)
    return payload


def load_frozen(path):
    data=json.loads(Path(path).read_text());sha=data.pop('sha256')
    if sha!=digest(data):raise ValueError('FROZEN_GOLD_CHANGED')
    for c in data['cases']:GoldCase.model_validate(c)
    return data|dict(sha256=sha)


def paths(value):return {tuple(p) for p in value}


def measurement_key(value):
    rows=value.get('source_paths',[]);indices=value.get('segment_indices') or [None,None]
    if len(rows)!=2 or len(indices)!=2:return None
    return sorted([(tuple(path),-1 if index is None else index) for path,index in zip(rows,indices)])


def score(case,prediction,guidance=()):
    case=GoldCase.model_validate(case)
    if prediction.get('source_sha256')!=case.source_sha256:raise ValueError('PREDICTION_SOURCE_MISMATCH')
    helps=[Guidance.model_validate(g) for g in guidance]
    assisted={k for g in helps if g.category=='direct_selection' for k in g.object_keys}
    all_assisted=any(g.category=='direct_selection' and not g.object_keys for g in helps)
    gold={name:o for o in case.objects for name in [o.object_key,*o.aliases]}
    matched=set();details=[];fp=0;unsupported_global_claims=[];counts={k:[0,0] for k in ('attribution','boundary_selection','numerical_precision','physical_unit','route_coverage','claim_scope')}
    for p in prediction.get('objects',[]):
        obj=gold.get(p.get('object_key'));issues=[]
        if obj is None or obj.object_key in matched:
            fp+=1;details.append(dict(predicted_object=p.get('object_key'),matched=False));continue
        matched.add(obj.object_key)
        if p.get('whole_route_complete') and (obj.route_segment_keys is None or not case.census_complete or set(p.get('covered_segment_keys',[]))!=set(obj.route_segment_keys)):
            unsupported_global_claims.append(obj.object_key)
        for key,expected,actual in [('attribution',obj.view_kind,p.get('view_kind')),
                                    ('boundary_selection',paths(obj.source_paths) if obj.source_paths is not None else None,paths(p.get('source_paths',[]))),
                                    ('physical_unit',obj.physical_unit,p.get('physical_unit'))]:
            if expected is not None:
                counts[key][1]+=1;counts[key][0]+=int(expected==actual)
                if expected!=actual:issues.append(key)
        for m in obj.measurements:
            candidates=[r for r in p.get('measurements',[]) if measurement_key(r)==measurement_key(m.model_dump())]
            counts['numerical_precision'][1]+=1
            ok=len(candidates)==1 and isinstance(candidates[0].get('value'),(int,float)) and not isinstance(candidates[0].get('value'),bool) and math.isfinite(candidates[0]['value']) and candidates[0].get('unit')==m.unit and abs(candidates[0]['value']-m.value)<=m.absolute_tolerance
            counts['numerical_precision'][0]+=int(ok)
            if not ok:issues.append('numerical_precision_or_source_pair')
        if obj.route_segment_keys is not None:
            correct=set(p.get('covered_segment_keys',[])) & set(obj.route_segment_keys)
            counts['route_coverage'][0]+=len(correct);counts['route_coverage'][1]+=len(obj.route_segment_keys)
            if set(p.get('covered_segment_keys',[]))!=set(obj.route_segment_keys):issues.append('route_coverage')
        # A coverage claim is evaluated only if a census was frozen; unknown is not a pass.
        if obj.route_segment_keys is not None:
            counts['claim_scope'][1]+=1
            asserted=p.get('whole_route_complete',False)
            counts['claim_scope'][0]+=int(not asserted or (case.census_complete and set(p.get('covered_segment_keys',[]))==set(obj.route_segment_keys)))
        details.append(dict(object_key=obj.object_key,matched=True,issues=issues,
            autonomous_selection=not all_assisted and obj.object_key not in assisted))
    # Missing objects must count against annotated dimensions, not disappear from denominators.
    for obj in case.objects:
        if obj.object_key in matched:continue
        for key,val in [('attribution',obj.view_kind),('boundary_selection',obj.source_paths),('physical_unit',obj.physical_unit)]:
            if val is not None:counts[key][1]+=1
        counts['numerical_precision'][1]+=len(obj.measurements)
        if obj.route_segment_keys is not None:
            counts['route_coverage'][1]+=len(obj.route_segment_keys);counts['claim_scope'][1]+=1
    metrics={k:dict(correct=a,total=b,rate=a/b if b else None,status='evaluated' if b else 'unannotated') for k,(a,b) in counts.items()}
    return dict(case_id=case.case_id,split=case.split,source_sha256=case.source_sha256,
        detection=dict(matched=len(matched),gold_objects=len(case.objects),false_positives=fp,
            recall=len(matched)/len(case.objects) if case.objects else None,
            precision=len(matched)/(len(matched)+fp) if case.census_complete and len(matched)+fp else None,
            census_complete=case.census_complete),metrics=metrics,objects=details,
        missing_objects=sorted({o.object_key for o in case.objects}-matched),unsupported_global_claims=unsupported_global_claims,
        guidance_counts={k:sum(g.category==k for g in helps) for k in ('runtime_repair','investigation_method','source_fact','direct_selection')},
        unassisted_matched_labels=sum(d.get('autonomous_selection',False) for d in details),
        unassisted_correct_on_annotated_dimensions=sum(d.get('autonomous_selection',False) and not d.get('issues') for d in details),
        note='Source and numeric agreement do not substitute for separately annotated engineering identity. Partial gold is not full acceptance.')


def compare_manifests(a,b,factor):
    allowed={'validation_mode','geometry_context_projection','budget_profile'}
    if factor not in allowed:raise ValueError('SUPPORTED_SINGLE_FACTOR_REQUIRED')
    required={'model','reasoning_effort','source_sha256','rule_sha256','question_sha256','code_files',
              'runner_sha256','tool_names','runtime','docker_image','code_execution_mode',
              'budget_profile','geometry_context_projection','validation_mode'}
    missing=sorted(k for k in required if k not in a or k not in b)
    ignored={'run_id','started_at'}
    differences={k:dict(a=a.get(k),b=b.get(k)) for k in set(a)|set(b) if k not in ignored and a.get(k)!=b.get(k)}
    if factor=='budget_profile' and 'budget' in differences:
        def coherent(m):
            profile=m.get('budget_profile')
            return isinstance(profile,dict) and m.get('budget')==(profile if any(v is not None for v in profile.values()) else None)
        if coherent(a) and coherent(b):differences.pop('budget')
    comparable=not missing and set(differences)=={factor}
    return dict(factor=factor,comparable=comparable,differences=differences,missing_frozen_fields=missing,
        reason='single_factor_only' if comparable else 'incomplete_manifest_or_confounded_or_no_treatment_difference',
        statistical_significance_established=False)


def usage(responses):
    records=[r.get('usage') or {} for r in responses]
    values=lambda key:[u[key] for u in records if isinstance(u.get(key),(int,float))]
    cached=[u.get('prompt_tokens_details',u.get('input_tokens_details',{})).get('cached_tokens') for u in records]
    cached=[v for v in cached if isinstance(v,(int,float))]
    return dict(responses=len(records),input_reporting_responses=sum('prompt_tokens' in u or 'input_tokens' in u for u in records),
        output_reporting_responses=sum('completion_tokens' in u or 'output_tokens' in u for u in records),input_tokens=sum(u.get('prompt_tokens',u.get('input_tokens',0)) for u in records),
        output_tokens=sum(u.get('completion_tokens',u.get('output_tokens',0)) for u in records),
        reported_cached_tokens=sum(cached),cached_reporting_responses=len(cached),
        cache_is_subset_of_input=True,cost=None,cost_reason='verified_channel_tariff_not_supplied')
