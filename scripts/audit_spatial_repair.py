"""Read-only audit: native source segment membership, independent arithmetic and delivered overlays.

Does not call production room/door measurement or open a Workspace (which would
recover active chat records). Publication and geometry checks are separate.
"""
from pathlib import Path
import sys,json,sqlite3,math,threading,logging,re
from types import SimpleNamespace
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from shapely.geometry import LineString,Polygon,Point
from cad_agent.geometry.source import native_source
logging.getLogger('ezdxf').setLevel(logging.ERROR)

root=Path(sys.argv[1]).resolve();db=root/'data/index.sqlite3'
c=sqlite3.connect('file:'+str(db)+'?mode=ro',uri=True)
records={key:json.loads(data) for key,data in c.execute('select id,data from records')}
reported={r['run_id'] for r in json.loads((root/'results.json').read_text()) if r.get('run_id')}
class ReadStore:
 def get(self,kind,key):return records[key]
 def artifact(self,sid,name):return root/'data/drawings'/sid/name
w=SimpleNamespace(store=ReadStore(),lock=threading.RLock())

def native_segments(sid,eid):
 e=native_source(w,sid)['entities'][eid][0]
 if e.dxftype()=='LINE':return [LineString([list(e.dxf.start)[:2],list(e.dxf.end)[:2]])]
 if e.dxftype() in ('LWPOLYLINE','POLYLINE'):
  from ezdxf.path import make_path
  points=[list(p)[:2] for p in make_path(e).flattening(.001)]
  return [LineString([a,b]) for a,b in zip(points,points[1:])]
 if e.dxftype()=='HATCH':
  # Audit the original hatch boundary, never its fill or display bounding box.
  from ezdxf.path import from_hatch
  out=[]
  for path in from_hatch(e):
   assert not path.has_curves, 'A curved hatch cannot prove an exact linear jamb'
   points=[list(p)[:2] for p in path.flattening(.001)]
   out.extend(LineString([a,b]) for a,b in zip(points,points[1:]))
  return out
 raise AssertionError('Unsupported audit entity kind '+e.dxftype())

def assert_source_segment(sid,jamb):
 p=LineString(jamb['points']);segments=native_segments(sid,jamb['entity_id'])
 assert any(g.buffer(1e-5).covers(p) for g in segments),('jamb not on original source',jamb)

def shoelace(points):
 # Translate before products to avoid cancellation on million-unit drawings.
 a,b=points[0];q=[(x-a,y-b) for x,y in points]
 return abs(sum(x*v-u*y for (x,y),(u,v) in zip(q,q[1:]+q[:1])))/2

def perimeter(points):return sum(math.dist(a,b) for a,b in zip(points,points[1:]+points[:1]))

