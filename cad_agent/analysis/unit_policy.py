"""User-owned measurement assumptions and bounded drawing-unit inspection.

The model cannot supply an override. Only a literal user instruction or source
unit can authorize conversion; absence of native DIMENSION alone is insufficient.
"""
import re
from copy import deepcopy
from pydantic import Field
from cad_agent.contracts import Record, stable_id
from cad_agent.geometry.source import UNIT_TO_M
from cad_agent.components.dimensions import read_dimensions

UNITS = {'毫米':'mm', '厘米':'cm', '英寸':'in', '英尺':'ft', '米':'m',
         'mm':'mm', 'cm':'cm', 'm':'m', 'in':'in', 'ft':'ft'}
UNIT = r'毫米|厘米|英寸|英尺|米|\bmm\b|\bcm\b|\bm\b|\bin\b|\bft\b'


def user_unit_policy(question):
    """Extract conservative explicit policies, independent of room/floor names."""
    found=[]
    for clause in re.split(r'[。？！?!；;\n]', question):
        if not re.search(r'默认|按|采用|使用|设为|单位(?:为|是)|assume|use', clause, re.I):continue
        units={UNITS[m.group().lower()] for m in re.finditer(UNIT,clause,re.I)}
        if len(units)!=1 or re.search(r'不要|不得|不能|不按|不采用|不使用',clause):continue
        conditional=bool(re.search(r'如果|若|没有|无尺寸|未标|缺少|否则|if\b',clause,re.I))
        condition='no_dimensions' if conditional and re.search(r'尺寸|标注|dimension',clause,re.I) else 'unknown_units' if conditional and re.search(r'单位|unit',clause,re.I) else 'explicit'
        # Unrecognized conditions must not be silently promoted to permission.
        if conditional and condition=='explicit':continue
        found.append(dict(unit=next(iter(units)),condition=condition,quote=clause.strip()))
    return found[0] if found and len({(p['unit'],p['condition']) for p in found})==1 else None


class ResolveRoomUnits(Record):
    batch_id: str = Field(min_length=1)


def resolve_units(workspace, sid, batch, policy):
    """Record both what was inspected and why a conversion was/wasn't allowed."""
    from cad_agent.geometry.room_batch import RoomBatchService
    service=RoomBatchService(workspace,sid)
    result=dict(status='needs_input',unit='unknown',basis=None,policy=policy,
                dimension_check=None,reason='图纸未声明单位，且没有可应用的用户单位约定。')
    source_unit=service.drawing['unit']
    if source_unit in UNIT_TO_M:
        result.update(status='resolved',unit=source_unit,basis='dxf_header',reason='使用图纸声明单位。')
        if policy and policy['condition']=='explicit' and policy['unit']!=source_unit:
            result.update(status='needs_input',unit='unknown',basis=None,reason='用户指定单位与图纸声明冲突，请选择本次测量单位。')
    elif policy and policy['condition'] in ('explicit','unknown_units'):
        result.update(status='resolved',unit=policy['unit'],basis='user_instruction',reason='已采用用户明确授权的本次测量单位。')
    elif policy and batch['scope']:
        scope=batch['scope']
        dimensions=read_dimensions(workspace,sid,scope['layout_id'],scope['bbox'],'unknown')
        # Keep numeric text as possible exploded dimensions; axis numbers and
        # unrelated quantities are ambiguous, never proof that dimensions exist.
        numeric=[r for r in service.index.scope(scope['layout_id'],scope['frame_id'])
                 if re.fullmatch(r'[±+\-]?\d+(?:\.\d+)?(?:\s*(?:mm|cm|m|毫米|厘米|米))?',r.text.strip(),re.I)]
        issues=dimensions['source_diagnostics']
        complete=not dimensions['truncated'] and not issues
        check=dict(native_count=dimensions['total_native_dimensions'],possible_exploded_count=len(numeric),
                   complete=complete,scope_bbox=scope['bbox'],layout_id=scope['layout_id'],
                   numeric_evidence=[service.index.evidence(r) for r in numeric[:12]],
                   diagnostics=issues[:12])
        result['dimension_check']=check
        if dimensions['total_native_dimensions']:
            result['reason']='范围内存在原生尺寸标注，“无尺寸时默认单位”的条件不成立；图纸尚未声明物理单位。'
        elif numeric or not complete:
            result['reason']='已检查原生尺寸和文字；仍有可能的炸开标注或读取缺口，不能确认无尺寸标注。可明确指定本次单位。'
        else:
            result.update(status='resolved',unit=policy['unit'],basis='user_conditional_assumption',
                          reason='目标范围内未检出原生尺寸或数字标注文字，按用户约定换算；单位属于本次假设。')
    elif not batch['scope']:
        result.update(status='pending',reason='先确定目标视图范围，再检查单位约定。')
    new=deepcopy(batch)
    new.update(parent_batch_id=batch['batch_id'],unit_resolution=result,
               batch_id=stable_id('rooms',batch['batch_id'],'unit-policy-v1',str(result)))
    if result['status']=='resolved':
        new.update(unit=result['unit'],unit_basis=result['basis'],status='needs_review')
    elif result['status']=='needs_input':
        new.update(unit='unknown',unit_basis='unresolved',status='needs_unit_confirmation')
        for item in new['items']:
            item.update(status='needs_unit_confirmation',area_m2=None,candidate_area_m2=None,
                        measurement_id=None,measurement_evidence_url=None)
    new['evidence_url']=f'/api/drawings/{sid}/room-batches/{new["batch_id"]}'
    service.measure_selected(new)
    workspace.store.put('room_batch',new['batch_id'],new)
    return new
