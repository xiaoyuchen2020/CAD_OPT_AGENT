"""Independent artifact arithmetic and delivery auditing, never semantic certification."""
from pathlib import Path
import sys,json,sqlite3,hashlib,math
from decimal import Decimal
from shapely.geometry import Polygon
ROOT=Path(__file__).resolve().parents[1]
FACTORS={'mm':.001,'cm':.01,'m':1.,'in':.0254,'ft':.3048}

def audit(folder):
 out=Path(folder);db=sqlite3.connect(f'file:{out}/data/index.sqlite3?mode=ro',uri=True)
 def read(key):return json.loads(db.execute('select data from records where id=?',(key,)).fetchone()[0])
 reports=json.loads((out/'results.json').read_text());results=[]
 for case in reports:
  if not case.get('run_id'):continue
  r=read(case['run_id']);t=read(r['task_id']);checks=[];calculations=[];chains=[]
  for batch in t['batches'].values():
   if batch['unit'] not in FACTORS:continue
   for i in batch['items']:
    h=i.get('highlight',{});reported=i.get('area_m2') if i.get('area_m2') is not None else i.get('candidate_area_m2')
    if h.get('kind')!='candidate_boundary' or reported is None:continue
    p=Polygon(h['boundary'],h.get('holes',[]));expected=round(p.area*FACTORS[batch['unit']]**2,6)
    checks.append(dict(object_id=i['region_id'],field='batch_area',reported=reported,independent=expected,passed=p.is_valid and abs(expected-reported)<=1e-5))
  for o in t['observations'].values():
   if o['tool']=='discover_rooms' and o['data'].get('items'):
    d=o['data'];source=read(d['measurement_id']);unit=source['applied_unit']
    if unit in FACTORS:
     seen={i['candidate_id'] for i in d['items']}
     for i in source['candidates']:
      if i['candidate_id'] not in seen:continue
      p=Polygon(i['boundary']);expected=round(p.area*FACTORS[unit]**2,6);reported=i['candidate_shell_area_m2']
      checks.append(dict(object_id=i['candidate_id'],field='discovered_outer_area',reported=reported,independent=expected,passed=p.is_valid and abs(expected-reported)<=1e-5))
   if o['tool']=='check_dimension_chain' and o['data'].get('parts'):
    d=o['data'];parts=d['parts'];total=d['total']
    distances=[math.dist(i['definition_points'][0],i['definition_points'][1]) for i in parts]
    span=math.dist(total['definition_points'][0],total['definition_points'][1])
    # Read native display strings independently of the chain calculator.
    raw=[i['text_override'] if i.get('text_override') not in ('','<>',None) else i['rendered_texts'][0] for i in parts+[total]]
    try:
     values=[Decimal(v) for v in raw];addition=sum(values[:-1]);difference=addition-values[-1]
     text_check=dict(independent_sum=float(addition),independent_difference=float(difference),basis='native displayed strings; distinct from unrounded CAD values')
    except (ValueError,ArithmeticError):text_check=dict(passed=None,reason='原文非纯数值，独立审计不猜测格式。')
    nums=[]
    for i in parts+[total]:
     override=i.get('text_override','').strip()
     nums.append(float(override) if override not in ('','<>') else math.dist(*i['definition_points'])*abs(i['style'].get('dimlfac',1)))
    native_sum=round(sum(nums[:-1]),6);native_diff=round(sum(nums[:-1])-nums[-1],6)
    native_check=dict(independent_sum=native_sum,independent_difference=native_diff,reported_sum=d['sum_value'],reported_difference=d['difference'],passed=abs(native_sum-d['sum_value'])<=1e-6 and abs(native_diff-d['difference'])<=1e-6)
    chains.append(dict(object_ids=[i['object_id'] for i in parts+[total]],native_texts=raw,display_arithmetic=text_check,native_arithmetic=native_check,
      independent_part_lengths=distances,independent_total_length=span,
      geometry_passed=abs(sum(distances)-d['geometric_sum'])<=1e-6 and abs(span-d['geometric_total'])<=1e-6,
      highlighted_entity_ids=d['highlight_entity_ids'],scope=d['scope']))
   if o['tool']!='analyze_measurements':continue
   d=o['data'];values=d['values'];bid=d['batch_id'];batch=t['batches'].get(bid)
   source=None if batch else read(bid)
   for value in values:
    if batch:
     item=next(i for i in batch['items'] if i['region_id']==value['object_id']);shape=Polygon(item['highlight']['boundary'],item['highlight'].get('holes',[]));unit=batch['unit']
    else:
     choices=source['selected'] if source['kind']=='discovery_review' else source['candidates']
     item=next(i for i in choices if i.get('candidate_id',i.get('item_id'))==value['object_id']);shape=Polygon(item['boundary']);unit=source['applied_unit']
    f=FACTORS[unit];expected=round(shape.area*f*f if value['field']=='area' else shape.exterior.length*f,6)
    checks.append(dict(object_id=value['object_id'],field=value['field'],reported=value['value'],independent=expected,passed=abs(expected-value['value'])<=1e-6))
   if d.get('calculation'):
    c=d['calculation'];nums=[Decimal(str(v['value'])) for v in values]
    expected=max(nums)-min(nums) if c['operation']=='range' else sum(nums)/len(nums) if c['operation']=='mean' else sum(nums)
    calculations.append(dict(operation=c['operation'],value=c['value'],independent=float(round(expected,6)),passed=abs(float(expected)-c['value'])<=1e-6))
  action=(r.get('result') or {}).get('highlight_action') or t.get('highlight_action')
  result=dict(case=case['case'],run_id=r['id'],published=r['status']=='completed',task_status=t['status'],
              measurement_checks=checks,calculations=calculations,dimension_checks=chains,highlight_kind=(action or {}).get('kind','room_batch'),
              selected_numbers=[i['number'] for i in (action or {}).get('items',[])],selected_object_ids=(action or {}).get('object_ids'),
              failed_tools=[{'tool':a['tool'],'error':a.get('error')} for a in r['trace'] if a['status']=='failed'],
              operation_gaps=[dict(requirement=s['requirement'],status=s['status'],reason=s.get('reason')) for s in t['subtasks'] if s['status']!='completed'],
              code_changed_during_run=case['code_changed_during_run'],tokens=case['tokens'],
              limitations='独立复算验证数值与集合，不确认房间身份、门净宽、真实洞口或净面积。')
  results.append(result)
 db.close();(out/'independent-audit.json').write_text(json.dumps(results,ensure_ascii=False,indent=2))
 print(json.dumps([{k:v for k,v in r.items() if k not in ('measurement_checks','operation_gaps')} for r in results],ensure_ascii=False,indent=2))
if __name__=='__main__':audit(sys.argv[1])