results=[]
for run in records.values():
 if not isinstance(run,dict) or not str(run.get('id','')).startswith('chat_') or run.get('status') in ('running','queued'):continue
 if run['id'] not in reported:continue
 task=records.get(run.get('task_id'),{});sid=run['snapshot_id'];action=task.get('highlight_action') or {};checks=[];reviewed=[]
 for obs in task.get('observations',{}).values():
  if obs['tool'] not in ('review_space_connections','review_discovered_spaces'):continue
  r=records[obs['data']['review_id']];reviewed.append(r)
  assert r['source_sha256']==records[sid]['sha256']
  source_images={i['image_id'] for i in r['images']}
  sent={i['image_id'] for u in run.get('trace',[]) for i in u.get('image_inputs',[])}
  # Actual image delivery is recorded on runtime usage/report; inspect saved result too.
  delivered=set((run.get('result') or {}).get('vision',{}).get('image_ids',[]))
  if not task.get('inherited_from') and run['status']=='completed':assert source_images<=delivered
  assert len({i['item_id'] for i in r['items']})==len(r['items'])
  if r['kind']=='connection_review':
   expected=[]
   for row in r['items']:
    choice=row['decision'];assert choice['item_id']==row['item_id']
    if choice['classification']=='door':assert row['selected_measurements']
    if choice['classification'] in ('wall','not_plan_space'):assert not row['selected_measurements']
    for m in row['selected_measurements']:
     assert len(m['jambs'])==2
     for jamb in m['jambs']:assert_source_segment(sid,jamb)
     a,b=[LineString(j['points']) for j in m['jambs']]
     factor={'mm':.001,'cm':.01,'m':1,'in':.0254,'ft':.3048}[r['applied_unit']]
     actual=round(a.distance(b)*factor,6)
     assert abs(actual-m['value'])<=1e-6
     assert abs(math.dist(*m['measurement_line'])*factor-m['value'])<=1e-6
     assert a.distance(Point(m['measurement_line'][0]))<=1e-5 and b.distance(Point(m['measurement_line'][1]))<=1e-5
     expected += [j['points'] for j in m['jambs']]+[m['measurement_line']]
     checks.append(dict(kind='native_jamb_distance',pair=row['number'],value=actual,unit='m',passed=True))
    if choice['classification']=='wall':
     for boundary in row['barriers']:assert_source_segment(sid,boundary)
     expected += [b['points'] for b in row['barriers']]
     checks.append(dict(kind='negative_relation_source_barrier',pair=row['number'],passed=bool(row['barriers'])))
    if choice['classification']=='open':expected.append(row['probe'])
   assert r['highlight_action']['paths']==expected
   checks.append(dict(kind='source_paths_match_highlight',count=len(expected),passed=True))
  else:
   factor={'mm':.001,'cm':.01,'m':1,'in':.0254,'ft':.3048}[r['applied_unit']]
   for row in r['selected']:
    assert row['decision']['classification']=='space'
    area=round(shoelace(row['boundary'])*factor**2,6);length=round(perimeter(row['boundary'])*factor,6)
    assert abs(area-row['area_m2'])<=1e-6 and abs(length-row['perimeter_m'])<=1e-6
    checks.append(dict(kind='independent_polygon_arithmetic',number=row['number'],area_m2=area,perimeter_m=length,passed=True))
    regions={i['region_id']:i for i in json.loads((root/'data/drawings'/sid/'regions.json').read_text())}
    for annotation in row.get('area_annotation_checks',[]):
     source=regions[annotation['region_id']];box=source['bbox']
     assert source['layout_id']==r['layout_id'] and source['text']==annotation['text']
     assert Polygon(row['boundary']).contains(Point((box[0]+box[2])/2,(box[1]+box[3])/2))
     values=[float(v) for v in re.findall(r'(\d+(?:\.\d+)?)\s*(?:㎡|m²|m2|平方米)',source['text'],re.I)]
     assert annotation['annotation_area_m2'] in values
     assert abs(area-annotation['annotation_area_m2']-annotation['difference_m2'])<1e-6
     checks.append(dict(kind='native_area_annotation_difference',number=row['number'],source_text=source['text'],
                       difference_m2=annotation['difference_m2'],comparison_status=annotation['status'],passed=True))
   assert r['highlight_action']['object_ids']==[i['item_id'] for i in r['selected']]
   assert [i['boundary'] for i in r['highlight_action']['regions']]==[i['boundary'] for i in r['selected']]
   assert [i['area_m2'] for i in r['selected']]==sorted([i['area_m2'] for i in r['selected']],reverse=True)
   if r['ranking_complete']:
    threshold=r['selected'][-1]['area_m2']
    assert not any(i['decision']['classification']=='uncertain' and i['area_m2']>=threshold for i in r['items'])
    assert r['largest_omitted_area_m2'] is None or r['largest_omitted_area_m2']<threshold
   checks.append(dict(kind='ranking_and_highlight_identity',selected_count=len(r['selected']),ranking_complete=r['ranking_complete'],passed=True))
 if reviewed:
  replacements=[o['data'] for o in task.get('observations',{}).values() if o['tool']=='analyze_measurements' and o['data'].get('ordered')]
  if not replacements:assert action==reviewed[-1]['highlight_action']
  if replacements:
   assert action['object_ids']==[v['object_id'] for v in replacements[-1]['ordered']]
   selected={i['item_id']:i for r in reviewed for i in r.get('selected',[])}
   assert len(action['object_ids'])==len(action['regions'])
   for oid,region in zip(action['object_ids'],action['regions']):assert region['boundary']==selected[oid]['boundary']
   checks.append(dict(kind='replacement_selected_identity',count=len(action['regions']),passed=True))
  if run.get('result'):assert run['result']['highlight_action']==action
 results.append(dict(run_id=run['id'],status=run['status'],question=run['request']['question'],review_count=len(reviewed),checks=checks,
    spatial_review_applicable=bool(reviewed),geometry_delivered=bool(action.get('paths') or action.get('regions')),strict_checks_passed=all(v['passed'] for v in checks) if checks else None,
    note='Arithmetic/source/highlight checks do not certify semantic identity or full-drawing recall.'))
out=root/'spatial-independent-audit.json';out.write_text(json.dumps(results,ensure_ascii=False,indent=2));print(out);print([(r['run_id'],r['geometry_delivered'],r['strict_checks_passed']) for r in results])
