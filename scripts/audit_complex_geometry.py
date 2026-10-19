"""Read-only arithmetic/artifact audit; does not grade room identity as true."""
from pathlib import Path
from decimal import Decimal
import hashlib,json,sqlite3
from shapely.geometry import Polygon
ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'artifacts/complex-geometry-20261016'
FACTOR={'mm':.001,'cm':.01,'m':1.,'in':.0254,'ft':.3048}

def main():
 reports=json.loads((OUT/'results.json').read_text());audits=[]
 db=sqlite3.connect(f'file:{OUT}/data/index.sqlite3?mode=ro',uri=True)
 for entry in reports:
  if not entry.get('run_id'):
   audits.append({'case':entry['case'],'status':entry['status']});continue
  run=json.loads((OUT/(entry['run_id']+'.json')).read_text());task=json.loads(db.execute('select data from records where id=?',(run['task_id'],)).fetchone()[0])
  rows=[];seen=set();duplicates=[];polys=[];images=[]
  for batch in task['batches'].values():
   for row in batch['items']:
    if row['region_id'] in seen:duplicates.append(row['region_id']);continue
    seen.add(row['region_id']);h=row.get('highlight',{});area=row.get('area_m2') if row.get('area_m2') is not None else row.get('candidate_area_m2')
    output={'number':row['number'],'region_id':row['region_id'],'label':row['label'],'highlight_kind':h.get('kind'),'reported_area_m2':area,'unit':batch['unit']}
    if h.get('kind')=='candidate_boundary':
     p=Polygon(h['boundary'],h.get('holes',[]));f=FACTOR.get(batch['unit'])
     output.update(valid_polygon=p.is_valid,polygon_area_m2=p.area*f*f if f else None,polygon_perimeter_m=p.length*f if f else None)
     output['area_matches_polygon']=area is None or (f is not None and abs(area-p.area*f*f)<=max(1e-5,abs(area)*1e-6))
     polys.append((row['region_id'],p,batch['scope']['frame_id']))
    rows.append(output)
   if batch.get('boundary_review_id'):
    review=json.loads(db.execute('select data from records where id=?',(batch['boundary_review_id'],)).fetchone()[0])
    for image in review['images']:
     p=OUT/'data/drawings'/batch['snapshot_id']/'vision'/(image['image_id']+'.png')
     images.append({'image_id':image['image_id'],'file':str(p.relative_to(ROOT)),'integrity_ok':p.exists() and hashlib.sha256(p.read_bytes()).hexdigest()==image['image_sha256']})
  overlaps=[(a,b) for n,(a,p,f) in enumerate(polys) for b,q,g in polys[n+1:] if f==g and p.intersection(q).area>min(p.area,q.area)*1e-6]
  measured=[r for r in rows if r['reported_area_m2'] is not None]
  ordered=sorted(measured,key=lambda r:(-Decimal(str(r['reported_area_m2'])),r['number']))
  result={'case':entry['case'],'run_id':run['id'],'published':run['status']=='completed','task_status':task['status'],'rows':rows,'duplicate_region_ids':duplicates,'overlap_pairs':overlaps,'images':images,
   'descending_numbers':[r['number'] for r in ordered],'independent_max_minus_min_m2':str(Decimal(str(ordered[0]['reported_area_m2']))-Decimal(str(ordered[-1]['reported_area_m2']))) if ordered else None,
   'image_inputs':(run.get('result') or {}).get('vision'), 'subtasks':[{k:s[k] for k in ['id','requirement','kind','status','delivered','reason']} for s in task['subtasks']],
   'failed_tools':[t for t in run['trace'] if t['status']=='failed'],'geometry_or_source_tools':[{k:o[k] for k in ['observation_id','tool','data'] if k in o} for o in task['observations'].values()],
   'limitations':'Polygon arithmetic/overlap/image integrity do not certify room identity or net-area basis.'}
  if entry['case']=='filter_followup':
   parent=next((a for a in reversed(audits) if a['case']=='area_ranking'),{})
   expected=parent.get('descending_numbers',[])[:2];actual=[r['number'] for r in rows if r['highlight_kind']=='candidate_boundary']
   result['filter_delivery']={'expected_numbers_from_parent':expected,'actual_numbers_in_result':actual,'passed':bool(expected) and set(expected)==set(actual) and len(actual)==2}
  audits.append(result)
 db.close();(OUT/'artifact-audit.json').write_text(json.dumps(audits,ensure_ascii=False,indent=2))
 print(json.dumps([{k:a[k] for k in ['case','published','task_status','descending_numbers','independent_max_minus_min_m2','filter_delivery'] if k in a} for a in audits],ensure_ascii=False,indent=2))
if __name__=='__main__':main()
